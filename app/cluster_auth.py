"""HMAC authentication for box-to-box HTTP requests."""
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlsplit

_NONCES = {}
_NONCE_LOCK = threading.Lock()
_MAX_CLOCK_SKEW = 60


def _body_bytes(body) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    return str(body).encode("utf-8")


def _message(method: str, path: str, body: bytes, timestamp: str, nonce: str) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{digest}".encode("utf-8")


def sign_headers(method: str, path: str, body, secret: str) -> dict:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signature = hmac.new(
        secret.encode("utf-8"),
        _message(method, path, _body_bytes(body), timestamp, nonce),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Cluster-Timestamp": timestamp,
        "X-Cluster-Nonce": nonce,
        "X-Cluster-Signature": signature,
    }


def signed_request(method: str, url: str, secret: str, timeout: int = 15, **kwargs):
    import requests

    prepared = requests.Request(method, url, **kwargs).prepare()
    path = urlsplit(prepared.url).path
    prepared.headers.update(sign_headers(
        prepared.method, path, prepared.body, secret,
    ))
    with requests.Session() as client:
        return client.send(prepared, timeout=timeout, allow_redirects=False)


def verify_request(method: str, path: str, body, headers, secret: str) -> bool:
    timestamp = headers.get("X-Cluster-Timestamp", "")
    nonce = headers.get("X-Cluster-Nonce", "")
    supplied = headers.get("X-Cluster-Signature", "")
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - request_time) > _MAX_CLOCK_SKEW:
        return False
    if len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        _message(method, path, _body_bytes(body), timestamp, nonce),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        return False

    now = time.time()
    with _NONCE_LOCK:
        expired = [key for key, seen_at in _NONCES.items()
                   if now - seen_at > _MAX_CLOCK_SKEW]
        for key in expired:
            del _NONCES[key]
        if nonce in _NONCES:
            return False
        _NONCES[nonce] = now
    return True
