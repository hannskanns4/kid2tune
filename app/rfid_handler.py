"""
rfid_handler.py – RC522 RFID reader daemon
Reads cards, looks up UID in config.json, controls LMS.
"""
import os
import sys
import time
import signal
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RFID] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
LAST_RFID_FILE = "/tmp/lms_last_rfid"
SLEEP_TIMER_FILE = "/tmp/lms_sleep_timer"  # Remaining seconds for LCD

sys.path.insert(0, DIR)
import config_manager
import lms_client
import sync_manager
import bluetooth_manager
import multiroom_manager

try:
    import RPi.GPIO as GPIO
    from mfrc522 import SimpleMFRC522
    READER = SimpleMFRC522()
    log.info("RC522 RFID reader initialized.")
except Exception as e:
    log.error(f"RC522 could not be initialized: {e}")
    log.error("Please enable SPI and install mfrc522.")
    sys.exit(1)


# Set by the SIGTERM/SIGINT handler so the main loop can exit cleanly
# (running GPIO.cleanup() and saving the resume position on `systemctl stop`).
_stop = threading.Event()


def load_config() -> dict:
    return config_manager.read_config()


def uid_to_hex(uid: int) -> str:
    return format(uid, "08X")


# ── Sleep Timer ──────────────────────────────────────────────────────────────
_sleep_timer = None  # Reference to active timer thread
_sleep_cancel = threading.Event()  # Cancel event of the *current* timer thread
_sleep_lock = threading.Lock()  # Protects _sleep_timer/_sleep_cancel access


def _write_sleep_file(seconds: int):
    """Writes sleep timer file atomically."""
    tmp = SLEEP_TIMER_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(seconds))
    os.replace(tmp, SLEEP_TIMER_FILE)


def _sleep_timer_thread(minutes: int, cancel: threading.Event):
    """Waits X minutes, then fades volume to 0 over 60s, stops playback.

    `cancel` belongs to this thread alone – a timer that was replaced keeps its
    own event, so a slow predecessor can never be revived by the next start.
    """
    total = minutes * 60
    fade_duration = 60  # seconds for fade-out
    wait_seconds = max(0, total - fade_duration)

    log.info(f"Sleep timer: {minutes} min ({wait_seconds}s wait + {fade_duration}s fade)")

    # Write countdown for LCD
    remaining = total
    while remaining > fade_duration and not cancel.is_set():
        _write_sleep_file(int(remaining))
        cancel.wait(1)
        remaining -= 1

    if cancel.is_set():
        _cleanup_sleep()
        return

    # Fade-out – read volume at the start of fade
    try:
        start_vol = lms_client.get_volume()
    except Exception:
        start_vol = 50
    steps = fade_duration
    for i in range(steps):
        if cancel.is_set():
            # On cancel, keep current volume (don't restore old one)
            _cleanup_sleep()
            return
        vol = int(start_vol * (1 - (i + 1) / steps))
        lms_client._player_cmd(["mixer", "volume", str(max(0, vol))])
        remaining -= 1
        _write_sleep_file(max(0, int(remaining)))
        cancel.wait(1)

    # Stop and restore volume
    lms_client._player_cmd(["stop"])
    time.sleep(1)
    lms_client._player_cmd(["mixer", "volume", str(start_vol)])
    _cleanup_sleep()
    log.info("Sleep timer expired – playback stopped, volume restored.")


def _cleanup_sleep():
    try:
        os.remove(SLEEP_TIMER_FILE)
    except OSError:
        pass


def _cancel_locked():
    """Cancels a running timer. Caller must already hold _sleep_lock."""
    global _sleep_timer
    if _sleep_timer and _sleep_timer.is_alive():
        _sleep_cancel.set()
        _sleep_timer.join(timeout=3)
        log.info("Sleep timer cancelled.")
    _sleep_timer = None
    _cleanup_sleep()


def start_sleep_timer(minutes: int):
    global _sleep_timer, _sleep_cancel
    with _sleep_lock:
        _cancel_locked()  # Cancel previous timer
        _sleep_cancel = threading.Event()  # fresh event, see _sleep_timer_thread
        _sleep_timer = threading.Thread(target=_sleep_timer_thread,
                                        args=(minutes, _sleep_cancel), daemon=True)
        _sleep_timer.start()
        log.info(f"Sleep timer started: {minutes} minutes")


def cancel_sleep_timer():
    with _sleep_lock:
        _cancel_locked()


