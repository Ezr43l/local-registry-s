import importlib.util
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


SERVER_PATH = Path(__file__).parents[1] / "docker" / "local-registry" / "server.py"
spec = importlib.util.spec_from_file_location("registry_server", SERVER_PATH)
server = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(server)


def digest(number: str) -> str:
    return "sha256:" + number * 64


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.old_keep = server.KEEP_LAST
        self.old_protected = server.PROTECTED
        self.old_peers = server.PEERS
        self.old_nodes = server._nodos
        self.old_inventory = server.inventario_global
        self.old_date = server.fecha_de_imagen
        server.KEEP_LAST = 2
        server.PROTECTED = {"latest"}
        server.PEERS = {}
        server._nodos = lambda: {"local": "http://registry"}

    def tearDown(self):
        server.KEEP_LAST = self.old_keep
        server.PROTECTED = self.old_protected
        server.PEERS = self.old_peers
        server._nodos = self.old_nodes
        server.inventario_global = self.old_inventory
        server.fecha_de_imagen = self.old_date

    def test_retention_is_per_branch_and_counts_unique_digests(self):
        inventory = {
            "app": {
                "v1.0.0": {"nodes": ["local"], "digests": {"local": digest("1")}},
                "v1.1.0": {"nodes": ["local"], "digests": {"local": digest("2")}},
                "v1.2.0": {"nodes": ["local"], "digests": {"local": digest("3")}},
                "v1.2.0-alias": {"nodes": ["local"], "digests": {"local": digest("3")}},
                "v1.0.0-d1": {"nodes": ["local"], "digests": {"local": digest("4")}},
                "v1.1.0-d1": {"nodes": ["local"], "digests": {"local": digest("5")}},
                "latest": {"nodes": ["local"], "digests": {"local": digest("3")}},
            }
        }
        dates = {
            "v1.0.0": 1, "v1.1.0": 2, "v1.2.0": 3,
            "v1.2.0-alias": 3, "v1.0.0-d1": 1, "v1.1.0-d1": 2,
            "latest": 3,
        }
        server.inventario_global = lambda: (inventory, [])
        server.fecha_de_imagen = lambda base, repo, tag: dates[tag]

        plan = server.plan_retencion()
        removed = set(plan["candidates"][0]["removing"])
        self.assertEqual(removed, {"v1.0.0"})
        self.assertNotIn("v1.2.0-alias", removed)
        self.assertNotIn("latest", removed)
        self.assertEqual(
            next(b for b in plan["candidates"][0]["branches"] if b["branch"] == "dev")["removing"],
            [],
        )

    def test_unknown_dates_are_kept(self):
        inventory = {
            "app": {
                "v1.0.0": {"nodes": ["local"], "digests": {"local": digest("1")}},
                "v1.1.0": {"nodes": ["local"], "digests": {"local": digest("2")}},
            }
        }
        server.inventario_global = lambda: (inventory, [])
        server.fecha_de_imagen = lambda base, repo, tag: 0
        self.assertEqual(server.plan_retencion()["candidates"], [])

    def test_shared_digest_with_protected_tag_is_not_deleted(self):
        inventory = {
            "app": {
                "v0.0.0": {"nodes": ["local"], "digests": {"local": digest("0")}},
                "v0.1.0": {"nodes": ["local"], "digests": {"local": digest("1")}},
                "latest": {"nodes": ["local"], "digests": {"local": digest("1")}},
                "v0.2.0": {"nodes": ["local"], "digests": {"local": digest("2")}},
                "v0.3.0": {"nodes": ["local"], "digests": {"local": digest("3")}},
            }
        }
        server.inventario_global = lambda: (inventory, [])
        server.fecha_de_imagen = lambda base, repo, tag: {
            "v0.0.0": 0.5, "v0.1.0": 1, "latest": 1, "v0.2.0": 2, "v0.3.0": 3,
        }[tag]
        plan = server.plan_retencion()
        removed = set(plan["candidates"][0]["removing"])
        self.assertNotIn("v0.1.0", removed)
        self.assertEqual(removed, {"v0.0.0"})


