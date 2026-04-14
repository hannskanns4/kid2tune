"""
web_app.py – Flask web interface for kid2tune
Port: 80
"""
import json
import os
import sys
import time
import logging
import threading
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEB] %(levelname)s: %(message)s",
)

DIR = os.path.dirname(os.path.abspath(__file__))
LAST_RFID_FILE = "/tmp/lms_last_rfid"

sys.path.insert(0, DIR)
import config_manager
import lms_client
import sync_manager
import wifi_manager
import bluetooth_manager
import multiroom_manager
import standby_manager
import update_manager
import i18n

# Load language from config.json
_lang = config_manager.read_config().get("language", "de")
i18n.load_language(_lang)

app = Flask(__name__, template_folder=os.path.join(DIR, "templates"),
            static_folder=os.path.join(DIR, "static"))


@app.context_processor
def inject_i18n():
    """Makes t() and the current language available in all templates."""
    return {"t": i18n.t, "current_lang": i18n.get_language()}


def load_config() -> dict:
    return config_manager.read_config()


def save_config(cfg: dict):
    config_manager.write_config(cfg)


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    try:
        status = lms_client.get_status()
    except Exception:
        status = {}
    return render_template("index.html", status=status)


@app.route("/api/status")
def api_status():
    try:
        return jsonify(lms_client.get_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/version")
def api_version():
    import socket
    version = "?"
    version_file = os.path.join(DIR, "version.txt")
    if os.path.exists(version_file):
        with open(version_file) as f:
            version = f.read().strip()
    return jsonify({"version": version, "hostname": socket.gethostname()})


@app.route("/api/status/full")
def api_status_full():
    """Full status for dashboard (player + multiroom + box info)."""
    import socket
    version = "?"
    version_file = os.path.join(DIR, "version.txt")
    if os.path.exists(version_file):
        with open(version_file) as f:
            version = f.read().strip()
    try:
        status = lms_client.get_status()
    except Exception:
        status = {}
    mr = multiroom_manager.get_status()
    return jsonify({
        "hostname": socket.gethostname(),
        "version": version,
        "player": status,
        "multiroom": mr,
    })


@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


# ── Playback Control via Web ─────────────────────────────────────────────────

@app.route("/api/control/<action>", methods=["POST"])
def api_control(action):
    actions = {
        "play":       lms_client.play,
        "pause":      lms_client.toggle_pause,
        "next":       lms_client.next_track,
        "prev":       lms_client.prev_track,
        "vol_up":     lambda: lms_client.volume_up(5),
        "vol_down":   lambda: lms_client.volume_down(5),
    }
    fn = actions.get(action)
    if fn:
        fn()
        return jsonify({"ok": True})
    return jsonify({"error": i18n.t("player.unknown_action")}), 400


@app.route("/api/play/url", methods=["POST"])
def api_play_url():
    """Plays a link directly (Spotify, stream, URL). Auto-detection."""
    import re as _re
    data = request.json or {}
    value = data.get("url", "").strip()
    if not value:
        return jsonify({"ok": False, "message": i18n.t("player.no_link")}), 400

    # Convert Spotify URL to URI
    sp = _re.match(
        r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist)/([a-zA-Z0-9]+)",
        value)
    if sp:
        value = f"spotify:{sp.group(1)}:{sp.group(2)}"

    lms_client.play_item("url", value)
    return jsonify({"ok": True, "message": i18n.t("player.playing", url=value)})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    try:
        val = int((request.json or {}).get("volume", 50))
    except (ValueError, TypeError):
        return jsonify({"error": i18n.t("player.invalid_volume")}), 400
    lms_client.set_volume(val)
    return jsonify({"ok": True})


@app.route("/api/volume_max", methods=["GET"])
def api_volume_max_get():
    cfg = load_config()
    return jsonify({"volume_max": cfg.get("volume_max", 100)})


@app.route("/api/volume_max", methods=["POST"])
def api_volume_max_set():
    val = (request.json or {}).get("volume_max", 100)
    val = max(10, min(100, int(val)))
    cfg = load_config()
    cfg["volume_max"] = val
    save_config(cfg)
    return jsonify({"ok": True, "volume_max": val})


# ── RFID Management ─────────────────────────────────────────────────────────

@app.route("/rfid")
def rfid_page():
    cfg = load_config()
    mappings = cfg.get("rfid_mappings", {})
    return render_template("rfid.html", mappings=mappings)


@app.route("/rfid/scan")
def rfid_scan():
    """Returns the last scanned unknown UID (for JS polling)."""
    if os.path.exists(LAST_RFID_FILE):
        with open(LAST_RFID_FILE) as f:
            uid = f.read().strip()
        cfg = load_config()
        if uid not in cfg.get("rfid_mappings", {}):
            return jsonify({"uid": uid})
    return jsonify({"uid": None})


