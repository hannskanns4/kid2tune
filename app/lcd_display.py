"""
lcd_display.py – I2C LCD 20x4 display daemon
Shows: Line1=Date+Time, Line2=Track (or IP when idle), Line3=Artist, Line4=Elapsed/Dur+Status
Backlight can be controlled via /tmp/lcd_backlight (0/1).
"""
import json
import os
import sys
import time
import socket
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [LCD] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, DIR)
import config_manager
import lms_client


def load_config() -> dict:
    return config_manager.read_config()


def init_lcd(cfg: dict):
    """Initialize LCD, tries 0x27 then 0x3F on failure."""
    from RPLCD.i2c import CharLCD
    lcd_cfg = cfg.get("lcd", {})
    cols    = lcd_cfg.get("cols", 20)
    rows    = lcd_cfg.get("rows", 4)
    addr_str = lcd_cfg.get("i2c_address", "0x27")

    addresses = [int(addr_str, 16)]
    if 0x27 not in addresses:
        addresses.append(0x27)
    if 0x3F not in addresses:
        addresses.append(0x3F)

    for addr in addresses:
        try:
            lcd = CharLCD("PCF8574", addr, cols=cols, rows=rows,
                          dotsize=8, charmap="A02", auto_linebreaks=False)
            log.info(f"LCD initialized at address {hex(addr)} ({cols}x{rows}).")
            # Persist address in config.json (atomically)
            found_addr = hex(addr)
            def _update_lcd_addr(c):
                c.setdefault("lcd", {})["i2c_address"] = found_addr
            config_manager.update_config(_update_lcd_addr)
            # Load German umlauts as custom characters
            _load_umlaut_chars(lcd)
            return lcd, cols
        except Exception as e:
            log.warning(f"LCD not reachable at {hex(addr)}: {e}")

    log.error("LCD could not be initialized on any known I2C address.")
    return None, cols


# ── German Umlauts (Custom Characters) ─────────────────────────────────────
# HD44780 supports max 8 custom chars (slot 0-7), 5x8 pixels

_CUSTOM_CHARS = {
    0: [0b01010, 0b00000, 0b01110, 0b00001, 0b01111, 0b10001, 0b01111, 0b00000],  # ä
    1: [0b01010, 0b00000, 0b01110, 0b10001, 0b10001, 0b10001, 0b01110, 0b00000],  # ö
    2: [0b01010, 0b00000, 0b10001, 0b10001, 0b10001, 0b10011, 0b01101, 0b00000],  # ü
    3: [0b01010, 0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b00000],  # Ä
    4: [0b01010, 0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110, 0b00000],  # Ö
    5: [0b01010, 0b00000, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110, 0b00000],  # Ü
    6: [0b01000, 0b01100, 0b01110, 0b01111, 0b01110, 0b01100, 0b01000, 0b00000],  # ▶ Play
    7: [0b11011, 0b11011, 0b11011, 0b11011, 0b11011, 0b11011, 0b11011, 0b00000],  # ⏸ Pause
}

CHAR_PLAY  = chr(6)
CHAR_PAUSE = chr(7)

_UMLAUT_MAP = {
    'ä': chr(0), 'Ä': chr(3),
    'ö': chr(1), 'Ö': chr(4),
    'ü': chr(2), 'Ü': chr(5),
    'ß': 'ss',
}


def _load_umlaut_chars(lcd):
    """Loads umlauts + Play/Pause as custom characters into LCD memory."""
    for slot, bitmap in _CUSTOM_CHARS.items():
        lcd.create_char(slot, bitmap)
    log.info("Custom characters loaded (ä ö ü Ä Ö Ü ▶ ⏸).")


def umlaut(text: str) -> str:
    """Replaces German umlauts with custom character codes."""
    for char, replacement in _UMLAUT_MAP.items():
        text = text.replace(char, replacement)
    return text


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def truncate(text: str, width: int) -> str:
    text = text or ""
    if len(text) <= width:
        return text.ljust(width)
    return text[:width - 1] + "~"


