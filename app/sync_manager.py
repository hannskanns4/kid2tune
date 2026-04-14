"""
sync_manager.py – NAS sync for RFID mappings (v2 – versioned)

Concept:
- Each RFID mapping entry on the NAS has a timestamp (updated_at)
  and a box ID (updated_by).
- Deletions are stored as tombstones (deleted=true + timestamp),
  so they don't reappear when syncing with older boxes.
- Offline queue: If the NAS is unreachable during sync, changes
  are buffered in sync_pending.json and applied on the next
  successful sync.
- Conflict resolution: Last-Write-Wins – the newer timestamp always wins.
"""
import json
import os
import subprocess
import logging
from datetime import datetime, timezone
from typing import Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SYNC] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_PATH = os.path.join(DIR, "sync_pending.json")
SHARED_FILE = "rfid_sync_v2.json"
MUSIC_DIR = "/home/music"
NAS_MUSIC_DIR = "music"  # Subdirectory in the NAS mount


# ── Helper Functions ────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


import config_manager


def _load_config() -> dict:
    return config_manager.read_config()


def _save_config(cfg: dict):
    config_manager.write_config(cfg)


def _box_id() -> str:
    cfg = _load_config()
    return cfg.get("sync", {}).get("box_id", "unknown")


def get_sync_config() -> dict:
    cfg = _load_config()
    return cfg.get("sync", {
        "enabled": False,
        "nas_share": "",
        "username": "",
        "password": "",
        "mount_point": "/mnt/lms-sync",
        "box_id": "unknown",
    })


def save_sync_config(nas_share: str, username: str, password: str,
                      box_id: str, enabled: bool = True):
    cfg = _load_config()
    old_sync = cfg.get("sync", {})
    cfg["sync"] = {
        "enabled": enabled,
        "nas_share": nas_share,
        "username": username,
        "password": password,
        "mount_point": old_sync.get("mount_point", "/mnt/lms-sync"),
        "box_id": box_id or old_sync.get("box_id", "unknown"),
    }
    _save_config(cfg)


# ── NAS Mount ───────────────────────────────────────────────────────────────