@app.route("/rfid/assign", methods=["POST"])
def rfid_assign():
    from datetime import datetime, timezone
    data  = request.form
    uid   = data.get("uid", "").strip().upper()
    label = data.get("label", uid)
    itype = data.get("type", "url")
    value = data.get("value", "").strip()

    # File upload for type "local"
    uploaded_file = request.files.get("music_file")
    if itype == "local" and uploaded_file and uploaded_file.filename:
        from werkzeug.utils import secure_filename
        filename = secure_filename(uploaded_file.filename)
        os.makedirs(sync_manager.MUSIC_DIR, exist_ok=True)
        local_path = os.path.join(sync_manager.MUSIC_DIR, filename)
        uploaded_file.save(local_path)
        value = filename
        # Push to NAS
        try:
            sync_manager.push_music_file(local_path)
        except Exception:
            pass

    if not uid or not value:
        return redirect(url_for("rfid_page"))

    # Convert Spotify URL to URI
    # https://open.spotify.com/playlist/3tNYL910jL5qlqfPFmncZj?si=...
    # -> spotify:playlist:3tNYL910jL5qlqfPFmncZj
    import re as _re
    sp = _re.match(r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist)/([a-zA-Z0-9]+)", value)
    if sp:
        value = f"spotify:{sp.group(1)}:{sp.group(2)}"
        itype = "url"

    now = datetime.now(timezone.utc).isoformat()
    cfg = load_config()
    box_id = cfg.get("sync", {}).get("box_id", "unknown")

    resume = request.form.get("resume") == "1"
    mapping_data = {
        "label": label,
        "type":  itype,
        "value": value,
        "resume": resume,
        "position": 0,
        "updated_at": now,
        "updated_by": box_id,
    }
    cfg.setdefault("rfid_mappings", {})[uid] = mapping_data
    save_config(cfg)

    # Remove processed card from tmp file
    if os.path.exists(LAST_RFID_FILE):
        os.remove(LAST_RFID_FILE)

    # NAS sync: write to queue, then try push
    sync_manager.queue_change("upsert", uid, mapping_data)
    try:
        sync_manager.push_mappings()
    except Exception:
        pass  # Queue remains for next sync

    return redirect(url_for("rfid_page"))


@app.route("/rfid/edit/<uid>", methods=["POST"])
def rfid_edit(uid):
    from datetime import datetime, timezone
    uid = uid.strip().upper()
    cfg = load_config()
    mappings = cfg.get("rfid_mappings", {})
    if uid not in mappings:
        return redirect(url_for("rfid_page"))

    data = request.form
    label = data.get("label", "").strip()
    itype = data.get("type", "url")
    value = data.get("value", "").strip()
    if not value:
        return redirect(url_for("rfid_page"))

    # Convert Spotify URL to URI
    import re as _re
    sp = _re.match(r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist)/([a-zA-Z0-9]+)", value)
    if sp:
        value = f"spotify:{sp.group(1)}:{sp.group(2)}"
        itype = "url"

    now = datetime.now(timezone.utc).isoformat()
    box_id = cfg.get("sync", {}).get("box_id", "unknown")

    resume = request.form.get("resume") == "1"
    old_position = mappings[uid].get("position", 0)
    mapping_data = {
        "label": label or uid,
        "type":  itype,
        "value": value,
        "resume": resume,
        "position": old_position if resume else 0,
        "updated_at": now,
        "updated_by": box_id,
    }
    mappings[uid] = mapping_data
    save_config(cfg)

    sync_manager.queue_change("upsert", uid, mapping_data)
    try:
        sync_manager.push_mappings()
    except Exception:
        pass

    return redirect(url_for("rfid_page"))


@app.route("/rfid/play/<uid>", methods=["POST"])
def rfid_play(uid):
    uid = uid.strip().upper()
    cfg = load_config()
    mappings = cfg.get("rfid_mappings", {})
    if uid not in mappings:
        return jsonify({"ok": False, "message": i18n.t("rfid.card_not_found")}), 404
    entry = mappings[uid]
    item_type = entry.get("type", "url")
    item_id = entry.get("value", "")
    try:
        if item_type == "bluetooth":
            ok, msg = bluetooth_manager.connect_device(item_id)
            if ok:
                bluetooth_manager.switch_audio_to_bluetooth(item_id)
            return jsonify({"ok": ok, "message": msg})
        elif item_type == "local":
            local_path = os.path.join(sync_manager.MUSIC_DIR, item_id)
            if not os.path.isfile(local_path):
                ok, result = sync_manager.pull_music_file(item_id)
                if ok:
                    local_path = result
                else:
                    return jsonify({"ok": False, "message": i18n.t("rfid.file_not_found", error=result)}), 404
            lms_client.play_item("url", f"file://{local_path}")
            try:
                sync_manager.push_music_file(local_path)
            except Exception:
                pass
            return jsonify({"ok": True, "message": i18n.t("rfid.playing", label=entry.get('label', uid))})
        elif item_type == "sleep":
            try:
                minutes = int(item_id)
            except (ValueError, TypeError):
                minutes = 15
            return jsonify({"ok": True, "message": i18n.t("rfid.sleep_timer", minutes=minutes)})
        elif item_type == "multiroom":
            mr_status = multiroom_manager.get_status()
            if mr_status.get("active"):
                if mr_status.get("role") == "master":
                    multiroom_manager.deactivate_master()
                    return jsonify({"ok": True, "message": i18n.t("multiroom.deactivated_short")})
                else:
                    multiroom_manager.leave_master()
                    return jsonify({"ok": True, "message": i18n.t("multiroom.slave_left")})
            else:
                ok = multiroom_manager.activate_master()
                msg = i18n.t("multiroom.activated") if ok else i18n.t("multiroom.no_boxes")
                return jsonify({"ok": ok, "message": msg})
        else:
            lms_client.play_item(item_type, item_id)
            return jsonify({"ok": True, "message": i18n.t("rfid.playing", label=entry.get('label', uid))})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/rfid/delete/<uid>", methods=["POST"])
def rfid_delete(uid):
    cfg = load_config()
    cfg.get("rfid_mappings", {}).pop(uid.upper(), None)
    save_config(cfg)

    # NAS sync: tombstone in queue, then try push
    sync_manager.queue_change("delete", uid.upper())
    try:
        sync_manager.push_mappings()
    except Exception:
        pass  # Queue remains for next sync

    return redirect(url_for("rfid_page"))


# ── LMS Search for RFID Assignment ──────────────────────────────────────────

@app.route("/api/lms/search")
def lms_search():
    query       = request.args.get("q", "")
    search_type = request.args.get("type", "tracks")
    if not query:
        return jsonify([])
    try:
        results = lms_client.search(query, search_type)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Button Pin Management ───────────────────────────────────────────────────