class Scroller:
    """Scrolls long text slowly across the LCD."""
    def __init__(self, width: int, pause: int = 3):
        self.width = width
        self.pause = pause  # Seconds to pause at start/end
        self._text = ""
        self._offset = 0
        self._wait = 0

    def set_text(self, text: str):
        text = text or ""
        if text != self._text:
            self._text = text
            self._offset = 0
            self._wait = self.pause

    def get_line(self) -> str:
        if len(self._text) <= self.width:
            return self._text.ljust(self.width)
        # Pause at start/end
        if self._wait > 0:
            self._wait -= 1
            return self._text[self._offset:self._offset + self.width].ljust(self.width)
        # Scroll
        max_offset = len(self._text) - self.width
        self._offset += 1
        if self._offset > max_offset:
            self._offset = 0
            self._wait = self.pause
        return self._text[self._offset:self._offset + self.width].ljust(self.width)


BACKLIGHT_FILE = "/tmp/lcd_backlight"
MULTIROOM_STATE = "/tmp/multiroom_active"
SHUTDOWN_PENDING_FILE = "/tmp/lms_shutdown_pending"
SHUTDOWN_CONFIRM_FILE = "/tmp/lms_shutdown_confirm"
STANDBY_PENDING_FILE = "/tmp/lms_standby_pending"
STANDBY_CONFIRM_FILE = "/tmp/lms_standby_confirm"
IDLE_STANDBY_SECONDS = 30 * 60  # Default, overridden from config


# ── System Status Cache ────────────────────────────────────────────────────
_status_cache = ""
_status_cache_time = 0
STATUS_CACHE_TTL = 10  # seconds


def get_system_status(cols: int) -> str:
    """Checks LMS, Spotify, Buttons, RFID, WiFi and returns a status line.
    Result is cached for 10s to reduce load."""
    import subprocess as _sp
    global _status_cache, _status_cache_time

    now = time.time()
    if _status_cache and (now - _status_cache_time) < STATUS_CACHE_TTL:
        return _status_cache

    parts = []

    # LMS Server
    try:
        lms_ok = lms_client.is_server_reachable()
        parts.append("L:" + ("OK" if lms_ok else "--"))
    except Exception:
        parts.append("L:--")

    # Spotify Plugin
    try:
        sp_ok = lms_client.is_spotify_available()
        parts.append("S:" + ("OK" if sp_ok else "--"))
    except Exception:
        parts.append("S:--")

    # Button service (lms-hardware now includes buttons)
    try:
        r = _sp.run(["systemctl", "is-active", "lms-hardware"],
                     capture_output=True, text=True, timeout=3)
        parts.append("B:" + ("OK" if r.stdout.strip() == "active" else "--"))
    except Exception:
        parts.append("B:--")

    # RFID service
    try:
        r = _sp.run(["systemctl", "is-active", "lms-rfid"],
                     capture_output=True, text=True, timeout=3)
        parts.append("R:" + ("OK" if r.stdout.strip() == "active" else "--"))
    except Exception:
        parts.append("R:--")

    # WiFi (IP available?)
    ip = get_ip()
    parts.append("W:" + ("OK" if ip != "No IP" else "--"))

    line = " ".join(parts)
    _status_cache = line[:cols].ljust(cols)
    _status_cache_time = now
    return _status_cache


_internet_cache = False
_internet_cache_time = 0


def _has_internet() -> bool:
    """Check if internet is reachable (cached for 30s)."""
    global _internet_cache, _internet_cache_time
    now = time.time()
    if (now - _internet_cache_time) < 30:
        return _internet_cache
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 53))
        s.close()
        _internet_cache = True
    except Exception:
        _internet_cache = False
    _internet_cache_time = now
    return _internet_cache


def get_ip() -> str:
    """Determines the current IP address (works even without internet)."""
    # Method 1: UDP socket (no internet needed, just routing table)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "0.0.0.0":
            return ip
    except Exception:
        pass
    # Method 2: Read interface IP directly
    try:
        import subprocess
        result = subprocess.run(
            ["ip", "-4", "addr", "show", "wlan0"],
            capture_output=True, text=True, timeout=3,
        )
        import re
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "No IP"


