"""
bluetooth_manager.py – Bluetooth audio manager

Scans, pairs, and connects Bluetooth audio devices.
Switches audio output between local speakers and Bluetooth.
"""
import json
import os
import re
import subprocess
import time
import logging
import signal
from typing import List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BT] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
ASOUND_CONF = "/etc/asound.conf"
CONNECT_TIMEOUT = 30
SCAN_DURATION = 10
SCAN_LOCK = "/tmp/bt_scanning"

import sys
sys.path.insert(0, DIR)
import config_manager


# ── Configuration ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    return config_manager.read_config()


def _save_config(cfg: dict):
    config_manager.write_config(cfg)


def get_bt_config() -> dict:
    cfg = _load_config()
    return cfg.get("bluetooth", {
        "active_device": "",
        "local_asound_conf": "",
        "auto_reconnect": True,
        "check_interval": 15,
    })


# ── Adapter ──────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 15) -> str:
    """Executes a command, returns stdout. On timeout returns partial output."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except subprocess.TimeoutExpired as e:
        # On timeout return partial output (e.g. for btmgmt find)
        log.info(f"Command {cmd[0]} finished after {timeout}s (timeout, output available).")
        return (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    except Exception as e:
        log.warning(f"Command failed: {cmd}: {e}")
        return ""


def is_bluetooth_available() -> bool:
    out = _run(["bluetoothctl", "show"])
    return "Powered: yes" in out


def ensure_adapter_powered() -> bool:
    if is_bluetooth_available():
        return True
    _run(["rfkill", "unblock", "bluetooth"])
    _run(["bluetoothctl", "power", "on"])
    time.sleep(1)
    return is_bluetooth_available()


# ── Device Information ───────────────────────────────────────────────────────

def _get_device_info(mac: str) -> dict:
    """Reads detailed info about a BT device via bluetoothctl info."""
    out = _run(["bluetoothctl", "info", mac])
    info = {"mac": mac, "name": mac, "paired": False, "connected": False,
            "trusted": False, "audio": False}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            info["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Paired:"):
            info["paired"] = "yes" in line.lower()
        elif line.startswith("Connected:"):
            info["connected"] = "yes" in line.lower()
        elif line.startswith("Trusted:"):
            info["trusted"] = "yes" in line.lower()
        elif "Audio Sink" in line or "A2DP" in line or "audio" in line.lower():
            info["audio"] = True
    return info


def get_paired_devices() -> List[dict]:
    out = _run(["bluetoothctl", "devices", "Paired"])
    if not out.strip():
        out = _run(["bluetoothctl", "paired-devices"])
    devices = []
    for line in out.splitlines():
        m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
        if m:
            info = _get_device_info(m.group(1))
            info["name"] = info["name"] if info["name"] != m.group(1) else m.group(2)
            devices.append(info)
    return devices


# ── Scan ─────────────────────────────────────────────────────────────────────

def scan_devices() -> List[dict]:
    if os.path.exists(SCAN_LOCK):
        log.warning("Scan already in progress.")
        return []

    if not ensure_adapter_powered():
        log.error("Bluetooth adapter not available.")
        return []

    try:
        with open(SCAN_LOCK, "w") as f:
            f.write(str(os.getpid()))

        # bluetoothctl scan finds both BR/EDR and LE devices
        try:
            proc = subprocess.Popen(
                ["bluetoothctl", "--timeout", str(SCAN_DURATION), "scan", "on"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                out, _ = proc.communicate(timeout=SCAN_DURATION + 5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
                log.info(f"bluetoothctl scan finished after {SCAN_DURATION + 5}s.")
        except Exception as e:
            log.warning(f"bluetoothctl scan failed: {e}")
            out = ""

        # After scan, query all known devices from bluetoothctl
        # (bluetoothctl devices lists everything from cache including newly scanned)
        dev_out = _run(["bluetoothctl", "devices"])
        devices = []
        seen_names = {}  # name -> device (prefer audio-capable ones)
        for line in dev_out.splitlines():
            dm = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
            if not dm:
                continue
            mac = dm.group(1).upper()
            fallback_name = dm.group(2).strip()
            if not fallback_name or fallback_name == mac:
                continue
            info = _get_device_info(mac)
            name = info["name"] if info["name"] != mac else fallback_name
            dev = {
                "mac": mac,
                "name": name,
                "paired": info["paired"],
                "connected": info["connected"],
                "trusted": info["trusted"],
                "audio": info["audio"],
            }
            # For duplicates (same name, LE + BR/EDR): prefer audio version
            if name in seen_names:
                if dev["audio"] and not seen_names[name]["audio"]:
                    seen_names[name] = dev
            else:
                seen_names[name] = dev
        devices = list(seen_names.values())

        log.info(f"Scan complete: {len(devices)} device(s) found.")
        return devices
    finally:
        try:
            os.remove(SCAN_LOCK)
        except OSError:
            pass


# ── Pairing / Connecting / Disconnecting ─────────────────────────────────────

def pair_device(mac: str) -> Tuple[bool, str]:
    if not ensure_adapter_powered():
        return False, "Bluetooth adapter not available."

    _run(["bluetoothctl", "pairable", "on"])
    out = _run(["bluetoothctl", "pair", mac], timeout=CONNECT_TIMEOUT)
    if "Failed" in out and "Already" not in out:
        return False, f"Pairing failed: {out.strip()}"

    _run(["bluetoothctl", "trust", mac])
    log.info(f"Device {mac} paired and trusted.")
    return True, "Device successfully paired."


def connect_device(mac: str) -> Tuple[bool, str]:
    if not ensure_adapter_powered():
        return False, "Bluetooth adapter not available."

    out = _run(["bluetoothctl", "connect", mac], timeout=CONNECT_TIMEOUT)

    # Wait for connection
    for _ in range(10):
        info = _get_device_info(mac)
        if info["connected"]:
            log.info(f"Device {mac} connected.")
            return True, f"Connected to {info['name']}."
        time.sleep(1)

    return False, "Connection failed (timeout)."


def disconnect_device(mac: str) -> Tuple[bool, str]:
    _run(["bluetoothctl", "disconnect", mac])

    # If audio was routed through this device, switch back to local
    bt_cfg = get_bt_config()
    if bt_cfg.get("active_device") == mac:
        switch_audio_to_local()

    log.info(f"Device {mac} disconnected.")
    return True, "Device disconnected."


def remove_device(mac: str) -> Tuple[bool, str]:
    # Disconnect first if connected
    info = _get_device_info(mac)
    if info["connected"]:
        disconnect_device(mac)

    _run(["bluetoothctl", "remove", mac])
    log.info(f"Device {mac} removed.")
    return True, "Device removed."


# ── Audio Switching ──────────────────────────────────────────────────────────

def _backup_asound_conf():
    """Backs up the current asound.conf on first BT switch."""
    cfg = _load_config()
    bt = cfg.get("bluetooth", {})
    if not bt.get("local_asound_conf"):
        try:
            with open(ASOUND_CONF) as f:
                bt["local_asound_conf"] = f.read()
            cfg["bluetooth"] = bt
            _save_config(cfg)
            log.info("Original asound.conf backed up.")
        except FileNotFoundError:
            bt["local_asound_conf"] = ""
            cfg["bluetooth"] = bt
            _save_config(cfg)


def switch_audio_to_bluetooth(mac: str) -> Tuple[bool, str]:
    info = _get_device_info(mac)
    if not info["connected"]:
        return False, "Device is not connected."

    _backup_asound_conf()

    # Write BlueALSA asound.conf
    asound = f"""# Bluetooth Audio Output (managed by bluetooth_manager)
