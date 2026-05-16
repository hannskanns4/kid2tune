"""
play_history.py – Stores the last 10 unique playback items.

Used so the user can quickly replay recently played items without having to
copy the Spotify/URL link again. Deduplication is by `value` (track id,
spotify URI, URL, etc.). On duplicate, the existing entry is moved to the top.
"""
import json
import os
import fcntl
import tempfile
import threading
import time
import logging

log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(DIR, "play_history.json")
_LOCK_PATH = HISTORY_PATH + ".lock"
MAX_ENTRIES = 10


def _read() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        with open(HISTORY_PATH) as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except (OSError, json.JSONDecodeError):
        return []
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _write(entries: list):
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=DIR, suffix=".json.tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(entries, f, indent=2)
            os.replace(tmp_path, HISTORY_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def get_history() -> list:
    """Returns the play history (newest first)."""
    return _read()


def clear_history():
    """Removes all entries."""
    _write([])


def record_play(item_type: str, value: str, label: str = ""):
    """Records a play. Deduplicates by `value`, keeps max 10 entries.

    If `label` is empty or looks like a URL, a background thread tries to
    fetch the actual title/artist from LMS a few seconds after the call.
    """
    value = (value or "").strip()
    if not value:
        return
    label = (label or "").strip()

    entries = _read()
    # Remove existing entry with same value (move-to-top), keep its metadata
    prev_title = ""
    prev_artist = ""
    for e in entries:
        if e.get("value") == value:
            prev_title = e.get("title", "") or ""
            prev_artist = e.get("artist", "") or ""
            break
    entries = [e for e in entries if e.get("value") != value]
    entry = {
        "label":  label or prev_title or value,
        "type":   item_type,
        "value":  value,
        "title":  prev_title,
        "artist": prev_artist,
        "timestamp": time.time(),
    }
    entries.insert(0, entry)
    entries = entries[:MAX_ENTRIES]
    try:
        _write(entries)
    except Exception as e:
        log.warning(f"play_history write failed: {e}")

    needs_metadata = (not label) or label.startswith(("http", "spotify:"))
    if needs_metadata:
        threading.Thread(target=_fetch_metadata_later,
                         args=(value,), daemon=True).start()


def _fetch_metadata_later(value: str):
    """After a short delay, query LMS for current title/artist and update
    the matching history entry. Best-effort, silently fails."""
    try:
        time.sleep(3)
        import lms_client
        status = lms_client.get_status()
        title = (status.get("title") or "").strip()
        artist = (status.get("artist") or "").strip()
        if not title:
            return
        entries = _read()
        for e in entries:
            if e.get("value") == value:
                e["title"] = title
                e["artist"] = artist
                if not e.get("label") or e["label"] == value:
                    e["label"] = title
                break
        _write(entries)
    except Exception as e:
        log.debug(f"metadata fetch failed: {e}")
