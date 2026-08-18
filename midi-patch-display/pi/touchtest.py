#!/usr/bin/env python3
"""
touchtest.py - touch calibration and command diagnostic.

Same as demo.py, but it puts the CYD into calibration mode so every touch
reports its raw XPT2046 coordinates, the screen coordinates they mapped to, and
which button zone that resolved to.

    python touchtest.py --port COM6

Then touch the four buttons along the bottom, left to right, and report what
prints. The summary at exit gives the raw ranges to put into src/config.h.
"""

import argparse
import json
import re
import time

import serial

PRESETS = [
    (0, 0, "Acoustic Grand"), (0, 4, "Rhodes MkI"), (0, 48, "String Ensemble"),
    (8, 4, "Detuned EP"), (8, 48, "Slow Strings"),
    (128, 0, "Standard Kit"), (128, 8, "Room Kit"),
]

RAW_RE = re.compile(r"raw x=\s*(\d+) y=\s*(\d+) z=\s*(\d+)\s+->\s+screen x=\s*(\d+) y=\s*(\d+)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.2)
    time.sleep(2.0)
    ser.reset_input_buffer()

    idx = 0
    buf = b""
    last_push = 0.0
    raw_x, raw_y = [], []
    cmds = []

    def push():
        bank, prog, name = PRESETS[idx]
        ser.write((json.dumps({"t": "state", "bank": bank, "prog": prog,
                               "name": name, "sf": "TOUCHTEST",
                               "batt": 87, "midi": True}) + "\n").encode())

    def banks():
        return sorted({p[0] for p in PRESETS})

    ser.write(b"cal\n")          # enable raw touch reporting
    time.sleep(0.3)
    push()

    print("Calibration mode on. Touch each bottom button LEFT to RIGHT,")
    print("then each screen corner. Ctrl-C when done.\n")

    try:
        while True:
            chunk = ser.read(128)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    s = line.strip().decode("utf-8", "replace")
                    if not s:
                        continue
                    if s.startswith("#"):
                        print(s)
                        m = RAW_RE.search(s)
                        if m:
                            raw_x.append(int(m.group(1)))
                            raw_y.append(int(m.group(2)))
                        continue
                    try:
                        msg = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("t") == "hello":
                        print(f"<- hello fw={msg.get('fw')}")
                        ser.write(b"cal\n")
                        time.sleep(0.2)
                        push()
                    elif msg.get("t") == "cmd":
                        a = msg.get("action")
                        cmds.append(a)
                        print(f"<- COMMAND: {a}")
                        if a == "patch_next":
                            idx = (idx + 1) % len(PRESETS)
                        elif a == "patch_prev":
                            idx = (idx - 1) % len(PRESETS)
                        elif a in ("bank_next", "bank_prev"):
                            bl = banks()
                            cur = PRESETS[idx][0]
                            step = 1 if a == "bank_next" else -1
                            tgt = bl[(bl.index(cur) + step) % len(bl)]
                            idx = next(n for n, p in enumerate(PRESETS) if p[0] == tgt)
                            print(f"   bank {cur} -> {tgt}, now {PRESETS[idx]}")
                        push()

            now = time.time()
            if now - last_push > 2:
                last_push = now
                push()
    except KeyboardInterrupt:
        print("\n--- summary ---")
        if raw_x:
            print(f"raw X seen: {min(raw_x)} .. {max(raw_x)}   ({len(raw_x)} touches)")
            print(f"raw Y seen: {min(raw_y)} .. {max(raw_y)}")
            print("\nIf you touched all four corners, put those into src/config.h as")
            print("TS_RAW_MINX / TS_RAW_MAXX / TS_RAW_MINY / TS_RAW_MAXY.")
        else:
            print("No raw touch lines seen - calibration mode did not engage.")
        print(f"\ncommands received: {cmds if cmds else '(none)'}")
        ser.write(b"cal\n")
        ser.close()


if __name__ == "__main__":
    main()
