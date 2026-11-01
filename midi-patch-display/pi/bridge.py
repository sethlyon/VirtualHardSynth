#!/usr/bin/env python3
"""
patchbridge - sits between FluidSynth and a CYD front panel over USB serial.

  keyboard --> FluidSynth --> audio HAT --> speakers
                   ^
                   |  control + state
                patchbridge <--USB serial--> CYD

Control path, in order of preference:
  1. FluidSynth command socket (fluidsynth -s), which also lets us load
     soundfonts on the fly.
  2. Plain MIDI Program Change / Bank Select sent to the FluidSynth input port.
     Works against any running instance with no reconfiguration, but can only
     drive whichever single soundfont FluidSynth was started with.

The navigable patch list - which bank/program slot maps to which sound in
which soundfont - lives in patches.json (see synth/webui.py), not in any
one .sf2 file. This file is polled for changes so edits made through the web
UI show up without restarting the bridge.
"""

import argparse
import bisect
import json
import logging
import math
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

import serial

import sf2

log = logging.getLogger("patchbridge")

HEARTBEAT_S = 2.0
BATTERY_S = 15.0
PATCHES_POLL_S = 3.0
CONTROL_PORT = 9900


# ---------------------------------------------------------------------------
# FluidSynth control
# ---------------------------------------------------------------------------

