"""
hardware_daemon.py – Combined hardware daemon (LCD + Buttons)

Runs LCD display and button handler in a single process
to save ~15 MB RAM (one fewer Python interpreter).

- LCD: I2C display in background thread (1s polling)
- Buttons: gpiozero event callbacks in main thread
"""
import os
import sys
import signal
import time
import logging
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HW] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)


def run_lcd():
    """Starts the LCD display main loop (runs in its own thread)."""
    try:
        import lcd_display
        lcd_display.main()
    except Exception as e:
        log.error(f"LCD thread error: {e}")


def run_buttons(stop_event: threading.Event):
    """Starts the button handler (runs in the main thread)."""
    try:
        import gpiozero
    except ImportError:
        log.error("gpiozero not installed.")
        return

    # On newer kernels (>=6.x) gpiozero needs lgpio explicitly as pin factory
    try:
        from gpiozero.pins.lgpio import LGPIOFactory
        gpiozero.Device.pin_factory = LGPIOFactory()
        log.info("Pin factory: lgpio")
    except Exception as e:
        log.warning(f"lgpio not available ({e}), using fallback.")

    import button_handler
    buttons = []
    retry_delay = 10

    while not stop_event.is_set() and not buttons:
        buttons = button_handler.init_buttons()
        if not buttons:
            log.warning(f"No buttons available. Retrying in {retry_delay}s...")
            for _ in range(retry_delay * 2):
                if stop_event.is_set():
                    return
                time.sleep(0.5)

    if not buttons:
        return

    log.info("Button handler active. Waiting for button press...")
    while not stop_event.is_set():
        time.sleep(0.5)

    log.info("Button handler stopped.")


def main():
    log.info("Hardware daemon started (LCD + Buttons).")

    stop_event = threading.Event()

    def _stop(signum, frame):
        log.info("Stop signal received.")
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Start LCD in its own daemon thread
    lcd_thread = threading.Thread(target=run_lcd, name="lcd", daemon=True)
    lcd_thread.start()
    log.info("LCD thread started.")

    # Brief wait for LCD initialization
    time.sleep(2)

    # Buttons in main thread (gpiozero needs the main thread for signal handling)
    run_buttons(stop_event)

    log.info("Hardware daemon stopped.")


if __name__ == "__main__":
    main()
