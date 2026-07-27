"""
update_manager.py – Git-based update system

Fetches the latest code from GitHub and updates the box.
config.json is NOT overwritten (contains user data only).

The file list (iter_code_files) is shared with the box-to-box update in
web_app.py so both update paths always cover the SAME set of files —
partial updates caused boxes to run version mixes in the past.
"""
import os
import hashlib
import subprocess
import shutil
import logging

log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = "/opt/lms-controller"
UPDATE_DIR = "/tmp/lms-update"
REPO_URL_BASE = "https://github.com/hannskanns4/kid2tune.git"

# Runtime data on the boxes that must NOT be overwritten
PROTECTED_FILES = {
    "config.json", "config.json.lock",
    "play_history.json", "play_history.json.lock",
    "sync_pending.json",
}

# Directories that never contain deployable code
SKIP_DIRS = {"__pycache__", "venv", ".git"}


def iter_code_files(base_dir: str):
    """Yields all deployable code files below base_dir as relative paths
    (forward slashes): Python, templates, static, lang, version.txt, ...
    Skips runtime data (PROTECTED_FILES), hidden files, virtualenv/cache
    directories and compiled/temporary artifacts."""
    for dirpath, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if fn in PROTECTED_FILES or fn.startswith(".") or fn.endswith((".pyc", ".tmp")):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), base_dir)
            yield rel.replace(os.sep, "/")


def file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


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


def _sanitize(msg: str) -> str:
    """Removes a configured GitHub token from error messages/logs."""
    try:
        import config_manager
        token = config_manager.read_config().get("github_token", "").strip()
        if token:
            msg = msg.replace(token, "***")
    except Exception:
        pass
    return msg


def check_for_update() -> dict:
    """Checks if a new version is available on GitHub (without updating).

    Reads app/version.txt from a shallow clone of the default branch —
    version.txt is the single source of truth. (Tags are NOT used: they
    lag behind the actual releases and reported wrong versions.)
    """
    try:
        version_file = os.path.join(APP_DIR, "version.txt")
        current = "unknown"
        if os.path.exists(version_file):
            with open(version_file) as f:
                current = f.read().strip()

        remote_version = ""
        tmp = UPDATE_DIR + "-check"
        try:
            if os.path.exists(tmp):
                shutil.rmtree(tmp)
            result = subprocess.run(
                ["git", "clone", "--depth", "1", _get_repo_url(), tmp],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                err = _sanitize(result.stderr.strip())
                return {"current": current, "remote": "?",
                        "update_available": False, "error": f"git clone failed: {err}"}
            vf = os.path.join(tmp, "app", "version.txt")
            if os.path.exists(vf):
                with open(vf) as f:
                    remote_version = f.read().strip()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if not remote_version:
            return {"current": current, "remote": "?",
                    "update_available": False, "error": "app/version.txt not found in repository"}

        return {
            "current": current,
            "remote": remote_version,
            "update_available": remote_version != current,
        }
    except Exception as e:
        log.error(f"Update check failed: {_sanitize(str(e))}")
        return {"current": "?", "remote": "?", "update_available": False,
                "error": _sanitize(str(e))}


def pull_and_update() -> tuple:
    """Fetches the latest code from GitHub and updates the local installation.

    All files below app/ are copied recursively (not just known categories),
    Python files are syntax-checked BEFORE installing, and afterwards every
    installed file is hash-verified against the clone.

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

        # 1. Clone
        if os.path.exists(UPDATE_DIR):
            shutil.rmtree(UPDATE_DIR)

        log.info(f"Cloning repository to {UPDATE_DIR}...")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", _get_repo_url(), UPDATE_DIR],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return False, f"git clone failed: {_sanitize(result.stderr.strip())}"

        src_app = os.path.join(UPDATE_DIR, "app")
        if not os.path.isdir(src_app):
            return False, "app/ directory not found in repository."

        rel_files = sorted(iter_code_files(src_app))
        if not any(r.endswith("web_app.py") for r in rel_files):
            return False, "Repository looks incomplete (web_app.py missing) — aborting."

        # 2. Syntax check BEFORE touching the installation — a repo state
        # that does not even compile must never be installed
        import py_compile
        for rel in rel_files:
            if rel.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(src_app, rel), doraise=True)
                except py_compile.PyCompileError as e:
                    return False, f"Syntax error in {rel}: {str(e)[:300]}"

        # 3. Copy ALL files (atomic per file: copy to temp name, then rename)
        copied = []
        for rel in rel_files:
            src = os.path.join(src_app, rel)
            dst = os.path.join(APP_DIR, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            tmp_dst = dst + ".update-tmp"
            shutil.copy2(src, tmp_dst)
            os.replace(tmp_dst, dst)
            copied.append(rel)

        # 4. Verify: every installed file must match the clone exactly
        mismatched = []
        for rel in rel_files:
            src = os.path.join(src_app, rel)
            dst = os.path.join(APP_DIR, rel.replace("/", os.sep))
            if not os.path.exists(dst) or file_md5(src) != file_md5(dst):
                mismatched.append(rel)
        if mismatched:
            return False, ("Verification failed, files differ after copy: "
                           + ", ".join(mismatched[:10]))

        # 5. Update version in config.json (without overwriting config.json)
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

        # 6. Clean up
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)

        # 7. Restart services
        _restart_services()

        log.info(f"Update complete: {old_version} -> {new_version} ({len(copied)} files, verified)")
        return True, f"Update {old_version} -> {new_version} ({len(copied)} files updated, verified)"

    except Exception as e:
        log.error(f"Update failed: {_sanitize(str(e))}")
        # Clean up on error
        if os.path.exists(UPDATE_DIR):
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        return False, f"Update failed: {_sanitize(str(e))}"


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