class FluidTCP:
    """FluidSynth telnet-style command shell (fluidsynth -s, default :9800)."""

    def __init__(self, host="127.0.0.1", port=9800, timeout=1.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        time.sleep(0.1)
        self._drain()

    def _drain(self):
        try:
            while self.sock.recv(4096):
                pass
        except (socket.timeout, BlockingIOError):
            pass

    def cmd(self, line):
        self.sock.sendall((line + "\n").encode())
        out = b""
        deadline = time.time() + 0.4
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            out += chunk
        return out.decode("utf-8", "replace")

    def fonts(self):
        """Return [(font_id, path), ...] from the fonts command."""
        found = []
        for line in self.cmd("fonts").splitlines():
            m = re.match(r"\s*(\d+)\s+(\S.*?)\s*$", line)
            if m and m.group(2) not in ("ID", "Name"):
                found.append((int(m.group(1)), m.group(2)))
        return found

    def select(self, chan, font_id, bank, prog):
        self.cmd("select %d %d %d %d" % (chan, font_id, bank, prog))

    def panic(self):
        self.cmd("reset")

    def fix_gain(self):
        # Volume lives entirely at the ALSA/hardware layer now (see
        # set_hw_volume) so FluidSynth's own master gain just gets pinned at
        # its ceiling once and left alone. The shell's standalone "gain"
        # command doesn't exist in this fluidsynth version and silently
        # no-ops; "set synth.gain" is the one that actually works.
        self.cmd("set synth.gain 1.0")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class FluidMIDI:
    """Fallback: drive FluidSynth with Bank Select + Program Change."""

    def __init__(self, port_match="fluid"):
        import mido
        self.mido = mido
        name = self._find(port_match)
        if not name:
            raise RuntimeError("no MIDI output matching %r" % port_match)
        self.out = mido.open_output(name)
        log.info("MIDI control via %s", name)

    def _find(self, match):
        for n in self.mido.get_output_names():
            if match.lower() in n.lower():
                return n
        return None

    def select(self, chan, font_id, bank, prog):
        m = self.mido
        self.out.send(m.Message("control_change", channel=chan, control=0,
                                value=(bank >> 7) & 0x7F))
        self.out.send(m.Message("control_change", channel=chan, control=32,
                                value=bank & 0x7F))
        self.out.send(m.Message("program_change", channel=chan,
                                program=prog & 0x7F))

    def panic(self):
        m = self.mido
        for ch in range(16):
            self.out.send(m.Message("control_change", channel=ch,
                                    control=123, value=0))
            self.out.send(m.Message("control_change", channel=ch,
                                    control=120, value=0))

    def fix_gain(self):
        # No command socket here, so no master gain to pin - volume is
        # entirely the ALSA layer's job in this fallback path too.
        pass

    def close(self):
        try:
            self.out.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Hardware volume - the panel slider drives this, not FluidSynth's own gain,
# so there is exactly one control between "how loud" and clipping instead of
# two stacked multipliers.
# ---------------------------------------------------------------------------

ALSA_CARD = "wm8960soundcard"
_HW_VOLUME_CONTROLS = ("Headphone", "Speaker")


def set_hw_volume(pct):
    pct = max(0, min(100, int(pct)))
    for ctrl in _HW_VOLUME_CONTROLS:
        try:
            subprocess.run(
                ["amixer", "-c", ALSA_CARD, "sset", ctrl, "%d%%" % pct],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1.0, check=True,
            )
        except Exception as e:
            log.error("hw volume set failed (%s): %s", ctrl, e)


def get_hw_volume():
    try:
        out = subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sget", _HW_VOLUME_CONTROLS[0]],
            capture_output=True, text=True, timeout=1.0, check=True,
        ).stdout
        m = re.search(r"\[(\d+)%\]", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

# INA219 on the Waveshare PS HAT (D), read in bus-voltage-only mode: no
# shunt calibration needed since we only care about an approximate charge
# percentage, not current/power.
_INA219_ADDR = 0x43
_INA219_BUS_VOLTAGE_REG = 0x02

# Approximate open-circuit voltage -> charge % for a single-cell Li-ion
# (matches the 21700 cell the PS HAT is built for). Interpolated linearly
# between points; not fuel-gauge accurate, just enough for a UI readout.
_LIION_CURVE = [
    (3.00, 0), (3.45, 0), (3.68, 10), (3.74, 20), (3.77, 30),
    (3.79, 40), (3.82, 50), (3.87, 60), (3.92, 70), (3.98, 80),
    (4.06, 90), (4.20, 100),
]


def _liion_voltage_to_percent(voltage):
    if voltage <= _LIION_CURVE[0][0]:
        return 0
    if voltage >= _LIION_CURVE[-1][0]:
        return 100
    for (v0, p0), (v1, p1) in zip(_LIION_CURVE, _LIION_CURVE[1:]):
        if v0 <= voltage <= v1:
            return int(round(p0 + (p1 - p0) * (voltage - v0) / (v1 - v0)))
    return 0


def read_battery(kind):
    if kind == "pisugar":
        try:
            s = socket.create_connection(("127.0.0.1", 8423), timeout=0.5)
            s.sendall(b"get battery\n")
            resp = s.recv(256).decode("utf-8", "replace")
            s.close()
            m = re.search(r"([\d.]+)", resp)
            if m:
                return int(round(float(m.group(1))))
        except Exception:
            return None
    if kind == "ina219":
        try:
            import smbus2
            bus = smbus2.SMBus(1)
            raw = bus.read_word_data(_INA219_ADDR, _INA219_BUS_VOLTAGE_REG)
            bus.close()
            raw = ((raw & 0xFF) << 8) | (raw >> 8)  # INA219 words are big-endian
            voltage = (raw >> 3) * 0.004  # bits 15:3, 4mV/LSB
            return _liion_voltage_to_percent(voltage)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# MIDI monitor - keeps the screen honest when the keyboard changes patch
# ---------------------------------------------------------------------------

class MidiMonitor(threading.Thread):
    daemon = True

    def __init__(self, port_match, events):
        super().__init__(name="midimon")
        self.port_match = port_match
        self.events = events

    def run(self):
        try:
            import mido
        except ImportError:
            log.warning("mido not installed - external patch changes will not sync")
            return

        while True:
            name = None
            for n in mido.get_input_names():
                # ALSA always has a virtual "Midi Through" client, listed
                # ahead of real hardware - never what an empty/unfiltered
                # match is looking for, so skip it unless someone explicitly
                # asked for it by name.
                if not self.port_match and "midi through" in n.lower():
                    continue
                if not self.port_match or self.port_match.lower() in n.lower():
                    name = n
                    break
            if not name:
                self.events.put(("midi_present", False))
                time.sleep(3)
                continue

            log.info("monitoring MIDI input %s", name)
            self.events.put(("midi_present", True))
            bank_msb, bank_lsb = 0, 0
            try:
                with mido.open_input(name) as inp:
                    for msg in inp:
                        if msg.type == "note_on" and msg.velocity > 0:
                            self.events.put(("note", (msg.note, True, msg.velocity)))
                            self.events.put(("activity", None))
                        elif msg.type == "note_off" or msg.type == "note_on":
                            # a note_on with velocity 0 is the running-status
                            # convention for note_off - same handling either way
                            self.events.put(("note", (msg.note, False, 0)))
                            self.events.put(("activity", None))
                        elif msg.type == "control_change":
                            if msg.control == 0:
                                bank_msb = msg.value
                            elif msg.control == 32:
                                bank_lsb = msg.value
                        elif msg.type == "program_change":
                            bank = (bank_msb << 7) | bank_lsb
                            self.events.put(("external_patch", (bank, msg.program)))
            except Exception as e:
                log.warning("MIDI input closed (%s), retrying", e)
                self.events.put(("midi_present", False))
                time.sleep(2)


# ---------------------------------------------------------------------------
# Local control channel - lets other local-only processes (the web UI) drive
# the exact same patch-select/bank/panic/volume actions the CYD does, and
# read current state. Bound to 127.0.0.1 only, never exposed off-box. This
# exists so bank/program state has exactly one owner (this process) instead
# of the web UI keeping its own separate idea of "what's selected."
# ---------------------------------------------------------------------------

class ControlServer(threading.Thread):
    daemon = True

    def __init__(self, bridge, port):
        super().__init__(name="control")
        self.bridge = bridge
        self.port = port

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(8)
        log.info("control channel listening on 127.0.0.1:%d", self.port)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        try:
            conn.settimeout(2.0)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
            # Hand the request to the main loop rather than acting on it here:
            # self.ser and self.ctl's socket are already touched by the main
            # thread (and SerialReader has its own thread reading self.ser),
            # neither pyserial nor our FluidTCP wrapper promise to be safe
            # under concurrent access, so every state change funnels through
            # one thread the same way serial "cmd" messages already do.
            reply_q = queue.Queue(maxsize=1)
            self.bridge.events.put(("control", (req, reply_q)))
            try:
                resp = reply_q.get(timeout=2.0)
            except queue.Empty:
                resp = {"error": "bridge main loop did not respond in time"}
            conn.sendall((json.dumps(resp) + "\n").encode())
        except Exception as e:
            try:
                conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Serial reader
# ---------------------------------------------------------------------------

class SerialReader(threading.Thread):
    daemon = True

    def __init__(self, ser, events):
        super().__init__(name="serial")
        self.ser = ser
        self.events = events

    def run(self):
        buf = b""
        while True:
            try:
                chunk = self.ser.read(128)
            except Exception as e:
                log.error("serial read failed: %s", e)
                time.sleep(1)
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line or line.startswith(b"#"):
                    continue
                try:
                    self.events.put(("rx", json.loads(line.decode("utf-8", "replace"))))
                except json.JSONDecodeError:
                    pass


# ---------------------------------------------------------------------------
# Looper - single-track MIDI recorder/player.
#
# Deliberately lives entirely on the Pi, not the CYD: the panel never sees
# raw MIDI today (only a generic "activity" pulse), and looping needs tight
# timing plus direct access to note events and FluidSynth, both of which
# the Pi already has. The panel is just a record/play/clear control surface.
#
# Talks to FluidSynth over its own dedicated connection rather than
# Bridge.ctl's - a background playback thread and the main loop must never
# share one command-socket connection (concurrent readers/writers cross-wire
# each other's responses; see ControlServer for the same lesson learned the
# hard way with self.ser).
# ---------------------------------------------------------------------------

# Loop playback lives on its own pool of MIDI channels, never the
# performance channel (0) or the drum channel (9). Each recording pass -
# the initial take, and every overdub after it - claims the next channel
# in the pool and snapshots whatever patch is active on the performance
# channel onto it at that moment. That's what lets an overdub carry a
# different sound than the base loop: browse to a new patch, then hit REC
# to overdub, and only the new material picks it up. It also means
# browsing patches while a loop plays no longer recolors the loop itself,
# since loop playback isn't sharing the performance channel any more.
LOOP_LAYER_CHANNELS = [1, 2, 3, 4, 5, 6, 7, 8]

# Grid unit expressed in beats (quarter notes), against the metronome's BPM.
# "off" is the only falsy value and means exactly that - raw human timing.
LOOP_QUANTIZE_GRIDS = {
    "off":   None,
    "1/4":   1.0,
    "1/8":   0.5,
    "1/16":  0.25,
    "1/32":  0.125,
    "1/8t":  1.0 / 3,
    "1/16t": 1.0 / 6,
}

# Fixed base-recording lengths the loop screen can pin, in 4/4 bars. "auto"
# (Looper.bars = None) is the original behavior - length follows however
# long you actually held the recording, rounded to the nearest bar if
# quantize is on.
LOOP_BAR_OPTIONS = [1, 2, 4, 8]

# One entry per committed recording/overdub pass, oldest first. _history_pos
# is which entry we're currently "at" (-1 = empty, nothing committed yet or
# undone all the way back). Undo/redo just move that pointer; a new pass
# after undoing truncates anything ahead of it, same as a text editor.
_LoopSnapshot = namedtuple("_LoopSnapshot", "events loop_len layer_channels added_chan")


class Looper:
    # empty --rec(+count-in)--> counting_in --> recording --rec--> playing
    # playing <--rec--> overdubbing (already playing - loop keeps running)
    # playing --stop--> stopped --rec--> overdubbing (resumes playback first,
    #   so you can stop, browse to a different patch, and overdub with it)
    # stopped --play--> playing
    # clear --> empty, from any state
    def __init__(self, fluid_host, fluid_port, events_queue=None, metronome=None,
                 get_current_patch=None):
        self.fluid_host = fluid_host
        self.fluid_port = fluid_port
        # Lets a background count-in thread ask the main loop to push_state()
        # once it's done - it can't touch self.ser itself (not the main
        # thread). Also reused for the count-in's click sound.
        self.events_queue = events_queue
        self.metronome = metronome
        # () -> (font_id, bank, prog) for whatever patch is live right now -
        # called synchronously on the main thread when a recording pass
        # starts, to snapshot that pass's sound onto its layer channel.
        self.get_current_patch = get_current_patch

        self.state = "empty"
        self.events = []           # [(offset_s, note, on, velocity, chan), ...] sorted
        self.pending_overdub = []  # captured mid-overdub, merged in on finish
        self.loop_len = 0.0
        self.quantize = "off"      # key into LOOP_QUANTIZE_GRIDS
        self.bars = None           # None = auto (length from how long you recorded);
                                    # an int pins the base recording to exactly that
                                    # many bars, auto-stopping once it's reached

        self._record_start = 0.0
        self._cycle_anchor = 0.0   # monotonic time the current playback cycle began
        self._events_version = 0   # bumped whenever self.events is replaced wholesale
        self._record_gen = 0       # bumped every fresh recording, so a stale scheduled
                                    # auto-finish can tell it's been superseded
        self._layer_channels_used = []       # pool channels claimed so far this loop
        self._current_layer_chan = LOOP_LAYER_CHANNELS[0]
        self._history = []       # [_LoopSnapshot, ...], oldest first
        self._history_pos = -1   # index into _history; -1 = empty
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._play_thread = None
        self._sock = None
        self._sock_lock = threading.Lock()

        # Runs for the Looper's whole lifetime, clicking on the beat any
        # time we're actively recording or overdubbing and the metronome
        # toggle is on. This is the only thing that ever calls
        # Metronome._click() outside a count-in - the click is tied
        # entirely to capture, never runs on its own.
        threading.Thread(target=self._capture_click_loop, daemon=True,
                          name="looper-click").start()

    # -- FluidSynth I/O, isolated from Bridge.ctl -----------------------

    def _connect(self):
        if self._sock is None:
            s = socket.create_connection((self.fluid_host, self.fluid_port), timeout=2.0)
            time.sleep(0.05)
            try:
                s.settimeout(0.1)
                s.recv(4096)
            except socket.timeout:
                pass
            s.settimeout(2.0)
            self._sock = s
        return self._sock

    def _note(self, note, on, vel, chan):
        # _sock_lock: the playback thread (steady stream of note on/off)
        # and the main thread (channel select on a new pass, all-notes-off
        # on undo) all share this one socket.
        with self._sock_lock:
            try:
                s = self._connect()
                cmd = ("noteon %d %d %d" % (chan, note, vel)) if on \
                    else ("noteoff %d %d" % (chan, note))
                s.sendall((cmd + "\n").encode())
            except Exception as e:
                log.error("looper note failed: %s", e)
                self._sock = None   # reconnect on the next attempt

    def _select(self, chan, font_id, bank, prog):
        with self._sock_lock:
            try:
                s = self._connect()
                s.sendall(("select %d %d %d %d\n" % (chan, font_id, bank, prog)).encode())
            except Exception as e:
                log.error("looper channel select failed: %s", e)
                self._sock = None

    def _all_notes_off(self, chan):
        """CC 123 (All Notes Off). Used when undo drops a layer that might
        have a currently-sounding note on its channel - the event data for
        that layer is already gone from self.events, so without this a
        note it started could hang until playback next stops."""
        with self._sock_lock:
            try:
                s = self._connect()
                s.sendall(("cc %d 123 0\n" % chan).encode())
            except Exception as e:
                log.error("looper all-notes-off failed: %s", e)
                self._sock = None

    def _claim_layer_channel(self, fresh):
        """Assign the channel this recording pass plays back on, pinned to
        whatever patch is live right now. fresh=True starts a brand new
        pool (a new loop from silence); fresh=False advances to the next
        channel for an overdub layered onto the existing loop. Once the
        pool (8 channels) is exhausted, further overdubs share the most
        recent layer's channel rather than reassigning - and silently
        recoloring - an earlier one."""
        if fresh:
            self._layer_channels_used = []
        if len(self._layer_channels_used) < len(LOOP_LAYER_CHANNELS):
            chan = LOOP_LAYER_CHANNELS[len(self._layer_channels_used)]
            self._layer_channels_used.append(chan)
        else:
            chan = self._layer_channels_used[-1]
        patch = self.get_current_patch() if self.get_current_patch else None
        if patch is not None:
            font_id, bank, prog = patch
            self._select(chan, font_id, bank, prog)
        self._current_layer_chan = chan

    # -- transport --------------------------------------------------------

    def rec_button(self):
        """The panel's REC key means something different depending on where
        we are in the cycle: start a fresh (count-in'd) recording from
        silence, close out the current recording, or layer an overdub onto
        the existing loop - whether it's already playing or paused (in
        which case playback resumes first, so overdub always has something
        to overdub against). That's also the intended way to swap sounds
        mid-loop: stop, browse to a different patch, hit REC. Mid count-in,
        it's ignored - let it finish."""
        with self._lock:
            state = self.state
        if state == "empty":
            self._begin_count_in()
        elif state == "recording":
            self._finish_recording()
        elif state in ("playing", "stopped"):
            self._begin_overdub()
        elif state == "overdubbing":
            self._finish_overdub()

    def _begin_count_in(self):
        # A count-in only exists to give a click to get ready by - with the
        # metronome off there's nothing to count in to, so skip straight to
        # recording rather than sit through a silent wait.
        metro_on = bool(self.metronome and self.metronome.on)
        with self._lock:
            self._stop_flag.set()   # any previous playback thread winds down on its own
            self.events = []
            self.pending_overdub = []
            self._events_version += 1
            self._record_gen += 1
            gen = self._record_gen
            if metro_on:
                self.state = "counting_in"
                # Fixed up front, not set when the count-in finishes - that
                # lets record_event() compute a correct (negative) offset
                # for anything played during the count-in itself, instead
                # of having nothing to measure against yet.
                self._record_start = time.monotonic() + 4 * (60.0 / self.metronome.bpm)
            else:
                self.state = "recording"
                self._record_start = time.monotonic()
        self._claim_layer_channel(fresh=True)
        if metro_on:
            threading.Thread(target=self._count_in_worker, args=(gen,), daemon=True,
                              name="looper-countin").start()
        else:
            self._maybe_schedule_auto_finish(gen)

    def _count_in_worker(self, gen):
        bpm = self.metronome.bpm
        interval = 60.0 / bpm
        for beat in range(4):
            self.metronome._click(accent=(beat == 0))
            time.sleep(interval)
        with self._lock:
            if self.state != "counting_in" or self._record_gen != gen:
                return   # cleared/cancelled mid count-in
            self.state = "recording"
            # _record_start was already fixed in _begin_count_in - a few ms
            # of scheduling drift across the 4 sleeps isn't worth resyncing.
        # Only the main thread may touch self.ser (push_state); ask it to.
        if self.events_queue is not None:
            self.events_queue.put(("looper_changed", None))
        self._maybe_schedule_auto_finish(gen)

    def _maybe_schedule_auto_finish(self, gen):
        """If a fixed bar count is set, schedule the base recording to
        close itself out automatically once that many bars have elapsed -
        no need to time a REC press to stop it. A manual REC press before
        then still wins (see _auto_finish_worker); this is purely a
        convenience, matching the same hands-free reasoning as the
        count-in itself."""
        with self._lock:
            bars = self.bars
            record_start = self._record_start
        if not bars:
            return
        bpm = self.metronome.bpm if self.metronome else 100
        bar = 4 * 60.0 / bpm
        target = record_start + bars * bar
        threading.Thread(target=self._auto_finish_worker, args=(gen, target, bars),
                          daemon=True, name="looper-autofinish").start()

    def _auto_finish_worker(self, gen, target, bars):
        sleep_for = target - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        with self._lock:
            if self.state != "recording" or self._record_gen != gen:
                return   # finished manually already, or superseded by a newer pass
        self._finish_recording(forced_bars=bars)
        if self.events_queue is not None:
            self.events_queue.put(("looper_changed", None))

    def _capture_click_loop(self):
        """Clicks on the beat for as long as we're actively recording or
        overdubbing and the metronome toggle is on - checked live, so
        flipping MET mid-capture takes effect immediately. Idle (and
        silent) the rest of the time; this is the only background clock
        the metronome has."""
        last_beat = -1
        while True:
            with self._lock:
                state = self.state
                t0 = self._record_start
            active = state in ("recording", "overdubbing") and self.metronome and self.metronome.on
            if not active:
                last_beat = -1
                time.sleep(0.05)
                continue
            interval = 60.0 / self.metronome.bpm
            now = time.monotonic()
            beat = int((now - t0) / interval) if now >= t0 else -1
            if beat >= 0 and beat > last_beat:
                last_beat = beat
                self.metronome._click(accent=(beat % 4 == 0))
            next_beat_at = t0 + (beat + 1) * interval if beat >= 0 else t0
            sleep_for = next_beat_at - time.monotonic()
            time.sleep(max(0.001, min(0.02, sleep_for if sleep_for > 0 else 0.02)))

    def record_event(self, note, on, vel):
        with self._lock:
            chan = self._current_layer_chan
            if self.state in ("recording", "counting_in"):
                # During counting_in this is negative - a pickup played
                # ahead of beat 1 rather than something to drop. Same list
                # either way; _finish_recording() normalizes it.
                offset = time.monotonic() - self._record_start
                self.events.append((offset, note, on, vel, chan))
            elif self.state == "overdubbing":
                offset = (time.monotonic() - self._record_start) % max(self.loop_len, 0.001)
                self.pending_overdub.append((offset, note, on, vel, chan))

    def set_quantize(self, key):
        if key not in LOOP_QUANTIZE_GRIDS:
            return
        with self._lock:
            self.quantize = key

    def set_bars(self, value):
        """value is the wire form: "auto" or a stringified int from
        LOOP_BAR_OPTIONS. Only takes effect on the *next* recording -
        one already in flight keeps whatever it started with, since a
        scheduled auto-finish is already anchored to the old value by
        the time this fires."""
        if value == "auto":
            bars = None
        else:
            try:
                bars = int(value)
            except (TypeError, ValueError):
                return
            if bars not in LOOP_BAR_OPTIONS:
                return
        with self._lock:
            self.bars = bars

    def _grid_seconds(self):
        beats = LOOP_QUANTIZE_GRIDS.get(self.quantize)
        if not beats:
            return None
        bpm = self.metronome.bpm if self.metronome else 100
        return beats * 60.0 / bpm

    def _quantize_events(self, events, loop_len, wrap=True):
        """Snap note-on offsets to the current grid, then shift each note's
        paired note-off by that same delta so held duration survives the
        snap - quantizing both ends independently can turn a held note into
        a clipped or stretched one. Assumes on/off events for a given
        (chan, note) alternate in time order, which holds here since each
        recording pass owns its own channel. No-ops when quantize is off.

        wrap=True clamps quantized offsets into [0, loop_len) - correct for
        an overdub, which has to fit the already-established cycle. The
        base recording passes wrap=False instead: a pickup note played
        during the count-in has a genuinely negative offset at this point,
        and wrapping it would land it at the *end* of the loop instead of
        leaving it for _finish_recording() to shift back to the start."""
        grid = self._grid_seconds()
        if grid is None or not events:
            return events
        out = list(events)
        pending_delta = {}   # (chan, note) -> delta applied to its open note-on
        for i, (offset, note, on, vel, chan) in enumerate(out):
            key = (chan, note)
            if on:
                q = round(offset / grid) * grid
                if wrap and loop_len:
                    q %= loop_len
                out[i] = (q, note, on, vel, chan)
                pending_delta[key] = q - offset
            else:
                delta = pending_delta.pop(key, 0.0)
                new_off = offset + delta
                if wrap and loop_len:
                    new_off %= loop_len
                out[i] = (new_off, note, on, vel, chan)
        out.sort(key=lambda e: e[0])
        return out

    def _finish_recording(self, forced_bars=None):
        with self._lock:
            if self.state != "recording":
                return
            raw_len = max(0.1, time.monotonic() - self._record_start)
            self.events.sort(key=lambda e: e[0])
            self.events = self._quantize_events(self.events, 0, wrap=False)

            # A pickup note played during the count-in sits at a negative
            # offset here rather than having been dropped - shift
            # everything so the earliest one becomes the start of the
            # loop, growing its length by that same amount, before
            # deciding the final loop_len.
            shift = 0.0
            if self.events and self.events[0][0] < 0:
                shift = -self.events[0][0]
                self.events = [(o + shift, n, on, v, c) for (o, n, on, v, c) in self.events]
            total_len = raw_len + shift
            content_end = max((e[0] for e in self.events), default=0.0)

            grid = self._grid_seconds()
            bpm = self.metronome.bpm if self.metronome else 100
            bar = 4 * 60.0 / bpm

            if forced_bars:
                # An explicit bar count (set on the loop screen) always
                # wins, quantize on or off - never truncates real content
                # in the rare case a note landed right as the auto-finish
                # fired.
                self.loop_len = max(forced_bars * bar, content_end)
            elif grid is not None:
                # Bar-align so quantized notes don't wrap on an off-beat
                # seam - round to the *nearest* bar rather than always up.
                # Reaction time pressing REC a beat or two into what would
                # be the next bar shouldn't commit a whole extra,
                # mostly-empty measure (that was the actual bug: ceil()
                # always rounded the overshoot up). Never round below
                # where the recorded content actually ends, though.
                bars = max(1, round(total_len / bar))
                if bars * bar < content_end:
                    bars += 1
                self.loop_len = bars * bar
            else:
                self.loop_len = total_len

            self.state = "playing"
            self._checkpoint()
        self._start_playback()

    def _begin_overdub(self):
        with self._lock:
            if self.state not in ("playing", "stopped"):
                return
            resume = self.state == "stopped"
            self.state = "overdubbing"
            self.pending_overdub = []
            if resume:
                # Nothing's playing to anchor against - the resumed loop
                # starts its cycle right now, and captures align to that.
                now = time.monotonic()
                self._record_start = now
                self._cycle_anchor = now
            else:
                # Align new captures to the loop's current cycle position
                # (it's already playing/audible) rather than starting a new
                # clock.
                self._record_start = self._cycle_anchor
        self._claim_layer_channel(fresh=False)
        if resume:
            self._start_playback(start_at=self._record_start)

    def _finish_overdub(self):
        with self._lock:
            if self.state != "overdubbing":
                return
            merged = self.events + self.pending_overdub
            merged.sort(key=lambda e: e[0])
            # Re-quantizing the whole list (not just the new part) is a
            # no-op for content that's already on the grid - each layer
            # lives on its own channel, so on/off pairing never crosses
            # layers even when everything's merged together like this.
            merged = self._quantize_events(merged, self.loop_len)
            self.events = merged
            self.pending_overdub = []
            self._events_version += 1
            self.state = "playing"
            self._checkpoint()

    # -- undo / redo ----------------------------------------------------

    def _checkpoint(self):
        """Call with self._lock already held, right after a recording or
        overdub pass commits. A new pass after undoing invalidates any
        redo-able future, same as a text editor, so that gets truncated
        first."""
        del self._history[self._history_pos + 1:]
        self._history.append(_LoopSnapshot(
            events=list(self.events),
            loop_len=self.loop_len,
            layer_channels=list(self._layer_channels_used),
            added_chan=self._layer_channels_used[-1],
        ))
        self._history_pos = len(self._history) - 1

    def undo(self):
        """Step back to the loop's state before the most recent recording
        or overdub pass - repeatable, so you can keep going back further,
        all the way to a fully empty loop."""
        with self._lock:
            if self.state not in ("playing", "stopped"):
                return   # nothing sensible to undo mid-capture
            if self._history_pos < 0:
                return
            removed = self._history[self._history_pos]
            self._history_pos -= 1
            if self._history_pos >= 0:
                snap = self._history[self._history_pos]
                self.events = list(snap.events)
                self.loop_len = snap.loop_len
                self._layer_channels_used = list(snap.layer_channels)
                go_empty = False
            else:
                self.events = []
                self.loop_len = 0.0
                self._layer_channels_used = []
                go_empty = True
            self._events_version += 1
        # The undone layer's event data is already gone from self.events -
        # if one of its notes was mid-sound, it would otherwise hang.
        self._all_notes_off(removed.added_chan)
        if go_empty:
            self._stop_playback()
            with self._lock:
                self.state = "empty"

    def redo(self):
        """Step forward again after an undo. Any new recording/overdub
        pass since the undo would have already discarded this - see
        _checkpoint - so this only ever restores exactly what undo took
        away."""
        with self._lock:
            if self.state not in ("playing", "stopped", "empty"):
                return   # nothing sensible to redo mid-capture
            if self._history_pos >= len(self._history) - 1:
                return
            was_empty = self.state == "empty"
            self._history_pos += 1
            snap = self._history[self._history_pos]
            self.events = list(snap.events)
            self.loop_len = snap.loop_len
            self._layer_channels_used = list(snap.layer_channels)
            self._events_version += 1
            if was_empty:
                self.state = "stopped"   # content again, but wait for PLAY

    def toggle_play(self):
        with self._lock:
            if self.state == "playing":
                pass   # fall through to _stop_playback() below
            elif self.state == "stopped" and self.events:
                self.state = "playing"
                self._start_playback()
                return
            else:
                return
        self._stop_playback()

    def clear(self):
        self._stop_playback()
        with self._lock:
            self.state = "empty"
            self.events = []
            self.pending_overdub = []
            self.loop_len = 0.0
            self._events_version += 1
            self._layer_channels_used = []
            self._history = []
            self._history_pos = -1

    def snapshot(self):
        with self._lock:
            can_undo = self._history_pos >= 0
            can_redo = self._history_pos < len(self._history) - 1
            bars_wire = "auto" if self.bars is None else str(self.bars)
            return (self.state, round(self.loop_len, 2), self.quantize,
                    can_undo, can_redo, bars_wire)

    # -- playback thread ----------------------------------------------------

    def _start_playback(self, start_at=None):
        if self._play_thread and self._play_thread.is_alive():
            return
        self._stop_flag.clear()
        self._play_thread = threading.Thread(target=self._play_loop, args=(start_at,),
                                              daemon=True, name="looper")
        self._play_thread.start()

    def _stop_playback(self):
        self._stop_flag.set()
        if self._play_thread:
            self._play_thread.join(timeout=1.0)
        with self._lock:
            if self.state == "playing":
                self.state = "stopped"

    def _play_loop(self, start_at=None):
        held = set()
        cycle_start = start_at if start_at is not None else time.monotonic()
        with self._lock:
            self._cycle_anchor = cycle_start
            events, loop_len, version = self.events, self.loop_len, self._events_version
        idx = 0
        while not self._stop_flag.is_set():
            with self._lock:
                if self._events_version != version:
                    # An overdub just merged in - self.events was replaced
                    # wholesale, so the old idx (a position, not a time) is
                    # meaningless against it. Recompute where "now" falls in
                    # the new list rather than assume anything lines up.
                    events, loop_len, version = self.events, self.loop_len, self._events_version
                    elapsed_now = time.monotonic() - cycle_start
                    idx = bisect.bisect_right(events, elapsed_now, key=lambda e: e[0])
                else:
                    loop_len = self.loop_len
            if not events or loop_len <= 0:
                break
            elapsed = time.monotonic() - cycle_start
            if elapsed >= loop_len:
                cycle_start += loop_len
                with self._lock:
                    self._cycle_anchor = cycle_start
                idx = 0
                continue
            if idx < len(events) and events[idx][0] <= elapsed:
                _, note, on, vel, chan = events[idx]
                self._note(note, on, vel, chan)
                (held.add if on else held.discard)((chan, note))
                idx += 1
                continue
            next_due = events[idx][0] if idx < len(events) else loop_len
            time.sleep(max(0.001, min(0.02, next_due - elapsed)))
        for chan, note in list(held):
            self._note(note, False, 0, chan)


# ---------------------------------------------------------------------------
# Metronome - a click primitive plus an on/off flag, not a standalone clock.
# It never runs on its own; the Looper is what actually schedules clicks
# (during a count-in and through an active recording/overdub pass), gated
# by Metronome.on. Fixed 4/4, accented downbeat. Plays on channel 9 (MIDI
# channel 10, the GM percussion convention) using claves (note 75). Channel
# 9 only reliably has claves mapped if it's actually pointed at a real GM
# drum kit - whatever a custom/chiptune soundfont happens to auto-assign
# there on load can be empty (confirmed: silent on the "8bitsf" font's
# default). So Bridge pins channel 9 to FluidR3_GM's drum kit explicitly at
# startup (see Bridge._setup_drum_kit), independent of whatever soundfont
# is active on the performance channel - the click only depends on that
# pin holding.
# ---------------------------------------------------------------------------

METRONOME_CHAN = 9
METRONOME_NOTE = 75
GM_DRUM_SF2 = "/usr/share/sounds/sf2/FluidR3_GM.sf2"


class Metronome:
    def __init__(self, fluid_host, fluid_port, bpm=100):
        self.fluid_host = fluid_host
        self.fluid_port = fluid_port
        self.bpm = bpm
        self.on = False
        self._lock = threading.Lock()
        # Guards _sock: the looper's count-in and its capture-click loop
        # are two different background threads and could in principle hit
        # this at nearly the same moment.
        self._sock_lock = threading.Lock()
        self._sock = None

    def _connect(self):
        if self._sock is None:
            s = socket.create_connection((self.fluid_host, self.fluid_port), timeout=2.0)
            time.sleep(0.05)
            try:
                s.settimeout(0.1)
                s.recv(4096)
            except socket.timeout:
                pass
            s.settimeout(2.0)
            self._sock = s
        return self._sock

    def _click(self, accent):
        with self._sock_lock:
            try:
                s = self._connect()
                vel = 127 if accent else 90
                s.sendall(("noteon %d %d %d\n" % (METRONOME_CHAN, METRONOME_NOTE, vel)).encode())
            except Exception as e:
                log.error("metronome click failed: %s", e)
                self._sock = None

    def toggle(self):
        with self._lock:
            self.on = not self.on


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, args):
        self.args = args
        self.events = queue.Queue()
        self.chan = args.channel
        self.batt = None
        self.midi_present = False
        self.patches_path = Path(args.patches)
        self.metronome = Metronome(args.fluid_host, args.fluid_port)
        self.looper = Looper(args.fluid_host, args.fluid_port, self.events, self.metronome,
                              get_current_patch=lambda: (self.current["font_id"],
                                                          self.current["src_bank"],
                                                          self.current["src_prog"]))

        self.ctl = None
        try:
            self.ctl = FluidTCP(args.fluid_host, args.fluid_port)
            log.info("FluidSynth command socket up")
        except Exception as e:
            log.warning("no FluidSynth command socket (%s), falling back to MIDI", e)
            try:
                self.ctl = FluidMIDI(args.fluid_midi_match)
            except Exception as e2:
                log.error("no control path available: %s", e2)
                sys.exit(1)

        try:
            self.ctl.fix_gain()
        except Exception as e:
            log.warning("could not pin synth gain: %s", e)
        self.vol = get_hw_volume()
        if self.vol is None:
            self.vol = 80

        self.index = 0
        self.load_patch_table()
        self._setup_drum_kit()

        self.ser = serial.Serial(args.port, args.baud, timeout=0.2)
        time.sleep(0.3)

    def _setup_drum_kit(self):
        """Pin channel 9 to FluidR3_GM's GM drum kit so the metronome (and
        any future percussion use) clicks reliably no matter what soundfont
        is active on the performance channel. Loads FluidR3_GM if it isn't
        already resident - it's the box's default font (see
        fluidsynth.service) so this is normally a no-op lookup."""
        if not isinstance(self.ctl, FluidTCP):
            return   # MIDI fallback can't target a specific font by path
        try:
            font_id = None
            for fid, path in self.ctl.fonts():
                if path == GM_DRUM_SF2:
                    font_id = fid
                    break
            if font_id is None:
                self.ctl.cmd("load %s" % GM_DRUM_SF2)
                for fid, path in self.ctl.fonts():
                    if path == GM_DRUM_SF2:
                        font_id = fid
                        break
            if font_id is not None:
                self.ctl.select(METRONOME_CHAN, font_id, 128, 0)
                log.info("drum channel (9) pinned to %s", GM_DRUM_SF2)
            else:
                log.error("could not load %s for the drum channel", GM_DRUM_SF2)
        except Exception as e:
            log.warning("could not set up drum channel: %s", e)

    # -- patch table ----------------------------------------------------

    def _fallback_gm_patches(self):
        """Last resort for load_patch_table(): reads presets straight out
        of FluidR3_GM.sf2 (the box's own default font, always on disk -
        see fluidsynth.service and _setup_drum_kit) and builds an
        identity bank/prog patch table directly from it. Doesn't touch
        patches.json - this is purely an in-memory safety net so the
        panel stays playable until the real config gets fixed."""
        try:
            presets = sf2.read_presets(GM_DRUM_SF2)
        except Exception as e:
            log.error("could not read presets from fallback font %s: %s", GM_DRUM_SF2, e)
            return []

        font_id = None
        if isinstance(self.ctl, FluidTCP):
            try:
                for fid, path in self.ctl.fonts():
                    if path == GM_DRUM_SF2:
                        font_id = fid
                        break
                if font_id is None:
                    self.ctl.cmd("load %s" % GM_DRUM_SF2)
                    for fid, path in self.ctl.fonts():
                        if path == GM_DRUM_SF2:
                            font_id = fid
                            break
            except Exception as e:
                log.error("could not load fallback font %s: %s", GM_DRUM_SF2, e)
        if font_id is None:
            font_id = 0   # MIDI fallback, or couldn't confirm it - best effort

        return [
            {"bank": b, "prog": p, "name": n, "font_id": font_id,
             "soundfont_display": "FluidR3 GM (fallback)", "src_bank": b, "src_prog": p}
            for b, p, n in presets
        ]

    def load_patch_table(self):
        """(Re)read patches.json: resolve each referenced soundfont to a
        live FluidSynth font id (loading it if it isn't already), then
        build the navigable patch list. The web UI owns this file; we just
        follow it."""
        data = json.loads(self.patches_path.read_text())
        raw_fonts = data.get("soundfonts", {})

        loaded_by_path = {}
        if isinstance(self.ctl, FluidTCP):
            for font_id, path in self.ctl.fonts():
                loaded_by_path[path] = font_id

        font_ids = {}
        font_display = {}
        for alias, info in raw_fonts.items():
            path = info["path"] if isinstance(info, dict) else info
            font_display[alias] = (info.get("name") if isinstance(info, dict) else None) or alias
            if path in loaded_by_path:
                font_ids[alias] = loaded_by_path[path]
                continue
            if not isinstance(self.ctl, FluidTCP):
                font_ids[alias] = 0   # MIDI fallback can't target a font explicitly anyway
                continue
            try:
                self.ctl.cmd("load %s" % path)
                for font_id, p in self.ctl.fonts():
                    if p == path:
                        font_ids[alias] = font_id
                        loaded_by_path[path] = font_id
                        break
                else:
                    log.error("loaded %s but could not find it in the font list", path)
            except Exception as e:
                log.error("could not load soundfont %r (%s): %s", alias, path, e)

        patches = []
        for entry in data.get("patches", []):
            alias = entry.get("soundfont")
            if alias not in font_ids:
                log.warning("patch bank=%s prog=%s references unloaded soundfont %r, skipping",
                            entry.get("bank"), entry.get("prog"), alias)
                continue
            patches.append({
                "bank": entry["bank"],
                "prog": entry["prog"],
                "name": entry["name"],
                "font_id": font_ids[alias],
                "soundfont_display": font_display.get(alias, alias),
                "src_bank": entry["src_bank"],
                "src_prog": entry["src_prog"],
            })
        patches.sort(key=lambda p: (p["bank"], p["prog"]))
        if not patches:
            # Every soundfont patches.json references failed to load - most
            # likely one went missing from disk after being registered
            # (deleted outside the web UI, a corrupted download, an
            # unclean shutdown). That shouldn't take the whole panel down:
            # fall back to the box's own built-in GM set, which stays
            # untouched on disk either way, so the *next* successful
            # reload (once the real config is fixed) picks it back up
            # automatically.
            log.error("patch table in %s produced zero playable patches - "
                      "every referenced soundfont failed to load. Falling "
                      "back to the built-in GM set so the panel stays up.",
                      self.patches_path)
            patches = self._fallback_gm_patches()
            if not patches:
                log.error("fallback GM patch table also failed - nothing to play")
                sys.exit(1)

        self.patches = patches
        self._patches_mtime = self.patches_path.stat().st_mtime
        log.info("%d patches loaded from %s (%d soundfonts)",
                 len(patches), self.patches_path, len(font_ids))

    def maybe_reload_patches(self):
        try:
            mtime = self.patches_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._patches_mtime:
            return
        was = self.current
        self.load_patch_table()
        self.index = 0
        for n, p in enumerate(self.patches):
            if p["bank"] == was["bank"] and p["prog"] == was["prog"]:
                self.index = n
                break
        self._apply()
        self.push_state()

    # -- patch navigation -----------------------------------------------

    @property
    def current(self):
        return self.patches[self.index]

    def _apply(self):
        p = self.current
        try:
            self.ctl.select(self.chan, p["font_id"], p["src_bank"], p["src_prog"])
        except Exception as e:
            log.error("patch change failed: %s", e)

    def patch_step(self, delta):
        self.index = (self.index + delta) % len(self.patches)
        self._apply()

    def bank_step(self, delta):
        cur_bank = self.current["bank"]
        banks = sorted({p["bank"] for p in self.patches})
        i = banks.index(cur_bank)
        target = banks[(i + delta) % len(banks)]
        for n, p in enumerate(self.patches):
            if p["bank"] == target:
                self.index = n
                break
        self._apply()

    def match_external(self, bank, prog):
        """Keyboard changed patch - move our cursor to match."""
        for n, p in enumerate(self.patches):
            if p["bank"] == bank and p["prog"] == prog:
                self.index = n
                return True
        for n, p in enumerate(self.patches):
            if p["prog"] == prog:
                self.index = n
                return True
        return False

    # -- link ---------------------------------------------------------------

    def send(self, obj):
        try:
            self.ser.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
        except Exception as e:
            log.error("serial write failed: %s", e)

    def push_state(self):
        p = self.current
        # Capped to match the panel's fixed-size banks[] array (MAX_BANKS in
        # ui.h) - if a soundfont somehow has more distinct banks than that,
        # the panel's bank list is just incomplete, nothing breaks.
        banks = sorted({p["bank"] for p in self.patches})[:24]
        loop_state, loop_len, quantize, can_undo, can_redo, bars = self.looper.snapshot()
        self.send({
            "t": "state",
            "bank": p["bank"],
            "prog": p["prog"],
            "name": p["name"][:39],
            "sf": p["soundfont_display"][:27],
            "batt": self.batt if self.batt is not None else -1,
            "vol": self.vol,
            "index": self.index,
            "count": len(self.patches),
            "banks": banks,
            "midi": self.midi_present,
            "loop_state": loop_state,
            "loop_len": loop_len,
            "metro_on": self.metronome.on,
            "quantize": quantize,
            "can_undo": can_undo,
            "can_redo": can_redo,
            "bars": bars,
        })

    # -- local control channel (web UI) --------------------------------------

    def _apply_control(self, req):
        """Same actions the CYD's serial 'cmd' messages trigger, plus a
        read-only query and direct patch_select by bank/prog for the web UI's
        patch table / pad buttons. Only ever called from the main loop (see
        the "control" case in run()) - never call this from ControlServer's
        connection threads directly, self.ser and self.ctl's socket are not
        safe under concurrent access."""
        action = req.get("action")
        if action == "patch_next":
            self.patch_step(1)
        elif action == "patch_prev":
            self.patch_step(-1)
        elif action == "bank_next":
            self.bank_step(1)
        elif action == "bank_prev":
            self.bank_step(-1)
        elif action == "patch_select":
            bank, prog = req.get("bank"), req.get("prog")
            for n, p in enumerate(self.patches):
                if p["bank"] == bank and p["prog"] == prog:
                    self.index = n
                    break
            self._apply()
        elif action == "panic":
            log.info("panic (web)")
            try:
                self.ctl.panic()
            except Exception as e:
                log.error("panic failed: %s", e)
        elif action == "volume_set":
            v = req.get("value")
            if isinstance(v, (int, float)):
                self.vol = max(0, min(100, int(v)))
                set_hw_volume(self.vol)
        elif action == "metronome_toggle":
            self.metronome.toggle()
        elif action == "quantize_set":
            self.looper.set_quantize(req.get("grid"))
        elif action == "get_state":
            pass
        else:
            return {"error": "unknown action %r" % action}

        self.push_state()   # web-triggered changes reach the physical panel too
        p = self.current
        return {
            "bank": p["bank"], "prog": p["prog"], "name": p["name"],
            "soundfont": p["soundfont_display"], "vol": self.vol,
            "midi": self.midi_present, "index": self.index,
            "count": len(self.patches),
            "metro_on": self.metronome.on,
            "quantize": self.looper.quantize,
        }

    # -- main loop ----------------------------------------------------------

    def run(self):
        SerialReader(self.ser, self.events).start()
        MidiMonitor(self.args.midi_in, self.events).start()
        ControlServer(self, self.args.control_port).start()

        self._apply()
        self.push_state()

        last_hb = 0.0
        last_batt = 0.0
        last_patches = 0.0

        while True:
            now = time.time()
            try:
                kind, payload = self.events.get(timeout=0.2)
            except queue.Empty:
                kind, payload = None, None

            if kind == "rx":
                t = payload.get("t")
                if t == "hello":
                    log.info("CYD connected, fw=%s", payload.get("fw"))
                    self.push_state()
                elif t == "cmd":
                    a = payload.get("action")
                    if a == "patch_next":
                        self.patch_step(1)
                    elif a == "patch_prev":
                        self.patch_step(-1)
                    elif a == "bank_next":
                        self.bank_step(1)
                    elif a == "bank_prev":
                        self.bank_step(-1)
                    elif a == "patch_select":
                        bank, prog = payload.get("bank"), payload.get("prog")
                        found = False
                        for n, p in enumerate(self.patches):
                            if p["bank"] == bank and p["prog"] == prog:
                                self.index = n
                                found = True
                                break
                        if found:
                            self._apply()
                        else:
                            self.send({"t": "toast", "msg": "NOT FOUND"})
                    elif a == "panic":
                        log.info("panic")
                        try:
                            self.ctl.panic()
                        except Exception as e:
                            log.error("panic failed: %s", e)
                    elif a == "volume_set":
                        v = payload.get("value")
                        if isinstance(v, (int, float)):
                            self.vol = max(0, min(100, int(v)))
                            set_hw_volume(self.vol)
                    elif a == "loop_record":
                        self.looper.rec_button()
                    elif a == "loop_play":
                        self.looper.toggle_play()
                    elif a == "loop_clear":
                        self.looper.clear()
                    elif a == "loop_undo":
                        self.looper.undo()
                    elif a == "loop_redo":
                        self.looper.redo()
                    elif a == "metronome_toggle":
                        self.metronome.toggle()
                    elif a == "quantize_set":
                        self.looper.set_quantize(payload.get("grid"))
                    elif a == "loop_bars_set":
                        self.looper.set_bars(payload.get("bars"))
                    self.push_state()

            elif kind == "control":
                req, reply_q = payload
                try:
                    reply_q.put(self._apply_control(req))
                except Exception as e:
                    log.error("control action failed: %s", e)
                    reply_q.put({"error": str(e)})

            elif kind == "note":
                note, on, vel = payload
                self.looper.record_event(note, on, vel)

            elif kind == "looper_changed":
                # Count-in finished on its own background thread and flipped
                # state to "recording" - it can't touch self.ser itself, so
                # it asked us (the main thread) to push the update instead.
                self.push_state()

            elif kind == "external_patch":
                bank, prog = payload
                if self.match_external(bank, prog):
                    log.info("keyboard selected bank=%d prog=%d", bank, prog)
                    self.push_state()

            elif kind == "activity":
                self.send({"t": "act"})

            elif kind == "midi_present":
                self.midi_present = payload
                self.push_state()

            if now - last_batt > BATTERY_S:
                last_batt = now
                new = read_battery(self.args.battery)
                if new != self.batt:
                    self.batt = new
                    self.push_state()

            if now - last_hb > HEARTBEAT_S:
                last_hb = now
                self.push_state()

            if now - last_patches > PATCHES_POLL_S:
                last_patches = now
                try:
                    self.maybe_reload_patches()
                except Exception as e:
                    log.error("patch table reload failed: %s", e)


def main():
    ap = argparse.ArgumentParser(
        description="FluidSynth to CYD patch display bridge")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="CYD serial port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--patches",
                    default=str(Path(__file__).resolve().parents[2] / "synth" / "patches.json"),
                    help="patch table JSON (also written by webui.py)")
    ap.add_argument("--control-port", type=int, default=CONTROL_PORT,
                    help="localhost-only control channel for the web UI")
    ap.add_argument("--channel", type=int, default=0,
                    help="MIDI channel to drive")
    ap.add_argument("--fluid-host", default="127.0.0.1")
    ap.add_argument("--fluid-port", type=int, default=9800)
    ap.add_argument("--fluid-midi-match", default="fluid",
                    help="substring of the FluidSynth MIDI input port (fallback)")
    ap.add_argument("--midi-in", default="",
                    help="substring of the keyboard MIDI port")
    ap.add_argument("--battery", default="none",
                    choices=["none", "pisugar", "ina219"])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    Bridge(args).run()


if __name__ == "__main__":
    main()
