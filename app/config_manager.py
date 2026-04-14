"""
config_manager.py – Central config management with file locking

All modules should use this module instead of reading/writing config.json directly.
- Atomic writes (tmp + os.replace)
- File locking (fcntl.flock) prevents race conditions between daemons
- read_config() / write_config() / update_config() as API
"""
import json
import os
import fcntl
import tempfile

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DIR, "config.json")
_LOCK_PATH = CONFIG_PATH + ".lock"


def read_config() -> dict:
    """Reads config.json with a shared lock (multiple concurrent readers allowed)."""
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        with open(CONFIG_PATH) as f:
            return json.load(f)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def write_config(cfg: dict):
    """Writes config.json atomically with an exclusive lock (blocks other readers/writers)."""
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=DIR, suffix=".json.tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp_path, CONFIG_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def update_config(updater):
    """Reads config, calls updater(cfg), writes back. All under exclusive lock.

    updater(cfg) should modify the cfg dict in-place.
    Returns the updated cfg dict.
    """
    lock_fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        updater(cfg)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=DIR, suffix=".json.tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp_path, CONFIG_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return cfg
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
