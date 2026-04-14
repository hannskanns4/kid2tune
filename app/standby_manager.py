"""
standby_manager.py – Deep standby mode

Standby = all services stopped, filesystem read-only, LCD off.
Pulling the power plug is safe in standby mode.
Wake up via button, RFID, or web API.
"""
import os
import subprocess
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [STANDBY] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

STANDBY_FILE = "/tmp/lms_standby_active"
BACKLIGHT_FILE = "/tmp/lcd_backlight"

# Services that are stopped in standby (order: audio first, then rest)
# After merge: lms-hardware = LCD+Buttons, WiFi+BT run as threads in lms-web
STOP_SERVICES = [
    "squeezelite",
    "lms-rfid",
]

# Services that keep running in standby:
# - lms-web (for wake via web UI, also contains WiFi+BT threads)
# - lms-hardware (for wake via button press, also contains LCD)

# WiFi/BT threads in lms-web are paused via flag instead of stopped
STANDBY_PAUSE_FILE = "/tmp/lms_standby_pause_threads"


def is_standby() -> bool:
    return os.path.exists(STANDBY_FILE)


def enter_standby() -> tuple:
    """Puts the box into deep standby."""
    if is_standby():
        return True, "Already in standby."

    log.info("Entering deep standby...")

    # 1. Set standby flag IMMEDIATELY (in /tmp = tmpfs, always writable)
    #    Must happen BEFORE stopping services, because lms-lcd stops itself
    #    and the button handler needs the flag to detect wake.
    with open(STANDBY_FILE, "w") as f:
        f.write("1")

    # 2. Turn off LCD backlight
    try:
        with open(BACKLIGHT_FILE, "w") as f:
            f.write("0")
    except Exception:
        pass

    # 2b. Pause WiFi/BT threads in lms-web
    try:
        with open(STANDBY_PAUSE_FILE, "w") as f:
            f.write("1")
    except Exception:
        pass

    # 3. Stop services
    for svc in STOP_SERVICES:
        try:
            subprocess.run(["systemctl", "stop", svc],
                           capture_output=True, timeout=10)
            log.info(f"Service {svc} stopped.")
        except Exception as e:
            log.warning(f"Stopping service {svc} failed: {e}")

    # 4. Flush pending writes
    subprocess.run(["sync"], capture_output=True, timeout=5)
    time.sleep(1)

    # 5. Remount filesystem read-only
    try:
        result = subprocess.run(
            ["mount", "-o", "remount,ro", "/"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log.info("Filesystem mounted read-only.")
        else:
            log.warning(f"Read-only remount: {result.stderr.strip()}")
    except Exception as e:
        log.warning(f"Read-only remount failed: {e}")

    log.info("Deep standby active. Unplugging power is safe.")
    return True, "Deep standby active."


def wake_up() -> tuple:
    """Wakes the box from deep standby."""
    if not is_standby():
        return True, "Box is already active."

    log.info("Waking up from deep standby...")

    # 1. Remount filesystem read-write
    try:
        subprocess.run(
            ["mount", "-o", "remount,rw", "/"],
            capture_output=True, timeout=10,
        )
        log.info("Filesystem mounted read-write.")
    except Exception as e:
        log.warning(f"Read-write remount failed: {e}")

    # 2. Remove standby flag
    try:
        os.remove(STANDBY_FILE)
    except OSError:
        pass

    # 2b. Resume WiFi/BT threads in lms-web
    try:
        os.remove(STANDBY_PAUSE_FILE)
    except OSError:
        pass

    # 3. Start services (reverse order)
    for svc in reversed(STOP_SERVICES):
        try:
            subprocess.run(["systemctl", "start", svc],
                           capture_output=True, timeout=15)
            log.info(f"Service {svc} started.")
        except Exception as e:
            log.warning(f"Starting service {svc} failed: {e}")

    # 4. Turn on LCD backlight
    try:
        with open(BACKLIGHT_FILE, "w") as f:
            f.write("1")
    except Exception:
        pass

    log.info("Wake-up complete – box active.")
    return True, "Box awake."


def ensure_awake_on_boot():
    """Ensures the box is not stuck in standby on system start.
    Called by lms-web at startup."""
    if is_standby():
        log.info("Standby flag from previous run found – cleaning up...")
        try:
            os.remove(STANDBY_FILE)
        except OSError:
            pass
    # Also clean up pause flag
    try:
        os.remove(STANDBY_PAUSE_FILE)
    except OSError:
        pass
    # Disable WiFi power save
    try:
        subprocess.run(["iw", "wlan0", "set", "power_save", "off"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    log.info("Boot cleanup complete – box is online.")