def _get_status_from_host(host: str, port: int) -> dict:
    """Fetches player status from a remote LMS (for slave mode).
    Searches for the player that is currently playing (isplaying=1)."""
    import requests as _req
    try:
        url = f"http://{host}:{port}/jsonrpc.js"
        # Get all players
        payload = {"id": 1, "method": "slim.request", "params": ["", ["players", 0, 20]]}
        r = _req.post(url, json=payload, timeout=2)
        players = r.json().get("result", {}).get("players_loop", [])
        if not players:
            return {"mode": "stop"}

        # Find the player that is currently playing (isplaying=1)
        pid = ""
        for p in players:
            if p.get("isplaying") and p.get("connected"):
                pid = p.get("playerid", "")
                break
        # Fallback: first connected player
        if not pid:
            for p in players:
                if p.get("connected"):
                    pid = p.get("playerid", "")
                    break
        if not pid:
            return {"mode": "stop"}

        # Query status of the playing player
        payload2 = {"id": 1, "method": "slim.request",
                    "params": [pid, ["status", "-", 1, "tags:adltuK"]]}
        r2 = _req.post(url, json=payload2, timeout=2)
        result = r2.json().get("result", {})
        pl = result.get("playlist_loop", [{}])
        track = pl[0] if pl else {}
        return {
            "mode": result.get("mode", "stop"),
            "title": track.get("title", ""),
            "artist": track.get("artist", ""),
            "album": track.get("album", ""),
            "duration": float(track.get("duration", 0) or 0),
            "elapsed": float(result.get("time", 0) or 0),
        }
    except Exception:
        return {"mode": "stop"}


