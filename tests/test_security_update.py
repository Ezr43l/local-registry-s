"""Focused regression checks for the secure registry migration (18 checks)."""
import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1] / "docker" / "local-registry"
sys.path.insert(0, str(SOURCE))
import security


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    do_HEAD = do_GET
    def do_POST(self):
        self.send_response(202); self.end_headers()
    do_DELETE = do_POST


class SecureRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        security.DATA_ROOT = cls.root
        security.SECURITY_DIR = cls.root / "security"
        security.POLICY_FILE = security.SECURITY_DIR / "policy.json"
        security.prepare("x" * 48)
        cls.original_policy = security.policy()
        os.environ["LOCAL_REGISTRY_CONFIG_FILE"] = str(cls.root / "config.json")
        spec = importlib.util.spec_from_file_location("security_test_server", SOURCE / "server.py")
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.backend = HTTPServer(("127.0.0.1", 5050), Backend)
        cls.backend_thread = threading.Thread(target=cls.backend.serve_forever, daemon=True)
        cls.backend_thread.start()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]
        cls.gateway = subprocess.Popen(["/bin/secure-registry", "--dir=" + str(security.SECURITY_DIR), "--addr=127.0.0.1:" + str(cls.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = "https://127.0.0.1:" + str(cls.port)
        for _ in range(50):
            try:
                with urllib.request.urlopen(cls.url + "/healthz", context=security.tls_context(), timeout=1):
                    break
            except OSError:
                time.sleep(.05)
        else:
            raise RuntimeError("secure listener did not start")

    @classmethod
    def tearDownClass(cls):
        cls.gateway.terminate(); cls.gateway.wait(timeout=5)
        cls.backend.shutdown(); cls.backend.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        security._save_policy(json.loads(json.dumps(self.original_policy)))
        self.app._auth_attempts.clear()

    def request(self, method="GET", path="/v2/_catalog", credential=None):
        req = urllib.request.Request(self.url + path, method=method)
        if credential:
            req.add_header("Authorization", "Basic " + base64.b64encode((credential["username"] + ":" + credential["token"]).encode()).decode())
        try:
            with urllib.request.urlopen(req, context=security.tls_context(), timeout=3) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def issue(self, permissions, repositories=None):
        return security.change_client({"action": "create", "name": "test client", "permissions": permissions, "repositories": repositories or ["*"]})

    def test_01_anonymous_catalog_denied(self):
        self.assertEqual(401, self.request())

    def test_02_reader_can_download(self):
        self.assertEqual(200, self.request(credential=self.issue(["pull"])))

    def test_03_reader_cannot_publish(self):
        self.assertEqual(403, self.request("POST", "/v2/demo/blobs/uploads/", self.issue(["pull"])))

    def test_04_publisher_cannot_delete(self):
        self.assertEqual(403, self.request("DELETE", "/v2/demo/manifests/sha256:" + "0" * 64, self.issue(["pull", "push"])))

    def test_05_repository_scope_enforced(self):
        self.assertEqual(403, self.request("GET", "/v2/other/manifests/latest", self.issue(["pull"], ["team/*"])))

    def test_06_revocation_immediate(self):
        c = self.issue(["pull"])
        doc = security.policy(); ident = next(x["id"] for x in doc["clients"] if x["username"] == c["username"])
        security.change_client({"action": "delete", "id": ident})
        self.assertEqual(401, self.request(credential=c))

    def test_07_untrusted_tls_rejected(self):
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(self.url + "/v2/", timeout=3)

    def test_08_plain_http_cannot_authenticate(self):
        req = urllib.request.Request(self.url.replace("https:", "http:") + "/v2/")
        with self.assertRaises((urllib.error.HTTPError, urllib.error.URLError, OSError)):
            urllib.request.urlopen(req, timeout=3)

    def test_09_rotating_login_names_limited(self):
        doc = {"username": "owner", "password_hash": "dummy"}
        with patch.object(self.app, "_read_auth", return_value=doc), patch.object(self.app, "_verify_password", return_value=False) as verify:
            statuses = []
            for n in range(12):
                try:
                    self.app._auth_login({"username": "user" + str(n), "password": "wrong"}, "192.0.2.1")
                except self.app.AuthError as e:
                    statuses.append(e.status)
            self.assertEqual([401] * 10 + [429] * 2, statuses)
            self.assertEqual(10, verify.call_count)

    def test_10_modified_manifest_rejected(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"schemaVersion":2}'
        response.headers = {"Docker-Content-Digest": "sha256:" + "0" * 64}
        with patch.object(self.app, "_urlopen_http", return_value=response):
            self.assertEqual((None, None, None), self.app._manifiesto_crudo("https://registry.example", "demo", "latest"))

    def test_11_cross_origin_redirect_rejected(self):
        handler = security.SameOriginRedirect()
        with self.assertRaises(ValueError):
            handler.redirect_request(urllib.request.Request("https://one.example/v2/"), None, 302, "", {}, "https://two.example/private")

    def test_12_cross_origin_pagination_rejected(self):
        with patch.object(self.app, "_fetch", return_value=({"repositories": []}, {"Link": '<https://other.example/private>; rel="next"'})) as fetch:
            with self.assertRaises(ValueError):
                self.app.catalog("https://registry.example")
            self.assertEqual(1, fetch.call_count)

    def test_13_unsigned_cluster_response_rejected(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"ok":true}'
        response.headers = {}; response.status = 200
        with patch.object(self.app, "PANEL_PEERS", {"peer": "https://peer.example"}), patch.object(self.app, "_urlopen_http", return_value=response):
            data, error = self.app._solicitud_panel("peer", "/api/internal/cluster/auth")
            self.assertIsNone(data); self.assertIsNotNone(error)

    def test_14_session_revocation(self):
        security.session_register("session-a", int(time.time()) + 60)
        expiry = json.loads(security.private_read(security.SECURITY_DIR / "sessions.json"))["session-a"]
        self.assertTrue(security.session_valid("session-a", expiry))
        security.session_revoke("session-a")
        self.assertFalse(security.session_valid("session-a", expiry))

    def test_15_bootstrap_required(self):
        with self.assertRaises(self.app.AuthError) as result:
            self.app._auth_setup({"username": "owner", "display_name": "Owner", "password": "long-safe-password", "password_confirmation": "long-safe-password", "bootstrap_code": "wrong"})
        self.assertEqual(403, result.exception.status)

    def test_16_secure_cookie(self):
        self.assertIn("; Secure", self.app.Handler._cookie("dummy", 60))

    def test_17_worldwide_network_not_permitted(self):
        with self.assertRaises(ValueError):
            security.validate_networks(["0.0.0.0/0"])

    def test_18_bounded_reply(self):
        response = security.BoundedResponse(io.BytesIO(b'x' * 20))
        response.remaining = 10
        with self.assertRaises(ValueError):
            response.read()


if __name__ == "__main__":
    unittest.main()
