"""
wifi_manager.py – WiFi manager with access point fallback

Features:
- Scans available WiFi networks
- Manages saved networks through NetworkManager, with legacy wpa_supplicant support
- Starts AP mode via NetworkManager when no known WiFi is reachable
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


def _secure_wpa_config():
    if os.path.exists(WPA_CONF):
        os.chmod(WPA_CONF, 0o600)


def _nmcli(args: List[str], timeout: int = 10):
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _split_nmcli_row(line: str) -> List[str]:
    fields = []
    field = []
    escaped = False
    for char in line:
        if escaped:
            field.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(field))
            field = []
        else:
            field.append(char)
    if escaped:
        field.append("\\")
    fields.append("".join(field))
    return fields


def _nm_wifi_profiles() -> List[dict]:
    """Returns saved WiFi profiles from NetworkManager."""
    try:
        result = _nmcli([
            "-t", "-e", "yes", "-f",
            "UUID,TYPE,802-11-wireless.ssid,"
            "802-11-wireless-security.key-mgmt",
            "connection", "show",
        ])
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Timed out reading NetworkManager profiles.") from e
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or "Could not read NetworkManager profiles."
        )

    profiles = []
    for line in result.stdout.splitlines():
        fields = _split_nmcli_row(line)
        if len(fields) != 4 or fields[1] != "802-11-wireless":
            continue
        uuid, _, ssid, key_mgmt = fields
        if not ssid or ssid == "--":
            continue
        profiles.append({
            "uuid": uuid,
            "ssid": ssid,
            "key_mgmt": "" if key_mgmt == "--" else key_mgmt,
        })
    return profiles


# ── Configuration ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    return config_manager.read_config()


def _save_config(cfg: dict):
    config_manager.write_config(cfg)


def get_wifi_config() -> dict:
    cfg = _load_config()
    return cfg.get("wifi", {
        "ap_ssid": "kid2tuneAP",
        "ap_password": "",
        "ap_channel": 7,
        "check_interval": 30,
    })


def save_wifi_config(ap_ssid: str, ap_password: str, ap_channel: int = 7,
                     check_interval: int = 30):
    # Read-modify-write in one locked step, otherwise a concurrent write from
    # the WiFi/Bluetooth daemon threads silently drops the change.
    def _update(cfg):
        cfg["wifi"] = {
            "ap_ssid": ap_ssid,
            "ap_password": ap_password,
            "ap_channel": ap_channel,
            "check_interval": check_interval,
        }
    config_manager.update_config(_update)


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
    """Reads saved networks from NetworkManager and legacy wpa_supplicant."""
    networks = {}
    try:
        for profile in _nm_wifi_profiles():
            key_mgmt = profile["key_mgmt"]
            networks[profile["ssid"]] = {
                "ssid": profile["ssid"],
                "has_password": bool(key_mgmt and key_mgmt.lower() != "none"),
                "key_mgmt": "Open" if not key_mgmt or key_mgmt.lower() == "none"
                else key_mgmt.upper(),
            }
    except (FileNotFoundError, RuntimeError) as e:
        log.warning(f"Could not read NetworkManager WiFi profiles: {e}")

    if not os.path.exists(WPA_CONF):
        return list(networks.values())
    try:
        _secure_wpa_config()
        with open(WPA_CONF) as f:
            content = f.read()
        # Parse network blocks
        blocks = re.findall(r'network=\{([^}]+)\}', content, re.DOTALL)
        for block in blocks:
            ssid_m = re.search(r'ssid="([^"]+)"', block)
            psk_m = re.search(r'psk="([^"]+)"', block)
            key_m = re.search(r'key_mgmt=(\S+)', block)
            if ssid_m:
                name = ssid_m.group(1)
                networks.setdefault(name, {
                    "ssid": name,
                    "has_password": psk_m is not None,
                    "key_mgmt": key_m.group(1) if key_m else "WPA-PSK",
                })
    except Exception as e:
        log.error(f"Error reading wpa_supplicant.conf: {e}")
    return list(networks.values())


def valid_ssid(ssid: str) -> bool:
    """SSID for a quoted wpa_supplicant value: max 32 bytes, and none of the
    characters that would end the string and inject further directives."""
    if not ssid or len(ssid.encode("utf-8")) > 32:
        return False
    return not re.search(r'["\\\n\r\x00]', ssid)


def valid_psk(password: str) -> bool:
    """WPA passphrase: 8-63 characters, same quoting restrictions as the SSID."""
    if not password:
        return True  # open network
    if not 8 <= len(password) <= 63:
        return False
    return not re.search(r'["\\\n\r\x00]', password)


def add_network(ssid: str, password: str = "") -> Tuple[bool, str]:
    """Adds a WiFi network."""
    if not ssid:
        return False, "SSID must not be empty."
    # wpa_supplicant.conf is parsed by a root daemon and supports directives
    # such as pkcs11_module_path – an unescaped quote plus newline in the SSID
    # or passphrase would let anyone inject them.
    if not valid_ssid(ssid):
        return False, "Invalid SSID (max 32 characters, no quotes or line breaks)."
    if not valid_psk(password):
        return False, "Invalid WiFi password (8-63 characters, no quotes or line breaks)."

    # Ensure wpa_supplicant.conf header exists
    if not os.path.exists(WPA_CONF):
        # Contains PSKs in plain text -> never world-readable
        fd = os.open(WPA_CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n")
            f.write("update_config=1\n")
            f.write(f"country=DE\n\n")
    else:
        _secure_wpa_config()

    # Check if network already exists
    with open(WPA_CONF) as f:
        content = f.read()
    if f'ssid="{ssid}"' in content:
        # Update existing network: remove and re-add
        ok, message = remove_network(ssid)
        if not ok:
            return False, message
        with open(WPA_CONF) as f:
            content = f.read()

    # Add new network
    if password:
        block = f'\nnetwork={{\n    ssid="{ssid}"\n    psk="{password}"\n    key_mgmt=WPA-PSK\n}}\n'
    else:
        block = f'\nnetwork={{\n    ssid="{ssid}"\n    key_mgmt=NONE\n}}\n'

    with open(WPA_CONF, "a") as f:
        f.write(block)
    _secure_wpa_config()

    log.info(f"Network '{ssid}' added.")
    return True, f"Network '{ssid}' added."


def remove_network(ssid: str) -> Tuple[bool, str]:
    """Removes the network from NetworkManager and legacy wpa_supplicant."""
    try:
        profiles = _nm_wifi_profiles()
    except FileNotFoundError:
        profiles = []
    except RuntimeError as e:
        return False, f"Could not inspect NetworkManager profiles: {e}"

    removed_profiles = 0
    for profile in profiles:
        if profile["ssid"] != ssid:
            continue
        try:
            result = _nmcli(["connection", "delete", "uuid", profile["uuid"]])
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"Could not remove NetworkManager profile: {e}"
        if result.returncode != 0:
            error = result.stderr.strip() or "NetworkManager rejected profile deletion."
            return False, f"Could not remove NetworkManager profile: {error}"
        removed_profiles += 1

    removed_wpa = _remove_wpa_network(ssid)
    if not removed_profiles and not removed_wpa:
        return False, f"Network '{ssid}' not found."

    log.info(f"Network '{ssid}' removed from saved WiFi profiles.")
    return True, f"Network '{ssid}' removed."


def _remove_wpa_network(ssid: str) -> bool:
    if not os.path.exists(WPA_CONF):
        return False

    _secure_wpa_config()
    with open(WPA_CONF) as f:
        content = f.read()

    pattern = rf'\n?network=\{{[^}}]*ssid="{re.escape(ssid)}"[^}}]*\}}\n?'
    new_content = re.sub(pattern, '\n', content, flags=re.DOTALL)
    if new_content == content:
        return False

    with open(WPA_CONF, "w") as f:
        f.write(new_content)
    _secure_wpa_config()
    return True


def reconfigure_wpa() -> bool:
    """Reload wpa_supplicant."""
    try:
        result = subprocess.run(["wpa_cli", "-i", IFACE, "reconfigure"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            log.warning(f"wpa_cli reconfigure failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        log.warning(f"wpa_cli reconfigure failed: {e}")
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
    password = wifi_cfg.get("ap_password", "")

    if len(password) < 8:
        return False, "AP password is not configured (minimum 8 characters)."

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
    # Validate up front: on the nmcli path the values are separate argv
    # elements and harmless, but the wpa_supplicant fallback below writes
    # them into a config file parsed by a root daemon.
    if not valid_ssid(ssid):
        return False, "Invalid SSID (max 32 characters, no quotes or line breaks)."
    if not valid_psk(password):
        return False, "Invalid WiFi password (8-63 characters, no quotes or line breaks)."

    # If AP is active, stop it first
    if is_ap_active():
        stopped, message = stop_ap()
        if not stopped:
            return False, message
        time.sleep(2)

    log.info(f"Connecting to '{ssid}' via nmcli...")
    profiles_to_cleanup = []
    try:
        profiles = _nm_wifi_profiles()
        matching = [profile for profile in profiles if profile["ssid"] == ssid]
        if matching and password:
            profile = matching[0]
            profiles_to_cleanup = matching[1:]
            result = _nmcli([
                "connection", "modify", "uuid", profile["uuid"],
                "connection.autoconnect", "yes",
                "connection.autoconnect-retries", "-1",
                "802-11-wireless-security.key-mgmt", "wpa-psk",
                "802-11-wireless-security.psk", password,
            ])
            if result.returncode != 0:
                message = result.stderr.strip().replace(password, "***")
                return False, message or "Could not update the saved WiFi credentials."

            result = _nmcli([
                "connection", "up", "uuid", profile["uuid"], "ifname", IFACE,
            ], timeout=30)
        else:
            for profile in matching:
                result = _nmcli([
                    "connection", "delete", "uuid", profile["uuid"],
                ])
                if result.returncode != 0:
                    return False, result.stderr.strip() or (
                        f"Could not replace the saved profile for '{ssid}'."
                    )

            command = ["device", "wifi", "connect", ssid]
            if password:
                command += ["password", password]
            command += ["ifname", IFACE]
            result = _nmcli(command, timeout=30)

        if result.returncode != 0:
            error = result.stderr.strip()
            if password:
                error = error.replace(password, "***")
            return False, f"Connection to '{ssid}' failed: {error}"

        for profile in profiles_to_cleanup:
            cleanup = _nmcli(["connection", "delete", "uuid", profile["uuid"]])
            if cleanup.returncode != 0:
                log.warning(
                    f"Connected to '{ssid}' but could not remove duplicate "
                    f"NetworkManager profile {profile['uuid']}: "
                    f"{cleanup.stderr.strip()}"
                )
        _remove_wpa_network(ssid)
        log.info(f"Connected to '{ssid}'.")
        return True, f"Connected to '{ssid}'"
    except FileNotFoundError:
        # Legacy systems without NetworkManager still use wpa_supplicant.
        saved, message = add_network(ssid, password)
        if not saved:
            return False, message
        reconfigure_wpa()
        time.sleep(5)

        status = get_connection_status()
        if status["connected"] and status["ssid"] == ssid:
            return True, f"Connected to '{ssid}'"
        return False, f"Connection to '{ssid}' failed."
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    except OSError as e:
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


def _retry_known_networks() -> bool:
    """Reactivates saved NetworkManager profiles visible in the current scan."""
    try:
        profiles = _nm_wifi_profiles()
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as e:
        log.warning(f"Could not retry saved WiFi profiles: {e}")
        return False

    available = {network["ssid"] for network in scan_networks()}
    candidates = [profile for profile in profiles if profile["ssid"] in available]
    for profile in candidates:
        try:
            result = _nmcli([
                "connection", "up", "uuid", profile["uuid"], "ifname", IFACE,
            ], timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning(f"Reconnect attempt for '{profile['ssid']}' failed: {e}")
            continue
        if result.returncode == 0:
            for _ in range(5):
                if _is_nm_connected():
                    log.info(f"Reconnected to '{profile['ssid']}'.")
                    return True
                time.sleep(1)
        else:
            error = result.stderr.strip()
            log.warning(
                f"Reconnect attempt for '{profile['ssid']}' failed: "
                f"{error or 'NetworkManager did not activate the profile.'}"
            )
    return _is_nm_connected()


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
            stopped, message = stop_ap()
            if not stopped:
                log.error(f"Could not stop AP for WiFi retry: {message}")
                return
            if _is_nm_connected() or _retry_known_networks():
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
            elif _retry_known_networks():
                log.info("WiFi restored using a saved connection profile.")
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
