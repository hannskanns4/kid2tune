"""
multiroom_manager.py – Multiroom sync via LMS player synchronization

When scanning a master card:
1. Redirect squeezelite of the other boxes to this box's LMS
2. Synchronize all players in LMS
3. Scan again -> reset all back to localhost
"""
import json
import os
import socket
import subprocess
import time
import logging
import threading
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MULTI] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = "/tmp/multiroom_active"
JOIN_TIMEOUT = 15
PLAYER_WAIT = 6  # Seconds to wait for squeezelite to register

import sys
sys.path.insert(0, DIR)
import config_manager
import lms_client


def _load_config() -> dict:
    return config_manager.read_config()


def _write_state(state: dict):
    """Writes state file atomically (tmp + rename)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def get_own_ip() -> str:
    """Determines own LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def discover_boxes() -> list:
    """Scans the local subnet for other kid2tune boxes (port 80, /api/multiroom/status)."""
    own_ip = get_own_ip()
    if not own_ip:
        return []
    subnet = ".".join(own_ip.split(".")[:3])
    found = []

    def check(ip):
        if ip == own_ip:
            return None
        try:
            r = requests.get(f"http://{ip}:80/api/multiroom/status", timeout=1)
            if r.ok:
                data = r.json()
                # Only real kid2tune boxes (response must contain "role" or "active")
                if "active" in data or "role" in data:
                    return ip
        except Exception:
            pass
        return None

    from concurrent.futures import ThreadPoolExecutor
    log.info(f"Scanning subnet {subnet}.0/24 for kid2tune boxes...")
    with ThreadPoolExecutor(max_workers=50) as ex:
        results = list(ex.map(check, [f"{subnet}.{i}" for i in range(1, 255)]))
    found = [r for r in results if r]
    log.info(f"{len(found)} box(es) found: {found}")
    return found


def is_synced() -> bool:
    """Checks if multiroom is active."""
    return os.path.exists(STATE_FILE)


def get_status() -> dict:
    """Returns the current multiroom status."""
    if not os.path.exists(STATE_FILE):
        return {"active": False, "role": "independent"}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"active": False, "role": "independent"}


def _call_box(host: str, endpoint: str, data: dict = None) -> bool:
    """Sends a POST request to another box."""
    url = f"http://{host}:80{endpoint}"
    try:
        r = requests.post(url, json=data or {}, timeout=JOIN_TIMEOUT)
        result = r.json()
        if result.get("ok"):
            log.info(f"{host}: {result.get('message', 'OK')}")
            return True
        else:
            log.warning(f"{host}: {result.get('message', 'Error')}")
            return False
    except Exception as e:
        log.warning(f"{host} not reachable: {e}")
        return False


def activate_master():
    """Activates multiroom: redirect slaves, synchronize players."""
    own_ip = get_own_ip()
    if not own_ip:
        log.error("Could not determine own IP.")
        return False

    boxes = discover_boxes()
    if not boxes:
        log.warning("No other kid2tune boxes found on the network.")
        return False

    log.info(f"Activating multiroom as master ({own_ip}), {len(boxes)} box(es)...")

    # Redirect slaves
    joined = []
    for box in boxes:
        if _call_box(box, "/api/multiroom/join", {"master_ip": own_ip}):
            joined.append(box)

    if not joined:
        log.warning("No box could join.")
        return False

    # Polling: wait until slave players register with LMS
    expected = len(joined) + 1  # Slaves + Master
    my_id = None
    players = []
    for attempt in range(15):  # max 30 seconds (15 x 2s)
        time.sleep(2)
        players = lms_client.get_all_players()
        if not my_id:
            my_id = lms_client._get_player_id()
        log.info(f"Waiting for players... {len(players)}/{expected} registered (attempt {attempt+1})")
        if len(players) >= expected:
            break

    if not my_id:
        log.error("Own player not found.")
        return False

    # Synchronize all players
    synced = []
    for p in players:
        pid = p.get("playerid", "")
        name = p.get("name", "")
        if pid and pid != my_id:
            lms_client.sync_to(pid, my_id)
            synced.append(name)
            log.info(f"Player '{name}' synchronized.")

    # If music was playing, ensure playback continues (sync takes over playlist,
    # but sometimes play needs to be triggered again)
    status = lms_client.get_status()
    if status.get("mode") == "play":
        log.info("Music playing – resuming playback on all players.")
        lms_client._player_cmd(["play"])

    # Save state (atomically)
    _write_state({
        "active": True,
        "role": "master",
        "master_ip": own_ip,
        "joined_boxes": joined,
        "synced_players": synced,
    })

    log.info(f"Multiroom active: {len(synced)} player(s) synchronized.")
    return True


