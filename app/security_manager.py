"""
security_manager.py – PIN protection for the adult area (opt-in)

The PIN is stored as a salted PBKDF2 hash in config.json:

    "security": {
        "adult_area_enabled": false,
        "pin_hash": "<hex>",
        "pin_salt": "<hex>"
    }

Used by web_app.py (unlock/config routes) and pin_tool.py (CLI reset when
the PIN is lost: sudo python3 /opt/lms-controller/pin_tool.py --help).
"""
import hashlib
import secrets

import config_manager

PBKDF2_ITERATIONS = 100_000


def _sec_cfg() -> dict:
    return config_manager.read_config().get("security", {})


def is_enabled() -> bool:
    return bool(_sec_cfg().get("adult_area_enabled"))


def has_pin() -> bool:
    sec = _sec_cfg()
    return bool(sec.get("pin_hash") and sec.get("pin_salt"))


def pin_valid_format(pin: str) -> bool:
    """4-8 digits."""
    return pin.isdigit() and 4 <= len(pin) <= 8


def _hash_pin(pin: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS).hex()


def verify_pin(pin: str) -> bool:
    sec = _sec_cfg()
    salt = sec.get("pin_salt", "")
    stored = sec.get("pin_hash", "")
    if not (pin and salt and stored):
        return False
    return secrets.compare_digest(_hash_pin(pin, salt), stored)


def set_pin(pin: str):
    """Stores a new PIN (new random salt)."""
    salt = secrets.token_hex(16)
    pin_hash = _hash_pin(pin, salt)
    def _update(cfg):
        sec = cfg.setdefault("security", {})
        sec["pin_salt"] = salt
        sec["pin_hash"] = pin_hash
    config_manager.update_config(_update)


def set_enabled(enabled: bool):
    def _update(cfg):
        cfg.setdefault("security", {})["adult_area_enabled"] = bool(enabled)
    config_manager.update_config(_update)