class UtilityTests(unittest.TestCase):
    def test_environment_parsers_fail_closed(self):
        with patch.dict(os.environ, {"TEST_BOOL": "perhaps"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "booleano explícito"):
                server._env_bool("TEST_BOOL", True)
        with patch.dict(os.environ, {"TEST_INT": "not-a-number"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "entero"):
                server._env_int("TEST_INT", 3)
        with patch.dict(os.environ, {"TEST_INT": "70000"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "fuera de rango"):
                server._env_int("TEST_INT", 5001, 1, 65535)

    def test_branch_conventions(self):
        self.assertEqual(server.rama_de_etiqueta("v1.2.3"), "stable")
        self.assertEqual(server.rama_de_etiqueta("v1.2.3-rc1"), "rc")
        self.assertEqual(server.rama_de_etiqueta("v1.2.3-d4"), "dev")

    def test_date_parser_accepts_nanoseconds(self):
        self.assertGreater(server._a_epoch("2026-01-01T00:00:00.123456789Z"), 0)

    def test_network_boundary_rejects_non_http_urls_and_credentials(self):
        for invalid in (
            "file:///etc/passwd",
            "ftp://registry.example.test/v2/",
            "https://user:password@registry.example.test/v2/",
            "https://registry.example.test/v2/#fragment",
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                server._urlopen_http(invalid)

    def test_configured_http_bases_and_peer_lists_fail_closed(self):
        self.assertEqual(
            server._validated_http_base(
                "TEST_URL", "https://registry.example.test:5443/prefix/"
            ),
            "https://registry.example.test:5443/prefix",
        )
        for invalid in (
            "file:///etc/passwd",
            "https://user:pass@registry.example.test",
            "https://registry.example.test/path/../secret",
            "https://registry.example.test/path/%2e%2e/secret",
            "https://registry.example.test/path?query=1",
            "https://registry .example.test",
        ):
            with self.subTest(url=invalid), self.assertRaises(RuntimeError):
                server._validated_http_base("TEST_URL", invalid)
        with self.assertRaisesRegex(RuntimeError, "origen sin ruta"):
            server._validated_http_base(
                "TEST_ORIGIN", "https://panel.example.test/path", origin_only=True
            )
        with patch.dict(os.environ, {
            "TEST_PEERS": "node-a=https://a.example,node-a=https://b.example",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "duplicado"):
                server._parse_peers("TEST_PEERS")
        with patch.dict(os.environ, {"TEST_PEERS": "missing-equals"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "nombre=http"):
                server._parse_peers("TEST_PEERS")
        with patch.dict(os.environ, {"TEST_PEERS": "node-a=https://a.example,"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "entrada vacía"):
                server._parse_peers("TEST_PEERS")
        with patch.dict(os.environ, {
            "TEST_PEERS": "node-a=https://a.example,node-b=https://a.example",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "endpoint duplicado"):
                server._parse_peers("TEST_PEERS")
        with patch.dict(os.environ, {
            "TEST_ORIGINS": "https://panel.example,https://panel.example",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "duplicados"):
                server._parse_http_origins("TEST_ORIGINS")
        with patch.dict(os.environ, {"TEST_TAGS": "latest,bad/tag"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "etiquetas OCI"):
                server._parse_protected_tags("TEST_TAGS", "latest")

    def test_network_boundary_delegates_valid_https_request(self):
        request = server.urllib.request.Request("https://registry.example.test/v2/")
        marker = object()
        with patch.object(server.urllib.request, "urlopen", return_value=marker) as opener:
            self.assertIs(server._urlopen_http(request, timeout=7), marker)
        opener.assert_called_once_with(request, timeout=7)


    def test_owner_setup_login_and_session_protect_the_api(self):
        auth_file = Path(tempfile.mkdtemp(prefix="registry-auth-")) / "auth.json"
        previous_auth = server.PERSISTED_AUTH
        server.PERSISTED_AUTH = auth_file
        server._auth_attempts.clear()
        httpd = server.PanelHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
        try:
            connection.request("GET", "/api/settings")
            denied = connection.getresponse()
            self.assertEqual(denied.status, 401)
            denied.read()

            payload = json.dumps({
                "username": "owner", "display_name": "Owner",
                "password": "correct-horse-battery-staple",
                "password_confirmation": "correct-horse-battery-staple",
            })
            connection.request("POST", "/api/auth/setup", body=payload, headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{httpd.server_address[1]}",
            })
            created = connection.getresponse()
            created_body = json.loads(created.read())
            self.assertEqual(created.status, 200)
            self.assertTrue(created_body["authenticated"])
            cookie = created.getheader("Set-Cookie").split(";", 1)[0]
            self.assertIn("HttpOnly", created.getheader("Set-Cookie"))
            self.assertIn("SameSite=Strict", created.getheader("Set-Cookie"))
            self.assertEqual(auth_file.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("correct-horse", auth_file.read_text())

            connection.request("GET", "/api/not-there", headers={"Cookie": cookie})
            authorized = connection.getresponse()
            self.assertEqual(authorized.status, 404)
            authorized.read()

            connection.request("GET", "/api/settings", headers={"Cookie": cookie})
            settings = connection.getresponse()
            settings_body = json.loads(settings.read())
            self.assertEqual(settings.status, 200)
            self.assertEqual(
                set(settings_body),
                {
                    "node", "members", "timezone", "enrollment_code",
                    "cache_ttl", "cors_allowed_origins", "maintenance",
                },
            )
            self.assertEqual(
                set(settings_body["maintenance"]),
                {
                    "enabled", "keep_last", "hour", "gc", "coordinator",
                    "protected_tags", "sync_source", "lease_ttl",
                    "require_all_nodes", "branch_pattern",
                },
            )

            connection.request("DELETE", "/api/auth/session", headers={
                "Cookie": cookie,
                "Origin": f"http://127.0.0.1:{httpd.server_address[1]}",
            })
            logout = connection.getresponse()
            self.assertEqual(logout.status, 200)
            self.assertIn("Max-Age=0", logout.getheader("Set-Cookie"))
            logout.read()

            login_payload = json.dumps({
                "username": "owner", "password": "correct-horse-battery-staple",
            })
            connection.request("POST", "/api/auth/session", body=login_payload, headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{httpd.server_address[1]}",
            })
            logged_in = connection.getresponse()
            logged_in_body = json.loads(logged_in.read())
            self.assertEqual(logged_in.status, 200)
            cookie = logged_in.getheader("Set-Cookie").split(";", 1)[0]
            csrf = logged_in_body["session"]["csrf_token"]

            with patch.object(
                server, "ejecutar_accion_mantenimiento",
                return_value={"ok": True, "action": "sync", "dry_run": True},
            ) as action:
                connection.request("POST", "/api/maintenance/sync", headers={
                    "Cookie": cookie,
                    "Origin": f"http://127.0.0.1:{httpd.server_address[1]}",
                    "X-Registry-Maintenance": "1",
                })
                missing_csrf = connection.getresponse()
                self.assertEqual(missing_csrf.status, 403)
                missing_csrf.read()

                connection.request("POST", "/api/maintenance/sync", headers={
                    "Cookie": cookie,
                    "Origin": f"http://127.0.0.1:{httpd.server_address[1]}",
                    "X-Registry-Maintenance": "1",
                    "X-CSRF-Token": csrf,
                })
                accepted = connection.getresponse()
                self.assertEqual(accepted.status, 200)
                accepted.read()
                action.assert_called_once_with("sync", True)
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)
            server.PERSISTED_AUTH = previous_auth
            server._auth_attempts.clear()
            import shutil
            shutil.rmtree(auth_file.parent, ignore_errors=True)

    def test_secret_file_is_supported_and_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = Path(tmp) / "admin-token"
            secret_file.write_text("s" * 32 + "\n", encoding="utf-8")
            secret_file.chmod(0o400)
            with patch.dict(os.environ, {
                "TEST_ADMIN_TOKEN": "",
                "TEST_ADMIN_TOKEN_FILE": str(secret_file),
            }, clear=False):
                self.assertEqual(server._secret_from_env("TEST_ADMIN_TOKEN"), "s" * 32)
            with patch.dict(os.environ, {
                "TEST_ADMIN_TOKEN": "direct-value",
                "TEST_ADMIN_TOKEN_FILE": str(secret_file),
            }, clear=False):
                with self.assertRaises(RuntimeError):
                    server._secret_from_env("TEST_ADMIN_TOKEN")

    def test_secret_file_rejects_symlinks_multiple_lines_and_oversize_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = Path(tmp) / "admin-token"
            secret_file.write_text("s" * 32 + "\n", encoding="utf-8")
            secret_file.chmod(0o400)
            secret_link = Path(tmp) / "admin-token-link"
            secret_link.symlink_to(secret_file)
            with patch.dict(os.environ, {
                "TEST_ADMIN_TOKEN": "",
                "TEST_ADMIN_TOKEN_FILE": str(secret_link),
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "enlace simbólico"):
                    server._secret_from_env("TEST_ADMIN_TOKEN")

            secret_file.write_text("a" * 32 + "\n" + "b" * 32 + "\n", encoding="utf-8")
            with patch.dict(os.environ, {
                "TEST_ADMIN_TOKEN": "",
                "TEST_ADMIN_TOKEN_FILE": str(secret_file),
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "una sola línea"):
                    server._secret_from_env("TEST_ADMIN_TOKEN")

            secret_file.write_text("x" * 257, encoding="utf-8")
            with patch.dict(os.environ, {
                "TEST_ADMIN_TOKEN": "",
                "TEST_ADMIN_TOKEN_FILE": str(secret_file),
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "supera 256 caracteres"):
                    server._secret_from_env("TEST_ADMIN_TOKEN")

            secret_file.write_text("s" * 32 + "\n", encoding="utf-8")
            secret_file.chmod(0o644)
            with patch.dict(os.environ, {
                "TEST_ADMIN_TOKEN": "",
                "TEST_ADMIN_TOKEN_FILE": str(secret_file),
            }, clear=False):
                with self.assertRaisesRegex(RuntimeError, "grupo u otros"):
                    server._secret_from_env("TEST_ADMIN_TOKEN")

    def test_direct_secrets_reject_weak_or_unsafe_values(self):
        for invalid in ("short", "x" * 31, "x" * 257, "x" * 31 + "!"):
            with self.subTest(value=invalid), patch.dict(os.environ, {
                "TEST_ADMIN_TOKEN": invalid,
                "TEST_ADMIN_TOKEN_FILE": "",
            }, clear=False):
                with self.assertRaises(RuntimeError):
                    server._secret_from_env("TEST_ADMIN_TOKEN")

    def test_cluster_only_requires_the_internal_cluster_token(self):
        old_cluster = server.MAINTENANCE_CLUSTER_TOKEN
        old_node = server.NODE_NAME
        old_peers = server.PEERS
        old_panels = server.PANEL_PEERS
        try:
            server.NODE_NAME = "node-a"
            server.PEERS = {"node-b": "http://node-b:5000"}
            server.PANEL_PEERS = {"node-b": "http://node-b:5001"}
            server.MAINTENANCE_CLUSTER_TOKEN = ""
            self.assertFalse(server._config_cluster()["ready"])
            server.MAINTENANCE_CLUSTER_TOKEN = "b" * 32
            self.assertTrue(server._config_cluster()["ready"])
        finally:
            server.MAINTENANCE_CLUSTER_TOKEN = old_cluster
            server.NODE_NAME = old_node
            server.PEERS = old_peers
            server.PANEL_PEERS = old_panels

    def test_rejected_post_body_does_not_corrupt_the_next_request(self):
        previous_auth = server.PERSISTED_AUTH
        server.PERSISTED_AUTH = Path(tempfile.mkdtemp(prefix="registry-auth-")) / "missing.json"
        httpd = server.PanelHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection(*httpd.server_address, timeout=5)
        try:
            connection.request(
                "POST",
                "/api/maintenance/preview",
                body="{}",
                headers={"Content-Type": "application/json"},
            )
            rejected = connection.getresponse()
            rejected.read()
            self.assertEqual(rejected.status, 403)

            connection.request("GET", "/api/health")
            health = connection.getresponse()
            payload = json.loads(health.read())
            self.assertEqual(health.status, 200)
            self.assertTrue(payload["ok"])
        finally:
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)
            import shutil
            shutil.rmtree(server.PERSISTED_AUTH.parent, ignore_errors=True)
            server.PERSISTED_AUTH = previous_auth


class AuthReplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="registry-shared-auth-")
        self.old_auth = server.PERSISTED_AUTH
        self.old_panels = server.PANEL_PEERS
        self.old_token = server.MAINTENANCE_CLUSTER_TOKEN
        server.PERSISTED_AUTH = Path(self.directory.name) / "auth.json"
        server.PANEL_PEERS = {
            "node-a": "http://node-a:5001",
            "node-b": "http://node-b:5001",
        }
        server.MAINTENANCE_CLUSTER_TOKEN = "c" * 32

    def tearDown(self):
        server.PERSISTED_AUTH = self.old_auth
        server.PANEL_PEERS = self.old_panels
        server.MAINTENANCE_CLUSTER_TOKEN = self.old_token
        self.directory.cleanup()

    @staticmethod
    def identity(username="owner", password="correct-horse-battery-staple"):
        document = {
            "schema": 1,
            "username": username,
            "display_name": username.title(),
            "password_hash": server._hash_password(password),
            "session_version": 1,
        }
        return document, server._auth_fingerprint(document)

    def test_node_without_account_adopts_the_only_cluster_identity(self):
        identity, fingerprint = self.identity()

        def panel_request(name, path, method="GET", payload=None, timeout=30):
            self.assertEqual(path, "/api/internal/cluster/auth")
            self.assertEqual(method, "GET")
            self.assertIsNone(payload)
            self.assertEqual(timeout, 4)
            if name == "node-a":
                return {
                    "ok": True, "configured": True,
                    "account": identity, "fingerprint": fingerprint,
                }, None
            return {"ok": True, "configured": False}, None

        with patch.object(server, "_solicitud_panel", side_effect=panel_request):
            result = server._import_auth_from_cluster()

        self.assertTrue(result["imported"])
        stored = server._read_auth()
        self.assertEqual(stored["username"], "owner")
        self.assertTrue(server._verify_password(
            "correct-horse-battery-staple", stored["password_hash"],
        ))
        self.assertNotIn("session_secret", server._shared_auth_document(stored))
        self.assertEqual(server.PERSISTED_AUTH.stat().st_mode & 0o777, 0o600)

    def test_distinct_cluster_identities_are_never_overwritten(self):
        first, first_fingerprint = self.identity("owner-a")
        second, second_fingerprint = self.identity("owner-b")
        responses = {
            "node-a": {
                "ok": True, "configured": True,
                "account": first, "fingerprint": first_fingerprint,
            },
            "node-b": {
                "ok": True, "configured": True,
                "account": second, "fingerprint": second_fingerprint,
            },
        }
        with patch.object(
            server, "_solicitud_panel",
            side_effect=lambda name, *args, **kwargs: (responses[name], None),
        ):
            result = server._import_auth_from_cluster()

        self.assertTrue(result["conflict"])
        self.assertEqual(result["sources"], ["node-a", "node-b"])
        self.assertFalse(server.PERSISTED_AUTH.exists())


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.old_node = server.NODE_NAME
        self.old_peers = server.PEERS
        self.old_panel_peers = server.PANEL_PEERS
        self.old_token = server.MAINTENANCE_CLUSTER_TOKEN
        self.old_coordinator = server.MAINTENANCE_COORDINATOR
        self.old_sync_source = server.SYNC_SOURCE
        self.old_state_file = server.APP_STATE_FILE
        self.old_app_state = server._estado_persistente_snapshot()
        self.old_ultimo = dict(server._ultimo)
        self.old_lease = dict(server._cluster_lease)
        server._cluster_nonces.clear()

    def tearDown(self):
        server.NODE_NAME = self.old_node
        server.PEERS = self.old_peers
        server.PANEL_PEERS = self.old_panel_peers
        server.MAINTENANCE_CLUSTER_TOKEN = self.old_token
        server.MAINTENANCE_COORDINATOR = self.old_coordinator
        server.SYNC_SOURCE = self.old_sync_source
        server.APP_STATE_FILE = self.old_state_file
        server._app_state.clear()
        server._app_state.update(self.old_app_state)
        server._ultimo.clear()
        server._ultimo.update(self.old_ultimo)
        server._cluster_lease.clear()
        server._cluster_lease.update(self.old_lease)
        server._cluster_nonces.clear()

    def configure_two_nodes(self):
        server.NODE_NAME = "node-b"
        server.PEERS = {"node-a": "http://node-a:5000"}
        server.PANEL_PEERS = {"node-a": "http://node-a:5001"}
        server.MAINTENANCE_CLUSTER_TOKEN = "a" * 32
        server.MAINTENANCE_COORDINATOR = "node-a"

    def test_cluster_requires_panel_urls_and_shared_secret(self):
        self.configure_two_nodes()
        self.assertTrue(server._config_cluster()["ready"])
        server.PANEL_PEERS = {}
        server.MAINTENANCE_CLUSTER_TOKEN = ""
        config = server._config_cluster()
        self.assertFalse(config["ready"])
        self.assertIn("faltan paneles", "; ".join(config["problems"]))
        self.assertIn("32 caracteres", "; ".join(config["problems"]))

        self.configure_two_nodes()
        server.PANEL_PEERS["ghost"] = "http://ghost:5001"
        server.SYNC_SOURCE = "ghost"
        config = server._config_cluster()
        self.assertFalse(config["ready"])
        problems = "; ".join(config["problems"])
        self.assertIn("origen de sincronización", problems)
        self.assertIn("paneles sin registro par", problems)

    def test_role_change_preflight_does_not_require_active_maintenance(self):
        self.configure_two_nodes()
        server._app_state["coordinator"] = None
        remote = {
            "cluster": {
                "coordinator": "node-a", "nodes": ["node-a", "node-b"],
                "ready": True,
            },
            "maintenance": {"enabled": False},
        }
        with patch.object(server, "MAINT_ENABLED", False), patch.object(
            server, "_solicitud_panel", return_value=(remote, None),
        ), patch.object(server, "_fetch", return_value=(None, {})):
            self.assertEqual(server._preflight_cluster(require_maintenance=False), [])
            errors = server._preflight_cluster(require_maintenance=True)

        self.assertIn("el mantenimiento está desactivado en node-b", errors)
        self.assertIn("el mantenimiento está desactivado en node-a", errors)

    def test_internal_signature_rejects_replay(self):
        self.configure_two_nodes()
        path = "/api/internal/cluster/status"
        headers = server._cabeceras_cluster("GET", path, b"")
        ok, sender = server._peticion_cluster_autorizada("GET", path, headers, b"")
        self.assertTrue(ok)
        self.assertEqual(sender, "node-b")
        ok, reason = server._peticion_cluster_autorizada("GET", path, headers, b"")
        self.assertFalse(ok)
        self.assertEqual(reason, "petición repetida")

    def test_lease_is_exclusive_and_releasable(self):
        server.NODE_NAME = "local"
        server.PEERS = {}
        server.MAINTENANCE_COORDINATOR = "local"
        first = "1" * 32
        second = "2" * 32
        self.assertTrue(server._adquirir_lease_local(first, "local", "window", 600))
        self.assertFalse(server._adquirir_lease_local(second, "local", "window", 600))
        self.assertTrue(server._liberar_lease_local(first, "local"))
        self.assertTrue(server._adquirir_lease_local(second, "local", "window", 600))

    def test_gc_stops_before_next_node_if_registry_does_not_return(self):
        config = {
            "mode": True, "nodes": ["node-a", "node-b", "node-c"],
            "coordinator": "node-a", "is_coordinator": True,
            "ready": True, "problems": [],
        }
        local_result = {
            "action": "gc", "dry_run": False, "ok": True,
            "bytes_before": 100, "bytes_after": 80, "freed_bytes": 20,
            "registry_back": True, "output": "", "at": 1,
        }
        failed_remote = {
            "action": "gc", "dry_run": False, "ok": False,
            "bytes_before": 100, "bytes_after": 100, "freed_bytes": 0,
            "registry_back": False, "output": "", "at": 1,
        }
        server.NODE_NAME = "node-a"

        def panel_request(name, path, method="GET", payload=None, timeout=30):
            self.assertEqual(name, "node-b")
            return failed_remote, None

        with patch.object(server, "_config_cluster", return_value=config), \
             patch.object(server, "_nodos", return_value={
                 "node-a": "http://node-a:5000",
                 "node-b": "http://node-b:5000",
                 "node-c": "http://node-c:5000",
             }), \
             patch.object(server, "_renovar_leases", return_value=[]), \
             patch.object(server, "_lease_local_valida", return_value=True), \
             patch.object(server, "recolectar_basura", return_value=local_result), \
             patch.object(server, "_solicitud_panel", side_effect=panel_request), \
             patch.object(server, "_fetch", return_value=(None, {"ok": "1"})):
            result = server._recolectar_cluster(
                False, "1" * 32, ["node-a", "node-b", "node-c"],
            )

        self.assertFalse(result["registry_back"])
        self.assertTrue(result["node_results"]["node-c"]["skipped"])

    def test_state_keeps_real_run_and_preview_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            server.APP_STATE_FILE = Path(directory) / "state.json"
            server._app_state.update({
                "coordinator": "node-a", "last_run": None, "last_preview": None,
            })
            real = {"run_id": "1" * 32, "dry_run": False, "finished_at": 100}
            preview = {"run_id": "2" * 32, "dry_run": True, "finished_at": 200}
            self.assertIsNone(server._actualizar_estado_persistente("last_run", real))
            self.assertIsNone(server._actualizar_estado_persistente("last_preview", preview))

            stored = json.loads(server.APP_STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual(stored["last_run"]["finished_at"], 100)
            self.assertEqual(stored["last_preview"]["finished_at"], 200)
            self.assertEqual(server.APP_STATE_FILE.stat().st_mode & 0o777, 0o600)

    def test_interface_coordinator_overrides_environment(self):
        self.configure_two_nodes()
        server._app_state["coordinator"] = "node-b"
        config = server._config_cluster()
        self.assertEqual(config["coordinator"], "node-b")
        self.assertEqual(config["coordinator_source"], "interface")
        self.assertTrue(config["is_coordinator"])

    def test_coordinator_change_is_propagated_and_releases_old_owner(self):
        server.NODE_NAME = "node-a"
        server.PEERS = {"node-b": "http://node-b:5000"}
        server.PANEL_PEERS = {"node-b": "http://node-b:5001"}
        server.MAINTENANCE_CLUSTER_TOKEN = "a" * 32
        server.MAINTENANCE_COORDINATOR = "node-a"
        server._app_state.update({
            "coordinator": None, "last_run": None, "last_preview": None,
        })
        with tempfile.TemporaryDirectory() as directory:
            server.APP_STATE_FILE = Path(directory) / "state.json"
            requests = []
            releases = []

            def panel_request(name, path, method="GET", payload=None, timeout=30):
                requests.append((name, path, payload))
                return {"ok": True}, None

            with patch.object(
                server, "_adquirir_leases", return_value=(["node-a", "node-b"], []),
            ), patch.object(
                server, "_solicitud_panel", side_effect=panel_request,
            ), patch.object(
                server, "_liberar_leases",
                side_effect=lambda run_id, nodes, owner=None: releases.append(owner),
            ):
                result = server._cambiar_coordinador_coordinado("node-b")

            self.assertTrue(result["ok"])
            self.assertEqual(server._config_cluster()["coordinator"], "node-b")
            self.assertEqual(requests[0][1], "/api/internal/cluster/config/coordinator")
            self.assertEqual(requests[0][2]["coordinator"], "node-b")
            self.assertEqual(releases, ["node-a"])

    def test_automatic_sync_source_is_the_coordinator(self):
        self.configure_two_nodes()
        server.SYNC_SOURCE = ""
        server._app_state["coordinator"] = None
        inventories = {
            "http://node-a:5000": {"app": {"stable": digest("a")}},
            server.REGISTRY_URL: {"app": {"stable": digest("b")}},
        }
        with patch.object(server, "MAINT_ENABLED", True), patch.object(
            server, "_fetch", return_value=(None, {}),
        ), patch.object(
            server, "_inventario", side_effect=lambda base: inventories[base],
        ), patch.object(
            server, "_a_descartar", return_value=set(),
        ), patch.object(
            server, "_copiar_etiqueta", return_value=(True, ""),
        ) as copy:
            result = server.sincronizar(False)

        self.assertEqual(result["mismatch_count"], 1)
        copy.assert_called_once_with(
            "http://node-a:5000", server.REGISTRY_URL, "app", "stable",
        )

    def test_window_summary_records_time_origin_and_metrics(self):
        result = {
            "run_id": "1" * 32,
            "dry_run": False,
            "ok": True,
            "coordinator": "node-a",
            "nodes": ["node-a", "node-b"],
            "at": 130,
            "progress": {
                "started_at": 100, "finished_at": 130,
                "message": "Mantenimiento completado correctamente.",
                "steps": [
                    {"id": "retention", "status": "completed"},
                    {"id": "gc", "status": "completed"},
                    {"id": "sync", "status": "completed"},
                ],
            },
            "steps": {
                "retention": {"removed": ["app:v1"]},
                "gc": {"ok": True, "registry_back": True, "freed_bytes": 1024},
                "sync": {"copied": ["app:v2"], "verification": {"ok": True}},
            },
        }
        summary = server._resumen_ventana(result, "scheduled")
        self.assertEqual(summary["duration_seconds"], 30)
        self.assertEqual(summary["trigger"], "scheduled")
        self.assertEqual(summary["retention"]["count"], 1)
        self.assertEqual(summary["gc"]["freed_bytes"], 1024)
        self.assertTrue(summary["sync"]["verified"])


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="registry-setup-"))
        self.patchers = [
            patch.object(server, "PERSISTED_CONFIG", self.temp / "config.json"),
            patch.object(server, "PERSISTED_SECRET_DIR", self.temp / "secrets"),
            patch.object(server, "PERSISTED_CLUSTER_TOKEN", self.temp / "secrets/cluster-token"),
            patch.object(server, "SETUP_REQUIRED", True),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        import shutil
        shutil.rmtree(self.temp, ignore_errors=True)

    @staticmethod
    def payload(code=""):
        return {
            "node": "node-a",
            "members": [
                {"name": "node-a", "registry_url": "http://registry-a:5000", "panel_url": ""},
                {"name": "node-b", "registry_url": "http://registry-b:5000",
                 "panel_url": "http://registry-b:5001"},
            ],
            "timezone": "UTC", "enrollment_code": code,
            "cache_ttl": 45,
            "cors_allowed_origins": "https://registry-admin.example",
            "maintenance": {
                "enabled": True, "keep_last": 3, "hour": "03:30", "gc": True,
                "coordinator": "node-a", "protected_tags": "latest,stable",
                "sync_source": "node-a", "lease_ttl": 900,
                "require_all_nodes": True,
                "branch_pattern": r"^v[0-9]+-(?P<branch>[a-z]+)$",
            },
        }

    def test_setup_generates_a_private_cluster_token_and_join_code(self):
        result = server._save_initial_setup(self.payload())
        self.assertTrue(result["enrollment_code"])
        self.assertNotIn("admin_token", result)
        self.assertEqual(server.PERSISTED_CLUSTER_TOKEN.stat().st_mode & 0o777, 0o600)
        raw = server.PERSISTED_CONFIG.read_text()
        self.assertNotIn(server.PERSISTED_CLUSTER_TOKEN.read_text().strip(), raw)
        environment = json.loads(raw)["environment"]
        self.assertEqual(environment["CACHE_TTL"], "45")
        self.assertEqual(environment["MAINTENANCE_LEASE_TTL"], "900")
        self.assertEqual(environment["RETENTION_REQUIRE_ALL_NODES"], "1")
        self.assertEqual(
            environment["CORS_ALLOWED_ORIGINS"], "https://registry-admin.example",
        )

    def test_join_code_reuses_the_cluster_credential(self):
        first = server._save_initial_setup(self.payload())
        cluster = server.PERSISTED_CLUSTER_TOKEN.read_text()
        import shutil
        shutil.rmtree(self.temp)
        self.temp.mkdir()
        payload = self.payload(first["enrollment_code"])
        payload["node"] = "node-b"
        payload["members"][0]["panel_url"] = "http://registry-a:5001"
        payload["members"][1]["panel_url"] = ""
        joined = server._save_initial_setup(payload)
        self.assertEqual(joined["enrollment_code"], "")
        self.assertEqual(server.PERSISTED_CLUSTER_TOKEN.read_text(), cluster)

    def test_the_local_member_does_not_need_public_urls(self):
        payload = self.payload()
        payload["members"][0]["registry_url"] = ""
        payload["members"][0]["panel_url"] = ""
        document, _ = server._validate_setup(payload)
        environment = document["environment"]
        self.assertNotIn("node-a=", environment["PEERS"])
        self.assertNotIn("node-a=", environment["PANEL_PEERS"])
        self.assertIn("node-b=http://registry-b:5000", environment["PEERS"])

    def test_settings_report_the_effective_coordinator(self):
        with patch.object(server, "MAINTENANCE_COORDINATOR", ""), patch.object(
            server, "_config_cluster", return_value={"coordinator": "node-a"},
        ):
            self.assertEqual(
                server._settings_payload()["maintenance"]["coordinator"], "node-a",
            )

    def test_dashboard_coordinator_is_also_persisted_in_configuration(self):
        server._save_initial_setup(self.payload())
        previous_state_file = server.APP_STATE_FILE
        previous_state = server._estado_persistente_snapshot()
        try:
            server.APP_STATE_FILE = self.temp / "state.json"
            server._app_state.update({
                "coordinator": "node-a", "last_run": None, "last_preview": None,
            })
            with patch.object(server, "_nodos", return_value={
                "node-a": "http://registry-a:5000",
                "node-b": "http://registry-b:5000",
            }):
                self.assertIsNone(server._aplicar_coordinador_local("node-b"))

            environment = json.loads(
                server.PERSISTED_CONFIG.read_text(encoding="utf-8"),
            )["environment"]
            self.assertEqual(environment["MAINTENANCE_COORDINATOR"], "node-b")
            self.assertEqual(server._config_cluster()["coordinator"], "node-b")
        finally:
            server.APP_STATE_FILE = previous_state_file
            server._app_state.clear()
            server._app_state.update(previous_state)

    def test_settings_can_change_every_persisted_value_and_keep_the_cluster_code(self):
        first = server._save_initial_setup(self.payload())
        cluster = server.PERSISTED_CLUSTER_TOKEN.read_text().strip()
        payload = self.payload("")
        payload["cache_ttl"] = 120
        payload["cors_allowed_origins"] = ""
        payload["maintenance"]["lease_ttl"] = 1800
        payload["maintenance"]["require_all_nodes"] = False
        payload["maintenance"]["branch_pattern"] = ""
        with patch.object(server, "MAINTENANCE_CLUSTER_TOKEN", cluster):
            result = server._save_settings(payload)

        self.assertTrue(result["restarting"])
        self.assertEqual(server.PERSISTED_CLUSTER_TOKEN.read_text().strip(), cluster)
        environment = json.loads(server.PERSISTED_CONFIG.read_text())["environment"]
        self.assertEqual(set(environment), server.PERSISTED_ENV_KEYS)
        self.assertEqual(environment["CACHE_TTL"], "120")
        self.assertEqual(environment["MAINTENANCE_LEASE_TTL"], "1800")
        self.assertEqual(environment["RETENTION_REQUIRE_ALL_NODES"], "0")
        self.assertEqual(environment["RETENTION_BRANCH_PATTERN"], "")

    def test_persisted_configuration_replaces_legacy_template_values(self):
        document, _ = server._validate_setup(self.payload())
        server.PERSISTED_CONFIG.write_text(json.dumps(document), encoding="utf-8")
        legacy_token = self.temp / "legacy-token"
        legacy_token.write_text("l" * 48, encoding="utf-8")
        legacy_token.chmod(0o600)
        legacy = {
            "NODE_NAME": "legacy-node",
            "CACHE_TTL": "999",
            "MAINTENANCE_CLUSTER_TOKEN": "d" * 48,
            "MAINTENANCE_CLUSTER_TOKEN_FILE": str(legacy_token),
        }
        with patch.dict(os.environ, legacy, clear=False):
            self.assertTrue(server._load_persisted_environment())
            self.assertEqual(os.environ["NODE_NAME"], "node-a")
            self.assertEqual(os.environ["CACHE_TTL"], "45")
            self.assertNotIn("MAINTENANCE_CLUSTER_TOKEN", os.environ)
            self.assertEqual(
                os.environ["MAINTENANCE_CLUSTER_TOKEN_FILE"],
                str(server.PERSISTED_CLUSTER_TOKEN),
            )


if __name__ == "__main__":
    unittest.main()
