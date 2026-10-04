"""
config_manager.py – Central config management with file locking

All modules should use this module instead of reading/writing config.json directly.
- Atomic writes (tmp + os.replace) that survive a power loss (fsync of file + dir)
- File locking (fcntl.flock) prevents race conditions between daemons
- Mode 0600: config.json holds WiFi/NAS passwords, the PIN hash and web_secret
- Recovery: a truncated/corrupt config falls back to the last known-good backup
  instead of crash-looping every service
- read_config() / write_config() / update_config() as API
"""
import json
import logging
import os
import fcntl
import shutil
import tempfile

log = logging.getLogger(__name__)

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DIR, "config.json")
_LOCK_PATH = CONFIG_PATH + ".lock"
_BAK_PATH = CONFIG_PATH + ".bak"

# Last resort when config.json AND the backup are unreadable. Keeping the
# services alive with defaults beats an endless Restart=always crash loop:
# the box still boots, the web UI comes up and a parent can fix it there.
_FALLBACK = {
    "language": "de",
    "lms_host": "localhost",
    "lms_port": 9000,
    "rfid_mappings": {},
}


def _load(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: top level is {type(cfg).__name__}, expected object")
    return cfg


def _read_locked() -> dict:
    """Reads config.json, falling back to the backup and finally to defaults.

    Caller must already hold the lock.
    """
    try:
        return _load(CONFIG_PATH)
    except FileNotFoundError:
        log.error("config.json missing – trying backup.")
    except (OSError, ValueError) as e:
        log.error(f"config.json unreadable ({e}) – trying backup.")
        # Keep the broken file for diagnosis, but only the first time so a
        # later good backup restore is not overwritten by another failure.
        try:
            if not os.path.exists(CONFIG_PATH + ".corrupt"):
                shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".corrupt")
        except OSError:
            pass

    try:
        cfg = _load(_BAK_PATH)
        log.warning("config.json restored from config.json.bak.")
        return cfg
    except (OSError, ValueError) as e:
        log.error(f"Backup unusable ({e}) – starting with default values!")
        return json.loads(json.dumps(_FALLBACK))  # fresh copy per caller


def _atomic_write_locked(cfg: dict):
    """Writes config.json crash-safely. Caller must already hold the lock.

    fsync of file and directory: os.replace is atomic against other processes,
    but without fsync a power loss can still leave a zero-length config behind.
    """
    # Back up the current (parseable) config before replacing it
    try:
        if os.path.exists(CONFIG_PATH):
            _load(CONFIG_PATH)  # only back up what is actually valid
            shutil.copy2(CONFIG_PATH, _BAK_PATH)
    except (OSError, ValueError):
        pass

    tmp_fd, tmp_path = tempfile.mkstemp(dir=DIR, suffix=".json.tmp")
    try:
        os.chmod(tmp_path, 0o600)  # secrets: never world-readable
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Persist the rename itself, otherwise the replace can be lost on unplug
    try:
        dir_fd = os.open(DIR, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_config() -> dict:
    """Reads config.json with a shared lock (multiple concurrent readers allowed)."""
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        return _read_locked()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def write_config(cfg: dict):
    """Writes config.json atomically with an exclusive lock (blocks other readers/writers)."""
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _atomic_write_locked(cfg)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def update_config(updater):
    """Reads config, calls updater(cfg), writes back. All under exclusive lock.

    updater(cfg) should modify the cfg dict in-place.
    Returns the updated cfg dict.
    """
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        cfg = _read_locked()
        updater(cfg)
        _atomic_write_locked(cfg)
        return cfg
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