def main():
    log.info("LCD display daemon started.")
    cfg  = load_config()
    lcd, cols = init_lcd(cfg)

    if lcd is None:
        log.error("LCD not available – daemon stopped.")
        sys.exit(1)

    rows = cfg.get("lcd", {}).get("rows", 4)
    lcd.clear()
    prev_lines = [""] * rows
    backlight_on = True
    manual_off = False
    scroll_title  = Scroller(cols)
    scroll_artist = Scroller(cols)
    idle_since = time.time()  # Timestamp since last stop
    standby_active = False

    shutdown_start = 0  # Timestamp when shutdown pending was detected
    shutdown_timeout = 15
    standby_start = 0
    standby_timeout = 15
    idle_standby_secs = IDLE_STANDBY_SECONDS  # Local copy, updated from config

    while True:
        try:
            # ── Shutdown Confirmation ────────────────────────────────────────
            if os.path.exists(SHUTDOWN_PENDING_FILE):
                if shutdown_start == 0:
                    # First detection
                    try:
                        shutdown_timeout = int(open(SHUTDOWN_PENDING_FILE).read().strip())
                    except (ValueError, OSError):
                        shutdown_timeout = 15
                    shutdown_start = time.time()
                    # Backlight on
                    lcd.backlight_enabled = True
                    backlight_on = True
                    log.info(f"Shutdown confirmation displayed ({shutdown_timeout}s timeout)")

                elapsed_sd = time.time() - shutdown_start
                remaining_sd = max(0, int(shutdown_timeout - elapsed_sd))

                if os.path.exists(SHUTDOWN_CONFIRM_FILE):
                    # Confirmed! Show message, then LCD off, then shutdown
                    lcd.clear()
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string("SHUTTING".center(cols))
                    lcd.cursor_pos = (2, 0)
                    lcd.write_string("DOWN...".center(cols))
                    time.sleep(3)
                    # LCD completely off
                    lcd.clear()
                    lcd.backlight_enabled = False
                    # Cleanup
                    for f in [SHUTDOWN_PENDING_FILE, SHUTDOWN_CONFIRM_FILE]:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                    log.info("LCD off – shutting down system.")
                    # Trigger shutdown
                    import subprocess
                    subprocess.run(["shutdown", "-h", "now"],
                                   capture_output=True, timeout=5)
                    time.sleep(10)
                    continue

                if remaining_sd <= 0:
                    # Timeout – cancel
                    log.info("Shutdown timeout – cancelled.")
                    try:
                        os.remove(SHUTDOWN_PENDING_FILE)
                    except OSError:
                        pass
                    shutdown_start = 0
                    prev_lines = [""] * rows
                    time.sleep(1)
                    continue

                # Display: SHUTDOWN?
                line0 = "SHUTDOWN?".center(cols)
                line1 = "".center(cols)
                line2 = f"Vol+ = Yes ({remaining_sd}s)".center(cols)
                line3 = "Wait...   Cancel".center(cols)
                lines = [line0, line1, line2, line3]
                for i, (new, old) in enumerate(zip(lines, prev_lines)):
                    if new != old:
                        lcd.cursor_pos = (i, 0)
                        lcd.write_string(new)
                prev_lines = lines
                time.sleep(1)
                continue
            elif shutdown_start != 0:
                # Pending file was removed externally
                shutdown_start = 0
                prev_lines = [""] * rows

            # ── Standby Confirmation ─────────────────────────────────────────
            if os.path.exists(STANDBY_PENDING_FILE):
                if standby_start == 0:
                    try:
                        standby_timeout = int(open(STANDBY_PENDING_FILE).read().strip())
                    except (ValueError, OSError):
                        standby_timeout = 15
                    standby_start = time.time()
                    lcd.backlight_enabled = True
                    backlight_on = True
                    log.info(f"Standby confirmation displayed ({standby_timeout}s timeout)")

                elapsed_sb = time.time() - standby_start
                remaining_sb = max(0, int(standby_timeout - elapsed_sb))

                if os.path.exists(STANDBY_CONFIRM_FILE):
                    # Confirmed! Show standby message, then LCD off
                    lcd.clear()
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string("DEEP STANDBY...".center(cols))
                    time.sleep(2)
                    lcd.clear()
                    lcd.backlight_enabled = False
                    backlight_on = False
                    for f in [STANDBY_PENDING_FILE, STANDBY_CONFIRM_FILE]:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                    standby_start = 0
                    prev_lines = [""] * rows
                    log.info("LCD off – waiting for standby manager.")
                    # LCD turns itself off, standby_manager is called by
                    # web_app or button_handler and stops lms-lcd
                    time.sleep(30)
                    continue

                if remaining_sb <= 0:
                    log.info("Standby timeout – cancelled.")
                    try:
                        os.remove(STANDBY_PENDING_FILE)
                    except OSError:
                        pass
                    standby_start = 0
                    prev_lines = [""] * rows
                    time.sleep(1)
                    continue

                # Display: STANDBY?
                line0 = "STANDBY?".center(cols)
                line1 = "".center(cols)
                line2 = f"Vol+ = Yes ({remaining_sb}s)".center(cols)
                line3 = "Wait...   Cancel".center(cols)
                lines = [line0, line1, line2, line3]
                for i, (new, old) in enumerate(zip(lines, prev_lines)):
                    if new != old:
                        lcd.cursor_pos = (i, 0)
                        lcd.write_string(new)
                prev_lines = lines
                time.sleep(1)
                continue
            elif standby_start != 0:
                standby_start = 0
                prev_lines = [""] * rows

            # Read display-off time from config (every 60 cycles = ~1 min)
            if int(time.time()) % 60 == 0:
                try:
                    _cfg = load_config()
                    idle_standby_secs = _cfg.get("display_off_minutes", 30) * 60
                except Exception:
                    pass

            # Backlight control via file (manual toggle takes priority)
            if os.path.exists(BACKLIGHT_FILE):
                try:
                    val = open(BACKLIGHT_FILE).read().strip()
                    want_on = val != "0"
                except Exception:
                    want_on = True
                if want_on != backlight_on:
                    lcd.backlight_enabled = want_on
                    backlight_on = want_on
                    if want_on:
                        manual_off = False
                        standby_active = False
                    else:
                        manual_off = True
                        standby_active = True
                    log.info(f"Backlight {'on' if want_on else 'off'} (manual).")

            now = datetime.now()
            date_str = now.strftime("%d.%m.%Y")
            time_str = now.strftime("%H:%M:%S")
            line0 = f"{date_str}  {time_str}"[:cols].ljust(cols)

            # Get status: in slave mode from master LMS
            if os.path.exists(MULTIROOM_STATE):
                try:
                    with open(MULTIROOM_STATE) as mf:
                        mr = json.load(mf)
                    if mr.get("role") == "slave" and mr.get("master_ip"):
                        status = _get_status_from_host(mr["master_ip"], cfg.get("lms_port", 9000))
                    else:
                        status = lms_client.get_status()
                except Exception:
                    status = lms_client.get_status()
            else:
                status = lms_client.get_status()

            title  = status.get("title",  "")
            artist = status.get("artist", "")
            mode   = status.get("mode",   "stop")
            dur    = status.get("duration", 0)
            ela    = status.get("elapsed",  0)

            # Idle standby: backlight off after 30 min without music
            # Only "play" resets the timer, "pause" counts as idle
            if mode == "play":
                idle_since = time.time()
                if standby_active and not manual_off:
                    # Music playing again -> backlight on (only for idle standby)
                    lcd.backlight_enabled = True
                    backlight_on = True
                    standby_active = False
                    with open(BACKLIGHT_FILE, "w") as bf:
                        bf.write("1")
                    log.info("Standby ended – backlight on.")
            elif not standby_active and (time.time() - idle_since) >= idle_standby_secs:
                lcd.backlight_enabled = False
                backlight_on = False
                standby_active = True
                with open(BACKLIGHT_FILE, "w") as bf:
                    bf.write("0")
                log.info("30 min idle – standby, backlight off.")

            # Line 2: IP address when no track is playing
            if mode == "stop" or not title:
                scroll_title.set_text("")
                scroll_artist.set_text("")
                # Hostname (with DNS suffix) and IP alternating every 5s
                show_hostname = (int(time.time()) // 5) % 2 == 0
                if show_hostname:
                    fqdn = socket.getfqdn()
                    if fqdn == socket.gethostname():
                        # No FQDN known, try domain from resolv.conf
                        try:
                            with open("/etc/resolv.conf") as rf:
                                for rline in rf:
                                    if rline.startswith(("search ", "domain ")):
                                        fqdn = socket.gethostname() + "." + rline.split()[1]
                                        break
                        except Exception:
                            pass
                    line1 = truncate(fqdn, cols)
                else:
                    line1 = truncate(get_ip(), cols)
                line2 = ("Online" if _has_internet() else "Local").center(cols)
                line3 = get_system_status(cols)
            else:
                scroll_title.set_text(title)
                scroll_artist.set_text(artist)
                line1 = scroll_title.get_line()
                line2 = scroll_artist.get_line()
                status_str = CHAR_PLAY + "PLAY" if mode == "play" else CHAR_PAUSE + "PAUS" if mode == "pause" else "[STOP]"
                # Show sleep timer countdown if active
                sleep_file = "/tmp/lms_sleep_timer"
                if os.path.exists(sleep_file):
                    try:
                        secs = int(open(sleep_file).read().strip())
                        status_str = f"[ZZZ {secs//60}:{secs%60:02d}]"
                    except Exception:
                        pass
                time_info  = f"{fmt_time(ela)}/{fmt_time(dur)}"
                line3 = f"{time_info}  {status_str}"[:cols].ljust(cols)

            lines = [line0, line1, line2, line3]

            # Only rewrite changed lines (avoids flickering)
            for i, (new, old) in enumerate(zip(lines, prev_lines)):
                if new != old:
                    lcd.cursor_pos = (i, 0)
                    lcd.write_string(umlaut(new))

            prev_lines = lines

        except KeyboardInterrupt:
            lcd.clear()
            log.info("LCD daemon stopped.")
            break
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(1)


if __name__ == "__main__":
    main()
