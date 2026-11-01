#!/usr/bin/env python3
"""
netsetup - drives the CYD panel on first boot, until setup/provision.sh has
installed FluidSynth and the bridge. Stdlib only: at this point the Pi has
nothing but the base OS, and possibly no network to fetch anything with.

Shows setup progress on the panel and, if the Pi is offline, opens the
panel's Wi-Fi picker so the rig can be put on a network from the
touchscreen alone. provision.sh stops this (vhs-netsetup.service) and
starts patchbridge when it finishes.
"""

import argparse
import json
import logging
import os
import select
import termios
import threading
import time
import tty

import wifi

log = logging.getLogger("netsetup")

STATUS_FILE = "/run/vhs/status"     # one line, written by provision.sh
STATE_S = 2.0                       # well inside the panel's 6s link timeout
WIFI_REFRESH_S = 10.0


class Port:
    """Raw 8N1 serial port without pyserial, which isn't installed yet."""

    def __init__(self, path, baud):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = getattr(termios, "B%d" % baud)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.lock = threading.Lock()

    def write(self, data):
        with self.lock:
            os.write(self.fd, data)

    def read(self, timeout):
        ready, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 256) if ready else b""


def setup_status():
    try:
        with open(STATUS_FILE) as f:
            return f.read().strip()[:39] or "starting"
    except OSError:
        return "starting"


def main():
    ap = argparse.ArgumentParser(description="first-boot CYD panel driver")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    port = Port(args.port, args.baud)

    def send(obj):
        port.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    panel = wifi.WifiPanel(send)

    def send_state():
        if panel.online:
            name = setup_status()
        else:
            name = "tap top-left for Wi-Fi"
        send({"t": "state", "bank": 0, "prog": 0, "name": name,
              "sf": "VHS-1 setup", "batt": -1, "midi": False,
              "wifi": panel.bars})

    buf = b""
    last_state = last_refresh = 0.0
    log.info("panel on %s, online=%s", args.port, panel.online)

    while True:
        buf += port.read(0.5)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line or line.startswith(b"#"):
                continue
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if panel.handle(msg):
                continue
            if msg.get("t") == "hello":
                # The panel just booted (or re-synced): bring it up to date,
                # and if there's still no network, put the picker in front.
                log.info("panel says hello, fw=%s", msg.get("fw"))
                send_state()
                if not panel.online:
                    panel.prompt()

        now = time.time()
        if now - last_refresh > WIFI_REFRESH_S:
            last_refresh = now
            panel.refresh()
        if now - last_state > STATE_S:
            last_state = now
            send_state()


if __name__ == "__main__":
    main()