def is_sleep_timer_active() -> bool:
    with _sleep_lock:
        return _sleep_timer is not None and _sleep_timer.is_alive()


# Only one NAS lookup at a time: on a hung CIFS mount every call blocks for
# minutes, and a child tapping an unknown card repeatedly would pile up threads.
_nas_lookup_busy = threading.Lock()


def _async_nas_lookup(uid_hex: str):
    """NAS sync in background – does not block the RFID reader."""
    if not _nas_lookup_busy.acquire(blocking=False):
        log.debug("NAS sync already running – skipping.")
        return
    try:
        ok, _ = sync_manager.pull_mappings()
        if ok:
            cfg = load_config()
            if uid_hex in cfg.get("rfid_mappings", {}):
                log.info(f"Card {uid_hex} found via NAS sync – please scan again.")
    except Exception as ex:
        log.warning(f"NAS sync failed: {ex}")
    finally:
        _nas_lookup_busy.release()


def _run_bg(fn, *fn_args):
    """Runs a slow action off the scan loop, so cards stay responsive."""
    def _wrapped():
        try:
            fn(*fn_args)
        except Exception as ex:
            log.warning(f"Background action {getattr(fn, '__name__', fn)} failed: {ex}")
    threading.Thread(target=_wrapped, daemon=True).start()


