import hashlib
import hmac
import secrets
import time
import unittest

from app.cluster_auth import _message, verify_request


class ClusterAuthTests(unittest.TestCase):
    def _headers(self, method, path, body, nonce, secret):
        timestamp = str(int(time.time()))
        signature = hmac.new(
            secret.encode(),
            _message(method, path, body, timestamp, nonce),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-Cluster-Timestamp": timestamp,
            "X-Cluster-Nonce": nonce,
            "X-Cluster-Signature": signature,
        }

    def test_signature_is_bound_to_body_and_path(self):
        secret = "s" * 40
        headers = self._headers(
            "POST", "/api/update/package", b"archive", secrets.token_hex(16), secret,
        )
        self.assertTrue(verify_request(
            "POST", "/api/update/package", b"archive", headers, secret,
        ))
        altered_body = self._headers(
            "POST", "/api/update/package", b"archive", secrets.token_hex(16), secret,
        )
        self.assertFalse(verify_request(
            "POST", "/api/update/package", b"changed", altered_body, secret,
        ))
        altered_path = self._headers(
            "POST", "/api/update/package", b"archive", secrets.token_hex(16), secret,
        )
        self.assertFalse(verify_request(
            "POST", "/api/update/other", b"archive", altered_path, secret,
        ))

    def test_request_replay_is_rejected(self):
        secret = "s" * 40
        headers = self._headers(
            "GET", "/api/update/version", b"", secrets.token_hex(16), secret,
        )
        self.assertTrue(verify_request("GET", "/api/update/version", b"", headers, secret))
        self.assertFalse(verify_request("GET", "/api/update/version", b"", headers, secret))


if __name__ == "__main__":
    unittest.main()
