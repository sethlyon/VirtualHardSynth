#!/usr/bin/env python3
"""
webui - browser UI + synth control surface for the VirtualHardSynth rig.

Lets you:
  - load additional soundfonts, either from a curated list or any URL
  - browse what's inside each loaded soundfont
  - assign a bank/program slot on the panel to any preset in any soundfont
  - drive the on-screen 49-key control surface: play real notes, change the
    selected patch, adjust volume - all of which stay in sync with the
    physical CYD panel, because they go through the same two channels the
    panel itself uses:
      1. patchbridge.py's local control socket (127.0.0.1:CONTROL_PORT) for
         patch/bank/volume/panic - this process never keeps its own copy of
         "what's selected", patchbridge.py remains the single owner of that
      2. FluidSynth's own command socket (127.0.0.1:9800) directly, for
         literal note-on/note-off - patchbridge.py has no involvement in
         audio, so there's nothing to duplicate there

Writes synth/patches.json, which patchbridge.py polls and reloads live.
"""

import argparse
import json
import logging
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "midi-patch-display" / "pi"))
import sf2  # noqa: E402

log = logging.getLogger("webui")

CONTROL_HOST, CONTROL_PORT = "127.0.0.1", 9900
FLUID_HOST, FLUID_PORT = "127.0.0.1", 9800

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCHES_PATH = REPO_ROOT / "synth" / "patches.json"
SOUNDFONTS_DIR = REPO_ROOT / "synth" / "soundfonts"
CONFIGS_DIR = REPO_ROOT / "synth" / "configs"

MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024   # generous, but this is a 2GB Pi
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# A hand-picked subset of https://archive.org/download/free-soundfonts-sf2-2019-04
# - the collection has 100+ files up to 550MB, most of which would be a poor
# idea to pull onto a battery-powered Pi. These are known-good, modest-size
# GM/GS soundfonts users can add with one click; anything else goes through
# the free-form URL field.
CURATED_SOUNDFONTS = [
    {"key": "gxscc", "name": "GXSCC (chiptune)", "size": "126 KB",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/GXSCC_gm_033.sf2"},
    {"key": "florestan", "name": "Florestan Basic GM/GS", "size": "3.1 MB",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/Florestan_Basic_GM_GS.sf2"},
    {"key": "yamahaxg", "name": "Yamaha XG Sound Set", "size": "3.7 MB",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/Yamaha_XG_Sound_Set.sf2"},
    {"key": "timgm6mb", "name": "TimGM6mb", "size": "5.7 MB",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/TimGM6mb.sf2"},
    {"key": "8bitsf", "name": "8bitsf (chiptune)", "size": "6.4 MB",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/8bitsf.SF2"},
    {"key": "aspirin", "name": "Aspirin GM/GS 2015", "size": "15.9 MB",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/Aspirin_160_GMGS_2015.sf2"},
    {"key": "generaluser", "name": "GeneralUser GS v1.471", "size": "29.8 MB",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/"
            "GeneralUser%20GS%20v1.471.sf2"},
    {"key": "arachno", "name": "Arachno SoundFont 1.0", "size": "148 MB (large)",
     "url": "https://archive.org/download/free-soundfonts-sf2-2019-04/"
            "Arachno_SoundFont_Version_1.0.sf2"},
]

app = Flask(__name__)


# ---------------------------------------------------------------------------
# patchbridge.py's control channel - patch select, bank, volume, panic. This
# process deliberately keeps no state of its own here; every call is a
# synchronous round trip that returns patchbridge's current truth.
# ---------------------------------------------------------------------------

def control(action, **kwargs):
    req = {"action": action}
    req.update(kwargs)
    try:
        with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=2.0) as s:
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
    except Exception as e:
        return {"error": "patchbridge unreachable: %s" % e}
    return resp


# ---------------------------------------------------------------------------
# FluidSynth's own command socket, for literal note-on/note-off. Kept as one
# persistent connection (opened lazily, reconnected on failure) rather than
# dialing fresh for every keypress - a new TCP handshake per note would make
# the on-screen keyboard feel laggy.
# ---------------------------------------------------------------------------

_fluid_lock = threading.Lock()
_fluid_sock = None


def _fluid_connect():
    global _fluid_sock
    s = socket.create_connection((FLUID_HOST, FLUID_PORT), timeout=2.0)
    time.sleep(0.05)
    try:
        s.settimeout(0.2)
        s.recv(4096)
    except socket.timeout:
        pass
    s.settimeout(2.0)
    _fluid_sock = s


def fluid_command(cmd):
    global _fluid_sock
    with _fluid_lock:
        for attempt in range(2):
            try:
                if _fluid_sock is None:
                    _fluid_connect()
                _fluid_sock.sendall((cmd + "\n").encode())
                return True
            except Exception as e:
                _fluid_sock = None
                if attempt == 1:
                    log.error("fluid command %r failed: %s", cmd, e)
                    return False


def fluid_note(midi_note, on, velocity=100, channel=0):
    midi_note = max(0, min(127, int(midi_note)))
    if on:
        velocity = max(1, min(127, int(velocity)))
        return fluid_command("noteon %d %d %d" % (channel, midi_note, velocity))
    return fluid_command("noteoff %d %d" % (channel, midi_note))


# ---------------------------------------------------------------------------
# patches.json access - always read fresh, always write atomically. This
# file is polled by patchbridge.py, so a half-written read must never happen.
# ---------------------------------------------------------------------------

def load_data():
    if not PATCHES_PATH.exists():
        return {"soundfonts": {}, "patches": []}
    return json.loads(PATCHES_PATH.read_text())


def save_data(data):
    fd, tmp = tempfile.mkstemp(dir=str(PATCHES_PATH.parent), suffix=".tmp")
    try:
        with open(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        Path(tmp).replace(PATCHES_PATH)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Saved configurations - named snapshots of the whole patches.json (which
# soundfonts are registered + the full patch table), so switching between
# e.g. a GM setup and an orchestral one is picking from a list, not
# re-importing and re-mapping every time.
# ---------------------------------------------------------------------------

def slugify(name):
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-").lower()
    return slug[:60] or "config"


def save_config(name, data=None):
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    data = data if data is not None else load_data()
    slug = slugify(name)
    payload = {"name": name, "soundfonts": data.get("soundfonts", {}),
               "patches": data.get("patches", [])}
    fd, tmp = tempfile.mkstemp(dir=str(CONFIGS_DIR), suffix=".tmp")
    with open(fd, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    Path(tmp).replace(CONFIGS_DIR / (slug + ".json"))
    return slug


# ---------------------------------------------------------------------------
# Soundfont download
# ---------------------------------------------------------------------------

def download_soundfont(url, dest_path):
    """Stream url to dest_path, capped at MAX_DOWNLOAD_BYTES. Raises on any
    problem; caller is responsible for cleaning up a partial file."""
    if not re.match(r"^https?://", url):
        raise ValueError("only http/https URLs are allowed")

    req = urllib.request.Request(url, headers={"User-Agent": "VirtualHardSynth/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        length = resp.headers.get("Content-Length")
        if length and int(length) > MAX_DOWNLOAD_BYTES:
            raise ValueError(
                "file is %.0fMB, larger than the %.0fMB limit"
                % (int(length) / 1e6, MAX_DOWNLOAD_BYTES / 1e6))

        written = 0
        with open(dest_path, "wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    raise ValueError("download exceeded the %.0fMB limit" %
                                      (MAX_DOWNLOAD_BYTES / 1e6))
                out.write(chunk)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

SYNTH_SVG_PATH = REPO_ROOT / "synth" / "assets" / "synth_control_surface.svg"


@app.route("/")
def index():
    svg = SYNTH_SVG_PATH.read_text()
    return render_template("index.html", curated=CURATED_SOUNDFONTS, synth_svg=svg)


@app.route("/api/soundfonts", methods=["GET"])
def get_soundfonts():
    data = load_data()
    out = {}
    for alias, info in data.get("soundfonts", {}).items():
        path = info["path"] if isinstance(info, dict) else info
        name = (info.get("name") if isinstance(info, dict) else None) or alias
        try:
            presets = sf2.read_presets(path)
        except Exception as e:
            presets = []
            log.warning("could not read presets from %s: %s", path, e)
        out[alias] = {
            "name": name,
            "path": path,
            "presets": [{"bank": b, "prog": p, "name": n} for b, p, n in presets],
        }
    return jsonify(out)


@app.route("/api/soundfonts", methods=["POST"])
def add_soundfont():
    body = request.get_json(force=True)
    alias = (body.get("alias") or "").strip()
    name = (body.get("name") or alias).strip()
    url = (body.get("url") or "").strip()

    if not ALIAS_RE.match(alias):
        return jsonify(error="alias must be 1-40 chars, letters/digits/-/_ only"), 400
    if not url:
        return jsonify(error="url is required"), 400

    data = load_data()
    if alias in data.get("soundfonts", {}):
        return jsonify(error="alias %r already exists" % alias), 400

    SOUNDFONTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = SOUNDFONTS_DIR / ("%s.sf2" % alias)
    tmp_dest = SOUNDFONTS_DIR / ("%s.sf2.part" % alias)

    try:
        download_soundfont(url, tmp_dest)
        presets = sf2.read_presets(tmp_dest)   # raises if not a real SF2
        if not presets:
            raise ValueError("no presets found in this soundfont")
        tmp_dest.replace(dest)
    except Exception as e:
        tmp_dest.unlink(missing_ok=True)
        log.error("soundfont add failed (%s): %s", url, e)
        return jsonify(error=str(e)), 400

    data.setdefault("soundfonts", {})[alias] = {"path": str(dest), "name": name}

    # The point of adding a soundfont is to play it, not to hand-map 100+
    # programs first: load every preset as the active patch table (identity
    # bank/prog mapping), auto-backing up whatever was active first so
    # switching soundfonts is never destructive in practice.
    backup_slug = None
    if data.get("patches"):
        backup_slug = save_config("before " + name, data)
    data["patches"] = [
        {"bank": b, "prog": p, "soundfont": alias, "src_bank": b, "src_prog": p, "name": n}
        for b, p, n in presets
    ]

    save_data(data)
    log.info("added soundfont %r (%s), %d presets, patch table replaced", alias, dest, len(presets))
    return jsonify(alias=alias, name=name, path=str(dest), preset_count=len(presets),
                   patch_table_replaced=True, backup_config=backup_slug)


@app.route("/api/soundfonts/<alias>", methods=["DELETE"])
def delete_soundfont(alias):
    data = load_data()
    if alias not in data.get("soundfonts", {}):
        return jsonify(error="no such soundfont"), 404

    before = data.get("patches", [])
    removed = [p for p in before if p.get("soundfont") == alias]
    data["patches"] = [p for p in before if p.get("soundfont") != alias]

    info = data["soundfonts"].pop(alias)
    save_data(data)
    path = Path(info["path"] if isinstance(info, dict) else info)
    if path.is_relative_to(SOUNDFONTS_DIR):
        path.unlink(missing_ok=True)
    log.info("removed soundfont %r and %d patch(es) that used it", alias, len(removed))
    return jsonify(ok=True, patches_removed=len(removed))


@app.route("/api/patches", methods=["GET"])
def get_patches():
    return jsonify(load_data().get("patches", []))


@app.route("/api/patches", methods=["POST"])
def upsert_patch():
    body = request.get_json(force=True)
    try:
        bank = int(body["bank"])
        prog = int(body["prog"])
        soundfont = str(body["soundfont"])
        src_bank = int(body["src_bank"])
        src_prog = int(body["src_prog"])
        name = str(body["name"])[:39]
    except (KeyError, ValueError, TypeError) as e:
        return jsonify(error="bad patch payload: %s" % e), 400

    data = load_data()
    if soundfont not in data.get("soundfonts", {}):
        return jsonify(error="unknown soundfont %r" % soundfont), 400

    patches = data.setdefault("patches", [])
    entry = {"bank": bank, "prog": prog, "soundfont": soundfont,
             "src_bank": src_bank, "src_prog": src_prog, "name": name}
    for i, p in enumerate(patches):
        if p["bank"] == bank and p["prog"] == prog:
            patches[i] = entry
            break
    else:
        patches.append(entry)

    save_data(data)
    return jsonify(entry)


@app.route("/api/patches/<int:bank>/<int:prog>", methods=["DELETE"])
def delete_patch(bank, prog):
    data = load_data()
    patches = data.get("patches", [])
    remaining = [p for p in patches if not (p["bank"] == bank and p["prog"] == prog)]
    if len(remaining) == len(patches):
        return jsonify(error="no such patch"), 404
    data["patches"] = remaining
    save_data(data)
    return jsonify(ok=True)


@app.route("/api/configs", methods=["GET"])
def list_configs():
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(CONFIGS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        out.append({
            "slug": p.stem,
            "name": d.get("name", p.stem),
            "patch_count": len(d.get("patches", [])),
            "soundfonts": [v.get("name", k) if isinstance(v, dict) else k
                           for k, v in d.get("soundfonts", {}).items()],
        })
    return jsonify(out)


@app.route("/api/configs", methods=["POST"])
def create_config():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="name is required"), 400
    slug = save_config(name)
    return jsonify(slug=slug, name=name)


@app.route("/api/configs/<slug>/load", methods=["POST"])
def load_config(slug):
    path = CONFIGS_DIR / (slug + ".json")
    if not path.exists():
        return jsonify(error="no such config"), 404
    d = json.loads(path.read_text())
    save_data({"soundfonts": d.get("soundfonts", {}), "patches": d.get("patches", [])})
    return jsonify(ok=True)


@app.route("/api/configs/<slug>", methods=["DELETE"])
def delete_config(slug):
    path = CONFIGS_DIR / (slug + ".json")
    if not path.exists():
        return jsonify(error="no such config"), 404
    path.unlink()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Synth control surface - state, patch/bank navigation, notes, wheels.
# ---------------------------------------------------------------------------

CONTROL_ACTIONS = {
    "patch_next", "patch_prev", "bank_next", "bank_prev",
    "patch_select", "panic", "volume_set", "get_state",
    "metronome_toggle", "quantize_set",
}


@app.route("/api/state")
def get_state():
    return jsonify(control("get_state"))


@app.route("/api/control", methods=["POST"])
def post_control():
    body = request.get_json(force=True)
    action = body.get("action")
    if action not in CONTROL_ACTIONS:
        return jsonify(error="unknown action %r" % action), 400
    kwargs = {k: v for k, v in body.items() if k != "action"}
    resp = control(action, **kwargs)
    if "error" in resp:
        return jsonify(resp), 502
    return jsonify(resp)


@app.route("/api/note", methods=["POST"])
def post_note():
    body = request.get_json(force=True)
    try:
        midi = int(body["midi"])
        on = bool(body["on"])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify(error="bad note payload: %s" % e), 400
    velocity = int(body.get("velocity", 100))
    ok = fluid_note(midi, on, velocity=velocity)
    if not ok:
        return jsonify(error="FluidSynth unreachable"), 502
    return jsonify(ok=True)


@app.route("/api/pitch", methods=["POST"])
def post_pitch():
    body = request.get_json(force=True)
    try:
        value = max(0, min(16383, int(body["value"])))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify(error="bad pitch payload: %s" % e), 400
    ok = fluid_command("pitch_bend 0 %d" % value)
    if not ok:
        return jsonify(error="FluidSynth unreachable"), 502
    return jsonify(ok=True)


@app.route("/api/cc", methods=["POST"])
def post_cc():
    """Generic MIDI control change - mod wheel (CC1) and sustain/hold
    (CC64) both go through here, since FluidSynth already understands both
    as standard MIDI with no extra work on our part."""
    body = request.get_json(force=True)
    try:
        controller = max(0, min(127, int(body["controller"])))
        value = max(0, min(127, int(body["value"])))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify(error="bad cc payload: %s" % e), 400
    ok = fluid_command("cc 0 %d %d" % (controller, value))
    if not ok:
        return jsonify(error="FluidSynth unreachable"), 502
    return jsonify(ok=True)


@app.route("/api/health")
def get_health():
    audio_ok = fluid_command("get synth.gain")
    state = control("get_state")
    return jsonify(
        audio=audio_ok,
        midi=bool(state.get("midi")) if "error" not in state else False,
        control=("error" not in state),
    )


def main():
    ap = argparse.ArgumentParser(description="VirtualHardSynth patch table web UI")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