def handle_card(uid_hex: str):
    cfg = load_config()
    mappings = cfg.get("rfid_mappings", {})

    # If not found locally, start NAS sync in background
    if uid_hex not in mappings:
        sync_cfg = cfg.get("sync", {})
        if sync_cfg.get("enabled"):
            threading.Thread(target=_async_nas_lookup, args=(uid_hex,), daemon=True).start()

    if uid_hex in mappings:
        entry = mappings[uid_hex]
        item_type = entry.get("type", "url")
        item_id   = entry.get("value", "")
        label     = entry.get("label", uid_hex)
        resume    = entry.get("resume", False)
        position  = entry.get("position", 0)
        log.info(f"Card {uid_hex} -> '{label}' ({item_type}: {item_id})")
        try:
            if item_type == "bluetooth":
                # Switch audio to Bluetooth device
                ok, msg = bluetooth_manager.connect_device(item_id)
                if ok:
                    bluetooth_manager.switch_audio_to_bluetooth(item_id)
                    log.info(f"Bluetooth audio switched to {item_id}.")
                else:
                    log.warning(f"Bluetooth connection failed: {msg}")
            elif item_type == "local":
                # Local music file – pull from NAS if needed, then play
                local_path = sync_manager.safe_music_path(item_id)
                if local_path is None:
                    log.error(f"Rejected unsafe local path: {item_id}")
                elif not os.path.isfile(local_path):
                    log.info(f"File missing locally, attempting NAS download: {item_id}")
                    ok, result = sync_manager.pull_music_file(item_id)
                    if ok:
                        local_path = result
                    else:
                        log.warning(f"Download failed: {result}")
                if local_path and os.path.isfile(local_path):
                    lms_client.play_item("url", f"file://{local_path}", label=label)
                    # Push file to NAS in background (in case other boxes need it).
                    # Must not run inline: a stalled CIFS mount would block the
                    # scan loop for minutes.
                    _run_bg(sync_manager.push_music_file, local_path)
            elif item_type == "local_album":
                tracks = sync_manager.pull_music_album(item_id)
                for index, track_path in enumerate(tracks):
                    if not os.path.isfile(track_path):
                        rel_path = os.path.relpath(track_path, sync_manager.MUSIC_DIR)
                        ok, result = sync_manager.pull_music_file(rel_path)
                        if ok:
                            tracks[index] = result
                        else:
                            log.warning(f"Album track unavailable: {rel_path}: {result}")
                if tracks:
                    lms_client.play_local_album(tracks, item_id, label=label)
                    for track_path in tracks:
                        _run_bg(sync_manager.push_music_file, track_path)
                else:
                    log.error(f"Local album has no playable tracks: {item_id}")
                else:
                    log.error(f"Local file not found: {local_path}")
            elif item_type == "sleep":
                # Toggle sleep timer
                if is_sleep_timer_active():
                    cancel_sleep_timer()
                    log.info("Sleep timer cancelled.")
                else:
                    try:
                        minutes = int(item_id)
                    except (ValueError, TypeError):
                        minutes = 15
                    start_sleep_timer(minutes)
            elif item_type == "shutdown":
                import subprocess
                log.info("Shutdown card scanned – shutting down...")
                # LCD backlight off
                with open("/tmp/lcd_backlight", "w") as f:
                    f.write("0")
                time.sleep(1)
                subprocess.Popen(["shutdown", "-h", "now"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            elif item_type == "multiroom":
                # Toggle multiroom sync (depends on role). Runs in the
                # background: discovery + join take up to a minute, during
                # which no card could be scanned or removed.
                mr_status = multiroom_manager.get_status()
                if mr_status.get("active"):
                    if mr_status.get("role") == "master":
                        log.info("Multiroom active (master) – deactivating sync...")
                        _run_bg(multiroom_manager.deactivate_master)
                    else:
                        log.info("Multiroom active (slave) – leaving sync...")
                        _run_bg(multiroom_manager.leave_master)
                else:
                    log.info("Activating multiroom sync as master...")
                    _run_bg(multiroom_manager.activate_master)
            else:
                lms_client.play_item(item_type, item_id, label=label)
            # Resume: jump to saved position
            if resume and position > 0 and item_type not in ("bluetooth", "multiroom", "sleep"):
                # Wait until playback actually started. Hard deadline: each
                # get_status() costs two RPCs, so a hung LMS could otherwise
                # stall the scan loop for a minute.
                deadline = time.time() + 5
                while time.time() < deadline:
                    time.sleep(0.5)
                    st = lms_client.get_status()
                    if st.get("mode") == "play":
                        break
                lms_client._player_cmd(["time", str(position)])
                log.info(f"Resume at {position:.0f}s")
        except Exception as ex:
            log.error(f"Error: {ex}")
    else:
        log.info(f"Unknown card: {uid_hex} – waiting for web assignment.")
        # Save for web interface
        with open(LAST_RFID_FILE, "w") as f:
            f.write(uid_hex)


def _save_resume_position(uid):
    """Persists the current playback position for a resume-enabled card.

    Called both when a card is lifted off the reader and on daemon shutdown."""
    if not uid:
        return
    try:
        cfg = load_config()
        entry = cfg.get("rfid_mappings", {}).get(uid)
        if entry and entry.get("resume"):
            status = lms_client.get_status()
            elapsed = status.get("elapsed", 0)
            if elapsed > 0:
                def _save_position(c):
                    e = c.get("rfid_mappings", {}).get(uid)
                    if e:
                        e["position"] = elapsed
                config_manager.update_config(_save_position)
                log.info(f"Position saved: {elapsed:.0f}s for {uid}")
    except Exception as ex:
        log.warning(f"Saving position failed: {ex}")


def main():
    log.info("RFID handler started. Waiting for cards...")

    def _handle_stop(signum, frame):
        log.info("Stop signal received – shutting down RFID handler...")
        _stop.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    last_uid     = None
    miss_count   = 0       # Counts consecutive missed scans
    MISS_THRESHOLD = 3     # Card considered removed only after 3 missed scans
    SCAN_INTERVAL  = 0.5   # Faster scanning for better detection

    while not _stop.is_set():
        try:
            rdr = READER.READER
            (status, _) = rdr.MFRC522_Request(rdr.PICC_REQIDL)
            if status == rdr.MI_OK:
                (status, raw_uid) = rdr.MFRC522_Anticoll()
                if status == rdr.MI_OK:
                    uid_hex = uid_to_hex(READER.uid_to_num(raw_uid))
                    rdr.MFRC522_SelectTag(raw_uid)
                    rdr.MFRC522_StopCrypto1()

                    miss_count = 0  # Card detected -> reset
                    if uid_hex != last_uid:
                        # New card placed
                        last_uid = uid_hex
                        handle_card(uid_hex)
                else:
                    miss_count += 1
            else:
                miss_count += 1

            # Consider card removed only after multiple missed scans
            if last_uid and miss_count >= MISS_THRESHOLD:
                _save_resume_position(last_uid)
                log.info(f"Card {last_uid} removed.")
                last_uid = None
                miss_count = 0

            _stop.wait(SCAN_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Read error: {e}")
            _stop.wait(SCAN_INTERVAL)

    # Clean shutdown (e.g. `systemctl stop`): persist the position of the card
    # still on the reader and release the GPIO pins.
    _save_resume_position(last_uid)
    GPIO.cleanup()
    log.info("RFID handler stopped.")


if __name__ == "__main__":
    main()
