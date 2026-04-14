"""
Internationalization (i18n) – JSON-based translations.

Loads language files from app/lang/<code>.json and provides a t() function
usable in both Python and Jinja2 templates.

Usage:
    from i18n import t
    t("nav.settings")          # -> "Einstellungen" (de) / "Settings" (en)
    t("sync.count", count=3)   # -> "3 in queue"
"""

import json
import os
import logging

log = logging.getLogger(__name__)

_LANG_DIR = os.path.join(os.path.dirname(__file__), "lang")
_strings: dict = {}
_current_lang: str = "de"


def load_language(lang: str = "de") -> None:
    """Load language file. Falls back to 'de', then empty strings if nothing found."""
    global _strings, _current_lang
    path = os.path.join(_LANG_DIR, f"{lang}.json")
    if not os.path.exists(path):
        log.warning("Language file %s not found, falling back to de.", path)
        lang = "de"
        path = os.path.join(_LANG_DIR, "de.json")
    if not os.path.exists(path):
        log.warning("No language files found in %s – keys will be shown as text.", _LANG_DIR)
        _strings = {}
        _current_lang = lang
        return
    with open(path, "r", encoding="utf-8") as f:
        _strings = json.load(f)
    _current_lang = lang
    log.info("Language loaded: %s", lang)


def get_language() -> str:
    return _current_lang


def available_languages() -> list:
    """List all available language codes."""
    if not os.path.isdir(_LANG_DIR):
        return ["de"]
    langs = []
    for f in sorted(os.listdir(_LANG_DIR)):
        if f.endswith(".json"):
            langs.append(f[:-5])
    return langs or ["de"]


def t(key: str, **kwargs) -> str:
    """Translate using a dot-separated key.

    Example: t("nav.settings") accesses {"nav": {"settings": "..."}}.
    Placeholders are replaced via str.format(): t("x", count=3).
    """
    parts = key.split(".")
    value = _strings
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
        if value is None:
            return key  # Show key as fallback
    if isinstance(value, str) and kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError):
            return value
    return value if isinstance(value, str) else key
