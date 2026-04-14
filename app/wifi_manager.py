"""
wifi_manager.py – WiFi manager with access point fallback

Features:
- Scans available WiFi networks
- Manages known networks via wpa_supplicant
- Starts AP mode (hostapd + dnsmasq) when no known WiFi is reachable
- Switches back to client mode as soon as a known WiFi is available again
"""
import json
import os
import subprocess
import time
import logging
import re
from typing import List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WIFI] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))

import sys
sys.path.insert(0, DIR)
import config_manager

WPA_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
IFACE = "wlan0"


# ── Configuration ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    return config_manager.read_config()


def _save_config(cfg: dict):
    config_manager.write_config(cfg)


def get_wifi_config() -> dict:
    cfg = _load_config()
    return cfg.get("wifi", {
        "ap_ssid": "kid2tuneAP",
        "ap_password": "Geheim123!",
        "ap_channel": 7,
        "check_interval": 30,
    })


def save_wifi_config(ap_ssid: str, ap_password: str, ap_channel: int = 7,
                     check_interval: int = 30):
    cfg = _load_config()
    cfg["wifi"] = {
        "ap_ssid": ap_ssid,
        "ap_password": ap_password,
        "ap_channel": ap_channel,
        "check_interval": check_interval,
    }
    _save_config(cfg)


# ── WiFi Scan ────────────────────────────────────────────────────────────────

