"""
update_manager.py – Git-based update system

Fetches the latest code from GitHub and updates the box.
config.json is NOT overwritten (contains user data only).
"""
import os
import subprocess
import shutil
import logging

log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = "/opt/lms-controller"
UPDATE_DIR = "/tmp/lms-update"
REPO_URL_BASE = "https://github.com/hannskanns4/kid2tune.git"


def _get_repo_url() -> str:
    """Returns the repo URL, with token if configured in config.json."""
    try:
        import config_manager
        cfg = config_manager.read_config()
        token = cfg.get("github_token", "").strip()
        if token:
            # https://<token>@github.com/user/repo.git
            return REPO_URL_BASE.replace("https://", f"https://{token}@")
    except Exception:
        pass
    return REPO_URL_BASE

# Files that must NOT be overwritten
PROTECTED_FILES = {"config.json", "config.json.lock", "sync_pending.json"}


def check_for_update() -> dict:
    """Checks if a new version is available on GitHub (without updating)."""
    try:
        # Currently installed version
        version_file = os.path.join(APP_DIR, "version.txt")
        current = "unknown"
        if os.path.exists(version_file):
            with open(version_file) as f:
                current = f.read().strip()

        # Fetch remote version (git ls-remote + raw file)
        result = subprocess.run(
            ["git", "ls-remote", "--refs", "--tags", _get_repo_url()],
            capture_output=True, text=True, timeout=15,
        )
        remote_version = current
        if result.returncode == 0 and result.stdout.strip():
            # Extract all tag versions and sort by version number
            tags = [line.split("refs/tags/")[-1].lstrip("v") for line in result.stdout.strip().split("\n")
                    if "refs/tags/" in line]
            if tags:
                try:
                    tags.sort(key=lambda v: [int(x) for x in v.split(".")])
                except (ValueError, AttributeError):
                    tags.sort()
                remote_version = tags[-1]

        # If no tags: read version.txt from the repo
        if remote_version == current:
            result2 = subprocess.run(
                ["git", "archive", "--remote=" + _get_repo_url(), "HEAD", "app/version.txt"],
                capture_output=True, timeout=15,
            )
            # Fallback: clone and read
            if result2.returncode != 0:
                # Shallow clone for version check
                tmp = UPDATE_DIR + "-check"
                try:
                    if os.path.exists(tmp):
                        shutil.rmtree(tmp)
                    subprocess.run(
                        ["git", "clone", "--depth", "1", _get_repo_url(), tmp],
                        capture_output=True, timeout=60,
                    )
                    vf = os.path.join(tmp, "app", "version.txt")
                    if os.path.exists(vf):
                        with open(vf) as f:
                            remote_version = f.read().strip()
                finally:
                    if os.path.exists(tmp):
                        shutil.rmtree(tmp, ignore_errors=True)

        update_available = remote_version != current
        return {
            "current": current,
            "remote": remote_version,
            "update_available": update_available,
        }
    except Exception as e:
        log.error(f"Update check failed: {e}")
        return {"current": "?", "remote": "?", "update_available": False, "error": str(e)}


def pull_and_update() -> tuple:
    """Fetches the latest code from GitHub and updates the local installation.

    Returns:
        (ok: bool, message: str)
    """
    try:
        # Remember old version
        version_file = os.path.join(APP_DIR, "version.txt")
        old_version = "unknown"
        if os.path.exists(version_file):
            with open(version_file) as f:
                old_version = f.read().strip()

        # 1. Clone/Pull
        if os.path.exists(UPDATE_DIR):
            shutil.rmtree(UPDATE_DIR)

        repo_url = _get_repo_url()
        log.info(f"Cloning repository to {UPDATE_DIR}...")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, UPDATE_DIR],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return False, f"git clone failed: {result.stderr.strip()}"

        src_app = os.path.join(UPDATE_DIR, "app")
        if not os.path.isdir(src_app):
            return False, "app/ directory not found in repository."

        # 2. Copy files
        copied = []

        # Python files
        for f in os.listdir(src_app):
            if f.endswith(".py") or f == "version.txt":
                if f not in PROTECTED_FILES:
                    shutil.copy2(os.path.join(src_app, f), os.path.join(APP_DIR, f))
                    copied.append(f)

        # Templates
        src_tpl = os.path.join(src_app, "templates")
        dst_tpl = os.path.join(APP_DIR, "templates")
        if os.path.isdir(src_tpl):
            os.makedirs(dst_tpl, exist_ok=True)
            for f in os.listdir(src_tpl):
                if f.endswith(".html"):
                    shutil.copy2(os.path.join(src_tpl, f), os.path.join(dst_tpl, f))
                    copied.append(f"templates/{f}")

        # Static (CSS etc.)
        src_static = os.path.join(src_app, "static")
        dst_static = os.path.join(APP_DIR, "static")
        if os.path.isdir(src_static):
            os.makedirs(dst_static, exist_ok=True)
            for f in os.listdir(src_static):
                shutil.copy2(os.path.join(src_static, f), os.path.join(dst_static, f))
                copied.append(f"static/{f}")

        # Language files (lang/)
        src_lang = os.path.join(src_app, "lang")
        dst_lang = os.path.join(APP_DIR, "lang")
        if os.path.isdir(src_lang):
            os.makedirs(dst_lang, exist_ok=True)
            for f in os.listdir(src_lang):
                if f.endswith(".json"):
                    shutil.copy2(os.path.join(src_lang, f), os.path.join(dst_lang, f))
                    copied.append(f"lang/{f}")

        # Copy config.json.template (for future installations)
        tpl = os.path.join(src_app, "config.json.template")
        if os.path.exists(tpl):
            shutil.copy2(tpl, os.path.join(APP_DIR, "config.json.template"))

        # 3. Update version in config.json (without overwriting config.json)
        new_version = old_version
        new_vf = os.path.join(APP_DIR, "version.txt")
        if os.path.exists(new_vf):
            with open(new_vf) as f:
                new_version = f.read().strip()

        try:
            import config_manager
            def _update_version(cfg):
                cfg["version"] = new_version
            config_manager.update_config(_update_version)
        except Exception as e:
            log.warning(f"Could not update version in config.json: {e}")

        # 4. Clean up
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)

        # 5. Restart services
        _restart_services()

        log.info(f"Update complete: {old_version} -> {new_version} ({len(copied)} files)")
        return True, f"Update {old_version} -> {new_version} ({len(copied)} files updated)"

    except Exception as e:
        log.error(f"Update failed: {e}")
        # Clean up on error
        if os.path.exists(UPDATE_DIR):
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        return False, f"Update failed: {e}"


def _restart_services():
    """Restarts the affected services."""
    services = ["lms-hardware", "lms-rfid"]
    for svc in services:
        try:
            subprocess.run(["systemctl", "restart", svc],
                           capture_output=True, timeout=15)
            log.info(f"Service {svc} restarted.")
        except Exception as e:
            log.warning(f"Service {svc} restart failed: {e}")

    # Restart lms-web last via Popen (own process will be killed)
    try:
        subprocess.Popen(["systemctl", "restart", "lms-web"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
