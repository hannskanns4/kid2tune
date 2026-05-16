"""
boot_timing.py – Records cold-boot milestones to /var/lib/lms-controller/boot_timing.json.

Run as a systemd oneshot service started at multi-user.target. Polls each
milestone until reachable, records the uptime delta from power-on. Writes
the JSON file once all milestones are reached or a global timeout fires.

Milestones:
  multi_user      – multi-user.target reached (from systemd-analyze)
  lms_port_9000   – LMS HTTP port responsive
  lms_rpc_ready   – LMS JSON-RPC returns a player list
  spotty_ready    – LMS reports the Spotty plugin as available
  squeezelite     – local squeezelite has registered as a player with LMS
  kid2tune_web    – kid2tune Flask web UI responds on port 80
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

OUT_DIR = "/var/lib/lms-controller"
OUT_FILE = os.path.join(OUT_DIR, "boot_timing.json")
GLOBAL_TIMEOUT = 240  # seconds — give up after this
POLL_INTERVAL = 1.0


def uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def lms_rpc(params, timeout: float = 2.0):
    body = json.dumps({"id": 1, "method": "slim.request", "params": params}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:9000/jsonrpc.js",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("result", {})


def players_ready() -> bool:
    try:
        return bool(lms_rpc(["", ["players", 0, 5]]).get("players_loop"))
    except Exception:
        return False


def spotty_ready() -> bool:
    try:
        return lms_rpc(["", ["can", "spotty", "items", "?"]]).get("_can") == 1
    except Exception:
        return False


def squeezelite_registered() -> bool:
    """True if this host's name appears in the LMS player list."""
    hostname = socket.gethostname().lower()
    try:
        players = lms_rpc(["", ["players", 0, 50]]).get("players_loop", [])
        for p in players:
            if p.get("name", "").lower() == hostname:
                return True
        return False
    except Exception:
        return False


def kid2tune_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:80/api/status", timeout=2):
            return True
    except Exception:
        return False


def multi_user_seconds() -> float:
    """Reads total userspace startup time from `systemd-analyze`.
    Output line example: "Startup finished in 6.854s (kernel) + 20.282s (userspace) = 27.136s"
    Returns the total (kernel + userspace) in seconds.
    """
    import re
    try:
        r = subprocess.run(
            ["systemd-analyze"], capture_output=True, text=True, timeout=5,
        )
        line = r.stdout.splitlines()[0] if r.stdout else ""
        m = re.search(r"=\s*([0-9.]+)s\s*$", line)
        return float(m.group(1)) if m else 0.0
    except Exception:
        return 0.0


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    boot_start_uptime = uptime()
    # `systemd-analyze` may not be ready right as multi-user.target fires;
    # if so, use the script's own start time as the multi-user marker.
    mu = multi_user_seconds()
    if not mu:
        mu = boot_start_uptime
    timings = {
        "recorded_at": time.time(),
        "multi_user":    round(mu, 2),
        "lms_port_9000": None,
        "lms_rpc_ready": None,
        "spotty_ready":  None,
        "squeezelite":   None,
        "kid2tune_web":  None,
        "complete":      False,
    }

    def remember(key, value):
        if timings.get(key) is None:
            timings[key] = round(value, 2)

    checks = [
        ("lms_port_9000", lambda: tcp_open("127.0.0.1", 9000)),
        ("lms_rpc_ready", players_ready),
        ("spotty_ready",  spotty_ready),
        ("squeezelite",   squeezelite_registered),
        ("kid2tune_web",  kid2tune_ready),
    ]

    deadline = boot_start_uptime + GLOBAL_TIMEOUT
    while uptime() < deadline:
        for key, fn in checks:
            if timings[key] is None:
                try:
                    if fn():
                        remember(key, uptime())
                except Exception:
                    pass
        if all(timings[k] is not None for k, _ in checks):
            timings["complete"] = True
            break
        time.sleep(POLL_INTERVAL)

    timings["measured_until"] = round(uptime(), 2)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(timings, f, indent=2)
    os.replace(tmp, OUT_FILE)


if __name__ == "__main__":
    main()
