#!/usr/bin/env python3
"""
demo.py - drive the CYD with fake state, no Pi and no FluidSynth needed.

Lets you verify the display, touch zones and serial protocol immediately after
flashing. Runs on Windows against COM6 just as happily as on the Pi.

    python demo.py --port COM6
    python demo.py --port /dev/ttyUSB0

Touch the buttons and watch the patch name change. Tapping the centre sends
panic, which prints here.
"""

import argparse
import json
import time

import serial

PRESETS = [
    (0, 0, "Acoustic Grand"), (0, 4, "Rhodes MkI"), (0, 5, "Chorused EP"),
    (0, 16, "Drawbar Organ"), (0, 19, "Church Organ"), (0, 24, "Nylon Guitar"),
    (0, 33, "Fingered Bass"), (0, 48, "String Ensemble"), (0, 56, "Trumpet"),
    (0, 73, "Flute"), (0, 81, "Saw Lead"), (0, 88, "Fantasia Pad"),
    (8, 4, "Detuned EP"), (8, 16, "Percussive Organ"), (8, 48, "Slow Strings"),
    (128, 0, "Standard Kit"), (128, 8, "Room Kit"), (128, 16, "Power Kit"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    time.sleep(2.0)          # ESP32 resets when the port opens
    ser.reset_input_buffer()

    idx = 0
    batt = 87
    last_push = 0.0
    last_batt = time.time()
    buf = b""

    def push():
        bank, prog, name = PRESETS[idx]
        msg = {"t": "state", "bank": bank, "prog": prog, "name": name,
               "sf": "FluidR3_GM", "batt": batt, "midi": True}
        ser.write((json.dumps(msg) + "\n").encode())
        print(f"-> bank={bank:3d} prog={prog:3d}  {name}")

    def banks():
        return sorted({p[0] for p in PRESETS})

    print(f"demo driver on {args.port}. Ctrl-C to stop.")
    push()

    while True:
        chunk = ser.read(128)
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith(b"#"):
                    print(line.decode("utf-8", "replace"))
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue

                if msg.get("t") == "hello":
                    print(f"<- CYD hello, fw={msg.get('fw')}")
                    push()
                elif msg.get("t") == "cmd":
                    a = msg.get("action")
                    print(f"<- {a}")
                    if a == "patch_next":
                        idx = (idx + 1) % len(PRESETS)
                    elif a == "patch_prev":
                        idx = (idx - 1) % len(PRESETS)
                    elif a in ("bank_next", "bank_prev"):
                        bl = banks()
                        cur = PRESETS[idx][0]
                        tgt = bl[(bl.index(cur) + (1 if a == "bank_next" else -1)) % len(bl)]
                        idx = next(n for n, p in enumerate(PRESETS) if p[0] == tgt)
                    elif a == "panic":
                        print("   PANIC - all notes off")
                    push()

        now = time.time()
        if now - last_batt > 10:
            last_batt = now
            batt = max(5, batt - 1)
        if now - last_push > 2:
            last_push = now
            push()


if __name__ == "__main__":
    main()