@app.route("/buttons")
def buttons_page():
    cfg = load_config()
    buttons = cfg.get("buttons", {})
    return render_template("buttons.html", buttons=buttons)


# GPIOs excluded from button assignment and detection
# 0/1=ID EEPROM, 2/3=I2C (LCD), 8-11=SPI (RFID), 25=RFID RST
RESERVED_GPIO = {0, 1, 2, 3, 8, 9, 10, 11, 25}


@app.route("/buttons/save", methods=["POST"])
def buttons_save():
    cfg = load_config()
    btn_map = {}
    for action in ["vol_up", "vol_down", "next", "prev", "pause", "lcd_backlight"]:
        val = request.form.get(action, "")
        if val.isdigit() and int(val) not in RESERVED_GPIO:
            btn_map[action] = int(val)
    cfg["buttons"] = btn_map
    save_config(cfg)
    import subprocess
    subprocess.run(["systemctl", "restart", "lms-hardware"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    return redirect(url_for("buttons_page"))


_detect_active = False
_detect_result = None


@app.route("/buttons/detect/start", methods=["POST"])
def buttons_detect_start():
    """Start GPIO button detection mode. Listens on all non-reserved GPIOs."""
    global _detect_active, _detect_result
    if _detect_active:
        return jsonify({"ok": False, "message": "Detection already running."})
    _detect_active = True
    _detect_result = None

    def _detect():
        global _detect_active, _detect_result
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            # All usable GPIOs (0-27 minus reserved)
            scan_pins = [p for p in range(28) if p not in RESERVED_GPIO]
            for pin in scan_pins:
                try:
                    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                except Exception:
                    pass
            # Wait briefly for pins to settle
            time.sleep(0.2)
            # Record initial state — ignore pins that are already LOW
            initial_low = set()
            for pin in scan_pins:
                try:
                    if GPIO.input(pin) == GPIO.LOW:
                        initial_low.add(pin)
                except Exception:
                    pass
            # Wait for a NEW button press (HIGH->LOW transition, max 30s)
            for _ in range(300):
                if not _detect_active:
                    break
                for pin in scan_pins:
                    if pin in initial_low:
                        continue
                    try:
                        if GPIO.input(pin) == GPIO.LOW:
                            _detect_result = pin
                            _detect_active = False
                            for p in scan_pins:
                                try:
                                    GPIO.cleanup(p)
                                except Exception:
                                    pass
                            return
                    except Exception:
                        pass
                time.sleep(0.1)
            _detect_active = False
            for p in scan_pins:
                try:
                    GPIO.cleanup(p)
                except Exception:
                    pass
        except Exception:
            _detect_active = False

    threading.Thread(target=_detect, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/buttons/detect/status")
def buttons_detect_status():
    """Poll detection result."""
    if _detect_result is not None:
        return jsonify({"active": False, "pin": _detect_result})
    return jsonify({"active": _detect_active, "pin": None})


@app.route("/buttons/detect/stop", methods=["POST"])
def buttons_detect_stop():
    """Stop detection mode."""
    global _detect_active
    _detect_active = False
    return jsonify({"ok": True})


# ── NAS Sync Management ─────────────────────────────────────────────────────

@app.route("/sync")
def sync_page():
    sync_cfg = sync_manager.get_sync_config()
    cfg = load_config()
    mapping_count = len(cfg.get("rfid_mappings", {}))
    pending_count = sync_manager.get_pending_count()
    return render_template("sync.html", sync=sync_cfg,
                           mapping_count=mapping_count,
                           pending_count=pending_count)


@app.route("/sync/save", methods=["POST"])
def sync_save():
    nas_share = request.form.get("nas_share", "").strip()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    box_id = request.form.get("box_id", "").strip()
    enabled = request.form.get("enabled") == "on"

    sync_manager.save_sync_config(nas_share, username, password, box_id, enabled)
    return redirect(url_for("sync_page"))


@app.route("/sync/test", methods=["POST"])
def sync_test():
    ok, msg = sync_manager.test_connection()
    return jsonify({"ok": ok, "message": msg})


@app.route("/sync/push", methods=["POST"])
def sync_push():
    ok, msg = sync_manager.push_mappings()
    return jsonify({"ok": ok, "message": msg})


@app.route("/sync/pull", methods=["POST"])
def sync_pull():
    ok, msg = sync_manager.pull_mappings()
    return jsonify({"ok": ok, "message": msg})


@app.route("/sync/full", methods=["POST"])
def sync_full():
    ok, msg = sync_manager.full_sync()
    return jsonify({"ok": ok, "message": msg})


@app.route("/sync/status")
def sync_status():
    return jsonify(sync_manager.get_sync_status())


# ── WiFi Management ─────────────────────────────────────────────────────────

@app.route("/wifi")
def wifi_page():
    wifi_cfg = wifi_manager.get_wifi_config()
    status = wifi_manager.get_connection_status()
    known = wifi_manager.get_known_networks()
    return render_template("wifi.html", wifi=wifi_cfg, status=status, known=known)


@app.route("/wifi/scan")
def wifi_scan():
    if wifi_manager.is_ap_active():
        return jsonify({"error": i18n.t("wifi.scan_ap_blocked")}), 409
    networks = wifi_manager.scan_networks()
    return jsonify(networks)


@app.route("/wifi/status")
def wifi_status():
    return jsonify(wifi_manager.get_connection_status())


@app.route("/wifi/connect", methods=["POST"])
def wifi_connect():
    ssid = (request.form or request.json or {}).get("ssid", "").strip()
    password = (request.form or request.json or {}).get("password", "")
    if not ssid:
        return jsonify({"ok": False, "message": i18n.t("wifi.ssid_missing")}), 400
    ok, msg = wifi_manager.connect_to_network(ssid, password)
    return jsonify({"ok": ok, "message": msg})


@app.route("/wifi/forget", methods=["POST"])
def wifi_forget():
    ssid = (request.form or request.json or {}).get("ssid", "").strip()
    if not ssid:
        return jsonify({"ok": False, "message": i18n.t("wifi.ssid_missing")}), 400
    ok, msg = wifi_manager.remove_network(ssid)
    if ok:
        wifi_manager.reconfigure_wpa()
    return jsonify({"ok": ok, "message": msg})


@app.route("/wifi/ap/start", methods=["POST"])
def wifi_ap_start():
    ok, msg = wifi_manager.start_ap()
    return jsonify({"ok": ok, "message": msg})


@app.route("/wifi/ap/stop", methods=["POST"])
def wifi_ap_stop():
    ok, msg = wifi_manager.stop_ap()
    return jsonify({"ok": ok, "message": msg})


@app.route("/wifi/ap/save", methods=["POST"])
def wifi_ap_save():
    data = request.form or request.json or {}
    ap_ssid = data.get("ap_ssid", "").strip()
    ap_password = data.get("ap_password", "").strip()
    ap_channel = int(data.get("ap_channel", 7))
    check_interval = int(data.get("check_interval", 30))

    if not ap_ssid:
        return jsonify({"ok": False, "message": i18n.t("wifi.ssid_empty")}), 400
    if len(ap_password) < 8:
        return jsonify({"ok": False, "message": i18n.t("wifi.ap_pw_short")}), 400

    wifi_manager.save_wifi_config(ap_ssid, ap_password, ap_channel, check_interval)
    return jsonify({"ok": True, "message": i18n.t("wifi.ap_saved")})


# ── Bluetooth Management ────────────────────────────────────────────────────

@app.route("/bluetooth")
def bluetooth_page():
    bt_status = bluetooth_manager.get_connection_status()
    paired = bluetooth_manager.get_paired_devices()
    bt_available = bluetooth_manager.is_bluetooth_available()
    return render_template("bluetooth.html",
                           status=bt_status, paired=paired,
                           bt_available=bt_available)


@app.route("/bluetooth/scan")
def bluetooth_scan():
    if not bluetooth_manager.is_bluetooth_available():
        return jsonify({"error": i18n.t("bluetooth.not_available")}), 503
    devices = bluetooth_manager.scan_devices()
    return jsonify(devices)


@app.route("/bluetooth/status")
def bluetooth_status():
    status = bluetooth_manager.get_connection_status()
    status["paired_devices"] = bluetooth_manager.get_paired_devices()
    return jsonify(status)


@app.route("/bluetooth/pair", methods=["POST"])
def bluetooth_pair():
    data = request.form or request.json or {}
    mac = data.get("mac", "").strip()
    if not mac:
        return jsonify({"ok": False, "message": i18n.t("bluetooth.mac_missing")}), 400
    ok, msg = bluetooth_manager.pair_device(mac)
    return jsonify({"ok": ok, "message": msg})


@app.route("/bluetooth/connect", methods=["POST"])
def bluetooth_connect():
    data = request.form or request.json or {}
    mac = data.get("mac", "").strip()
    if not mac:
        return jsonify({"ok": False, "message": i18n.t("bluetooth.mac_missing")}), 400
    ok, msg = bluetooth_manager.connect_device(mac)
    return jsonify({"ok": ok, "message": msg})


@app.route("/bluetooth/disconnect", methods=["POST"])
def bluetooth_disconnect():
    data = request.form or request.json or {}
    mac = data.get("mac", "").strip()
    if not mac:
        return jsonify({"ok": False, "message": i18n.t("bluetooth.mac_missing")}), 400
    ok, msg = bluetooth_manager.disconnect_device(mac)
    return jsonify({"ok": ok, "message": msg})


@app.route("/bluetooth/remove", methods=["POST"])
def bluetooth_remove():
    data = request.form or request.json or {}
    mac = data.get("mac", "").strip()
    if not mac:
        return jsonify({"ok": False, "message": i18n.t("bluetooth.mac_missing")}), 400
    ok, msg = bluetooth_manager.remove_device(mac)
    return jsonify({"ok": ok, "message": msg})


@app.route("/bluetooth/switch", methods=["POST"])
def bluetooth_switch():
    data = request.form or request.json or {}
    target = data.get("target", "")
    mac = data.get("mac", "")
    if target == "bluetooth":
        if not mac:
            return jsonify({"ok": False, "message": i18n.t("bluetooth.mac_missing")}), 400
        ok, msg = bluetooth_manager.switch_audio_to_bluetooth(mac)
    else:
        ok, msg = bluetooth_manager.switch_audio_to_local()
    return jsonify({"ok": ok, "message": msg})


# ── LCD Backlight ────────────────────────────────────────────────────────────

BACKLIGHT_FILE = "/tmp/lcd_backlight"

@app.route("/lcd/backlight", methods=["GET"])
def lcd_backlight_status():
    try:
        val = open(BACKLIGHT_FILE).read().strip() if os.path.exists(BACKLIGHT_FILE) else "1"
    except Exception:
        val = "1"
    return jsonify({"on": val != "0"})


@app.route("/lcd/backlight", methods=["POST"])
def lcd_backlight_set():
    data = request.json or request.form or {}
    on = data.get("on", True)
    with open(BACKLIGHT_FILE, "w") as f:
        f.write("1" if on else "0")
    return jsonify({"ok": True, "on": bool(on)})


# ── Multiroom Sync ───────────────────────────────────────────────────────────

def _load_known_boxes() -> dict:
    """Loads known boxes from config.json. Format: {hostname: ip}"""
    try:
        cfg = load_config()
        return cfg.get("known_boxes", {})
    except Exception:
        return {}


def _save_known_boxes(boxes: dict):
    """Saves known boxes to config.json."""
    try:
        def _update(cfg):
            cfg["known_boxes"] = boxes
        config_manager.update_config(_update)
    except Exception:
        pass


def _check_box_status(ip, own_ip):
    """Checks a single box and returns the result."""
    import requests as _req
    if ip == own_ip:
        return None
    try:
        r = _req.get(f"http://{ip}:80/api/status/full", timeout=2)
        if r.ok:
            data = r.json()
            if data.get("hostname"):
                return {"ip": ip, "data": data}
    except Exception:
        pass
    return None


@app.route("/api/discover/known")
def api_discover_known():
    """Fast: Only ping known boxes from cache (~1-2s)."""
    from concurrent.futures import ThreadPoolExecutor

    own_ip = multiroom_manager.get_own_ip()
    if not own_ip:
        return jsonify([])

    known = _load_known_boxes()
    if not known:
        return jsonify([])

    known_ips = list(set(known.values()))
    results = []
    found_hostnames = set()

    with ThreadPoolExecutor(max_workers=10) as ex:
        for result in ex.map(lambda ip: _check_box_status(ip, own_ip), known_ips):
            if result:
                hostname = result["data"].get("hostname", "")
                if hostname and hostname not in found_hostnames:
                    results.append(result)
                    found_hostnames.add(hostname)

    return jsonify(results)


@app.route("/api/discover")
def api_discover():
    """Full subnet scan. Known boxes first, then the rest."""
    from concurrent.futures import ThreadPoolExecutor

    own_ip = multiroom_manager.get_own_ip()
    if not own_ip:
        return jsonify([])

    results = []
    found_ips = set()
    found_hostnames = set()
    known = _load_known_boxes()

    def _add_result(result):
        if not result:
            return
        hostname = result["data"].get("hostname", "")
        if hostname and hostname in found_hostnames:
            return
        results.append(result)
        found_ips.add(result["ip"])
        if hostname:
            found_hostnames.add(hostname)

    # 1. Known IPs first
    known_ips = list(set(known.values()))
    if known_ips:
        with ThreadPoolExecutor(max_workers=10) as ex:
            for result in ex.map(lambda ip: _check_box_status(ip, own_ip), known_ips):
                _add_result(result)

    # 2. Rest of the subnet
    subnet = ".".join(own_ip.split(".")[:3])
    remaining = [f"{subnet}.{i}" for i in range(1, 255)
                 if f"{subnet}.{i}" not in found_ips and f"{subnet}.{i}" != own_ip]

    with ThreadPoolExecutor(max_workers=50) as ex:
        for result in ex.map(lambda ip: _check_box_status(ip, own_ip), remaining):
            _add_result(result)

    # 3. Update known boxes in config.json
    updated = {}
    for r in results:
        hostname = r["data"].get("hostname", "")
        if hostname:
            updated[hostname] = r["ip"]
    if updated != known:
        _save_known_boxes(updated)

    return jsonify(results)


@app.route("/api/multiroom/join", methods=["POST"])
def multiroom_join():
    """Called by master: redirect Squeezelite to master LMS."""
    data = request.json or {}
    master_ip = data.get("master_ip", "")
    if not master_ip:
        return jsonify({"ok": False, "message": "master_ip missing."}), 400
    try:
        multiroom_manager.join_master(master_ip)
        return jsonify({"ok": True, "message": i18n.t("multiroom.redirected_to", ip=master_ip)})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/multiroom/leave", methods=["POST"])
def multiroom_leave():
    """Called by master: redirect Squeezelite back to localhost."""
    try:
        multiroom_manager.leave_master()
        return jsonify({"ok": True, "message": i18n.t("multiroom.back_localhost")})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/multiroom/status")
def multiroom_status():
    """Returns the current multiroom status."""
    return jsonify(multiroom_manager.get_status())


@app.route("/api/multiroom/sync", methods=["POST"])
def multiroom_sync():
    """Synchronizes selected boxes with this box as master."""
    data = request.json or {}
    box_ips = data.get("boxes", [])
    if not box_ips:
        return jsonify({"ok": False, "message": i18n.t("multiroom.no_boxes_selected")}), 400
    result = multiroom_manager.sync_boxes(box_ips)
    return jsonify(result)


@app.route("/api/multiroom/unsync", methods=["POST"])
def multiroom_unsync():
    """Removes a single box from the sync group."""
    data = request.json or {}
    box_ip = data.get("box_ip", "").strip()
    if not box_ip:
        return jsonify({"ok": False, "message": "box_ip missing"}), 400
    result = multiroom_manager.unsync_box(box_ip)
    return jsonify(result)


@app.route("/api/multiroom/unsync/all", methods=["POST"])
def multiroom_unsync_all():
    """Disconnects all boxes."""
    result = multiroom_manager.unsync_all()
    return jsonify(result)


# ── OTA Update ───────────────────────────────────────────────────────────────

@app.route("/api/update/version")
def update_version():
    """Returns file hashes for version comparison."""
    import hashlib
    files = {}
    for f in os.listdir(DIR):
        if f.endswith((".py", ".txt")):
            path = os.path.join(DIR, f)
            with open(path, "rb") as fh:
                h = hashlib.md5(fh.read()).hexdigest()
            files[f] = h
    tpl_dir = os.path.join(DIR, "templates")
    if os.path.isdir(tpl_dir):
        for f in os.listdir(tpl_dir):
            if f.endswith(".html"):
                path = os.path.join(tpl_dir, f)
                with open(path, "rb") as fh:
                    h = hashlib.md5(fh.read()).hexdigest()
                files[f"templates/{f}"] = h
    version = "?"
    vf = os.path.join(DIR, "version.txt")
    if os.path.exists(vf):
        with open(vf) as fh:
            version = fh.read().strip()
    return jsonify({"version": version, "files": files})


@app.route("/api/update/package", methods=["POST"])
def update_package():
    """Receives a tar.gz update package, extracts it and restarts services."""
    import tarfile, io, subprocess
    if "package" not in request.files:
        return jsonify({"ok": False, "message": i18n.t("security.no_package")}), 400
    pkg = request.files["package"]
    try:
        tar = tarfile.open(fileobj=io.BytesIO(pkg.read()), mode="r:gz")
        # Security check: no paths outside APP_DIR, no symlinks
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in member.name:
                return jsonify({"ok": False, "message": i18n.t("security.unsafe_path", name=member.name)}), 400
            if member.issym() or member.islnk():
                return jsonify({"ok": False, "message": i18n.t("security.symlink", name=member.name)}), 400
            # Ensure extracted path stays within DIR
            target = os.path.realpath(os.path.join(DIR, member.name))
            if not target.startswith(os.path.realpath(DIR)):
                return jsonify({"ok": False, "message": i18n.t("security.traversal", name=member.name)}), 400
        tar.extractall(path=DIR, filter="data")
        tar.close()
        # Restart services (others first, lms-web last since it's our own process)
        subprocess.run(["systemctl", "restart", "lms-rfid", "lms-hardware"],
                       capture_output=True, timeout=30)
        # lms-web via Popen, since our own process gets killed
        subprocess.Popen(["systemctl", "restart", "lms-web"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True, "message": i18n.t("update.installed")})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/update/trigger", methods=["POST"])
def update_trigger():
    """Bundles local files and sends them to all boxes on the network."""
    import tarfile, io
    # Create tar.gz
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in os.listdir(DIR):
            if f.endswith((".py", ".txt", ".json")) and f not in ("config.json", "config.json.lock", "sync_pending.json"):
                tar.add(os.path.join(DIR, f), arcname=f)
        tpl_dir = os.path.join(DIR, "templates")
        if os.path.isdir(tpl_dir):
            for f in os.listdir(tpl_dir):
                if f.endswith(".html"):
                    tar.add(os.path.join(tpl_dir, f), arcname=f"templates/{f}")
        lang_dir = os.path.join(DIR, "lang")
        if os.path.isdir(lang_dir):
            for f in os.listdir(lang_dir):
                if f.endswith(".json"):
                    tar.add(os.path.join(lang_dir, f), arcname=f"lang/{f}")
    buf.seek(0)
    package_data = buf.read()

    # Send to all boxes
    boxes = multiroom_manager.discover_boxes()
    results = []
    for box_ip in boxes:
        try:
            import requests as _req
            files = {"package": ("update.tar.gz", io.BytesIO(package_data), "application/gzip")}
            r = _req.post(f"http://{box_ip}:80/api/update/package", files=files, timeout=30)
            d = r.json()
            results.append({"box": box_ip, "ok": d.get("ok"), "message": d.get("message")})
        except Exception as e:
            results.append({"box": box_ip, "ok": False, "message": str(e)})

    ok_count = sum(1 for r in results if r["ok"])
    return jsonify({
        "ok": True,
        "message": i18n.t("update.distributed", count=ok_count, total=len(boxes)),
        "details": results,
    })


# ── Git Update ───────────────────────────────────────────────────────────────

@app.route("/api/update/check")
def update_check():
    """Checks whether a new version is available on GitHub."""
    return jsonify(update_manager.check_for_update())


@app.route("/api/update/git", methods=["POST"])
def update_git():
    """Fetches the latest code from GitHub and updates this box.
    Runs asynchronously — response returns immediately, update runs in background."""
    def _do_update():
        import time as _t
        _t.sleep(1)  # Brief wait so HTTP response is sent first
        update_manager.pull_and_update()
    threading.Thread(target=_do_update, daemon=True).start()
    return jsonify({"ok": True, "message": i18n.t("update.started_reboot")})


@app.route("/api/update/git/all", methods=["POST"])
def update_git_all():
    """Updates all boxes on the network via git pull.
    Runs completely asynchronously — response returns immediately."""
    cfg = load_config()
    token = cfg.get("github_token", "")
    known = cfg.get("known_boxes", {})

    def _do_all():
        import requests as _req
        import time as _t
        _t.sleep(1)

        # 1. Known boxes from config (fast, no subnet scan needed)
        box_ips = list(set(known.values())) if known else multiroom_manager.discover_boxes()
        own_ip = multiroom_manager.get_own_ip()

        # 2. On each remote box: set token, check, update if needed
        for box_ip in box_ips:
            if box_ip == own_ip:
                continue
            try:
                # Set token
                if token:
                    try:
                        _req.post(f"http://{box_ip}:80/api/update/token",
                                  json={"token": token}, timeout=5)
                    except Exception:
                        pass
                # Check if update is needed
                try:
                    r = _req.get(f"http://{box_ip}:80/api/update/check", timeout=60)
                    info = r.json()
                    if not info.get("update_available"):
                        log.info(f"{box_ip}: already up to date ({info.get('current')})")
                        continue
                    log.info(f"{box_ip}: update available {info.get('current')} -> {info.get('remote')}")
                except Exception:
                    pass  # Update anyway if in doubt
                # Trigger update
                _req.post(f"http://{box_ip}:80/api/update/git", timeout=10)
                log.info(f"{box_ip}: update started")
            except Exception as e:
                log.warning(f"{box_ip}: update failed: {e}")

        # 3. Wait for remote boxes to finish (git clone ~30s)
        _t.sleep(30)

        # 4. Own box last
        update_manager.pull_and_update()

    threading.Thread(target=_do_all, daemon=True).start()

    box_count = len(known) if known else "?"
    return jsonify({
        "ok": True,
        "message": i18n.t("update.started_all", count=box_count),
    })


@app.route("/api/update/token", methods=["GET"])
def update_token_get():
    cfg = load_config()
    token = cfg.get("github_token", "")
    # Only show whether a token is set, not the value itself
    return jsonify({"has_token": bool(token)})


@app.route("/api/update/token", methods=["POST"])
def update_token_set():
    data = request.json or {}
    token = data.get("token", "").strip()
    def _update(cfg):
        cfg["github_token"] = token
    config_manager.update_config(_update)
    return jsonify({"ok": True, "message": i18n.t("settings.token_saved") if token else i18n.t("settings.token_removed")})


# ── Language ─────────────────────────────────────────────────────────────────

@app.route("/api/language", methods=["POST"])
def api_language():
    data = request.json or {}
    lang = data.get("language", "de").strip()
    if lang not in i18n.available_languages():
        lang = "de"
    def _update(cfg):
        cfg["language"] = lang
    config_manager.update_config(_update)
    i18n.load_language(lang)
    return jsonify({"ok": True, "language": lang})


# ── Shutdown ─────────────────────────────────────────────────────────────────

SHUTDOWN_PENDING_FILE = "/tmp/lms_shutdown_pending"
SHUTDOWN_CONFIRM_FILE = "/tmp/lms_shutdown_confirm"


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Shuts down the box safely – LCD is turned off first."""
    import subprocess
    logging.getLogger("WEB").info("Shutdown requested via web UI.")
    # LCD backlight off
    with open("/tmp/lcd_backlight", "w") as f:
        f.write("0")
    # Brief wait for LCD daemon to turn off backlight
    import time as _t
    _t.sleep(1)
    subprocess.Popen(["shutdown", "-h", "now"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True, "message": i18n.t("update.shutting_down")})


@app.route("/api/shutdown/config", methods=["GET"])
def shutdown_config_get():
    cfg = load_config()
    sd = cfg.get("shutdown", {})
    return jsonify({
        "hold_time": sd.get("hold_time", 5),
        "confirm_timeout": sd.get("confirm_timeout", 15),
    })


@app.route("/api/shutdown/config", methods=["POST"])
def shutdown_config_set():
    data = request.json or {}
    hold_time = max(2, min(15, int(data.get("hold_time", 5))))
    confirm_timeout = max(5, min(60, int(data.get("confirm_timeout", 15))))
    def _update(cfg):
        cfg["shutdown"] = {
            "hold_time": hold_time,
            "confirm_timeout": confirm_timeout,
        }
    config_manager.update_config(_update)
    return jsonify({"ok": True, "hold_time": hold_time, "confirm_timeout": confirm_timeout})


@app.route("/api/standby", methods=["POST"])
def api_standby():
    """Puts the box into deep standby (LCD off, services stopped, ro filesystem)."""
    logging.getLogger("WEB").info("Deep standby requested via web UI.")
    ok, msg = standby_manager.enter_standby()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/wake", methods=["POST"])
def api_wake():
    """Wakes the box from deep standby."""
    logging.getLogger("WEB").info("Wake-up requested via web UI.")
    ok, msg = standby_manager.wake_up()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/standby/status")
def api_standby_status():
    return jsonify({"standby": standby_manager.is_standby()})


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    """Reboots the box."""
    import subprocess
    logging.getLogger("WEB").info("Reboot requested via web UI.")
    subprocess.Popen(["reboot"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True, "message": i18n.t("update.rebooting")})


# ── Settings ─────────────────────────────────────────────────────────────────

@app.route("/settings")
def settings_page():
    import socket
    cfg = load_config()
    sd = cfg.get("shutdown", {})
    version = "?"
    vf = os.path.join(DIR, "version.txt")
    if os.path.exists(vf):
        with open(vf) as f:
            version = f.read().strip()
    settings = {
        "auto_standby_minutes": cfg.get("auto_standby_minutes", 30),
        "display_off_minutes": cfg.get("display_off_minutes", 30),
        "hold_time": sd.get("hold_time", 5),
        "confirm_timeout": sd.get("confirm_timeout", 15),
    }
    return render_template("settings.html",
                           hostname=socket.gethostname(),
                           version=version,
                           settings=settings,
                           languages=i18n.available_languages())


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.json or {}
    def _update(cfg):
        cfg["auto_standby_minutes"] = max(0, min(480, int(data.get("auto_standby_minutes", 30))))
        cfg["display_off_minutes"] = max(1, min(480, int(data.get("display_off_minutes", 30))))
        cfg["shutdown"] = {
            "hold_time": max(2, min(15, int(data.get("hold_time", 5)))),
            "confirm_timeout": max(5, min(60, int(data.get("confirm_timeout", 15)))),
        }
    config_manager.update_config(_update)
    return jsonify({"ok": True})


@app.route("/api/hostname", methods=["POST"])
def api_hostname():
    """Changes the hostname of the box and reboots."""
    import subprocess
    data = request.json or {}
    new_name = data.get("hostname", "").strip().lower()
    if not new_name or len(new_name) < 2:
        return jsonify({"ok": False, "message": i18n.t("settings.hostname_short")}), 400
    if not all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in new_name):
        return jsonify({"ok": False, "message": i18n.t("settings.hostname_invalid")}), 400

    import socket
    old_name = socket.gethostname()
    if new_name == old_name:
        return jsonify({"ok": True, "message": i18n.t("settings.hostname_same")})

    try:
        # 1. Set hostname via hostnamectl
        subprocess.run(["hostnamectl", "set-hostname", new_name],
                       capture_output=True, timeout=10, check=True)

        # 2. Update /etc/hosts
        with open("/etc/hosts") as f:
            hosts = f.read()
        hosts = hosts.replace(old_name, new_name)
        with open("/etc/hosts", "w") as f:
            f.write(hosts)

        # 3. Invalidate player cache (Squeezelite uses $(hostname) dynamically)
        lms_client.invalidate_player_cache()

        # 4. Update config: AP SSID, box_id, known_boxes
        def _update_hostname_refs(cfg):
            # WiFi AP SSID
            wifi = cfg.get("wifi", {})
            old_ssid = wifi.get("ap_ssid", "")
            if old_name in old_ssid:
                wifi["ap_ssid"] = old_ssid.replace(old_name, new_name)
            elif not old_ssid or old_ssid == "kid2tuneAP":
                wifi["ap_ssid"] = f"{new_name}-kid2tune"
            cfg["wifi"] = wifi

            # Update sync box_id
            sync = cfg.get("sync", {})
            old_box_id = sync.get("box_id", "")
            if old_name in old_box_id:
                sync["box_id"] = old_box_id.replace(old_name, new_name)
            cfg["sync"] = sync

            # known_boxes: rename old hostname entry
            known = cfg.get("known_boxes", {})
            if old_name in known:
                known[new_name] = known.pop(old_name)
                cfg["known_boxes"] = known
        config_manager.update_config(_update_hostname_refs)

        logging.getLogger("WEB").info(f"Hostname changed: {old_name} -> {new_name}")

        # 5. Reboot after brief delay
        subprocess.Popen(["bash", "-c", "sleep 2 && reboot"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        return jsonify({"ok": True, "message": i18n.t("settings.hostname_changed", name=new_name)})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ── Alarm/Clock ──────────────────────────────────────────────────────────────

_last_alarm_minute = ""  # Prevents double triggering in the same minute
_last_active_time = None  # Last timestamp with music (for auto standby)


def _auto_standby_loop():
    """Periodically checks whether auto standby should be triggered."""
    import time as _t
    global _last_active_time
    _last_active_time = _t.time()

    while True:
        try:
            cfg = load_config()
            minutes = cfg.get("auto_standby_minutes", 30)
            if minutes <= 0 or standby_manager.is_standby():
                _t.sleep(30)
                continue

            # Check status
            try:
                status = lms_client.get_status()
                if status.get("mode") == "play":
                    _last_active_time = _t.time()
            except Exception:
                pass

            idle_seconds = _t.time() - _last_active_time
            if idle_seconds >= minutes * 60:
                logging.getLogger("WEB").info(
                    f"Auto standby: {minutes} min idle – entering deep standby.")
                standby_manager.enter_standby()
                _last_active_time = _t.time()  # Reset after wake
        except Exception as e:
            logging.getLogger("WEB").error(f"Auto standby error: {e}")
        _t.sleep(30)


def _alarm_check_loop():
    """Checks every minute whether an alarm is due."""
    global _last_alarm_minute
    while True:
        try:
            cfg = load_config()
            alarms = cfg.get("alarms", [])
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            current_day = now.isoweekday()  # 1=Mon, 7=Sun

            # Only trigger once per minute
            if current_time != _last_alarm_minute:
                for alarm in alarms:
                    if not alarm.get("enabled", True):
                        continue
                    if alarm.get("time") == current_time:
                        days = alarm.get("days", [1,2,3,4,5,6,7])
                        if current_day in days:
                            uid = alarm.get("rfid_uid", "")
                            vol = alarm.get("volume", 30)
                            if uid:
                                entry = cfg.get("rfid_mappings", {}).get(uid)
                                if entry:
                                    lms_client.set_volume(vol)
                                    lms_client.play_item(entry.get("type", "url"), entry.get("value", ""))
                                    logging.getLogger("WEB").info(f"Alarm: '{entry.get('label', uid)}' at vol {vol}")
                _last_alarm_minute = current_time
        except Exception as e:
            logging.getLogger("WEB").error(f"Alarm check error: {e}")
        import time as _t
        _t.sleep(60)


@app.route("/alarms")
def alarms_page():
    cfg = load_config()
    alarms = cfg.get("alarms", [])
    mappings = cfg.get("rfid_mappings", {})
    return render_template("alarms.html", alarms=alarms, mappings=mappings)


@app.route("/alarms/save", methods=["POST"])
def alarms_save():
    data = request.json or {}
    cfg = load_config()
    cfg["alarms"] = data.get("alarms", [])
    save_config(cfg)
    return jsonify({"ok": True})


STANDBY_PAUSE_FILE = "/tmp/lms_standby_pause_threads"


def _wifi_daemon_loop():
    """WiFi manager daemon loop (runs as thread in lms-web)."""
    time.sleep(15)  # Wait until wpa_supplicant is ready
    while True:
        try:
            if os.path.exists(STANDBY_PAUSE_FILE):
                time.sleep(5)
                continue
            wifi_manager.daemon_tick()
        except Exception as e:
            logging.getLogger("WEB").error(f"WiFi thread error: {e}")
        cfg = load_config()
        interval = cfg.get("wifi", {}).get("check_interval", 30)
        time.sleep(interval)


def _bluetooth_daemon_loop():
    """Bluetooth manager daemon loop (runs as thread in lms-web)."""
    bluetooth_manager.ensure_adapter_powered()
    while True:
        try:
            if os.path.exists(STANDBY_PAUSE_FILE):
                time.sleep(5)
                continue
            bluetooth_manager.daemon_tick()
        except Exception as e:
            logging.getLogger("WEB").error(f"Bluetooth thread error: {e}")
        cfg = load_config()
        interval = cfg.get("bluetooth", {}).get("check_interval", 15)
        time.sleep(interval)


if __name__ == "__main__":
    # Boot cleanup: remove standby flag, disable WiFi power save
    standby_manager.ensure_awake_on_boot()

    # Start background threads
    threading.Thread(target=_alarm_check_loop, daemon=True).start()
    threading.Thread(target=_auto_standby_loop, daemon=True).start()
    threading.Thread(target=_wifi_daemon_loop, daemon=True, name="wifi").start()
    threading.Thread(target=_bluetooth_daemon_loop, daemon=True, name="bluetooth").start()
    logging.getLogger("WEB").info("WiFi and Bluetooth threads started.")

    app.run(host="0.0.0.0", port=80, debug=False, threaded=True)
