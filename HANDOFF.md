# Handoff

Session state as of 2026-08-18, end of day. Rewritten from scratch after a
full provisioning-to-features session - the previous version described a
state that no longer exists (the Pi was reflashed from a fresh SD card
during this session).

## Where this stands in one paragraph

The Pi is provisioned, reachable over SSH, and running the whole rig as
systemd services that survive a reboot. The panel shows real patch data,
the volume slider works end to end (drives the WM8960 HAT's hardware
level directly), the Yamaha keyboard plays through FluidSynth at full
volume, and there's now a web UI for loading soundfonts from a URL and
mapping bank/program slots to whatever preset in whatever soundfont you
want. Two known rough edges remain, both deliberately deferred: touch
input has some lag, and the panel's serial connection doesn't recover on
its own if the CYD is physically unplugged and replugged.

## Resuming on this machine

```bash
ssh vhs        # ~/.ssh/config.d/slyon.sshconfig has the Host entry already
```

That alias points at a fixed IP (`10.255.2.236` at last use), not a
hostname - **if the Pi is on a different network or that lease expired,
`ssh vhs` will fail and the IP in that config entry needs updating.**
mDNS (`vhs-pi.local`) has never successfully resolved from this Mac despite
the hostname being set correctly on the Pi; nobody has root-caused why.

sudo on the Pi is passwordless for the `vhs` user
(`/etc/sudoers.d/010_vhs-nopasswd`) - deliberate, for exactly this kind of
remote-session workflow on a single-purpose device.

## Hardware, all verified this session

