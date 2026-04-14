"""
lms_client.py – JSON-RPC wrapper for Lyrion Music Server
"""
import json
import os
import time
import socket
import logging
import requests

log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))

import config_manager


def _cfg():
    return config_manager.read_config()


def _rpc(params: list):
    cfg = _cfg()
    url = f"http://{cfg['lms_host']}:{cfg['lms_port']}/jsonrpc.js"
    payload = {"id": 1, "method": "slim.request", "params": params}
    try:
        r = requests.post(url, json=payload, timeout=3)
        return r.json().get("result", {})
    except Exception as e:
        log.warning(f"LMS RPC failed: {e}")
        return {}


# ── Player ID Cache ──────────────────────────────────────────────────────────
_cached_player_id = ""
_player_id_time = 0.0
_PLAYER_CACHE_TTL = 60  # seconds


def _get_player_id() -> str:
    """Determines the MAC address of the local player (with caching)."""
    global _cached_player_id, _player_id_time
    now = time.time()
    if _cached_player_id and (now - _player_id_time) < _PLAYER_CACHE_TTL:
        return _cached_player_id

    hostname = socket.gethostname()
    result = _rpc(["", ["players", 0, 50]])
    players = result.get("players_loop", [])
    pid = ""
    for p in players:
        if p.get("name", "").lower() == hostname.lower():
            pid = p.get("playerid", "")
            break
    if not pid and players:
        pid = players[0].get("playerid", "")
    if pid:
        _cached_player_id = pid
        _player_id_time = now
    return pid


def invalidate_player_cache():
    """Reset cache (e.g. after multiroom switch)."""
    global _cached_player_id, _player_id_time
    _cached_player_id = ""
    _player_id_time = 0.0


def _player_cmd(cmd: list):
    pid = _get_player_id()
    if pid:
        return _rpc([pid, cmd])
    return {}


# ── Playback Control ─────────────────────────────────────────────────────────

def play():
    _player_cmd(["play"])


def pause():
    _player_cmd(["pause"])


def toggle_pause():
    status = get_status()
    if status.get("mode") == "play":
        _player_cmd(["pause"])
    else:
        _player_cmd(["play"])


def next_track():
    _player_cmd(["playlist", "index", "+1"])


def prev_track():
    _player_cmd(["playlist", "index", "-1"])


def _get_max_volume() -> int:
    """Reads the volume limit from config.json, with optional day/night schedule."""
    try:
        cfg = _cfg()
        base_max = int(cfg.get("volume_max", 100))

        schedule = cfg.get("volume_schedule")
        if schedule:
            from datetime import datetime
            now = datetime.now().strftime("%H:%M")
            for period in schedule.values():
                start = period.get("from", "00:00")
                end = period.get("to", "23:59")
                limit = int(period.get("max", 100))
                if start <= end:
                    if start <= now < end:
                        return min(base_max, limit)
                else:  # Across midnight (e.g. 20:00–06:00)
                    if now >= start or now < end:
                        return min(base_max, limit)
        return base_max
    except Exception:
        return 100


def set_volume(level: int):
    """Set volume (0 to volume_max)."""
    vmax = _get_max_volume()
    level = max(0, min(vmax, int(level)))
    _player_cmd(["mixer", "volume", str(level)])


def get_volume() -> int:
    result = _player_cmd(["mixer", "volume", "?"])
    try:
        return int(float(result.get("_volume", 0)))
    except Exception:
        return 0


def volume_up(step: int = 5):
    current = get_volume()
    vmax = _get_max_volume()
    new_vol = min(current + step, vmax)
    _player_cmd(["mixer", "volume", str(new_vol)])


def volume_down(step: int = 5):
    _player_cmd(["mixer", "volume", f"-{step}"])


def get_status() -> dict:
    """Returns the current playback status."""
    result = _player_cmd(["status", "-", 1, "tags:adltuK"])
    pl = result.get("playlist_loop", [{}])
    track = pl[0] if pl else {}
    duration = track.get("duration", 0) or 0
    elapsed = result.get("time", 0) or 0
    return {
        "mode":     result.get("mode", "stop"),        # play / pause / stop
        "title":    track.get("title", ""),
        "artist":   track.get("artist", ""),
        "album":    track.get("album", ""),
        "duration": float(duration),
        "elapsed":  float(elapsed),
        "volume":   get_volume(),
    }


# ── Library Search ───────────────────────────────────────────────────────────

def search(query: str, search_type: str = "tracks") -> list:
    """
    Searches the LMS library.
    search_type: 'tracks', 'albums', 'playlists'
    The LMS command for songs is 'titles', not 'tracks'.
    """
    cmd_map = {
        "tracks":    "titles",
        "albums":    "albums",
        "playlists": "playlists",
    }
    key_map = {
        "tracks":    "titles_loop",
        "albums":    "albums_loop",
        "playlists": "playlists_loop",
    }
    lms_cmd = cmd_map.get(search_type, "titles")
    result = _rpc(["", [lms_cmd, 0, 50, "search:" + query]])
    items = result.get(key_map.get(search_type, "titles_loop"), [])
    return items


def play_item(item_type: str, item_id: str):
    """
    Plays an LMS item.
    item_type: 'track', 'album', 'playlist', 'url'
    item_id:   LMS ID or URL
    """
    pid = _get_player_id()
    if not pid:
        return
    # Spotify URIs and URLs are always treated as URL type
    if item_id.startswith(("spotify:", "http://", "https://")):
        item_type = "url"
    if item_type == "track":
        _rpc([pid, ["playlistcontrol", "cmd:load", f"track_id:{item_id}"]])
    elif item_type == "album":
        _rpc([pid, ["playlistcontrol", "cmd:load", f"album_id:{item_id}"]])
    elif item_type == "playlist":
        _rpc([pid, ["playlistcontrol", "cmd:load", f"playlist_id:{item_id}"]])
    elif item_type == "url":
        _rpc([pid, ["playlist", "play", item_id]])


# ── Multiroom Sync ───────────────────────────────────────────────────────────

def get_all_players() -> list:
    """Returns all players registered with LMS."""
    result = _rpc(["", ["players", 0, 50]])
    return result.get("players_loop", [])


def sync_to(slave_id: str, master_id: str):
    """Synchronizes a slave player with the master.
    LMS sync command: master adds slave to its group."""
    _rpc([master_id, ["sync", slave_id]])


def unsync(player_id: str):
    """Removes a player from the sync group."""
    _rpc([player_id, ["sync", "-"]])


def get_sync_groups() -> list:
    """Returns the current sync groups."""
    result = _rpc(["", ["syncgroups", "?"]])
    return result.get("syncgroups_loop", [])


def is_server_reachable() -> bool:
    """Checks whether the LMS server is reachable."""
    try:
        cfg = _cfg()
        host = cfg.get("lms_host", "localhost")
        port = cfg.get("lms_port", 9000)
        # Direct TCP check on IPv4 (avoids IPv6 timeout)
        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", int(port)))
        s.close()
        return True
    except Exception:
        return False


def is_spotify_available() -> bool:
    """Checks whether the Spotify plugin (Spotty) is active in LMS."""
    try:
        # Method 1: Query plugin list
        result = _rpc(["", ["can", "spotty", "items", "?"]])
        if result.get("_can", 0) == 1:
            return True
        # Method 2: Check Spotify Connect
        result2 = _rpc(["", ["can", "spotifyconnect", "items", "?"]])
        if result2.get("_can", 0) == 1:
            return True
        return False
    except Exception:
        return False
