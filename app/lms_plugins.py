"""
lms_plugins.py – Helpers to check for and install LMS plugin updates.

LMS exposes no JSON-RPC for plugin updates, so we scrape the standard
`/settings/server/plugins.html` page. Updates are marked by hidden form
inputs `name="update:PluginName"`; submitting them with the required `rand`
anti-CSRF token marks the plugins for install on the next LMS restart.
"""
import re
import logging

log = logging.getLogger(__name__)

PLUGIN_PAGE = "/settings/server/plugins.html"


def _base_url() -> str:
    import config_manager
    cfg = config_manager.read_config()
    return f"http://{cfg.get('lms_host', '127.0.0.1')}:{cfg.get('lms_port', 9000)}"


def parse(html: str):
    """Returns (updates, action_url, installed_names, rand_token).

    updates:         list of {name, label}
    action_url:      form action path (with playerid query) or default
    installed_names: currently-checked plugin checkbox names (to preserve on submit)
    rand_token:      LMS anti-CSRF "rand" hidden field value
    """
    upd_pairs = re.findall(
        r'<input[^>]*\bname="update:([A-Za-z0-9_]+)"[^>]*\bvalue="([^"]*)"',
        html,
    )
    updates = [{"name": n, "label": l or n} for n, l in upd_pairs]

    m = re.search(r'<form[^>]+\bname="settingsForm"[^>]+\baction="([^"]+)"', html)
    action_url = m.group(1) if m else PLUGIN_PAGE

    installed = []
    for m2 in re.finditer(
        r'<input[^>]*\bname="([A-Za-z0-9_]+)"[^>]*\bchecked=checked[^>]*>',
        html,
    ):
        name = m2.group(1)
        if not name.startswith("update:"):
            installed.append(name)

    rm = re.search(r'<input[^>]*\bname="rand"[^>]*\bvalue="([^"]+)"', html)
    rand_token = rm.group(1) if rm else ""

    return updates, action_url, installed, rand_token


def fetch_page(timeout: int = 10) -> str:
    """Fetches the LMS plugins page. Raises on connection error."""
    import requests
    r = requests.get(f"{_base_url()}{PLUGIN_PAGE}", timeout=timeout)
    return r.text


def get_available_updates(timeout: int = 5) -> list:
    """Returns the list of available plugin updates, or [] on any error."""
    try:
        html = fetch_page(timeout=timeout)
        updates, _action, _installed, _rand = parse(html)
        return updates
    except Exception as e:
        log.debug(f"LMS plugin update check failed: {e}")
        return []


def install_all_available(timeout: int = 30):
    """Marks all available plugin updates for install via the LMS form.

    Returns (ok: bool, updates: list, message_or_error: str).
    Caller is responsible for restarting LMS afterwards (LMS installs on restart).
    """
    import requests
    try:
        html = fetch_page(timeout=15)
    except Exception as e:
        return False, [], f"LMS not reachable: {e}"

    updates, action_url, installed, rand_token = parse(html)
    if not updates:
        return True, [], "no_updates"
    if not rand_token:
        return False, updates, "rand token not found on plugins page"

    form_data = {"saveSettings": "1", "rand": rand_token}
    for name in installed:
        form_data[name] = "1"
    for upd in updates:
        form_data[f"update:{upd['name']}"] = "1"

    base = _base_url()
    target = action_url if action_url.startswith("http") else f"{base}{action_url}"
    try:
        requests.post(target, data=form_data, timeout=timeout)
    except Exception as e:
        return False, updates, f"submit failed: {e}"
    return True, updates, "ok"
