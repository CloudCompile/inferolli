"""desktop_gate.py — signed tokens, expiry, tampering, the Discord gate."""

import hashlib
import hmac
import json
import time
import unittest
from pathlib import Path

import desktop_gate as gate
from fastapi.testclient import TestClient


class TokenTests(unittest.TestCase):
    def test_roundtrip(self):
        tok = gate.make_token(123, rw=True)
        data = gate.read_token(tok)
        self.assertEqual(data["uid"], 123)
        self.assertTrue(data["rw"])
        self.assertGreater(data["exp"], time.time())

    def test_tampered_signature_rejected(self):
        tok = gate.make_token(1, rw=False)
        body, sig = tok.split(".", 1)
        bad = body + "." + ("0" * len(sig))
        self.assertIsNone(gate.read_token(bad))

    def test_tampered_body_rejected(self):
        tok = gate.make_token(1, rw=False)
        body, sig = tok.split(".", 1)
        forged = json.dumps({"uid": 999, "rw": True, "exp": time.time() + 60}).encode().hex()
        self.assertIsNone(gate.read_token(forged + "." + sig))

    def test_expired_rejected(self):
        body = json.dumps({"uid": 1, "rw": True,
                           "exp": time.time() - 10}).encode()
        tok = body.hex() + "." + hmac.new(gate.SECRET, body, hashlib.sha256).hexdigest()[:32]
        self.assertIsNone(gate.read_token(tok))

    def test_garbage_rejected(self):
        for junk in ("", "x", "a.b", "zz.zz"):
            self.assertIsNone(gate.read_token(junk))


class GateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(gate.app)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True})

    def test_no_token_is_401(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 401)

    def test_bad_token_is_401(self):
        r = self.client.get("/?token=garbage", follow_redirects=False)
        self.assertEqual(r.status_code, 401)

    def test_valid_token_redirects_to_viewer(self):
        tok = gate.make_token(5, rw=False)
        r = self.client.get(f"/?token={tok}", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("vnc.html", r.headers["location"])
        self.assertIn("dgtok", r.cookies)

    def test_websocket_route_is_gated(self):
        r = self.client.get("/websockify")
        self.assertEqual(r.status_code, 426)

    @unittest.skipUnless(Path(gate.NOVNC, "vnc.html").exists(), "noVNC not installed here")
    def test_novnc_assets_are_mounted(self):
        paths = {r.path for r in gate.app.routes}
        for sub in ("app", "core", "vendor"):
            if Path(gate.NOVNC, sub).is_dir():
                self.assertIn(f"/{sub}", paths)


if __name__ == "__main__":
    unittest.main()
