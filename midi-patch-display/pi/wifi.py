"""
wifi - lets the CYD panel put the Pi on a Wi-Fi network, through
NetworkManager's nmcli.

Shared by bridge.py (normal running) and netsetup.py (first boot, before
anything beyond the base OS is installed), so stdlib only.

  CYD -> Pi  {"t":"cmd","action":"wifi_scan"}
  CYD -> Pi  {"t":"wifi_join","ssid":"Home","psk":"..."}
  Pi -> CYD  {"t":"wifi","st":"busy","msg":"scanning..."}
  Pi -> CYD  {"t":"wifi","st":"list","cur":"Home","ip":"192.168.50.20",
              "nets":[{"s":"Home","b":4,"l":true,"c":true}, ...]}
  Pi -> CYD  {"t":"wifi","st":"ok","ssid":"Home","ip":"192.168.50.20"}
  Pi -> CYD  {"t":"wifi","st":"fail","msg":"wrong password?"}

A "list" frame with "prompt":true makes the panel open the picker by itself.
"""

import logging
import subprocess
import threading

log = logging.getLogger("wifi")

MAX_NETS = 6     # matches WIFI_MAX_NETS in the firmware's ui.h


def _nmcli(*args, timeout=40):
    return subprocess.run(["nmcli", *args], capture_output=True, text=True,
                          timeout=timeout)


def _split_terse(line):
    """nmcli -t separates fields with ':' and backslash-escapes ':' and '\\'
    inside them - SSIDs can contain either."""
    fields, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def _bars(signal):
    if signal >= 75:
        return 4
    if signal >= 50:
        return 3
    if signal >= 25:
        return 2
    return 1 if signal > 0 else 0


def _short_error(stderr):
    text = (stderr or "").strip()
    if "Secrets were required" in text or "802-1x" in text or "psk" in text.lower():
        return "wrong password?"
    if "No network with SSID" in text:
        return "network not found"
    if "timed out" in text.lower() or "Timeout" in text:
        return "timed out"
    first = text.splitlines()[-1] if text else "failed"
    return first.replace("Error: ", "")[:40]


def scan(rescan=True):
    """Strongest few networks, the current one first, as panel 'nets'."""
    r = _nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
               "--rescan", "yes" if rescan else "no")
    if r.returncode != 0:
        raise RuntimeError(_short_error(r.stderr))
    best = {}
    for line in r.stdout.splitlines():
        f = _split_terse(line)
        if len(f) < 4 or not f[1]:
            continue                 # hidden networks have no SSID to show
        in_use, ssid = f[0] == "*", f[1]
        signal = int(f[2]) if f[2].isdigit() else 0
        locked = f[3] not in ("", "--")
        # One SSID is often several access points / bands - keep the best.
        prev = best.get(ssid)
        if prev is None or signal > prev[0]:
            best[ssid] = (signal, locked, in_use or (prev is not None and prev[2]))
        elif in_use:
            best[ssid] = (prev[0], prev[1], True)
    ordered = sorted(best.items(), key=lambda kv: (not kv[1][2], -kv[1][0]))
    return [{"s": ssid, "b": _bars(sig), "l": locked, "c": cur}
            for ssid, (sig, locked, cur) in ordered[:MAX_NETS]]


def current():
    """(ssid, ip, bars) for the active Wi-Fi connection. ssid is None when
    not on Wi-Fi; ip is whatever address the Pi has, Ethernet included."""
    ssid, bars = None, -1
    r = _nmcli("-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi", "list",
               "--rescan", "no", timeout=10)
    for line in r.stdout.splitlines():
        f = _split_terse(line)
        if len(f) >= 3 and f[0] == "yes" and f[1]:
            ssid = f[1]
            bars = _bars(int(f[2]) if f[2].isdigit() else 0)
            break
    ip = ""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                             timeout=5).stdout.split()
        ip = out[0] if out else ""
    except Exception:
        pass
    return ssid, ip, bars


def _delete_profiles(ssid):
    r = _nmcli("-t", "-f", "NAME,TYPE", "con", "show", timeout=10)
    for line in r.stdout.splitlines():
        f = _split_terse(line)
        if len(f) >= 2 and f[0] == ssid and "wireless" in f[1]:
            _nmcli("con", "delete", f[0], timeout=10)


def join(ssid, psk):
    # An old profile for this SSID would be reused with its old password.
    _delete_profiles(ssid)
    args = ["--wait", "30", "dev", "wifi", "connect", ssid]
    if psk:
        args += ["password", psk]
    r = _nmcli(*args, timeout=45)
    if r.returncode != 0:
        # Don't leave a profile behind that keeps retrying a bad password.
        _delete_profiles(ssid)
        raise RuntimeError(_short_error(r.stderr))


class WifiPanel:
    """Answers the panel's Wi-Fi frames. Scans and joins take seconds, so
    they run on a worker thread; send(obj) is called from that thread and
    must be safe to call from there."""

    def __init__(self, send):
        self.send = send
        self._busy = threading.Lock()
        self.status = (None, "", -1)
        self.refresh()

    @property
    def bars(self):
        return self.status[2]

    @property
    def online(self):
        return bool(self.status[1])

    def refresh(self):
        try:
            self.status = current()
        except Exception as e:
            log.debug("wifi status unavailable: %s", e)
            self.status = (None, "", -1)
        return self.status

    def handle(self, msg):
        """Returns True if msg was a Wi-Fi frame (and has been dealt with)."""
        if msg.get("t") == "cmd" and msg.get("action") == "wifi_scan":
            self._spawn(self._scan)
        elif msg.get("t") == "wifi_join":
            self._spawn(self._join, str(msg.get("ssid", ""))[:32],
                        str(msg.get("psk", ""))[:63])
        else:
            return False
        return True

    def prompt(self):
        """Open the picker on the panel unasked (first boot, no network)."""
        self._spawn(self._send_list, True)

    def _spawn(self, fn, *args):
        if not self._busy.acquire(blocking=False):
            self.send({"t": "wifi", "st": "busy", "msg": "still working..."})
            return

        def run():
            try:
                fn(*args)
            except subprocess.TimeoutExpired:
                self.send({"t": "wifi", "st": "fail", "msg": "timed out"})
            except Exception as e:
                log.warning("wifi: %s", e)
                self.send({"t": "wifi", "st": "fail", "msg": str(e)[:40]})
            finally:
                self._busy.release()

        threading.Thread(target=run, name="wifi", daemon=True).start()

    def _send_list(self, prompt=False):
        nets = scan()
        ssid, ip, _ = self.refresh()
        frame = {"t": "wifi", "st": "list", "cur": ssid or "", "ip": ip,
                 "nets": nets}
        if prompt:
            frame["prompt"] = True
        self.send(frame)

    def _scan(self):
        self.send({"t": "wifi", "st": "busy", "msg": "scanning..."})
        self._send_list()

    def _join(self, ssid, psk):
        if not ssid:
            raise RuntimeError("no network chosen")
        log.info("joining %s", ssid)
        self.send({"t": "wifi", "st": "busy", "msg": "joining %s..." % ssid[:20]})
        join(ssid, psk)
        ssid, ip, _ = self.refresh()
        log.info("on %s as %s", ssid, ip)
        self.send({"t": "wifi", "st": "ok", "ssid": ssid or "", "ip": ip})
