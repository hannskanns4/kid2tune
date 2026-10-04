"""
web_app.py – Flask web interface for kid2tune
Port: 80
"""
import json
import csv
import io
import os
import re
import sys
import time
import logging
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEB] %(levelname)s: %(message)s",
)
log = logging.getLogger("WEB")

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
import play_history
import lms_plugins
import i18n

# Load language from config.json
_lang = config_manager.read_config().get("language", "de")
i18n.load_language(_lang)

app = Flask(__name__, template_folder=os.path.join(DIR, "templates"),
            static_folder=os.path.join(DIR, "static"))


def _ensure_secret_key() -> str:
    """Session secret for Flask, generated once and stored in config.json."""
    import secrets as _secrets
    cfg = config_manager.read_config()
    secret = cfg.get("web_secret", "")
    if not secret:
        secret = _secrets.token_hex(32)
        def _update(c):
            # Another process may have generated one in the meantime
            if not c.get("web_secret"):
                c["web_secret"] = secret
        config_manager.update_config(_update)
        secret = config_manager.read_config().get("web_secret", secret)
    return secret


app.secret_key = _ensure_secret_key()

# Uploads (music files, update packages) are read into memory – without a cap
# a single large POST is enough to OOM-kill the service on a Pi.
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024
# Do not send the session cookie on cross-site requests: without it any page
# opened in the household could act on an unlocked adult session.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True


@app.context_processor
def inject_i18n():
    """Makes t() and the current language available in all templates."""
    return {"t": i18n.t, "current_lang": i18n.get_language()}


# ── Adult area (PIN protection, opt-in) ──────────────────────────────────────
# PIN hashing lives in security_manager; pin_tool.py resets the PIN via CLI.

ADULT_SESSION_MINUTES = 30

# Failed PIN attempts per client IP: {ip: {"fails": int, "until": float}}.
# Flask runs threaded, so every access is taken under _pin_lock.
_pin_attempts = {}
_pin_lock = threading.Lock()

# Routes children may use while the adult area is locked: playback, volume,
# history, artwork and the unlock dialog itself. Everything not listed here is
# locked by default — a deny-list would silently expose every new route.
PUBLIC_ENDPOINTS = {
    "static",
    # Pages
    "index", "dashboard_page", "history_page",
    # Status
    "api_status", "api_status_full", "api_version", "api_standby_status",
    # Playback
    "api_control", "api_play_url", "api_volume", "api_volume_max_get",
    "rfid_play", "rfid_pending_play", "lms_search",
    "api_history", "api_history_play",
    "api_artwork_current", "api_artwork_lms", "api_artwork_resolve",
    # Display / wake-up (a locked box must not stay dark)
    "lcd_backlight_status", "lcd_backlight_set", "api_wake",
    # Multiroom is a play feature (there are RFID cards for it)
    "multiroom_status", "multiroom_sync", "multiroom_unsync",
    "multiroom_unsync_all", "api_discover", "api_discover_known",
    # Unlock dialog
    "api_security_status", "api_security_unlock", "api_security_lock",
}

# Boxes call these on each other and have no browser session, so the PIN can
# never apply. They are protected by the cluster secret instead (see
# _cluster_authorized) and are restricted to callers on the local network.
CLUSTER_ENDPOINTS = {
    "update_version", "update_package", "update_git",
    "multiroom_join", "multiroom_leave",
}

# Locked pages render the PIN dialog; everything else gets a JSON 403.
PAGE_ENDPOINTS = {
    "rfid_page", "buttons_page", "sync_page", "wifi_page", "bluetooth_page",
    "lcd_layout_page", "settings_page", "cards_page", "local_music_page",
}


def _adult_locked() -> bool:
    """True if the adult area is enabled and this session is not unlocked."""
    import security_manager
    if not security_manager.is_enabled():
        return False
    ts = session.get("adult_unlock_ts", 0)
    return (time.time() - ts) > ADULT_SESSION_MINUTES * 60


def _is_local_caller() -> bool:
    """True if the request comes from the local network.

    Not a security boundary on its own — it only keeps the box-to-box
    endpoints from being reachable if the router ever exposes port 80.
    """
    import ipaddress
    try:
        addr = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def _cluster_authorized() -> bool:
    """Checks the shared secret for box-to-box calls.

    `security.cluster_secret` must be identical on all boxes of a household.
    As long as it is unset the endpoints stay open (as before) — otherwise an
    update would lock out boxes that do not know the secret yet.
    """
    import secrets as _secrets
    cfg = load_config()
    expected = cfg.get("security", {}).get("cluster_secret", "")
    if not expected:
        return True
    supplied = request.headers.get("X-Cluster-Secret", "")
    return _secrets.compare_digest(supplied, expected)


@app.before_request
def _adult_gate():
    """Central lock for the adult area (default deny).

    Applied here rather than per route so a newly added route is protected
    automatically instead of being forgotten.
    """
    endpoint = request.endpoint
    if endpoint is None:
        return None  # 404 – let Flask handle it

    if endpoint in CLUSTER_ENDPOINTS:
        if not _is_local_caller():
            log.warning(f"Box-to-box call from outside the network rejected: "
                        f"{request.remote_addr} -> {request.path}")
            return jsonify({"ok": False, "message": "Not allowed."}), 403
        if not _cluster_authorized():
            log.warning(f"Box-to-box call with wrong cluster secret: "
                        f"{request.remote_addr} -> {request.path}")
            return jsonify({"ok": False, "message": "Invalid cluster secret."}), 403
        return None

    if endpoint in PUBLIC_ENDPOINTS:
        return None

    if _adult_locked():
        if endpoint in PAGE_ENDPOINTS:
            return render_template("pin.html")
        return jsonify({"ok": False, "locked": True,
                        "message": i18n.t("security.locked_msg")}), 403
    return None