| Property | Value |
|---|---|
| Pi model | Raspberry Pi 4, **2GB RAM** (1.8GiB usable) |
| OS | Raspberry Pi OS Lite (64-bit), Bookworm-based, freshly flashed today |
| Hostname / user | `vhs-pi` / `vhs`, SSH key at `~/.ssh/vhs_pi` on this Mac |
| Audio HAT | **Waveshare WM8960 Hi-Fi Sound Card HAT** - stereo codec, I2S. Confirmed by product photo + I2C address 0x1a. |
| Battery module | Almost certainly a **Waveshare PS HAT (D)** (UPS-style, 21700 cell) - inferred from an INA219 power monitor at I2C 0x43 (confirmed via register readback matching the chip's power-on-reset default) plus the product photo found alongside the WM8960 listing. Never explicitly confirmed by opening the case. |
| CYD panel | Unchanged from before - ESP32-D0WD-V3, ILI9341 320x240, XPT2046 touch, CH340 USB serial (`VID_1A86 PID_7523`), MAC `d4:e9:f4:6a:fd:9c` |
| MIDI keyboard | Yamaha MX Series - exposes 5 USB MIDI ports; the actual keyboard performance data is on port 1. Has a "MIDI cables only vs USB" setting on the keyboard itself that must be set to USB or nothing plays. |

### The WM8960 needs two things a stock driver load doesn't give you

1. `dtoverlay=wm8960-soundcard` in `/boot/firmware/config.txt` (the overlay
   ships with Raspberry Pi OS already, no Waveshare install script needed),
   reboot required.
2. **`'Left Output Mixer PCM'` and `'Right Output Mixer PCM'` playback
   switches default to off.** Digital audio never reaches the analog output
   at all until these are explicitly turned on - `speaker-test` runs with
   zero ALSA errors and produces total silence, which looks exactly like a
   deeper problem. This is a known WM8960-on-Linux gotcha, not anything rig-specific.

Reference the card by name, not index - `hw:CARD=wm8960soundcard`, not
`hw:1`. The numeric index is not guaranteed stable across boots.

## What's running, and how it starts at boot

Two systemd services, both `enable`d:

- **`fluidsynth.service`** (`synth/fluidsynth.service`) - FluidSynth itself,
  `-s -i -a alsa -o audio.alsa.device=hw:CARD=wm8960soundcard -o
  midi.autoconnect=1`. The `-i` matters: without it, running under systemd
  with no controlling terminal causes fluidsynth to panic on EOF from
  stdin. `midi.autoconnect=1` means any USB MIDI keyboard gets wired
  straight into FluidSynth's synth input automatically - no manual
  `aconnect` needed, including for keyboards that show up as multiple ALSA
  ports.
- **`patchbridge.service`** (`midi-patch-display/pi/patchbridge.service`) -
  the Python bridge. `Requires=`/`After=fluidsynth.service`, plus an
  `ExecStartPre` that polls `127.0.0.1:9800` until FluidSynth's command
  socket actually accepts connections - `After=` only guarantees the unit
  *started*, not that the socket is listening yet, and without this gate
  the first connection attempt reliably fails on every boot (it still
  recovered via `Restart=always`, but noisily and slower than necessary).

```bash
sudo systemctl status fluidsynth.service patchbridge.service
sudo journalctl -u patchbridge -f
```

### Known gap: physical unplug/replug of the CYD does not self-heal

If the CYD's USB cable is pulled and reinserted while `patchbridge.service`
is running, the Python process does not notice its serial file descriptor
is dead - it spins forever logging `serial read failed` / `serial write
failed` instead of crashing (which would let `Restart=always` fix it).
Fix today: `sudo systemctl restart patchbridge.service`. This happened
repeatedly during firmware reflashing (which requires moving the CYD to
the Mac) and got old fast. Real fix would be to detect the I/O error in
`SerialReader`/`Bridge.send` and exit the process instead of swallowing
it. **Deferred, not done.**

## Volume: one control, not two stacked ones

Originally the panel's volume slider drove FluidSynth's own `synth.gain`
(0.0-1.0) while the WM8960's hardware output levels sat wherever their
driver defaults left them. That's two independent multipliers in the
signal path, which stacked unpredictably and clipped even at a
conservative-looking slider position once the hardware levels got maxed
out separately.

Current design: **`synth.gain` is pinned to `1.0` once at bridge startup
and never touched again** (`FluidTCP.fix_gain()` / `FluidMIDI.fix_gain()`
- note `set synth.gain <value>`, not the shell's bare `gain` command,
which silently no-ops on this FluidSynth version with no error). The
panel slider instead calls `amixer -c wm8960soundcard sset Headphone/Speaker
<pct>%` directly (`set_hw_volume()` in `bridge.py`). One knob, and it's
the one that's actually audible.

Worth knowing: on this HAT, `Headphone`/`Speaker` read **-20dB at 80%**
and **+6dB at 100%** - a steep, non-linear jump concentrated at the very
top of the range, and 100% is *above* unity gain. Levels are currently
maxed (set during this session); if things sound hot or clip, pull those
back before touching anything else. `alsactl store` was run so this
persists across reboots.

## The patch table replaces "one soundfont file, presets in file order"

This is the biggest structural change this session. Previously
`bridge.py` just read whatever presets were baked into one `.sf2` file, in
the order they appeared in the file, and that order *was* the bank/program
addressing. Now:

- **`synth/patches.json`** is the single source of truth: a `soundfonts`
  dict (alias -> `{path, name}`) and a `patches` list, where each entry is
  `{bank, prog, name, soundfont, src_bank, src_prog}`. `bank`/`prog` is
  what the panel displays and what a MIDI keyboard would need to send to
  select that slot; `soundfont`/`src_bank`/`src_prog` is where the actual
  sound comes from. These are deliberately decoupled - that's what makes
  "assign whatever sound to whatever slot" possible.
- `bridge.py` polls this file's mtime every `PATCHES_POLL_S` (3s) and
  reloads live - no restart needed after editing through the web UI.
  Soundfonts referenced by the table that aren't already loaded into
  FluidSynth get `load`ed automatically, resolved by file path (not a
  hardcoded numeric font id, which FluidSynth assigns per-load-order and
  would drift).
- The default `patches.json` committed today is an **identity mapping**
  generated from the 189 presets already in `FluidR3_GM.sf2` (bank/prog
  in = bank/prog out, one soundfont alias `gm`) - today's behavior is
  unchanged unless you actually edit something.

### Web UI for editing it

- **`synth/webui.py`** (Flask) + **`synth/templates/index.html`**, runs as
  **`synth/patchweb.service`** on port 8080 - `http://vhs-pi:8080/` (or the
  IP). **Not yet installed as a systemd service on the Pi** - built and
  smoke-tested locally on the Mac (full add/browse/assign/delete lifecycle
  verified against the real `patches.json` schema, including a real
  download from archive.org), but the deploy-to-Pi step didn't happen this
  session because the Pi went unreachable.
- Add a soundfont from a curated shortlist (hand-picked subset of
  [archive.org's free-soundfonts-sf2-2019-04](https://archive.org/download/free-soundfonts-sf2-2019-04)
  collection, filtered to reasonable sizes for a 2GB Pi - that collection
  has 100+ files up to 550MB) or paste any `.sf2` URL directly.
  Downloads are capped at 300MB, validated as a real SF2 (RIFF/sfbk magic
  bytes via the existing `sf2.py` parser) before being accepted, and the
  alias is restricted to `[A-Za-z0-9_-]` to keep it path-safe.
- Browse every loaded soundfont's actual preset list (parsed straight from
  the file via `sf2.py`, independent of whether it's loaded into
  FluidSynth) and assign any preset to any bank/program slot.
- `synth/requirements.txt` has the one new dependency: `Flask`.

## What is verified this session

- Fresh Raspberry Pi OS Lite flash, WiFi, SSH, passwordless sudo - all
  working, `ssh vhs` is live.
- WM8960 HAT: full stereo output, both channels, confirmed by ear.
- Yamaha MX keyboard: plays through FluidSynth at real volume, both when
  driving `synth.gain` (retired) and the current hardware-volume design.
- `patchbridge.service` / `fluidsynth.service`: survive a full reboot
  cleanly, zero restarts needed, in the right order, thanks to the
  `ExecStartPre` socket-wait.
- CYD screen, seen by human eyes for the first time ever this session:
  legible, touch calibration good enough to use as-is.
- Volume slider: drag tracking is smooth (a touch-handling bug where
  vertical drift or a one-frame touch dropout mid-drag could
  misclassify into the panic zone was found and fixed - see below), and
  it audibly changes output level now.
- MIDI-present indicator (the dot next to the battery icon): was silently
  broken since the very first bridge run (`--midi-in ""` made the
  substring match always fail, an empty string is falsy in Python) - fixed
  both the deployed arg (now `--midi-in "Yamaha"`) and the underlying bug
  in `MidiMonitor` (empty string now means "match anything" instead of
  "match nothing").
- Panic relocated from "tap almost anywhere on the main screen" to a small
  labelled button in the status bar, next to the soundfont name. The old
  huge hit-zone made it too easy to trigger by accident while using the
  volume slider directly below it - confirmed by watching debug logs
  during a real slider test, where spurious `panic` commands showed up
  interleaved with `volume_set` ones.
- Web UI backend: full lifecycle tested locally (add soundfont from a real
  URL, browse its presets, assign a slot, reject a duplicate/invalid
  alias, reject an unknown-soundfont patch reference, refuse to delete an
  in-use soundfont, size-cap enforcement) - see git history / this session
  for the exact curl commands if it needs re-verifying.

## What is NOT verified / not done

1. **`patchweb.service` has never run on the Pi.** Built and tested
   locally only. Needs: copy `synth/webui.py`, `synth/templates/`,
   `synth/requirements.txt`, `synth/patches.json` (if not already
   present), `synth/patchweb.service` to the Pi; `pip install -r
   synth/requirements.txt`; install + enable + start the service; hit it
   from a browser and do one real end-to-end edit (assign a slot, confirm
   the panel picks it up within `PATCHES_POLL_S`).
2. **Touch lag.** User-reported, not investigated. `TOUCH_DEBOUNCE_MS`
   (180) and `TOUCH_REPEAT_MS` (420) in `config.h` are the obvious first
   place to look, but the actual cause hasn't been profiled - could be
   those constants, could be something in the touch controller polling
   itself, could be link latency. Explicitly next on the list.
3. **CYD unplug/replug self-healing** - see above, needs `SerialReader`/
   `Bridge.send` to detect the I/O error and let the process die instead
   of spinning.
4. **Battery percentage accuracy.** The INA219 reader
   (`bridge.py:_liion_voltage_to_percent`) is a hand-fit linear
   interpolation over a typical single-cell Li-ion discharge curve, not a
   calibrated fuel gauge. It reads plausible values (e.g. 18% at ~3.73V)
   but "plausible" and "accurate" are different claims - never
   cross-checked against a multimeter or the battery's own indicator, if
   it has one.
5. **Battery module identity** - inferred from I2C signature + a product
   photo, never physically confirmed by opening the case.

## Decisions taken this session (in addition to the ones already in synth/ENGINES.md)

- **Volume is entirely an ALSA/hardware-layer concern.** FluidSynth's own
  gain is fixed. If a future audio engine other than FluidSynth gets added
  (per `synth/ENGINES.md`'s resident-engines plan), the volume story
  should stay hardware-layer and not need per-engine handling.
- **Soundfont-to-patch mapping lives in a JSON file the bridge polls**,
  not in a database and not pushed to the bridge via a signal/socket. Optimizes
  for "two independent, restart-independent processes" over "instant
  propagation" - a few seconds of lag between saving an edit and it taking
  effect was judged acceptable.
- **Web UI is a separate systemd service**, not folded into
  `patchbridge.py`. Keeps the always-must-be-running panel bridge simple
  and crash-resistant, independent of a web server's dependency footprint
  (Flask, Werkzeug, Jinja2) and attack surface.
- **Soundfont downloads are capped and validated before being trusted.**
  This is the one place in the whole system that takes arbitrary URLs
  from a human and writes the result to disk / eventually feeds it to
  FluidSynth - treated as a real trust boundary (size cap, magic-byte
  validation, alias sanitization against path traversal), not just "it's
  my own Pi on my own network."

## Suggested order of work next session

1. **Deploy `patchweb.service` to the Pi** and do one real end-to-end
   patch edit through the browser, confirming the panel picks it up.
2. **Profile the touch lag** rather than guessing - instrument
   `pollTouch()` with timestamps, or just try halving
   `TOUCH_DEBOUNCE_MS`/`TOUCH_REPEAT_MS` first since that's the cheapest
   experiment.
3. Fix the **CYD replug self-heal** gap (detect serial I/O errors, exit
   the process, let `Restart=always` do its job).
4. If there's appetite: physically confirm the battery HAT model and
   sanity-check the INA219 percentage curve against a real measurement.

## Repo layout (updated)

```
VirtualHardSynth/
  IDEA.md, README.md, HANDOFF.md   (this file)
  midi-patch-display/
    src/                 firmware: config.h, main.cpp, ui.cpp/h
    platformio.ini        upload_speed is 115200, not the espressif default
                           921600 - this Mac's CH340/cable combo corrupted
                           reliably at the higher speed, dropped it deliberately
    pi/                   bridge.py, sf2.py, demo.py, touchtest.py,
                           patchbridge.service, 99-midi-panel.rules
  synth/
    IDEA.md, ENGINES.md    requirements/decisions, engine comparison
    fluidsynth.service     new this session
    patches.json           new this session - the patch table
    webui.py                new this session - Flask app
    templates/index.html    new this session
    patchweb.service         new this session - not yet deployed
    requirements.txt         new this session - just Flask
```

Firmware flashing from this Mac: `pio run -t upload --upload-port
/dev/cu.usbserial-210` (or whatever the CYD enumerates as) - CYD must be
plugged into the Mac directly, not the Pi, for this.