def deactivate_master():
    """Deactivates multiroom: unsync all players, return slaves to localhost."""
    log.info("Deactivating multiroom...")
    lms_client.invalidate_player_cache()

    # Unsync all players
    players = lms_client.get_all_players()
    for p in players:
        pid = p.get("playerid", "")
        if pid:
            lms_client.unsync(pid)

    # Send slaves back
    state = get_status()
    boxes = state.get("joined_boxes", [])
    for box in boxes:
        _call_box(box, "/api/multiroom/leave")

    # Remove state
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    log.info("Multiroom deactivated – all boxes independent again.")
    return True


PID_FILE = "/tmp/squeezelite_multiroom.pid"


def _kill_manual_squeezelite():
    """Terminates a manually started squeezelite using the saved PID."""
    if os.path.exists(PID_FILE):
        try:
            pid = int(open(PID_FILE).read().strip())
            os.kill(pid, 15)  # SIGTERM
            time.sleep(1)
            try:
                os.kill(pid, 9)  # SIGKILL if still running
            except ProcessLookupError:
                pass
        except (ValueError, ProcessLookupError, OSError):
            pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def join_master(master_ip: str):
    """Called on slave boxes: redirect squeezelite to master."""
    hostname = socket.gethostname()
    log.info(f"Redirecting squeezelite to master {master_ip}...")

    # Terminate previous manual squeezelite if any
    _kill_manual_squeezelite()

    # Stop systemd service
    subprocess.run(["systemctl", "stop", "squeezelite"],
                   capture_output=True, timeout=10)
    time.sleep(1)

    # Start squeezelite manually with master IP
    proc = subprocess.Popen(
        ["/usr/bin/squeezelite", "-n", hostname, "-s", master_ip, "-o", "default",
         "-b", "512:1024"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # Save PID for clean shutdown
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    # Wait until squeezelite has registered with the master LMS
    time.sleep(3)

    # Save state (atomically)
    _write_state({
        "active": True,
        "role": "slave",
        "master_ip": master_ip,
    })

    log.info(f"Squeezelite redirected to {master_ip} (PID {proc.pid}).")
    return True


def leave_master():
    """Called on slave boxes: reset squeezelite back to localhost."""
    log.info("Resetting squeezelite to localhost...")
    lms_client.invalidate_player_cache()

    # Cleanly terminate manually started squeezelite
    _kill_manual_squeezelite()

    # Restart systemd service (goes back to 127.0.0.1)
    subprocess.run(["systemctl", "start", "squeezelite"],
                   capture_output=True, timeout=10)

    # Remove state
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    log.info("Squeezelite back on localhost.")
    return True


# ── Targeted Sync Functions (for Dashboard) ─────────────────────────────────

_sync_lock = threading.Lock()


def sync_boxes(box_ips: list) -> dict:
    """Synchronizes selected boxes with this box as master."""
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "message": "Sync already in progress."}
    try:
        return _do_sync(box_ips)
    finally:
        _sync_lock.release()


def _do_sync(box_ips: list) -> dict:
    own_ip = get_own_ip()
    if not own_ip:
        return {"ok": False, "message": "Could not determine own IP."}

    if not box_ips:
        return {"ok": False, "message": "No boxes selected."}

    state = get_status()
    already_joined = state.get("joined_boxes", []) if state.get("role") == "master" else []
    new_boxes = [ip for ip in box_ips if ip not in already_joined and ip != own_ip]

    if not new_boxes and not already_joined:
        return {"ok": False, "message": "No new boxes to synchronize."}

    my_id = lms_client._get_player_id()
    if not my_id:
        return {"ok": False, "message": "Own player not found."}

    log.info(f"Synchronizing {len(new_boxes)} new box(es) as master ({own_ip})...")

    # Redirect new slaves (parallel)
    from concurrent.futures import ThreadPoolExecutor
    joined = list(already_joined)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(lambda ip=ip: _call_box(ip, "/api/multiroom/join", {"master_ip": own_ip})): ip for ip in new_boxes}
        for future in futures:
            box_ip = futures[future]
            try:
                result = future.result(timeout=10)
                if result:
                    joined.append(box_ip)
            except Exception:
                log.warning(f"Box {box_ip} join failed.")

    # Wait until NEW players register (max 20s, only count new ones)
    new_hostnames = set()
    for box_ip in new_boxes:
        try:
            r = requests.get(f"http://{box_ip}:80/api/version", timeout=2)
            h = r.json().get("hostname", "")
            if h:
                new_hostnames.add(h.lower())
        except Exception:
            pass

    synced = []
    for attempt in range(10):
        time.sleep(2)
        players = lms_client.get_all_players()
        new_connected = [p for p in players
                         if p.get("connected") and p.get("name", "").lower() in new_hostnames]
        log.info(f"Waiting for new players... {len(new_connected)}/{len(new_boxes)} (attempt {attempt+1})")
        if len(new_connected) >= len(new_boxes):
            break

    # Remember if music was playing
    was_playing = lms_client.get_status().get("mode") == "play"

    # Only sync NEW players (hostname-based)
    players = lms_client.get_all_players()
    for p in players:
        pid = p.get("playerid", "")
        name = p.get("name", "")
        connected = p.get("connected", 0)
        if pid and connected and name.lower() in new_hostnames:
            lms_client.sync_to(pid, my_id)
            synced.append(name)
            log.info(f"Player '{name}' ({pid}) added to sync group.")

    # sync_to stops playback — restart if music was playing
    if was_playing and synced:
        time.sleep(1)
        lms_client._player_cmd(["play"])
        log.info("Playback resumed after sync.")

    # Save state
    _write_state({
        "active": True,
        "role": "master",
        "master_ip": own_ip,
        "joined_boxes": joined,
        "synced_players": synced,
    })

    return {"ok": True, "message": f"{len(joined)} box(es) synchronized.", "joined": joined}


