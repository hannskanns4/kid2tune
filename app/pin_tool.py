#!/usr/bin/env python3
"""
pin_tool.py – CLI for the adult-area PIN (recovery when the PIN is lost)

Must run as root (config.json is root-only):

    sudo python3 /opt/lms-controller/pin_tool.py status
    sudo python3 /opt/lms-controller/pin_tool.py set 1234
    sudo python3 /opt/lms-controller/pin_tool.py disable

`set` stores a new PIN (and enables protection), `disable` switches the
adult area off entirely. Takes effect immediately, no service restart needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import security_manager


def usage():
    print(__doc__.strip())
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        usage()
    cmd = sys.argv[1].lower()

    if cmd == "status":
        enabled = security_manager.is_enabled()
        has_pin = security_manager.has_pin()
        print(f"Adult area enabled: {'yes' if enabled else 'no'}")
        print(f"PIN set:            {'yes' if has_pin else 'no'}")

    elif cmd == "set":
        if len(sys.argv) < 3:
            print("Usage: pin_tool.py set <PIN>  (4-8 digits)")
            sys.exit(1)
        pin = sys.argv[2].strip()
        if not security_manager.pin_valid_format(pin):
            print("Error: PIN must be 4-8 digits.")
            sys.exit(1)
        security_manager.set_pin(pin)
        security_manager.set_enabled(True)
        print("New PIN saved, adult area enabled.")

    elif cmd == "disable":
        security_manager.set_enabled(False)
        print("Adult area disabled. (Stored PIN is kept and can be "
              "re-enabled in the web UI.)")

    else:
        usage()


if __name__ == "__main__":
    try:
        main()
    except PermissionError:
        print("Error: no permission for config.json — run with sudo.")
        sys.exit(1)