def require_adult(f):
    """Explicit guard for adult-area routes.

    Redundant with the _adult_gate before_request hook, kept so the protection
    remains visible at the route and survives a refactor of the allowlist."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _adult_locked():
            return jsonify({"ok": False, "locked": True,
                            "message": i18n.t("security.locked_msg")}), 403
        return f(*args, **kwargs)
    return wrapper


@app.route("/api/security/status")
def api_security_status():
    import security_manager
    return jsonify({"enabled": security_manager.is_enabled(),
                    "locked": _adult_locked()})


@app.route("/api/security/unlock", methods=["POST"])
def api_security_unlock():
    """Verifies the PIN and unlocks the adult area for this session."""
    import security_manager
    client = request.remote_addr or "?"

    with _pin_lock:
        state = _pin_attempts.get(client)
        if state and time.time() < state["until"]:
            wait = int(state["until"] - time.time()) + 1
            return jsonify({"ok": False,
                            "message": i18n.t("security.too_many_attempts", seconds=wait)}), 429

    pin = ((request.get_json(silent=True) or {}).get("pin") or "").strip()
    if security_manager.verify_pin(pin):
        with _pin_lock:
            _pin_attempts.pop(client, None)
        session["adult_unlock_ts"] = time.time()
        return jsonify({"ok": True})

    with _pin_lock:
        # Per client, so one attacker cannot lock the parent out of the box.
        state = _pin_attempts.setdefault(client, {"fails": 0, "until": 0.0})
        state["fails"] += 1
        if state["fails"] >= 3:
            # Exponential: 30s, 60s, 120s ... capped at one hour. A 4-digit PIN
            # is no longer exhaustible this way.
            delay = min(30 * 2 ** (state["fails"] - 3), 3600)
            state["until"] = time.time() + delay
        if len(_pin_attempts) > 256:  # no unbounded growth from spoofed sources
            cutoff = time.time()
            for ip in [k for k, v in _pin_attempts.items()
                       if v["until"] < cutoff and k != client][:128]:
                _pin_attempts.pop(ip, None)

    time.sleep(0.5)  # slow down brute force
    return jsonify({"ok": False, "message": i18n.t("security.wrong_pin")}), 401


@app.route("/api/security/lock", methods=["POST"])
def api_security_lock():
    session.pop("adult_unlock_ts", None)
    return jsonify({"ok": True})


@app.route("/api/security/config", methods=["POST"])
@require_adult
def api_security_config():
    """Enables/disables the adult area and sets the PIN.
    Only reachable when the area is disabled or the session is unlocked."""
    import security_manager
    data = request.json or {}
    enabled = bool(data.get("enabled"))
    pin = (data.get("pin") or "").strip()

    if enabled:
        if pin:
            if not security_manager.pin_valid_format(pin):
                return jsonify({"ok": False,
                                "message": i18n.t("security.pin_format")}), 400
            security_manager.set_pin(pin)
        elif not security_manager.has_pin():
            return jsonify({"ok": False,
                            "message": i18n.t("security.pin_required")}), 400
        security_manager.set_enabled(True)
        # The session that enabled protection stays unlocked
        session["adult_unlock_ts"] = time.time()
    else:
        security_manager.set_enabled(False)
    return jsonify({"ok": True, "enabled": enabled})


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
        val = int((request.get_json(silent=True) or {}).get("volume", 50))
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
    try:
        val = max(10, min(100, int((request.get_json(silent=True) or {}).get("volume_max", 100))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": i18n.t("player.invalid_volume")}), 400
    def _update(cfg):
        cfg["volume_max"] = val
    config_manager.update_config(_update)
    return jsonify({"ok": True, "volume_max": val})


# ── RFID Management ─────────────────────────────────────────────────────────

RFID_CSV_COLUMNS = ("card_id", "link", "description", "type", "resume")
RFID_MAPPING_TYPES = {
    "track", "album", "playlist", "url", "local", "local_album",
    "bluetooth", "multiroom", "sleep", "shutdown",
}


def _normalize_rfid_uid(value):
    uid = re.sub(r"[\s:-]", "", str(value or "")).upper()
    return uid if re.fullmatch(r"(?:[0-9A-F]{2}){4,20}", uid) else ""


def _validate_rfid_csv_rows(rows):
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("empty_or_large")
    validated = []
    seen_uids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid_row")
        card_id = _normalize_rfid_uid(row.get("card_id", ""))
        if row.get("card_id") and not card_id:
            raise ValueError("invalid_uid")
        if card_id and card_id in seen_uids:
            raise ValueError("duplicate_uid")
        if card_id:
            seen_uids.add(card_id)
        value = str(row.get("link", "") or "").strip()
        if not value:
            raise ValueError("missing_link")
        item_type = str(row.get("type", "url") or "url").strip().lower()
        if item_type not in RFID_MAPPING_TYPES:
            raise ValueError("invalid_type")
        label = str(row.get("description", "") or "").strip() or value
        resume = str(row.get("resume", "") or "").strip().lower() in {"1", "true", "yes", "ja", "on"}
        spotify = re.match(
            r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist)/([a-zA-Z0-9]+)",
            value,
        )
        if spotify:
            value = f"spotify:{spotify.group(1)}:{spotify.group(2)}"
            item_type = "url"
        validated.append({
            "card_id": card_id,
            "link": value,
            "description": label,
            "type": item_type,
            "resume": resume,
        })
    return validated


def _parse_rfid_csv(text):
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = {str(name or "").strip().lower(): name for name in (reader.fieldnames or [])}

    def find_header(*names):
        return next((headers[name] for name in names if name in headers), None)

    link_key = find_header("link", "url", "value", "inhalt")
    if not link_key:
        raise ValueError("missing_header")
    card_key = find_header("card_id", "uid", "karten_id", "karten-id")
    description_key = find_header("description", "beschreibung", "comment", "kommentar", "label")
    type_key = find_header("type", "typ")
    resume_key = find_header("resume", "position_merken")
    rows = []
    for raw in reader:
        if not any(str(value or "").strip() for value in raw.values()):
            continue
        rows.append({
            "card_id": raw.get(card_key, "") if card_key else "",
            "link": raw.get(link_key, ""),
            "description": raw.get(description_key, "") if description_key else "",
            "type": raw.get(type_key, "url") if type_key else "url",
            "resume": raw.get(resume_key, "") if resume_key else "",
        })
    return _validate_rfid_csv_rows(rows)


def _rfid_express_queue(cfg=None):
    cfg = cfg or load_config()
    pending = cfg.get("pending_mappings", [])
    mappings = cfg.get("rfid_mappings", {})
    queue = []
    reserved_files = set()
    reserved_albums = []

    for index, entry in enumerate(pending):
        pending_id = str(entry.get("id") or f"pending-{index}")
        queue.append({
            "key": f"pending:{pending_id}",
            "pending_id": pending_id,
            "label": entry.get("label", entry.get("value", "")),
            "type": entry.get("type", "url"),
            "value": entry.get("value", ""),
            "resume": bool(entry.get("resume", False)),
        })

    for entry in list(mappings.values()) + list(pending):
        value = entry.get("value", "")
        item_type = entry.get("type", "url")
        safe_path = sync_manager.safe_music_path(value) if item_type in ("local", "local_album") else None
        if safe_path is None:
            continue
        real_path = os.path.realpath(safe_path)
        if item_type == "local":
            reserved_files.add(real_path)
        else:
            reserved_albums.append(real_path)

    music_root = os.path.realpath(sync_manager.MUSIC_DIR)
    if os.path.isdir(music_root):
        for root, directories, filenames in os.walk(music_root, followlinks=False):
            directories[:] = [name for name in directories if not os.path.islink(os.path.join(root, name))]
            for filename in filenames:
                path = os.path.join(root, filename)
                if os.path.splitext(filename)[1].lower() not in sync_manager.MUSIC_EXTENSIONS:
                    continue
                real_path = os.path.realpath(path)
                try:
                    if os.path.commonpath((music_root, real_path)) != music_root:
                        continue
                except ValueError:
                    continue
                if real_path in reserved_files or any(
                    real_path.startswith(album + os.sep) for album in reserved_albums
                ):
                    continue
                relative = os.path.relpath(real_path, music_root).replace(os.sep, "/")
                queue.append({
                    "key": f"file:{relative}",
                    "pending_id": "",
                    "label": os.path.splitext(os.path.basename(filename))[0],
                    "type": "local",
                    "value": relative,
                    "resume": False,
                })
    return queue


def _render_rfid_page(**extra):
    cfg = load_config()
    context = {
        "mappings": cfg.get("rfid_mappings", {}),
        "pending": cfg.get("pending_mappings", []),
        "albums": sync_manager.list_music_albums(),
        "express_queue": _rfid_express_queue(cfg),
        "csv_error": request.args.get("csv_error", ""),
        "csv_imported": request.args.get("csv_imported", ""),
        "csv_skipped": request.args.get("csv_skipped", ""),
    }
    context.update(extra)
    return render_template("rfid.html", **context)


@app.route("/rfid")
def rfid_page():
    return _render_rfid_page()


@app.route("/rfid/csv/template")
def rfid_csv_template():
    output = io.StringIO(newline="")
    csv.writer(output).writerow(RFID_CSV_COLUMNS)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="rfid-vorlage.csv",
    )


@app.route("/rfid/csv/export")
def rfid_csv_export():
    cfg = load_config()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=RFID_CSV_COLUMNS)
    writer.writeheader()
    for card_id, entry in sorted(cfg.get("rfid_mappings", {}).items()):
        writer.writerow({
            "card_id": card_id,
            "link": entry.get("value", ""),
            "description": entry.get("label", ""),
            "type": entry.get("type", "url"),
            "resume": "1" if entry.get("resume") else "0",
        })
    for entry in cfg.get("pending_mappings", []):
        writer.writerow({
            "link": entry.get("value", ""),
            "description": entry.get("label", ""),
            "type": entry.get("type", "url"),
            "resume": "1" if entry.get("resume") else "0",
        })
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name="rfid-backup.csv",
    )


@app.route("/rfid/csv/import", methods=["POST"])
def rfid_csv_import():
    if request.form.get("confirm") == "1":
        try:
            rows = _validate_rfid_csv_rows(json.loads(request.form.get("rows", "")))
        except (ValueError, TypeError, json.JSONDecodeError):
            return redirect(url_for("rfid_page", csv_error="1"))
        decisions = [request.form.get(f"conflict_{index}", "keep") for index in range(len(rows))]
        imported, skipped = _apply_rfid_csv(rows, decisions)
        return redirect(url_for("rfid_page", csv_imported=imported, csv_skipped=skipped))

    upload = request.files.get("csv_file")
    if upload is None or not upload.filename:
        return redirect(url_for("rfid_page", csv_error="1"))
    try:
        content = upload.read()
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1252")
        rows = _parse_rfid_csv(text)
    except (UnicodeDecodeError, csv.Error, ValueError):
        return redirect(url_for("rfid_page", csv_error="1"))

    mappings = load_config().get("rfid_mappings", {})
    for row in rows:
        row["conflict"] = bool(row["card_id"] and row["card_id"] in mappings)
    return _render_rfid_page(
        import_preview=rows,
        import_rows=json.dumps([
            {key: row[key] for key in RFID_CSV_COLUMNS} for row in rows
        ], ensure_ascii=False),
    )


def _apply_rfid_csv(rows, decisions):
    from datetime import timezone
    now = datetime.now(timezone.utc).isoformat()
    imported = 0
    skipped = 0
    changed_mappings = []

    def _update(cfg):
        nonlocal imported, skipped
        mappings = cfg.setdefault("rfid_mappings", {})
        pending = cfg.setdefault("pending_mappings", [])
        box_id = cfg.get("sync", {}).get("box_id", "unknown")
        for index, row in enumerate(rows):
            card_id = row["card_id"]
            if card_id:
                if card_id in mappings and (index >= len(decisions) or decisions[index] != "replace"):
                    skipped += 1
                    continue
                entry = {
                    "label": row["description"],
                    "type": row["type"],
                    "value": row["link"],
                    "resume": row["resume"],
                    "position": 0,
                    "updated_at": now,
                    "updated_by": box_id,
                }
                mappings[card_id] = entry
                changed_mappings.append((card_id, entry))
            else:
                if any(
                    entry.get("type", "url") == row["type"]
                    and entry.get("value") == row["link"]
                    for entry in pending
                ):
                    skipped += 1
                    continue
                pending.append({
                    "id": uuid.uuid4().hex[:8],
                    "label": row["description"],
                    "type": row["type"],
                    "value": row["link"],
                    "resume": row["resume"],
                    "created_at": now,
                })
            imported += 1

    config_manager.update_config(_update)
    for card_id, entry in changed_mappings:
        sync_manager.queue_change("upsert", card_id, entry)
    if changed_mappings:
        try:
            sync_manager.push_mappings()
        except Exception:
            pass
    return imported, skipped


@app.route("/api/rfid/express/queue")
def rfid_express_queue():
    return jsonify({"items": _rfid_express_queue()})


@app.route("/api/rfid/express/assign", methods=["POST"])
def rfid_express_assign():
    data = request.get_json(silent=True) or {}
    uid = _normalize_rfid_uid(data.get("uid", ""))
    item_key = str(data.get("item_key", ""))
    if not uid:
        return jsonify({"ok": False, "message": i18n.t("rfid.express_invalid_card")}), 400
    try:
        with open(LAST_RFID_FILE) as source:
            scanned_uid = source.read().strip().upper()
    except OSError:
        scanned_uid = ""
    if scanned_uid != uid:
        return jsonify({"ok": False, "message": i18n.t("rfid.express_card_missing")}), 409

    from datetime import timezone
    now = datetime.now(timezone.utc).isoformat()
    result = {"entry": None, "error": "stale"}

    def _update(cfg):
        mappings = cfg.setdefault("rfid_mappings", {})
        if uid in mappings:
            result["error"] = "assigned"
            return
        item = next((item for item in _rfid_express_queue(cfg) if item["key"] == item_key), None)
        if item is None:
            return
        if item.get("pending_id"):
            pending = cfg.get("pending_mappings", [])
            if not any(entry.get("id") == item["pending_id"] for entry in pending):
                return
            cfg["pending_mappings"] = [
                entry for entry in pending if entry.get("id") != item["pending_id"]
            ]
        entry = {
            "label": item["label"],
            "type": item["type"],
            "value": item["value"],
            "resume": item["resume"],
            "position": 0,
            "updated_at": now,
            "updated_by": cfg.get("sync", {}).get("box_id", "unknown"),
        }
        mappings[uid] = entry
        result["entry"] = entry
        result["error"] = ""

    config_manager.update_config(_update)
    if result["entry"] is None:
        message = i18n.t("rfid.express_card_assigned") if result["error"] == "assigned" else i18n.t("rfid.express_stale")
        try:
            with open(LAST_RFID_FILE) as source:
                if source.read().strip().upper() == uid:
                    os.remove(LAST_RFID_FILE)
        except OSError:
            pass
        return jsonify({"ok": False, "message": message,
                        "items": _rfid_express_queue()}), 409

    try:
        with open(LAST_RFID_FILE) as source:
            if source.read().strip().upper() == uid:
                os.remove(LAST_RFID_FILE)
    except OSError:
        pass
    sync_manager.queue_change("upsert", uid, result["entry"])
    try:
        sync_manager.push_mappings()
    except Exception:
        pass
    return jsonify({
        "ok": True,
        "assigned": {"uid": uid, **result["entry"]},
        "items": _rfid_express_queue(),
    })


@app.route("/local-music")
def local_music_page():
    return render_template("local_music.html",
                           albums=sync_manager.list_music_albums(),
                           message=request.args.get("message", ""))


@app.route("/local-music/upload", methods=["POST"])
def local_music_upload():
    from werkzeug.utils import secure_filename

    album_name = secure_filename(request.form.get("album", "").strip())
    uploads = request.files.getlist("music_files")
    allowed_extensions = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma"}
    files = []
    for upload in uploads:
        if not upload.filename:
            continue
        filename = secure_filename(upload.filename.replace("\\", "/").rsplit("/", 1)[-1])
        if not filename or os.path.splitext(filename)[1].lower() not in allowed_extensions:
            return redirect(url_for("local_music_page", message=i18n.t("local_music.invalid_file")))
        files.append((filename, upload))

    if not album_name or not files:
        return redirect(url_for("local_music_page", message=i18n.t("local_music.album_required")))
    if len({filename.lower() for filename, _ in files}) != len(files):
        return redirect(url_for("local_music_page", message=i18n.t("local_music.duplicate_files")))

    album_path = sync_manager.safe_music_path(album_name)
    if album_path is None or os.path.exists(album_path):
        return redirect(url_for("local_music_page", message=i18n.t("local_music.album_exists")))
    os.makedirs(album_path, exist_ok=False)
    for filename, upload in files:
        upload.save(os.path.join(album_path, filename))
    for filename, _ in files:
        try:
            sync_manager.push_music_file(os.path.join(album_path, filename))
        except Exception:
            pass
    return redirect(url_for("local_music_page", message=i18n.t("local_music.uploaded")))


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

    if itype == "local_album":
        value = data.get("album_value", "").strip()
        album_path = sync_manager.safe_music_path(value)
        if not value or album_path is None or not os.path.isdir(album_path):
            return redirect(url_for("rfid_page"))

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
    resume = request.form.get("resume") == "1"
    # If this assignment came from a pre-saved ("pending") entry, remove it
    pending_id = request.form.get("pending_id", "").strip()

    _holder = {}
    def _update(cfg):
        box_id = cfg.get("sync", {}).get("box_id", "unknown")
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
        if pending_id:
            cfg["pending_mappings"] = [
                e for e in cfg.get("pending_mappings", []) if e.get("id") != pending_id
            ]
        _holder["mapping"] = mapping_data
    config_manager.update_config(_update)
    mapping_data = _holder["mapping"]

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
    resume = request.form.get("resume") == "1"

    _holder = {}
    def _update(cfg):
        mappings = cfg.get("rfid_mappings", {})
        if uid not in mappings:
            return
        box_id = cfg.get("sync", {}).get("box_id", "unknown")
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
        _holder["mapping"] = mapping_data
    config_manager.update_config(_update)
    mapping_data = _holder.get("mapping")
    if mapping_data is None:
        return redirect(url_for("rfid_page"))

    sync_manager.queue_change("upsert", uid, mapping_data)
    try:
        sync_manager.push_mappings()
    except Exception:
        pass

    return redirect(url_for("rfid_page"))


def _play_mapping(entry, fallback_label=""):
    """Plays a mapping entry (real card or pending). Returns (json_dict, status).

    Shared by /rfid/play/<uid> and /rfid/pending/play/<pid> so pending entries
    behave exactly like assigned cards – just without a UID."""
    item_type = entry.get("type", "url")
    item_id   = entry.get("value", "")
    label     = entry.get("label", fallback_label) or fallback_label
    try:
        if item_type == "bluetooth":
            ok, msg = bluetooth_manager.connect_device(item_id)
            if ok:
                bluetooth_manager.switch_audio_to_bluetooth(item_id)
            return {"ok": ok, "message": msg}, (200 if ok else 500)
        elif item_type == "local":
            local_path = sync_manager.safe_music_path(item_id)
            if local_path is None:
                return {"ok": False, "message": i18n.t("rfid.file_not_found", error=item_id)}, 404
            if not os.path.isfile(local_path):
                ok, result = sync_manager.pull_music_file(item_id)
                if not ok:
                    return {"ok": False, "message": i18n.t("rfid.file_not_found", error=result)}, 404
                local_path = result
            lms_client.play_item("url", f"file://{local_path}", label=label)
            try:
                sync_manager.push_music_file(local_path)
            except Exception:
                pass
            return {"ok": True, "message": i18n.t("rfid.playing", label=label)}, 200
        elif item_type == "local_album":
            tracks = sync_manager.pull_music_album(item_id)
            for index, track_path in enumerate(tracks):
                if not os.path.isfile(track_path):
                    rel_path = os.path.relpath(track_path, sync_manager.MUSIC_DIR)
                    ok, result = sync_manager.pull_music_file(rel_path)
                    if not ok:
                        return {"ok": False, "message": i18n.t("rfid.file_not_found", error=result)}, 404
                    tracks[index] = result
            if not tracks:
                return {"ok": False, "message": i18n.t("rfid.file_not_found", error=item_id)}, 404
            lms_client.play_local_album(tracks, item_id, label=label)
            for track_path in tracks:
                try:
                    sync_manager.push_music_file(track_path)
                except Exception:
                    pass
            return {"ok": True, "message": i18n.t("rfid.playing", label=label)}, 200
        elif item_type == "sleep":
            try:
                minutes = int(item_id)
            except (ValueError, TypeError):
                minutes = 15
            return {"ok": True, "message": i18n.t("rfid.sleep_timer", minutes=minutes)}, 200
        elif item_type == "multiroom":
            mr_status = multiroom_manager.get_status()
            if mr_status.get("active"):
                if mr_status.get("role") == "master":
                    multiroom_manager.deactivate_master()
                    return {"ok": True, "message": i18n.t("multiroom.deactivated_short")}, 200
                else:
                    multiroom_manager.leave_master()
                    return {"ok": True, "message": i18n.t("multiroom.slave_left")}, 200
            else:
                ok = multiroom_manager.activate_master()
                msg = i18n.t("multiroom.activated") if ok else i18n.t("multiroom.no_boxes")
                return {"ok": ok, "message": msg}, 200
        else:
            lms_client.play_item(item_type, item_id, label=label)
            return {"ok": True, "message": i18n.t("rfid.playing", label=label)}, 200
    except Exception as e:
        return {"ok": False, "message": str(e)}, 500


@app.route("/rfid/play/<uid>", methods=["POST"])
def rfid_play(uid):
    uid = uid.strip().upper()
    cfg = load_config()
    mappings = cfg.get("rfid_mappings", {})
    if uid not in mappings:
        return jsonify({"ok": False, "message": i18n.t("rfid.card_not_found")}), 404
    result, status = _play_mapping(mappings[uid], fallback_label=uid)
    return jsonify(result), status


@app.route("/rfid/delete/<uid>", methods=["POST"])
def rfid_delete(uid):
    def _update(cfg):
        cfg.get("rfid_mappings", {}).pop(uid.upper(), None)
    config_manager.update_config(_update)

    # NAS sync: tombstone in queue, then try push
    sync_manager.queue_change("delete", uid.upper())
    try:
        sync_manager.push_mappings()
    except Exception:
        pass  # Queue remains for next sync

    return redirect(url_for("rfid_page"))


# ── Pending mappings (saved without a card yet) ─────────────────────────────

@app.route("/rfid/pending/add", methods=["POST"])
def rfid_pending_add():
    """Stores a playable item without a card. Later linkable to a UID via the
    assign form. Used by the history page's 'remember' button."""
    from datetime import timezone
    import uuid as _uuid
    data  = request.json or request.form
    label = (data.get("label") or "").strip()
    itype = (data.get("type") or "url").strip()
    value = (data.get("value") or "").strip()
    if not value:
        return jsonify({"ok": False, "message": i18n.t("player.no_link")}), 400
    resume = str(data.get("resume")) in ("1", "true", "True", "on")
    entry = {
        "id":     _uuid.uuid4().hex[:8],
        "label":  label or value,
        "type":   itype,
        "value":  value,
        "resume": resume,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    def _add(cfg):
        lst = cfg.setdefault("pending_mappings", [])
        if any(e.get("value") == value for e in lst):
            return  # already pending – no duplicate
        lst.insert(0, entry)
    config_manager.update_config(_add)
    return jsonify({"ok": True, "entry": entry})


@app.route("/rfid/pending/delete/<pid>", methods=["POST"])
def rfid_pending_delete(pid):
    def _del(cfg):
        lst = cfg.get("pending_mappings", [])
        cfg["pending_mappings"] = [e for e in lst if e.get("id") != pid]
    config_manager.update_config(_del)
    return jsonify({"ok": True})


@app.route("/rfid/pending/edit/<pid>", methods=["POST"])
def rfid_pending_edit(pid):
    """Edits a pending entry (label/type/value/resume) – mirrors /rfid/edit."""
    data  = request.form
    label = data.get("label", "").strip()
    itype = data.get("type", "url")
    value = data.get("value", "").strip()
    if not value:
        return redirect(url_for("rfid_page"))

    # Convert Spotify URL to URI (same as card assign/edit)
    import re as _re
    sp = _re.match(r"https?://open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist)/([a-zA-Z0-9]+)", value)
    if sp:
        value = f"spotify:{sp.group(1)}:{sp.group(2)}"
        itype = "url"

    resume = request.form.get("resume") == "1"

    def _edit(cfg):
        for e in cfg.get("pending_mappings", []):
            if e.get("id") == pid:
                e["label"]  = label or value
                e["type"]   = itype
                e["value"]  = value
                e["resume"] = resume
                break
    config_manager.update_config(_edit)
    return redirect(url_for("rfid_page"))


@app.route("/rfid/pending/play/<pid>", methods=["POST"])
def rfid_pending_play(pid):
    cfg = load_config()
    entry = next((e for e in cfg.get("pending_mappings", []) if e.get("id") == pid), None)
    if not entry:
        return jsonify({"ok": False, "message": i18n.t("rfid.card_not_found")}), 404
    result, status = _play_mapping(entry, fallback_label=entry.get("label", ""))
    return jsonify(result), status


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


# ── LCD Layout ───────────────────────────────────────────────────────────────

@app.route("/lcd-layout")
def lcd_layout_page():
    cfg = load_config()
    lcd_cfg = cfg.get("lcd", {})
    play_layout = lcd_cfg.get("play_layout", [
        "{title}", "{artist}", "{elapsed}/{duration}  {mode}", "{date}  {time}"
    ])
    return render_template("lcd_layout.html", play_layout=play_layout)


@app.route("/lcd-layout/save", methods=["POST"])
def lcd_layout_save():
    data = request.get_json(silent=True) or {}
    lines = data.get("play_layout", [])
    if not isinstance(lines, list) or len(lines) != 4:
        return jsonify({"ok": False, "message": "4 lines required."}), 400
    # The LCD daemon renders these strings once per second; a non-string entry
    # would raise there every tick and flood the journal.
    if not all(isinstance(l, str) and len(l) <= 80 for l in lines):
        return jsonify({"ok": False, "message": "Invalid line (text, max 80 characters)."}), 400
    def _update(cfg):
        cfg.setdefault("lcd", {})["play_layout"] = lines
    config_manager.update_config(_update)
    return jsonify({"ok": True})


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
    btn_map = {}
    for action in ["vol_up", "vol_down", "next", "prev", "pause", "lcd_backlight"]:
        val = request.form.get(action, "")
        if val.isdigit() and 0 <= int(val) <= 27 and int(val) not in RESERVED_GPIO:
            btn_map[action] = int(val)
    # Locked read-modify-write: the WiFi/Bluetooth daemon threads write to the
    # same file, and a plain read+write would drop one of the two changes.
    def _update(cfg):
        cfg["buttons"] = btn_map
    config_manager.update_config(_update)
    import subprocess
    subprocess.run(["systemctl", "restart", "lms-hardware"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    return redirect(url_for("buttons_page"))


_detect_active = False
_detect_result = None
_detect_lock = threading.Lock()  # detection thread vs. polling request threads


@app.route("/buttons/detect/start", methods=["POST"])
def buttons_detect_start():
    """Start GPIO button detection mode. Listens on all non-reserved GPIOs."""
    global _detect_active, _detect_result
    with _detect_lock:
        if _detect_active:
            return jsonify({"ok": False, "message": "Detection already running."})
        _detect_active = True
        _detect_result = None

    def _detect():
        global _detect_active, _detect_result
        scan_pins = []
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
                with _detect_lock:
                    if not _detect_active:
                        break
                for pin in scan_pins:
                    if pin in initial_low:
                        continue
                    try:
                        if GPIO.input(pin) == GPIO.LOW:
                            with _detect_lock:
                                _detect_result = pin
                                _detect_active = False
                            return
                    except Exception:
                        pass
                time.sleep(0.1)
        except Exception as e:
            log.warning(f"Button detection failed: {e}")
        finally:
            # Always release the pins – otherwise they stay configured and
            # collide with the lms-hardware service.
            try:
                import RPi.GPIO as GPIO
                for p in scan_pins:
                    try:
                        GPIO.cleanup(p)
                    except Exception:
                        pass
            except Exception:
                pass
            with _detect_lock:
                _detect_active = False

    threading.Thread(target=_detect, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/buttons/detect/status")
def buttons_detect_status():
    """Poll detection result (consumed once, so a later poll is not stale)."""
    global _detect_result
    with _detect_lock:
        if _detect_result is not None:
            pin, _detect_result = _detect_result, None
            return jsonify({"active": False, "pin": pin})
        return jsonify({"active": _detect_active, "pin": None})


@app.route("/buttons/detect/stop", methods=["POST"])
def buttons_detect_stop():
    """Stop detection mode."""
    global _detect_active
    with _detect_lock:
        _detect_active = False
    return jsonify({"ok": True})


# ── NAS Sync Management ─────────────────────────────────────────────────────

@app.route("/sync")
def sync_page():
    sync_cfg = dict(sync_manager.get_sync_config())
    # Never send the NAS password to the browser – the template only needs to
    # know whether one is stored.
    sync_cfg["has_password"] = bool(sync_cfg.pop("password", ""))
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

    # Empty field = keep the stored password (it is never sent to the browser)
    if not password:
        password = sync_manager.get_sync_config().get("password", "")

    if nas_share and not sync_manager.valid_share(nas_share):
        return render_template("sync.html",
                               sync={**sync_manager.get_sync_config(),
                                     "password": "", "has_password": True,
                                     "nas_share": nas_share},
                               mapping_count=len(load_config().get("rfid_mappings", {})),
                               pending_count=sync_manager.get_pending_count(),
                               error=i18n.t("sync.invalid_share")), 400
    if not (sync_manager.valid_credential(username)
            and sync_manager.valid_credential(password)):
        return render_template("sync.html",
                               sync={**sync_manager.get_sync_config(),
                                     "password": "", "has_password": True},
                               mapping_count=len(load_config().get("rfid_mappings", {})),
                               pending_count=sync_manager.get_pending_count(),
                               error=i18n.t("sync.invalid_credentials")), 400

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
    wifi_cfg = dict(wifi_manager.get_wifi_config())
    # The AP passphrase must not end up in the page source
    wifi_cfg["has_ap_password"] = bool(wifi_cfg.pop("ap_password", ""))
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
    data = request.form or request.get_json(silent=True) or {}
    ap_ssid = (data.get("ap_ssid") or "").strip()
    ap_password = (data.get("ap_password") or "").strip()
    try:
        ap_channel = max(1, min(13, int(data.get("ap_channel", 7))))
        check_interval = max(10, min(300, int(data.get("check_interval", 30))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": i18n.t("wifi.invalid_values")}), 400

    # Empty field = keep the stored passphrase (it is not sent to the browser)
    if not ap_password:
        ap_password = wifi_manager.get_wifi_config().get("ap_password", "")

    if not ap_ssid:
        return jsonify({"ok": False, "message": i18n.t("wifi.ssid_empty")}), 400
    if not wifi_manager.valid_ssid(ap_ssid):
        return jsonify({"ok": False, "message": i18n.t("wifi.ssid_invalid")}), 400
    if len(ap_password) < 8:
        return jsonify({"ok": False, "message": i18n.t("wifi.ap_pw_short")}), 400
    if not wifi_manager.valid_psk(ap_password):
        return jsonify({"ok": False, "message": i18n.t("wifi.ap_pw_invalid")}), 400

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
        if os.path.exists(BACKLIGHT_FILE):
            with open(BACKLIGHT_FILE) as f:
                val = f.read().strip()
        else:
            val = "1"
    except Exception:
        val = "1"
    return jsonify({"on": val != "0"})


@app.route("/lcd/backlight", methods=["POST"])
def lcd_backlight_set():
    data = request.json or request.form or {}
    on = data.get("on", True)
    # Form values arrive as strings – "0"/"false"/"off" must count as False,
    # otherwise a non-empty string like "0" is truthy and the backlight never turns off.
    if isinstance(on, str):
        on = on.strip().lower() not in ("0", "false", "off", "no", "")
    else:
        on = bool(on)
    with open(BACKLIGHT_FILE, "w") as f:
        f.write("1" if on else "0")
    return jsonify({"ok": True, "on": on})


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
    data = request.get_json(silent=True) or {}
    master_ip = (data.get("master_ip") or "").strip()
    if not master_ip:
        return jsonify({"ok": False, "message": "master_ip missing."}), 400
    if not multiroom_manager.valid_ip(master_ip):
        return jsonify({"ok": False, "message": "Invalid master IP."}), 400
    try:
        if not multiroom_manager.join_master(master_ip):
            return jsonify({"ok": False, "message": i18n.t("multiroom.join_failed")}), 500
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
    """Returns file hashes for version comparison (ALL code files)."""
    files = {}
    for rel in update_manager.iter_code_files(DIR):
        files[rel] = update_manager.file_md5(os.path.join(DIR, rel))
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
            # Ensure extracted path stays within DIR. The separator matters:
            # without it '/opt/lms-controller-evil' would pass the check.
            base = os.path.realpath(DIR)
            target = os.path.realpath(os.path.join(DIR, member.name))
            if target != base and not target.startswith(base + os.sep):
                return jsonify({"ok": False, "message": i18n.t("security.traversal", name=member.name)}), 400
        # Runtime data (config, history) must never come from a package
        safe_members = [m for m in tar.getmembers()
                        if os.path.basename(m.name) not in update_manager.PROTECTED_FILES]
        # filter= was only backported to 3.9.17/3.10.12/3.11.4 – on an older
        # interpreter it raises TypeError and the update would fail entirely.
        # The members are already validated above.
        try:
            tar.extractall(path=DIR, members=safe_members, filter="data")
        except TypeError:
            tar.extractall(path=DIR, members=safe_members)
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
@require_adult
def update_trigger():
    """Bundles ALL local code files and sends them to all boxes on the network.
    Uses the same file list as the git update (update_manager.iter_code_files),
    so no category (e.g. static/) can be forgotten."""
    import tarfile, io
    # Create tar.gz
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in update_manager.iter_code_files(DIR):
            tar.add(os.path.join(DIR, rel), arcname=rel)
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
@require_adult
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
@require_adult
def update_token_set():
    data = request.json or {}
    token = data.get("token", "").strip()
    def _update(cfg):
        cfg["github_token"] = token
    config_manager.update_config(_update)
    return jsonify({"ok": True, "message": i18n.t("settings.token_saved") if token else i18n.t("settings.token_removed")})


# ── Play History ─────────────────────────────────────────────────────────────

@app.route("/history")
def history_page():
    return render_template("history.html", history=play_history.get_history())


@app.route("/api/history")
def api_history():
    return jsonify(play_history.get_history())


@app.route("/api/history/play", methods=["POST"])
def api_history_play():
    """Re-plays an entry from the history by its `value` (track id, URI, URL)."""
    data = request.json or {}
    value = (data.get("value") or "").strip()
    if not value:
        return jsonify({"ok": False, "message": i18n.t("player.no_link")}), 400
    item_type = (data.get("type") or "url").strip()
    label = (data.get("label") or "").strip()
    entry = {"type": item_type, "value": value, "label": label}
    result, status = _play_mapping(entry, fallback_label=label or value)
    return jsonify(result), status


@app.route("/api/history/clear", methods=["POST"])
def api_history_clear():
    play_history.clear_history()
    return jsonify({"ok": True})


# ── Artwork (Cover-Proxy zum LMS) ────────────────────────────────────────────

@app.route("/api/artwork/current")
def api_artwork_current():
    """Cover of the currently playing track, proxied from LMS."""
    import requests as _req
    url = lms_client.get_current_artwork_url()
    if not url:
        return "", 404
    try:
        r = _req.get(url, timeout=5)
        if r.status_code != 200 or not r.content:
            return "", 404
        resp = app.response_class(
            r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except Exception:
        return "", 404


# Only artwork paths may be proxied — no open proxy to arbitrary LMS URLs
_ARTWORK_PREFIXES = ("/music/", "/imageproxy/", "/html/", "/plugins/")


@app.route("/api/artwork/lms")
def api_artwork_lms():
    """Proxies an LMS-relative artwork path (e.g. /music/<id>/cover.jpg)."""
    import requests as _req
    path = (request.args.get("path") or "").strip()
    if not path.startswith(_ARTWORK_PREFIXES) or ".." in path:
        return "", 400
    try:
        r = _req.get(lms_client.lms_base_url() + path, timeout=5)
        if r.status_code != 200 or not r.content:
            return "", 404
        resp = app.response_class(
            r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except Exception:
        return "", 404


# value -> artwork url ("" = resolved, none found). Bounded and lock-protected:
# the key comes from the request, so an unbounded dict would be a remote
# memory leak, and Flask serves requests from several threads.
_resolve_cache = OrderedDict()
_resolve_cache_lock = threading.Lock()
_RESOLVE_CACHE_MAX = 512


def _tunein_logo(link: str) -> str:
    """Station logo for a TuneIn link (opml.radiotime.com/Tune.ashx?id=s…)
    via the public Describe endpoint (no API key)."""
    import re as _re
    import requests as _req
    m = _re.search(r"[?&]id=([sp]\d+)", link)
    if not m:
        return ""
    try:
        r = _req.get("http://opml.radiotime.com/Describe.ashx",
                     params={"id": m.group(1), "render": "json"}, timeout=6)
        if r.status_code == 200:
            body = r.json().get("body", [])
            logo = (body[0].get("logo") or "").strip() if body else ""
            if logo.startswith(("http://", "https://")):
                return logo
    except Exception:
        pass
    return ""


def _radio_browser_favicon(stream_url: str) -> str:
    """Station logo for a radio stream URL from the community Radio Browser
    directory (no API key). Mirrors are tried in order; an empty match list
    from a reachable mirror is authoritative (data is replicated)."""
    import requests as _req
    for host in ("de1.api.radio-browser.info", "fi1.api.radio-browser.info"):
        try:
            r = _req.get(f"https://{host}/json/stations/byurl",
                         params={"url": stream_url},
                         headers={"User-Agent": "kid2tune"}, timeout=6)
            if r.status_code != 200:
                continue
            for st in r.json():
                fav = (st.get("favicon") or "").strip()
                if fav.startswith(("http://", "https://")):
                    return fav
            return ""
        except Exception:
            continue
    return ""


@app.route("/api/artwork/resolve")
def api_artwork_resolve():
    """Resolves artwork for a mapping value (for card printing etc.).
    Spotify links via public oEmbed (no API key), LMS library items
    (album/playlist/track/local) via the LMS database, radio streams via
    the Radio Browser directory, otherwise the artwork stored in the play
    history for the same value."""
    import re as _re
    value = (request.args.get("value") or "").strip()
    itype = (request.args.get("type") or "url").strip()
    if not value or len(value) > 512:
        return jsonify({"artwork": ""})
    cache_key = f"{itype}:{value}"
    with _resolve_cache_lock:
        if cache_key in _resolve_cache:
            _resolve_cache.move_to_end(cache_key)
            return jsonify({"artwork": _resolve_cache[cache_key]})

    artwork = ""
    # 1. Spotify: spotify:album:ID / open.spotify.com links -> oEmbed thumbnail
    url = None
    m = _re.match(r"spotify:(track|album|playlist|artist|show|episode):([A-Za-z0-9]+)", value)
    if m:
        url = f"https://open.spotify.com/{m.group(1)}/{m.group(2)}"
    elif "open.spotify.com/" in value:
        url = value
    if url:
        try:
            import requests as _req
            r = _req.get("https://open.spotify.com/oembed",
                         params={"url": url}, timeout=6)
            if r.status_code == 200:
                artwork = (r.json().get("thumbnail_url") or "").strip()
        except Exception:
            pass

    # URLs are URLs regardless of the stored type (play_item coerces the
    # same way), so a radio card mislabeled as e.g. 'track' still resolves.
    is_url = value.startswith(("http://", "https://"))

    # 2. Radio: TuneIn links via Describe, other streams via Radio Browser
    if not artwork and not url and is_url:
        if "radiotime.com" in value or "tunein.com" in value:
            artwork = _tunein_logo(value)
        else:
            artwork = _radio_browser_favicon(value)

    # 3. LMS library items (album/playlist/track ids, local files)
    if not artwork and not is_url and itype in ("album", "playlist", "track"):
        artwork = lms_client.get_item_artwork(itype, value)
    if not artwork and itype == "local":
        try:
            local_path = sync_manager.safe_music_path(value)
            if local_path:
                artwork = lms_client.get_item_artwork("url", f"file://{local_path}")
        except Exception:
            pass

    # 4. Fallback: artwork captured in the play history
    if not artwork:
        try:
            for e in play_history.get_history():
                if e.get("value") == value and e.get("artwork"):
                    artwork = e["artwork"]
                    break
        except Exception:
            pass

    with _resolve_cache_lock:
        _resolve_cache[cache_key] = artwork
        while len(_resolve_cache) > _RESOLVE_CACHE_MAX:
            _resolve_cache.popitem(last=False)
    return jsonify({"artwork": artwork})


# ── Card printing ────────────────────────────────────────────────────────────

@app.route("/cards")
def cards_page():
    """Printable card labels (credit-card size) for all mappings."""
    cfg = load_config()
    mappings = cfg.get("rfid_mappings", {})
    pending = cfg.get("pending_mappings", [])
    return render_template("cards.html", mappings=mappings, pending=pending)


# ── LMS Server Restart ──────────────────────────────────────────────────────

def _restart_lms_service():
    """Restart the LMS systemd service. Tries lyrionmusicserver, then logitechmediaserver."""
    import subprocess
    last_err = ""
    for service in ("lyrionmusicserver", "logitechmediaserver"):
        try:
            r = subprocess.run(["systemctl", "restart", service],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                logging.getLogger("WEB").info(f"{service} restart triggered.")
                return True, service
            last_err = r.stderr.strip()
        except Exception as e:
            last_err = str(e)
    return False, last_err


@app.route("/api/lms/restart", methods=["POST"])
def api_lms_restart():
    """Restarts the LMS server. Pending plugin updates are installed on startup."""
    ok, info = _restart_lms_service()
    if ok:
        return jsonify({"ok": True, "message": i18n.t("lms.restart_started")})
    return jsonify({"ok": False, "message": info or "LMS service not found"}), 500


@app.route("/api/player/restart", methods=["POST"])
def api_player_restart():
    """Restart the local Squeezelite player service."""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "restart", "squeezelite"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        log.error(f"Could not restart Squeezelite: {e}")
        return jsonify({"ok": False, "message": i18n.t("settings.player_restart_failed")}), 500

    if result.returncode != 0:
        log.error(f"Squeezelite restart failed: {result.stderr.strip()}")
        return jsonify({"ok": False, "message": i18n.t("settings.player_restart_failed")}), 500
    return jsonify({"ok": True, "message": i18n.t("settings.player_restart_done")})


# ── LMS Plugin Updates (one-click) ──────────────────────────────────────────

@app.route("/api/lms/plugins/check")
def api_lms_plugins_check():
    """Returns the list of plugin updates currently available in LMS."""
    try:
        html = lms_plugins.fetch_page(timeout=10)
    except Exception as e:
        return jsonify({"ok": False, "updates": [], "count": 0, "message": str(e)}), 200
    updates, _action, _installed, _rand = lms_plugins.parse(html)
    return jsonify({"ok": True, "count": len(updates), "updates": updates})


@app.route("/api/lms/plugins/update", methods=["POST"])
def api_lms_plugins_update():
    """One-click plugin update: select all available updates, submit form, restart LMS."""
    ok, updates, info = lms_plugins.install_all_available(timeout=30)
    if not ok:
        return jsonify({"ok": False, "message": info}), 500
    if not updates:
        return jsonify({"ok": True, "count": 0,
                        "message": i18n.t("lms.plugins_none")})

    # Restart LMS in background so the HTTP response is sent first.
    def _delayed_restart():
        import time as _t
        _t.sleep(2)
        _restart_lms_service()
    threading.Thread(target=_delayed_restart, daemon=True).start()

    names = [u["label"] for u in updates]
    logging.getLogger("WEB").info(
        f"LMS plugin update: {len(updates)} plugin(s) marked, LMS restarting: {names}")
    return jsonify({
        "ok": True,
        "count": len(updates),
        "plugins": names,
        "message": i18n.t("lms.plugins_updating",
                          count=len(updates), names=", ".join(names)),
    })


# ── Boot Timing ──────────────────────────────────────────────────────────────

@app.route("/api/boot-timing")
def api_boot_timing():
    """Returns the last boot timing measurement (or {} if not yet recorded)."""
    path = "/var/lib/lms-controller/boot_timing.json"
    if not os.path.exists(path):
        return jsonify({})
    try:
        with open(path) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)})


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
    data = request.get_json(silent=True) or {}
    try:
        hold_time = max(2, min(15, int(data.get("hold_time", 5))))
        confirm_timeout = max(5, min(60, int(data.get("confirm_timeout", 15))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": i18n.t("settings.invalid_values")}), 400
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
    if _adult_locked():
        return render_template("pin.html")
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
    import security_manager
    return render_template("settings.html",
                           hostname=socket.gethostname(),
                           version=version,
                           settings=settings,
                           security_enabled=security_manager.is_enabled(),
                           languages=i18n.available_languages())


@app.route("/api/settings", methods=["POST"])
@require_adult
def api_settings():
    data = request.get_json(silent=True) or {}
    try:
        # Converted before the config lock is taken: a TypeError inside the
        # updater would abort the write half-way.
        values = {
            "auto_standby_minutes": max(0, min(480, int(data.get("auto_standby_minutes", 30)))),
            "display_off_minutes": max(1, min(480, int(data.get("display_off_minutes", 30)))),
            "hold_time": max(2, min(15, int(data.get("hold_time", 5)))),
            "confirm_timeout": max(5, min(60, int(data.get("confirm_timeout", 15)))),
        }
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": i18n.t("settings.invalid_values")}), 400

    def _update(cfg):
        cfg["auto_standby_minutes"] = values["auto_standby_minutes"]
        cfg["display_off_minutes"] = values["display_off_minutes"]
        cfg["shutdown"] = {
            "hold_time": values["hold_time"],
            "confirm_timeout": values["confirm_timeout"],
        }
    config_manager.update_config(_update)
    return jsonify({"ok": True})


@app.route("/api/hostname", methods=["POST"])
@require_adult
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
                                    lms_client.play_item(entry.get("type", "url"), entry.get("value", ""), label=entry.get('label', uid))
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
    data = request.get_json(silent=True) or {}
    alarms = data.get("alarms", [])
    if not isinstance(alarms, list) or len(alarms) > 50:
        return jsonify({"ok": False, "message": "Invalid alarm list."}), 400

    # Validated here because _alarm_check_loop runs on this data every minute;
    # a wrong type would kill that thread and silently disable all alarms.
    clean = []
    for a in alarms:
        if not isinstance(a, dict):
            return jsonify({"ok": False, "message": "Invalid alarm."}), 400
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(a.get("time", ""))):
            return jsonify({"ok": False, "message": "Invalid time (HH:MM)."}), 400
        days = a.get("days", [1, 2, 3, 4, 5, 6, 7])
        if not isinstance(days, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in days):
            return jsonify({"ok": False, "message": "Invalid weekdays."}), 400
        try:
            volume = max(0, min(100, int(a.get("volume", 30))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Invalid volume."}), 400
        clean.append({
            "time": str(a.get("time")),
            "days": days,
            "volume": volume,
            "enabled": bool(a.get("enabled", True)),
            "rfid_uid": str(a.get("rfid_uid", "")),
        })

    def _update(cfg):
        cfg["alarms"] = clean
    config_manager.update_config(_update)
    return jsonify({"ok": True})


STANDBY_PAUSE_FILE = "/tmp/lms_standby_pause_threads"


def _wifi_daemon_loop():
    """WiFi manager daemon loop (runs as thread in lms-web)."""
    time.sleep(15)  # Wait until wpa_supplicant is ready
    while True:
        # Everything inside the try: an exception while reading the config
        # would kill the thread, and WiFi reconnect/AP fallback would stay
        # dead until the next reboot.
        interval = 30
        try:
            cfg = load_config()
            interval = int(cfg.get("wifi", {}).get("check_interval", 30))
            if os.path.exists(STANDBY_PAUSE_FILE):
                time.sleep(5)
                continue
            wifi_manager.daemon_tick()
        except Exception as e:
            logging.getLogger("WEB").error(f"WiFi thread error: {e}")
        time.sleep(max(5, interval))


def _bluetooth_daemon_loop():
    """Bluetooth manager daemon loop (runs as thread in lms-web)."""
    try:
        bluetooth_manager.ensure_adapter_powered()
    except Exception as e:
        logging.getLogger("WEB").error(f"Bluetooth init error: {e}")
    while True:
        interval = 15
        try:
            cfg = load_config()
            interval = int(cfg.get("bluetooth", {}).get("check_interval", 15))
            if os.path.exists(STANDBY_PAUSE_FILE):
                time.sleep(5)
                continue
            bluetooth_manager.daemon_tick()
        except Exception as e:
            logging.getLogger("WEB").error(f"Bluetooth thread error: {e}")
        time.sleep(max(5, interval))


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