def scan_networks() -> List[dict]:
    """Scans available WiFi networks. Returns list of {ssid, signal, security}."""
    networks = []
    try:
        result = subprocess.run(
            ["iwlist", IFACE, "scan"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log.warning(f"iwlist scan failed: {result.stderr}")
            return networks

        current = {}
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("Cell "):
                if current.get("ssid"):
                    networks.append(current)
                current = {"ssid": "", "signal": 0, "security": "Open"}
            elif "ESSID:" in line:
                m = re.search(r'ESSID:"(.+)"', line)
                if m:
                    current["ssid"] = m.group(1)
            elif "Signal level=" in line:
                m = re.search(r'Signal level=(-?\d+)', line)
                if m:
                    current["signal"] = int(m.group(1))
            elif "IE:" in line and "WPA" in line:
                current["security"] = "WPA"
            elif "key:on" in line.lower():
                if current.get("security") == "Open":
                    current["security"] = "WEP"

        if current.get("ssid"):
            networks.append(current)

        # Remove duplicates (keep strongest entry per SSID)
        seen = {}
        for net in networks:
            ssid = net["ssid"]
            if ssid not in seen or net["signal"] > seen[ssid]["signal"]:
                seen[ssid] = net
        networks = sorted(seen.values(), key=lambda x: x["signal"], reverse=True)

    except subprocess.TimeoutExpired:
        log.warning("WiFi scan timeout")
    except Exception as e:
        log.error(f"Scan error: {e}")
    return networks


# ── wpa_supplicant Management ────────────────────────────────────────────────

def get_known_networks() -> List[dict]:
    """Reads known networks from wpa_supplicant.conf."""
    networks = []
    if not os.path.exists(WPA_CONF):
        return networks
    try:
        with open(WPA_CONF) as f:
            content = f.read()
        # Parse network blocks
        blocks = re.findall(r'network=\{([^}]+)\}', content, re.DOTALL)
        for block in blocks:
            ssid_m = re.search(r'ssid="([^"]+)"', block)
            psk_m = re.search(r'psk="([^"]+)"', block)
            key_m = re.search(r'key_mgmt=(\S+)', block)
            if ssid_m:
                networks.append({
                    "ssid": ssid_m.group(1),
                    "has_password": psk_m is not None,
                    "key_mgmt": key_m.group(1) if key_m else "WPA-PSK",
                })
    except Exception as e:
        log.error(f"Error reading wpa_supplicant.conf: {e}")
    return networks


def add_network(ssid: str, password: str = "") -> Tuple[bool, str]:
    """Adds a WiFi network."""
    if not ssid:
        return False, "SSID must not be empty."

    # Ensure wpa_supplicant.conf header exists
    if not os.path.exists(WPA_CONF):
        with open(WPA_CONF, "w") as f:
            f.write("ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n")
            f.write("update_config=1\n")
            f.write(f"country=DE\n\n")

    # Check if network already exists
    with open(WPA_CONF) as f:
        content = f.read()
    if f'ssid="{ssid}"' in content:
        # Update existing network: remove and re-add
        remove_network(ssid)
        with open(WPA_CONF) as f:
            content = f.read()

    # Add new network
    if password:
        block = f'\nnetwork={{\n    ssid="{ssid}"\n    psk="{password}"\n    key_mgmt=WPA-PSK\n}}\n'
    else:
        block = f'\nnetwork={{\n    ssid="{ssid}"\n    key_mgmt=NONE\n}}\n'

    with open(WPA_CONF, "a") as f:
        f.write(block)

    log.info(f"Network '{ssid}' added.")
    return True, f"Network '{ssid}' added."


def remove_network(ssid: str) -> Tuple[bool, str]:
    """Removes a WiFi network."""
    if not os.path.exists(WPA_CONF):
        return False, "wpa_supplicant.conf not found."

    with open(WPA_CONF) as f:
        content = f.read()

    # Remove network block with this SSID
    pattern = rf'\n?network=\{{[^}}]*ssid="{re.escape(ssid)}"[^}}]*\}}\n?'
    new_content = re.sub(pattern, '\n', content, flags=re.DOTALL)

    if new_content == content:
        return False, f"Network '{ssid}' not found."

    with open(WPA_CONF, "w") as f:
        f.write(new_content)

    log.info(f"Network '{ssid}' removed.")
    return True, f"Network '{ssid}' removed."


def reconfigure_wpa() -> bool:
    """Reload wpa_supplicant."""
    try:
        subprocess.run(["wpa_cli", "-i", IFACE, "reconfigure"],
                       capture_output=True, timeout=10)
        return True
    except Exception:
        return False


# ── Connection Status ────────────────────────────────────────────────────────

def get_connection_status() -> dict:
    """Determines current WiFi status."""
    status = {
        "connected": False,
        "ssid": "",
        "ip": "",
        "signal": 0,
        "mode": "unknown",  # client / ap
    }
    try:
        result = subprocess.run(
            ["iwconfig", IFACE],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout
        if "Mode:Master" in output:
            status["mode"] = "ap"
            wifi_cfg = get_wifi_config()
            status["ssid"] = wifi_cfg.get("ap_ssid", "")
            status["connected"] = True
        else:
            status["mode"] = "client"
            ssid_m = re.search(r'ESSID:"([^"]+)"', output)
            if ssid_m:
                status["ssid"] = ssid_m.group(1)
                status["connected"] = True
            signal_m = re.search(r'Signal level=(-?\d+)', output)
            if signal_m:
                status["signal"] = int(signal_m.group(1))

        # IP address
        ip_result = subprocess.run(
            ["ip", "-4", "addr", "show", IFACE],
            capture_output=True, text=True, timeout=5,
        )
        ip_m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', ip_result.stdout)
        if ip_m:
            status["ip"] = ip_m.group(1)
    except Exception as e:
        log.error(f"Status error: {e}")
    return status


def is_connected_to_known() -> bool:
    """True if connected to a known WiFi network."""
    status = get_connection_status()
    return status["connected"] and status["mode"] == "client" and status["ssid"] != ""


# ── Access Point Mode ────────────────────────────────────────────────────────

def start_ap() -> Tuple[bool, str]:
    """Starts Access Point mode via NetworkManager."""
    wifi_cfg = get_wifi_config()
    ssid = wifi_cfg.get("ap_ssid", "kid2tuneAP")
    password = wifi_cfg.get("ap_password", "Geheim123!")

    if len(password) < 8:
        return False, "AP password must be at least 8 characters."

    log.info(f"Starting AP mode: SSID={ssid}")

    try:
        # Disconnect existing connection
        subprocess.run(["nmcli", "device", "disconnect", IFACE],
                       capture_output=True, timeout=10)
        time.sleep(1)

        # Delete old hotspot connection if present
        subprocess.run(["nmcli", "connection", "delete", "kid2tuneAP"],
                       capture_output=True, timeout=5)

        # Start hotspot via NetworkManager (NM manages DHCP + DNS itself)
        result = subprocess.run(
            ["nmcli", "device", "wifi", "hotspot",
             "ifname", IFACE,
             "con-name", "kid2tuneAP",
             "ssid", ssid,
             "password", password],
            capture_output=True, text=True, timeout=15)

        if result.returncode != 0:
            log.error(f"nmcli hotspot failed: {result.stderr.strip()}")
            return False, f"AP start failed: {result.stderr.strip()}"

        time.sleep(2)

        # Determine AP IP (nmcli assigns 10.42.0.1)
        ap_ip = "10.42.0.1"
        try:
            r = subprocess.run(["ip", "-4", "addr", "show", IFACE],
                               capture_output=True, text=True, timeout=3)
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout)
            if m:
                ap_ip = m.group(1)
        except Exception:
            pass

        log.info(f"AP mode active: {ssid} on {ap_ip}")
        return True, f"AP active: {ssid} (IP: {ap_ip})"

    except Exception as e:
        log.error(f"AP start failed: {e}")
        return False, str(e)


def stop_ap() -> Tuple[bool, str]:
    """Stops AP mode. NetworkManager automatically connects to known WiFi."""
    log.info("Stopping AP mode...")
    try:
        # Deactivate + delete hotspot connection
        subprocess.run(["nmcli", "connection", "down", "kid2tuneAP"],
                       capture_output=True, timeout=10)
        subprocess.run(["nmcli", "connection", "delete", "kid2tuneAP"],
                       capture_output=True, timeout=5)

        # Stop hostapd/dnsmasq if still active (legacy)
        subprocess.run(["systemctl", "stop", "hostapd"], capture_output=True, timeout=5)
        subprocess.run(["systemctl", "stop", "dnsmasq"], capture_output=True, timeout=5)
        subprocess.run(["systemctl", "disable", "hostapd"], capture_output=True, timeout=5)

        # Let NetworkManager manage wlan0 again and autoconnect
        subprocess.run(["nmcli", "device", "set", IFACE, "managed", "yes"],
                       capture_output=True, timeout=5)
        subprocess.run(["nmcli", "device", "set", IFACE, "autoconnect", "yes"],
                       capture_output=True, timeout=5)

        # Wait until NM connects (max 15s)
        for _ in range(5):
            time.sleep(3)
            if _is_nm_connected():
                log.info("Client mode restored (NM autoconnect).")
                return True, "Client mode active."

        log.warning("NM did not connect automatically.")
        return True, "AP stopped. Waiting for WiFi connection..."

    except Exception as e:
        log.error(f"AP stop failed: {e}")
        return False, str(e)


def _is_nm_connected() -> bool:
    """Checks if NetworkManager is connected to a WiFi (not hotspot)."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[2] == IFACE and parts[0] != "kid2tuneAP":
                return True
    except Exception:
        pass
    return False


def is_ap_active() -> bool:
    """Checks if AP mode is active."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=5)
        if "kid2tuneAP" in result.stdout:
            return True
        # Fallback: hostapd
        result2 = subprocess.run(["systemctl", "is-active", "hostapd"],
                                 capture_output=True, text=True, timeout=5)
        return result2.stdout.strip() == "active"
    except Exception:
        return False


def connect_to_network(ssid: str, password: str = "") -> Tuple[bool, str]:
    """Connects to a specific WiFi network via NetworkManager."""
    # If AP is active, stop it first
    if is_ap_active():
        stop_ap()
        time.sleep(2)

    log.info(f"Connecting to '{ssid}' via nmcli...")
    try:
        # Try to connect directly via nmcli
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        cmd += ["ifname", IFACE]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log.info(f"Connected to '{ssid}'.")
            return True, f"Connected to '{ssid}'"

        # Fallback: wpa_supplicant
        add_network(ssid, password)
        reconfigure_wpa()
        time.sleep(5)

        status = get_connection_status()
        if status["connected"] and status["ssid"] == ssid:
            return True, f"Connected to '{ssid}'"

        return False, f"Connection to '{ssid}' failed: {result.stderr.strip()}"
    except Exception as e:
        return False, str(e)


# ── Main Loop (Daemon) ──────────────────────────────────────────────────────

AP_RESCAN_INTERVAL = 300  # In AP mode, only check for known WiFi every 5 minutes

def _any_known_available() -> bool:
    """Checks if a known WiFi network is within range."""
    known = {n["ssid"] for n in get_known_networks()}
    if not known:
        return False
    available = scan_networks()
    for net in available:
        if net["ssid"] in known:
            return True
    return False


_ap_check_counter = 0


def daemon_tick():
    """A single check cycle of the WiFi manager.
    Called periodically by the lms-web thread.

    Logic (purely nmcli-based):
    - AP active? -> Every 2 min: stop AP, try NM autoconnect, restart AP on failure
    - Client? -> Connection OK? Good. Lost? -> Wait 15s, then start AP
    """
    global _ap_check_counter

    wifi_cfg = get_wifi_config()
    interval = wifi_cfg.get("check_interval", 30)

    if is_ap_active():
        _ap_check_counter += interval
        # Check every 2 minutes if home WiFi is back
        if _ap_check_counter >= 120:
            _ap_check_counter = 0
            log.info("AP active – checking if home WiFi is available...")
            # Stop AP -> NM tries to connect to known WiFi automatically
            stop_ap()
            time.sleep(10)

            if _is_nm_connected():
                log.info("Home WiFi found – client mode active.")
            else:
                log.info("No known WiFi – restarting AP.")
                start_ap()
    else:
        _ap_check_counter = 0
        if not _is_nm_connected():
            log.info("WiFi connection lost – waiting 15s...")
            time.sleep(15)
            if _is_nm_connected():
                log.info("WiFi back (brief interruption).")
            else:
                log.info("No WiFi – starting AP mode.")
                start_ap()


def main():
    """Standalone daemon main loop (no longer used as a separate service,
    but kept for manual testing)."""
    log.info("WiFi manager started.")
    time.sleep(15)

    while True:
        try:
            daemon_tick()
        except Exception as e:
            log.error(f"Error in WiFi main loop: {e}")
        wifi_cfg = get_wifi_config()
        time.sleep(wifi_cfg.get("check_interval", 30))


if __name__ == "__main__":
    main()
