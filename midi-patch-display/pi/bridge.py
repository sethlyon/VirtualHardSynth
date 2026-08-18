#!/usr/bin/env python3
"""
patchbridge - sits between FluidSynth and a CYD front panel over USB serial.

  keyboard --> FluidSynth --> audio HAT --> speakers
                   ^
                   |  control + state
                patchbridge <--USB serial--> CYD

Control path, in order of preference:
  1. FluidSynth command socket (fluidsynth -s), which also lets us query
     loaded soundfonts and set gain.
  2. Plain MIDI Program Change / Bank Select sent to the FluidSynth input port.
     Works against any running instance with no reconfiguration.

Patch names are read straight out of the .sf2 file, so they are correct even
when the command socket is unavailable.
"""

import argparse
import json
import logging
import queue
import re
import socket
import sys
import threading
import time

import serial

import sf2

log = logging.getLogger("patchbridge")

HEARTBEAT_S = 2.0
BATTERY_S = 15.0


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

    def close(self):
        try:
            self.out.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

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
                if self.port_match and self.port_match.lower() in n.lower():
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
                        if msg.type in ("note_on", "note_off"):
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
# Bridge
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self, args):
        self.args = args
        self.events = queue.Queue()
        self.chan = args.channel
        self.font_id = 1
        self.batt = None
        self.midi_present = False

        self.ctl = None
        sf2_path = args.sf2
        try:
            self.ctl = FluidTCP(args.fluid_host, args.fluid_port)
            fonts = self.ctl.fonts()
            log.info("FluidSynth command socket up, fonts=%s", fonts)
            if fonts:
                self.font_id = fonts[0][0]
                if not sf2_path:
                    sf2_path = fonts[0][1]
        except Exception as e:
            log.warning("no FluidSynth command socket (%s), falling back to MIDI", e)
            try:
                self.ctl = FluidMIDI(args.fluid_midi_match)
            except Exception as e2:
                log.error("no control path available: %s", e2)
                sys.exit(1)

        if not sf2_path:
            log.error("could not determine the soundfont path - pass --sf2")
            sys.exit(1)

        self.presets = sf2.read_presets(sf2_path)
        if not self.presets:
            log.error("no presets found in %s", sf2_path)
            sys.exit(1)
        self.sf_name = sf2_path.split("/")[-1].replace(".sf2", "")[:27]
        log.info("%d presets loaded from %s", len(self.presets), sf2_path)

        self.index = 0
        self.ser = serial.Serial(args.port, args.baud, timeout=0.2)
        time.sleep(0.3)

    # -- preset navigation --------------------------------------------------

    @property
    def current(self):
        return self.presets[self.index]

    def _apply(self):
        bank, prog, _ = self.current
        try:
            self.ctl.select(self.chan, self.font_id, bank, prog)
        except Exception as e:
            log.error("patch change failed: %s", e)

    def patch_step(self, delta):
        self.index = (self.index + delta) % len(self.presets)
        self._apply()

    def bank_step(self, delta):
        cur_bank = self.current[0]
        banks = sorted({p[0] for p in self.presets})
        i = banks.index(cur_bank)
        target = banks[(i + delta) % len(banks)]
        for n, p in enumerate(self.presets):
            if p[0] == target:
                self.index = n
                break
        self._apply()

    def match_external(self, bank, prog):
        """Keyboard changed patch - move our cursor to match."""
        for n, p in enumerate(self.presets):
            if p[0] == bank and p[1] == prog:
                self.index = n
                return True
        for n, p in enumerate(self.presets):
            if p[1] == prog:
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
        bank, prog, name = self.current
        self.send({
            "t": "state",
            "bank": bank,
            "prog": prog,
            "name": name[:39],
            "sf": self.sf_name,
            "batt": self.batt if self.batt is not None else -1,
            "midi": self.midi_present,
        })

    # -- main loop ----------------------------------------------------------

    def run(self):
        SerialReader(self.ser, self.events).start()
        MidiMonitor(self.args.midi_in, self.events).start()

        self._apply()
        self.push_state()

        last_hb = 0.0
        last_batt = 0.0

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
                    elif a == "panic":
                        log.info("panic")
                        try:
                            self.ctl.panic()
                        except Exception as e:
                            log.error("panic failed: %s", e)
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


def main():
    ap = argparse.ArgumentParser(
        description="FluidSynth to CYD patch display bridge")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="CYD serial port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--sf2", default=None,
                    help="soundfont path (auto-detected if omitted)")
    ap.add_argument("--channel", type=int, default=0,
                    help="MIDI channel to drive")
    ap.add_argument("--fluid-host", default="127.0.0.1")
    ap.add_argument("--fluid-port", type=int, default=9800)
    ap.add_argument("--fluid-midi-match", default="fluid",
                    help="substring of the FluidSynth MIDI input port (fallback)")
    ap.add_argument("--midi-in", default="",
                    help="substring of the keyboard MIDI port")
    ap.add_argument("--battery", default="none", choices=["none", "pisugar"])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    Bridge(args).run()


if __name__ == "__main__":
    main()