def unsync_box(box_ip: str) -> dict:
    """Removes a single box from the sync group (from master)."""
    state = get_status()
    if state.get("role") != "master":
        return {"ok": False, "message": "Only the master can unsync boxes."}

    joined = state.get("joined_boxes", [])
    if box_ip not in joined:
        return {"ok": False, "message": f"{box_ip} is not synchronized."}

    log.info(f"Unsyncing {box_ip}...")

    # Unsync this box's player in LMS
    players = lms_client.get_all_players()
    for p in players:
        pid = p.get("playerid", "")
        name = p.get("name", "").lower()
        # Try to find the player by hostname
        try:
            r = requests.get(f"http://{box_ip}:80/api/version", timeout=2)
            remote_hostname = r.json().get("hostname", "").lower()
            if name == remote_hostname and pid:
                lms_client.unsync(pid)
                log.info(f"Player '{name}' unsynced.")
        except Exception:
            pass

    # Send slave back
    _call_box(box_ip, "/api/multiroom/leave")

    # Update state
    joined.remove(box_ip)
    if joined:
        _write_state({
            "active": True,
            "role": "master",
            "master_ip": state.get("master_ip", ""),
            "joined_boxes": joined,
            "synced_players": state.get("synced_players", []),
        })
        return {"ok": True, "message": f"{box_ip} unsynced. {len(joined)} box(es) still synchronized."}
    else:
        # Last box removed -> clear master status
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        return {"ok": True, "message": f"{box_ip} unsynced. Multiroom ended."}


def unsync_all() -> dict:
    """Removes all boxes from sync (from master)."""
    state = get_status()
    if state.get("role") == "master":
        deactivate_master()
        return {"ok": True, "message": "All boxes unsynced."}
    elif state.get("role") == "slave":
        leave_master()
        return {"ok": True, "message": "Left sync."}
    return {"ok": True, "message": "No sync active."}