def _mount_share() -> Tuple[bool, str]:
    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled") or not sync_cfg.get("nas_share"):
        return False, "Sync not enabled or no share configured."

    mount_point = sync_cfg.get("mount_point", "/mnt/lms-sync")
    nas_share = sync_cfg.get("nas_share", "")
    username = sync_cfg.get("username", "")
    password = sync_cfg.get("password", "")

    os.makedirs(mount_point, exist_ok=True)

    # Already mounted?
    try:
        result = subprocess.run(
            ["mountpoint", "-q", mount_point],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            return True, ""
    except Exception:
        pass

    # Mount SMB share (vers=3.0 -> 2.0 -> 1.0 fallback)
    base_opts = "iocharset=utf8,file_mode=0666,dir_mode=0777"
    if username:
        cred_opts = f"username={username},password={password}"
    else:
        cred_opts = "guest"

    # Try different SMB versions (newest first)
    for vers in ["3.0", "2.0", "1.0"]:
        mount_opts = f"{cred_opts},{base_opts},vers={vers}"

        try:
            result = subprocess.run(
                ["mount", "-t", "cifs", nas_share, mount_point, "-o", mount_opts],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                log.info(f"NAS mounted: {nas_share} -> {mount_point} (SMB {vers})")
                return True, ""
            last_err = result.stderr.strip() or "Mount failed."
            log.warning(f"Mount with SMB {vers} failed: {last_err}")
        except subprocess.TimeoutExpired:
            return False, "Timeout during mount – is the NAS reachable?"
        except Exception as e:
            last_err = str(e)

    log.error(f"Mount failed with all SMB versions: {last_err}")
    return False, last_err


# ── Shared File on NAS ──────────────────────────────────────────────────────
#
# Format of rfid_sync_v2.json:
# {
#   "format_version": 2,
#   "entries": {
#     "AABBCCDD": {
#       "label":      "Favorite Album",
#       "type":       "album",
#       "value":      "123",
#       "updated_at": "2026-03-22T14:30:00+00:00",
#       "updated_by": "livingroom-box",
#       "deleted":    false
#     }
#   }
# }

def _shared_path() -> str:
    mount_point = get_sync_config().get("mount_point", "/mnt/lms-sync")
    return os.path.join(mount_point, SHARED_FILE)


def _load_shared() -> dict:
    path = _shared_path()
    mount_point = get_sync_config().get("mount_point", "/mnt/lms-sync")

    # v2 file present?
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("format_version") == 2:
                return data
        except (json.JSONDecodeError, IOError):
            log.warning("Shared file corrupted – starting empty.")

    # v1 migration: old file (rfid_mappings_shared.json) present?
    v1_path = os.path.join(mount_point, "rfid_mappings_shared.json")
    if os.path.exists(v1_path):
        try:
            with open(v1_path) as f:
                v1_data = json.load(f)
            log.info("Migrating shared file from v1 -> v2")
            now = _now_iso()
            entries = {}
            for uid, entry in v1_data.items():
                if isinstance(entry, dict) and "value" in entry:
                    entries[uid] = {
                        "label": entry.get("label", ""),
                        "type": entry.get("type", ""),
                        "value": entry.get("value", ""),
                        "updated_at": now,
                        "updated_by": "v1-migration",
                        "deleted": False,
                    }
            migrated = {"format_version": 2, "entries": entries}
            log.info(f"v1 migration: {len(entries)} entries imported.")
            return migrated
        except (json.JSONDecodeError, IOError):
            log.warning("v1 file corrupted – starting empty.")

    return {"format_version": 2, "entries": {}}


def _save_shared(data: dict):
    path = _shared_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.replace(tmp, path)
    except OSError:
        # Fallback for CIFS mounts that don't support atomic rename
        import shutil
        shutil.move(tmp, path)


# ── Offline Queue (Pending Changes) ────────────────────────────────────────
#
# Format of sync_pending.json:
# [
#   {
#     "action":     "upsert" | "delete",
#     "uid":        "AABBCCDD",
#     "data":       { ... },      // only for upsert
#     "timestamp":  "2026-03-22T14:30:00+00:00",
#     "box_id":     "livingroom-box"
#   }
# ]

def _load_pending() -> list:
    if os.path.exists(PENDING_PATH):
        try:
            with open(PENDING_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return []


def _save_pending(pending: list):
    tmp = PENDING_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pending, f, indent=2)
    os.replace(tmp, PENDING_PATH)


def _clear_pending():
    if os.path.exists(PENDING_PATH):
        os.remove(PENDING_PATH)


def queue_change(action: str, uid: str, data: dict = None):
    """Writes a change to the offline queue."""
    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return
    pending = _load_pending()
    entry = {
        "action": action,
        "uid": uid,
        "timestamp": _now_iso(),
        "box_id": _box_id(),
    }
    if action == "upsert" and data:
        entry["data"] = data
    # Remove previous entries for the same UID (only the newest counts)
    pending = [p for p in pending if p.get("uid") != uid]
    pending.append(entry)
    _save_pending(pending)
    log.info(f"Offline queue: {action} {uid} ({len(pending)} pending)")


def get_pending_count() -> int:
    return len(_load_pending())


# ── Merge Logic (Last-Write-Wins) ──────────────────────────────────────────

def _entry_wins(new_entry: dict, existing_entry: dict) -> bool:
    """True if new_entry is strictly newer than existing_entry."""
    new_ts = new_entry.get("updated_at", "")
    old_ts = existing_entry.get("updated_at", "")
    return new_ts > old_ts


def _merge_into_shared(shared: dict, uid: str, entry: dict) -> bool:
    """Merges an entry into the shared dict. Returns True if changed."""
    entries = shared.setdefault("entries", {})
    existing = entries.get(uid)

    if existing is None or _entry_wins(entry, existing):
        entries[uid] = entry
        return True
    return False


# ── Sync Operations ─────────────────────────────────────────────────────────

def push_mappings() -> Tuple[bool, str]:
    """Pushes local mappings + pending queue to NAS."""
    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return False, "Sync not enabled."

    ok, err = _mount_share()
    if not ok:
        # NAS unreachable -> nothing to do, pending stays
        return False, f"NAS not reachable: {err}"

    try:
        cfg = _load_config()
        box_id = sync_cfg.get("box_id", "unknown")
        now = _now_iso()
        local_mappings = cfg.get("rfid_mappings", {})

        shared = _load_shared()
        changes = 0

        # 1. Process pending queue (has priority, as these are explicit actions)
        pending = _load_pending()
        for p in pending:
            uid = p["uid"]
            ts = p["timestamp"]
            p_box = p.get("box_id", box_id)

            if p["action"] == "delete":
                entry = {
                    "label": "",
                    "type": "",
                    "value": "",
                    "updated_at": ts,
                    "updated_by": p_box,
                    "deleted": True,
                }
            else:  # upsert
                data = p.get("data", {})
                entry = {
                    "label": data.get("label", ""),
                    "type": data.get("type", ""),
                    "value": data.get("value", ""),
                    "updated_at": ts,
                    "updated_by": p_box,
                    "deleted": False,
                }

            if _merge_into_shared(shared, uid, entry):
                changes += 1

        # 2. Push current local mappings (only if not already newer in shared)
        for uid, mapping in local_mappings.items():
            entry = {
                "label": mapping.get("label", ""),
                "type": mapping.get("type", ""),
                "value": mapping.get("value", ""),
                "updated_at": mapping.get("updated_at", now),
                "updated_by": mapping.get("updated_by", box_id),
                "deleted": False,
            }
            if _merge_into_shared(shared, uid, entry):
                changes += 1

        _save_shared(shared)
        _clear_pending()

        active = sum(1 for e in shared["entries"].values() if not e.get("deleted"))
        deleted = sum(1 for e in shared["entries"].values() if e.get("deleted"))
        msg = f"Push: {changes} changes -> NAS ({active} active, {deleted} deleted)"
        log.info(msg)
        return True, msg

    except Exception as e:
        log.error(f"Push failed: {e}")
        return False, str(e)


def pull_mappings() -> Tuple[bool, str]:
    """Loads shared mappings from NAS and merges locally (Last-Write-Wins)."""
    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return False, "Sync not enabled."

    ok, err = _mount_share()
    if not ok:
        return False, f"NAS not reachable: {err}"

    try:
        cfg = _load_config()
        local_mappings = cfg.get("rfid_mappings", {})
        box_id = sync_cfg.get("box_id", "unknown")
        now = _now_iso()

        shared = _load_shared()
        entries = shared.get("entries", {})

        added = 0
        updated = 0
        removed = 0

        for uid, shared_entry in entries.items():
            local_entry = local_mappings.get(uid)
            local_ts = ""
            if local_entry:
                local_ts = local_entry.get("updated_at", "")

            shared_ts = shared_entry.get("updated_at", "")

            if shared_entry.get("deleted"):
                # Tombstone: delete locally if shared is newer
                if uid in local_mappings and shared_ts > local_ts:
                    del local_mappings[uid]
                    removed += 1
            else:
                # Upsert: apply if shared is newer or not present locally
                new_data = {
                    "label": shared_entry.get("label", ""),
                    "type": shared_entry.get("type", ""),
                    "value": shared_entry.get("value", ""),
                    "updated_at": shared_ts,
                    "updated_by": shared_entry.get("updated_by", ""),
                }
                if uid not in local_mappings:
                    local_mappings[uid] = new_data
                    added += 1
                elif shared_ts > local_ts:
                    # Preserve local resume/position fields
                    old_local = local_mappings[uid]
                    new_data["resume"] = old_local.get("resume", False)
                    new_data["position"] = old_local.get("position", 0)
                    local_mappings[uid] = new_data
                    updated += 1

        cfg["rfid_mappings"] = local_mappings
        _save_config(cfg)

        msg = f"Pull: +{added} new, ~{updated} updated, -{removed} deleted ({len(local_mappings)} local)"
        log.info(msg)
        return True, msg

    except Exception as e:
        log.error(f"Pull failed: {e}")
        return False, str(e)


def full_sync() -> Tuple[bool, str]:
    """Bidirectional sync: push (incl. pending) -> pull -> music files."""
    # Push first (so pending queue + local changes reach NAS)
    ok1, msg1 = push_mappings()
    if not ok1:
        return False, msg1

    # Then pull (so changes from other boxes arrive locally)
    ok2, msg2 = pull_mappings()
    if not ok2:
        return False, msg2

    # Synchronize music files (download missing locally, upload new ones)
    ok3, msg3 = sync_all_music()
    music_info = f" | {msg3}" if msg3 else ""

    return True, f"Sync complete. {msg1} | {msg2}{music_info}"


def test_connection() -> Tuple[bool, str]:
    """Tests NAS connection with a write test."""
    ok, err = _mount_share()
    if not ok:
        return False, err
    try:
        test_path = os.path.join(
            get_sync_config().get("mount_point", "/mnt/lms-sync"),
            ".lms_sync_test"
        )
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        return True, "Connection successful – read and write possible."
    except Exception as e:
        return False, f"Connection established, but write test failed: {e}"


def get_sync_status() -> dict:
    """Status information for the web interface."""
    sync_cfg = get_sync_config()
    pending = get_pending_count()
    shared_count = 0
    shared_deleted = 0
    nas_reachable = False

    if sync_cfg.get("enabled"):
        ok, _ = _mount_share()
        if ok:
            nas_reachable = True
            shared = _load_shared()
            entries = shared.get("entries", {})
            shared_count = sum(1 for e in entries.values() if not e.get("deleted"))
            shared_deleted = sum(1 for e in entries.values() if e.get("deleted"))

    return {
        "enabled": sync_cfg.get("enabled", False),
        "box_id": sync_cfg.get("box_id", "unknown"),
        "nas_reachable": nas_reachable,
        "pending_changes": pending,
        "shared_active": shared_count,
        "shared_deleted": shared_deleted,
    }


# ── Music File Sync ─────────────────────────────────────────────────────────

def push_music_file(local_path: str) -> Tuple[bool, str]:
    """Copies a local music file to the NAS (if not already present)."""
    if not os.path.isfile(local_path):
        return False, f"File not found: {local_path}"

    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return False, "Sync not enabled."

    ok, err = _mount_share()
    if not ok:
        return False, f"NAS not reachable: {err}"

    mount_point = sync_cfg.get("mount_point", "/mnt/lms-sync")
    nas_music = os.path.join(mount_point, NAS_MUSIC_DIR)
    os.makedirs(nas_music, exist_ok=True)

    # Preserve relative path from MUSIC_DIR (including subdirectories)
    rel_path = os.path.relpath(local_path, MUSIC_DIR)
    dest = os.path.join(nas_music, rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    import shutil
    if os.path.exists(dest) and os.path.getsize(dest) == os.path.getsize(local_path):
        log.info(f"Music file already on NAS: {rel_path}")
        return True, rel_path

    try:
        shutil.copy2(local_path, dest)
        log.info(f"Music file copied to NAS: {rel_path}")
        return True, rel_path
    except Exception as e:
        log.error(f"Music file upload failed: {e}")
        return False, str(e)


def pull_music_file(rel_path: str) -> Tuple[bool, str]:
    """Downloads a music file from the NAS to the local MUSIC_DIR."""
    local_path = os.path.join(MUSIC_DIR, rel_path)
    if os.path.isfile(local_path):
        return True, local_path  # Already present locally

    sync_cfg = get_sync_config()
    if not sync_cfg.get("enabled"):
        return False, "Sync not enabled."

    ok, err = _mount_share()
    if not ok:
        return False, f"NAS not reachable: {err}"

    mount_point = sync_cfg.get("mount_point", "/mnt/lms-sync")
    nas_file = os.path.join(mount_point, NAS_MUSIC_DIR, rel_path)

    if not os.path.isfile(nas_file):
        return False, f"File not found on NAS: {rel_path}"

    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    import shutil
    try:
        shutil.copy2(nas_file, local_path)
        log.info(f"Music file downloaded from NAS: {rel_path} -> {local_path}")
        return True, local_path
    except Exception as e:
        log.error(f"Music file download failed: {e}")
        return False, str(e)


def sync_all_music() -> Tuple[bool, str]:
    """Synchronizes all music files referenced in RFID mappings."""
    cfg = _load_config()
    mappings = cfg.get("rfid_mappings", {})
    pulled = 0
    pushed = 0
    errors = 0

    for uid, entry in mappings.items():
        if entry.get("type") != "local" or entry.get("deleted"):
            continue
        rel_path = entry.get("value", "")
        if not rel_path:
            continue
        local_path = os.path.join(MUSIC_DIR, rel_path)

        if os.path.isfile(local_path):
            # File present locally -> push to NAS
            ok, _ = push_music_file(local_path)
            if ok:
                pushed += 1
            else:
                errors += 1
        else:
            # File missing locally -> pull from NAS
            ok, _ = pull_music_file(rel_path)
            if ok:
                pulled += 1
            else:
                errors += 1

    msg = f"Music sync: {pushed} uploaded, {pulled} downloaded, {errors} errors"
    log.info(msg)
    return errors == 0, msg
