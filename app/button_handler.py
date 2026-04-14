"""
button_handler.py – GPIO button daemon
Reads 5 buttons (configurable pins), controls LMS.
"""
import json
import os
import sys
import time
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BTN] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, DIR)
import config_manager
import lms_client
import standby_manager


def load_config() -> dict:
    return config_manager.read_config()


BACKLIGHT_FILE = "/tmp/lcd_backlight"
SHUTDOWN_PENDING_FILE = "/tmp/lms_shutdown_pending"
SHUTDOWN_CONFIRM_FILE = "/tmp/lms_shutdown_confirm"
STANDBY_PENDING_FILE = "/tmp/lms_standby_pending"
STANDBY_CONFIRM_FILE = "/tmp/lms_standby_confirm"


def toggle_lcd_backlight():
    """Toggles LCD backlight."""
    try:
        current = open(BACKLIGHT_FILE).read().strip() if os.path.exists(BACKLIGHT_FILE) else "1"
        new_val = "0" if current == "1" else "1"
    except Exception:
        new_val = "0"
    with open(BACKLIGHT_FILE, "w") as f:
        f.write(new_val)
    log.info(f"LCD backlight -> {'on' if new_val == '1' else 'off'}")


QUEUE_FLAG = "/tmp/rfid_queue_mode"

# ── Standby Confirmation ────────────────────────────────────────────────────

def _is_standby_pending() -> bool:
    return os.path.exists(STANDBY_PENDING_FILE)


def _confirm_standby():
    """Confirms standby – writes confirm file, LCD reacts, then enters standby."""
    with open(STANDBY_CONFIRM_FILE, "w") as f:
        f.write("1")
    log.info("Standby confirmed via Vol+ button.")
    # LCD needs ~3s for display, then trigger standby
    import time as _t
    _t.sleep(4)
    standby_manager.enter_standby()


def _wake_from_standby():
    """Wakes the box from deep standby."""
    log.info("Button pressed – waking up from standby...")
    standby_manager.wake_up()
    log.info("Box awake.")


# ── Simple Actions (short press) ────────────────────────────────────────────
ACTIONS = {
    "vol_up":        lambda: lms_client.volume_up(5),
    "vol_down":      lambda: lms_client.volume_down(5),
    "next":          lambda: lms_client.next_track(),
    "prev":          lambda: lms_client.prev_track(),
    "pause":         lambda: lms_client.toggle_pause(),
    "lcd_backlight": lambda: toggle_lcd_backlight(),
}

# ── Long Press Actions (held >1s) ──────────────────────────────────────────
HOLD_ACTIONS = {
    "next":  lambda: (lms_client._player_cmd(["playlist", "index", "+10"]),
                      log.info("Long press Next -> +10 tracks")),
    "prev":  lambda: (lms_client._player_cmd(["playlist", "index", "0"]),
                      log.info("Long press Prev -> beginning of playlist")),
    "pause": lambda: (lms_client._player_cmd(["stop"]),
                      log.info("Long press Pause -> stop")),
}


def _activate_queue_mode():
    """Activates queue mode for 10 seconds."""
    with open(QUEUE_FLAG, "w") as f:
        f.write(str(time.time()))
    log.info("Queue mode activated (10s)")