defaults.bluealsa.device "{mac}"
defaults.bluealsa.profile "a2dp"

pcm.!default {{
    type plug
    slave.pcm "bluealsa"
}}

ctl.!default {{
    type bluealsa
}}
"""
    try:
        with open(ASOUND_CONF, "w") as f:
            f.write(asound)
    except Exception as e:
        return False, f"Error writing asound.conf: {e}"

    # Save active device
    cfg = _load_config()
    cfg.setdefault("bluetooth", {})["active_device"] = mac
    _save_config(cfg)

    # Restart squeezelite
    subprocess.run(["systemctl", "restart", "squeezelite"], timeout=10,
                   capture_output=True)
    time.sleep(2)

    log.info(f"Audio output switched to Bluetooth device {mac}.")
    return True, f"Audio output switched to {info['name']}."


def _generate_local_asound_conf() -> str:
    """Generates a softvol+dmix asound.conf for the USB sound card."""
    try:
        out = subprocess.check_output(["aplay", "-l"], text=True, timeout=5)
        for line in out.splitlines():
            if "usb" in line.lower():
                idx = re.search(r"card\s+(\d+)", line)
                if idx:
                    card = idx.group(1)
                    break
        else:
            card = "1"
    except Exception:
        card = "1"

    return f"""# Local audio output (softvol+dmix)