def init_buttons():
    from gpiozero import Button

    cfg     = load_config()
    btn_cfg = cfg.get("buttons", {})
    buttons = []
    btn_objects = {}  # action -> Button for combo detection
    # Track whether a hold was triggered (per button) -> suppress short press
    held_flags = {}

    for action, pin in btn_cfg.items():
        if action not in ACTIONS:
            log.warning(f"Unknown action '{action}' – skipped.")
            continue
        try:
            has_hold = action in HOLD_ACTIONS
            btn = Button(pin, pull_up=True, bounce_time=0.05,
                         hold_time=1.0)

            held_flags[action] = False

            if has_hold:
                # For buttons with hold action:
                # when_held -> execute hold action + set flag
                # when_released -> short press only if hold was NOT triggered
                act = action  # Closure copy

                def _make_hold(a):
                    def _on_hold():
                        held_flags[a] = True
                        HOLD_ACTIONS[a]()
                    return _on_hold

                def _make_release(a):
                    def _on_release():
                        if held_flags[a]:
                            held_flags[a] = False
                        else:
                            ACTIONS[a]()
                    return _on_release

                btn.when_held = _make_hold(act)
                btn.when_released = _make_release(act)
            else:
                # Buttons without hold -> direct when_pressed
                btn.when_pressed = ACTIONS[action]

            buttons.append(btn)
            btn_objects[action] = btn
            log.info(f"Button '{action}' registered on GPIO {pin}.")
        except Exception as e:
            log.error(f"Error on GPIO {pin} ({action}): {e}")

    # ── Vol+/Vol- Combo -> Queue Mode ────────────────────────────────────────
    vol_up_btn = btn_objects.get("vol_up")
    vol_down_btn = btn_objects.get("vol_down")
    if vol_up_btn and vol_down_btn:
        orig_vol_up = ACTIONS["vol_up"]
        orig_vol_down = ACTIONS["vol_down"]

        def _vol_up_with_combo():
            time.sleep(0.1)
            if vol_down_btn.is_pressed:
                _activate_queue_mode()
            else:
                orig_vol_up()

        def _vol_down_with_combo():
            time.sleep(0.1)
            if vol_up_btn.is_pressed:
                _activate_queue_mode()
            else:
                orig_vol_down()

        vol_up_btn.when_pressed = _vol_up_with_combo
        vol_down_btn.when_pressed = _vol_down_with_combo
        log.info("Vol+/Vol- combo -> queue mode registered.")

    # ── 3-Button Standby Combo (vol_up + vol_down + pause) ──────────────────
    pause_btn = btn_objects.get("pause")
    shutdown_cfg = cfg.get("shutdown", {})
    hold_time = shutdown_cfg.get("hold_time", 5)

    if vol_up_btn and vol_down_btn and pause_btn:
        def _check_standby_combo():
            """Checks if all 3 buttons are held long enough."""
            import time as _t
            start = _t.time()
            while vol_up_btn.is_pressed and vol_down_btn.is_pressed and pause_btn.is_pressed:
                held = _t.time() - start
                if held >= hold_time:
                    confirm_timeout = shutdown_cfg.get("confirm_timeout", 15)
                    log.info(f"3-button combo detected ({hold_time}s) – waiting for confirmation ({confirm_timeout}s)")
                    with open(STANDBY_PENDING_FILE, "w") as f:
                        f.write(str(confirm_timeout))
                    return
                _t.sleep(0.1)

    # ── Central Standby Guard for ALL Buttons ────────────────────────────────
    # Wrap all when_pressed and when_released handlers with standby check
    for action, btn in btn_objects.items():
        # Wrap when_pressed
        original_pressed = btn.when_pressed
        if original_pressed:
            if action == "vol_up":
                # Vol-Up: Wake + Standby Confirm + 3-Button Combo + Normal
                def _make_vol_up_handler(orig):
                    def _handler():
                        if standby_manager.is_standby():
                            _wake_from_standby()
                            return
                        if _is_standby_pending():
                            _confirm_standby()
                            return
                        if vol_down_btn and pause_btn and vol_down_btn.is_pressed and pause_btn.is_pressed:
                            threading.Thread(target=_check_standby_combo, daemon=True).start()
                            return
                        orig()
                    return _handler
                btn.when_pressed = _make_vol_up_handler(original_pressed)
            else:
                # All others: wake from standby, otherwise normal
                def _make_wake_pressed(orig):
                    def _handler():
                        if standby_manager.is_standby():
                            _wake_from_standby()
                            return
                        orig()
                    return _handler
                btn.when_pressed = _make_wake_pressed(original_pressed)

        # Wrap when_released (if present)
        original_released = btn.when_released
        if original_released:
            def _make_wake_released(orig):
                def _handler():
                    if standby_manager.is_standby():
                        return  # Wake was already triggered by when_pressed
                    orig()
                return _handler
            btn.when_released = _make_wake_released(original_released)

    if vol_up_btn and vol_down_btn and pause_btn:
        log.info(f"3-button standby combo registered (hold time: {hold_time}s).")

    return buttons


def main():
    try:
        import gpiozero
        import signal
    except ImportError:
        log.error("gpiozero not installed.")
        sys.exit(1)

    # On newer kernels (>=6.x) gpiozero needs lgpio explicitly as pin factory
    try:
        from gpiozero.pins.lgpio import LGPIOFactory
        gpiozero.Device.pin_factory = LGPIOFactory()
        log.info("Pin factory: lgpio")
    except Exception as e:
        log.warning(f"lgpio not available ({e}), using fallback.")

    running = [True]

    def _stop(signum, frame):
        running[0] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    buttons = []
    retry_delay = 10

    while running[0] and not buttons:
        buttons = init_buttons()
        if not buttons:
            log.warning(f"No buttons available. Retrying in {retry_delay}s...")
            for _ in range(retry_delay * 2):
                if not running[0]:
                    return
                time.sleep(0.5)

    if not buttons:
        return

    log.info("Button handler active. Waiting for button press...")
    while running[0]:
        time.sleep(0.5)

    log.info("Button handler stopped.")


if __name__ == "__main__":
    main()