pcm.!default {{
    type plug
    slave.pcm "softvol"
}}

pcm.softvol {{
    type softvol
    slave.pcm "dmixer"
    control {{
        name "SoftMaster"
        card {card}
    }}
    min_dB -51.0
    max_dB 0.0
}}

pcm.dmixer {{
    type dmix
    ipc_key 1024
    slave {{
        pcm "hw:{card},0"
        period_time 0
        period_size 1024
        buffer_size 4096
        rate 44100
    }}
}}

ctl.!default {{
    type hw
    card {card}
}}
"""


def switch_audio_to_local() -> Tuple[bool, str]:
    cfg = _load_config()
    bt = cfg.get("bluetooth", {})
    local_conf = bt.get("local_asound_conf", "")

    try:
        if local_conf:
            with open(ASOUND_CONF, "w") as f:
                f.write(local_conf)
        else:
            # No backup: generate fresh softvol+dmix configuration
            with open(ASOUND_CONF, "w") as f:
                f.write(_generate_local_asound_conf())
    except Exception as e:
        return False, f"Error restoring asound.conf: {e}"

    bt["active_device"] = ""
    cfg["bluetooth"] = bt
    _save_config(cfg)

    subprocess.run(["systemctl", "restart", "squeezelite"], timeout=10,
                   capture_output=True)
    time.sleep(2)

    log.info("Audio output switched to local speakers.")
    return True, "Audio output switched to local speakers."


# ── Status ───────────────────────────────────────────────────────────────────

def get_connection_status() -> dict:
    bt_cfg = get_bt_config()
    active_mac = bt_cfg.get("active_device", "")
    available = is_bluetooth_available()

    result = {
        "available": available,
        "active_output": "local",
        "connected_device": None,
        "scanning": os.path.exists(SCAN_LOCK),
    }

    if active_mac:
        info = _get_device_info(active_mac)
        if info["connected"]:
            result["active_output"] = "bluetooth"
            result["connected_device"] = {
                "mac": active_mac,
                "name": info["name"],
            }
        else:
            # Report status, but do NOT switch automatically
            # (the daemon loop handles that with auto_reconnect logic)
            result["active_output"] = "bluetooth_disconnected"

    return result


# ── Daemon ───────────────────────────────────────────────────────────────────

def daemon_tick():
    """A single check cycle of the Bluetooth manager.
    Called periodically by the lms-web thread."""
    bt_cfg = get_bt_config()
    active_mac = bt_cfg.get("active_device", "")

    if active_mac:
        info = _get_device_info(active_mac)
        if not info["connected"]:
            log.warning(f"Bluetooth device {active_mac} no longer connected.")
            if bt_cfg.get("auto_reconnect", True):
                log.info("Attempting to reconnect...")
                ok, msg = connect_device(active_mac)
                if not ok:
                    log.warning(f"Reconnection failed: {msg}")
                    log.info("Switching back to local speakers.")
                    switch_audio_to_local()
            else:
                switch_audio_to_local()


def main():
    """Standalone daemon main loop (no longer used as a separate service,
    but kept for manual testing)."""
    log.info("Bluetooth manager started.")

    running = [True]

    def _stop(signum, frame):
        running[0] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    ensure_adapter_powered()

    while running[0]:
        try:
            daemon_tick()
        except Exception as e:
            log.error(f"Error in daemon loop: {e}")

        bt_cfg = get_bt_config()
        interval = bt_cfg.get("check_interval", 15)
        for _ in range(interval * 2):
            if not running[0]:
                break
            time.sleep(0.5)

    log.info("Bluetooth manager stopped.")


if __name__ == "__main__":
    main()
