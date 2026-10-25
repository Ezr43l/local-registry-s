"""Private TLS, machine credentials, session revocation and bounded HTTP clients.

Copyright 2026 Ezrael. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DATA_ROOT = Path(os.environ.get("LOCAL_REGISTRY_CONFIG_FILE", "/var/lib/registry/.local-registry/config.json")).parent
SECURITY_DIR = DATA_ROOT / "security"
POLICY_FILE = SECURITY_DIR / "policy.json"
MAX_REPLY = 4 * 1024 * 1024
_lock = threading.RLock()


def audit(event: str, **fields) -> None:
    print(json.dumps({"at": int(time.time()), "event": event, **fields}, ensure_ascii=True, separators=(",", ":")), flush=True)


def private_read(path: Path, maximum: int = 1024 * 1024) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077 or meta.st_size > maximum:
            raise RuntimeError("fichero de seguridad con permisos, tipo o tamaño inválidos")
        data = os.read(fd, maximum + 1)
        if len(data) > maximum:
            raise RuntimeError("fichero de seguridad demasiado grande")
        return data
    finally:
        os.close(fd)


def private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError("la seguridad no admite enlaces simbólicos")
    temp = path.parent / (".write-" + secrets.token_hex(12))
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _save_policy(document: dict) -> None:
    raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    if len(raw) > 60000:
        raise ValueError("la lista de permisos supera el tamaño seguro del grupo")
    private_write(POLICY_FILE, raw)


def policy() -> dict:
    doc = json.loads(private_read(POLICY_FILE))
    if not isinstance(doc, dict) or set(doc) != {"schema", "allowed_networks", "tls_names", "clients", "generation"} or doc["schema"] != 1:
        raise RuntimeError("política de seguridad desconocida")
    if not isinstance(doc["clients"], list) or len(doc["clients"]) > 128:
        raise RuntimeError("demasiadas credenciales")
    validate_networks(doc["allowed_networks"])
    validate_names(doc["tls_names"])
    for client in doc["clients"]:
        validate_client(client)
    return doc


def validate_networks(values) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("indica entre una y 64 redes autorizadas")
    result = []
    for value in values:
        network = ipaddress.ip_network(str(value).strip(), strict=False)
        if network.prefixlen == 0:
            raise ValueError("no se permite autorizar todo Internet")
        result.append(str(network))
    for value in ["127.0.0.1/32", "::1/128"]:
        if value not in result:
            result.append(value)
    if len(result) > 64:
        raise ValueError("demasiadas redes autorizadas")
    return sorted(set(result))


def validate_names(values) -> list[str]:
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError("demasiadas direcciones de acceso")
    result = {"localhost", "127.0.0.1", "::1"}
    for value in values:
        name = str(value).strip()
        try:
            name = str(ipaddress.ip_address(name))
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", name):
                raise ValueError("las direcciones del certificado deben ser IP o nombres DNS sin puerto") from None
        result.add(name)
    if len(result) > 64:
        raise ValueError("demasiadas direcciones de acceso")
    return sorted(result)


def validate_client(client) -> dict:
    keys = {"id", "name", "username", "token_hash", "permissions", "repositories"}
    if not isinstance(client, dict) or set(client) != keys:
        raise ValueError("credencial con formato desconocido")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", str(client["id"])) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", str(client["username"])):
        raise ValueError("identificador de credencial inválido")
    if not isinstance(client["name"], str) or not 1 <= len(client["name"]) <= 120:
        raise ValueError("nombre de aplicación inválido")
    if not re.fullmatch(r"[0-9a-f]{64}", str(client["token_hash"])):
        raise ValueError("huella de credencial inválida")
    if not isinstance(client["permissions"], list) or not client["permissions"] or not set(client["permissions"]) <= {"pull", "push", "delete", "admin"}:
        raise ValueError("permisos inválidos")
    repos = client["repositories"]
    if not isinstance(repos, list) or not 1 <= len(repos) <= 64:
        raise ValueError("indica los repositorios autorizados")
    for repo in repos:
        if not isinstance(repo, str) or len(repo) > 255 or not re.fullmatch(r"\*|[a-z0-9][a-z0-9._/-]*(?:/\*)?", repo) or ".." in repo or "//" in repo:
            raise ValueError("repositorio no válido; usa nombre, espacio/* o *")
    return client


def machine_credentials(cluster_token: str | None = None) -> tuple[str, str]:
    if cluster_token is None:
        token_file = DATA_ROOT / "secrets" / "cluster-token"
        if token_file.exists():
            cluster_token = private_read(token_file, 258).decode().strip()
    if cluster_token:
        token = base64.urlsafe_b64encode(hmac.new(cluster_token.encode(), b"local-registry-machine-v1", hashlib.sha256).digest()).decode().rstrip("=")
    else:
        path = SECURITY_DIR / "machine-token"
        if not path.exists():
            private_write(path, secrets.token_urlsafe(48).encode())
        token = private_read(path, 256).decode()
    return "local-registry-cluster", token


def refresh_certificate() -> None:
    names = policy()["tls_names"]
    subprocess.run(["/bin/secure-registry", "--mode=init", "--dir=" + str(SECURITY_DIR), "--names=" + ",".join(names)], check=True, timeout=15, capture_output=True)


def prepare(cluster_token: str | None = None) -> None:
    with _lock:
        SECURITY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        if SECURITY_DIR.is_symlink():
            raise RuntimeError("directorio de seguridad inválido")
        os.chmod(SECURITY_DIR, 0o700)
        if POLICY_FILE.exists():
            doc = policy()
        else:
            names = ["localhost", "127.0.0.1", "::1", socket.gethostname()]
            for key in ["HOST_HOSTNAME", "LOCAL_REGISTRY_TLS_NAMES"]:
                names.extend(filter(None, os.environ.get(key, "").split(",")))
            doc = {"schema": 1, "allowed_networks": ["127.0.0.1/32", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"], "tls_names": validate_names(names), "clients": [], "generation": 1}
        username, token = machine_credentials(cluster_token)
        system = {"id": "system", "name": "Mantenimiento y réplica del clúster", "username": username, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "permissions": ["admin"], "repositories": ["*"]}
        doc["clients"] = [system] + [c for c in doc["clients"] if c["id"] != "system"]
        _save_policy(doc)
        if not (DATA_ROOT / "auth.json").exists() and not (SECURITY_DIR / "bootstrap-code").exists():
            private_write(SECURITY_DIR / "bootstrap-code", (secrets.token_urlsafe(32) + "\n").encode())
        refresh_certificate()


def bootstrap_valid(value: str) -> bool:
    try:
        expected = private_read(SECURITY_DIR / "bootstrap-code", 256).decode().strip()
    except FileNotFoundError:
        return False
    return isinstance(value, str) and hmac.compare_digest(value, expected)


def bootstrap_consumed() -> None:
    (SECURITY_DIR / "bootstrap-code").unlink(missing_ok=True)


def public_settings() -> dict:
    doc = policy()
    return {"tls_names": doc["tls_names"], "allowed_networks": doc["allowed_networks"], "generation": doc["generation"], "clients": [{k: v for k, v in c.items() if k != "token_hash"} for c in doc["clients"] if c["id"] != "system"], "ca_fingerprint": hashlib.sha256(ssl.PEM_cert_to_DER_cert(private_read(SECURITY_DIR / "ca.pem").decode())).hexdigest()}


def save_settings(payload) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"tls_names", "allowed_networks"}:
        raise ValueError("configuración de seguridad incompleta")
    with _lock:
        doc = policy()
        doc["allowed_networks"] = validate_networks(payload["allowed_networks"])
        doc["tls_names"] = validate_names(payload["tls_names"])
        previous = policy()
        doc["generation"] += 1
        _save_policy(doc)
        try:
            refresh_certificate()
        except Exception:
            _save_policy(previous)
            raise
    audit("security_settings_changed")
    return {"ok": True, "restarting": True}


def change_client(payload) -> dict:
    if not isinstance(payload, dict) or payload.get("action") not in {"create", "delete"}:
        raise ValueError("acción de credencial inválida")
    with _lock:
        doc = policy()
        if payload["action"] == "delete":
            ident = str(payload.get("id", ""))
            if ident == "system" or not any(c["id"] == ident for c in doc["clients"]):
                raise ValueError("aplicación no encontrada")
            doc["clients"] = [c for c in doc["clients"] if c["id"] != ident]
            result = {"ok": True}
        else:
            if len(doc["clients"]) >= 128:
                raise ValueError("límite de aplicaciones alcanzado")
            ident = secrets.token_hex(12)
            token = secrets.token_urlsafe(48)
            client = validate_client({"id": ident, "name": str(payload.get("name", "")).strip(), "username": "lr_" + ident, "token_hash": hashlib.sha256(token.encode()).hexdigest(), "permissions": payload.get("permissions"), "repositories": payload.get("repositories")})
            doc["clients"].append(client)
            result = {"ok": True, "username": client["username"], "token": token}
        doc["generation"] += 1
        _save_policy(doc)
    audit("registry_credential_" + payload["action"], client=ident)
    return result


def credential_snapshot() -> dict:
    doc = policy()
    return {"generation": doc["generation"], "clients": [c for c in doc["clients"] if c["id"] != "system"]}


def install_credentials(payload) -> None:
    if not isinstance(payload, dict) or set(payload) != {"generation", "clients"} or not isinstance(payload["generation"], int) or not isinstance(payload["clients"], list) or len(payload["clients"]) > 127:
        raise ValueError("lista de credenciales inválida")
    clients = [validate_client(c) for c in payload["clients"]]
    if any(c["id"] == "system" for c in clients) or len({c["id"] for c in clients}) != len(clients) or len({c["username"] for c in clients}) != len(clients):
        raise ValueError("credenciales duplicadas o reservadas")
    with _lock:
        doc = policy()
        if [c for c in doc["clients"] if c["id"] != "system"] == clients:
            return
        doc["clients"] = [c for c in doc["clients"] if c["id"] == "system"] + clients
        doc["generation"] = max(doc["generation"] + 1, payload["generation"])
        _save_policy(doc)


def export_enrollment() -> dict:
    return {"ca_certificate": private_read(SECURITY_DIR / "ca.pem").decode(), "ca_private_key": private_read(SECURITY_DIR / "ca-key.pem").decode()}


def import_enrollment(value) -> None:
    if not isinstance(value, dict) or set(value) != {"ca_certificate", "ca_private_key"}:
        raise ValueError("el código no incluye la identidad segura del clúster")
    for key, filename in [("ca_certificate", "ca.pem"), ("ca_private_key", "ca-key.pem")]:
        text = value[key]
        if not isinstance(text, str) or not 100 <= len(text) <= 8192 or not text.startswith("-----BEGIN "):
            raise ValueError("certificado de incorporación no válido")
    # Validate the pair before replacing the trusted identity.
    trial = SECURITY_DIR / (".enroll-" + secrets.token_hex(8))
    trial.mkdir(mode=0o700, parents=True)
    try:
        private_write(trial / "ca.pem", value["ca_certificate"].encode())
        private_write(trial / "ca-key.pem", value["ca_private_key"].encode())
        subprocess.run(["/bin/secure-registry", "--mode=init", "--dir=" + str(trial)], check=True, timeout=15, capture_output=True)
        private_write(SECURITY_DIR / "ca.pem", value["ca_certificate"].encode())
        private_write(SECURITY_DIR / "ca-key.pem", value["ca_private_key"].encode())
    finally:
        for path in trial.iterdir():
            path.unlink()
        trial.rmdir()


def allowed_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in ipaddress.ip_network(value) for value in policy()["allowed_networks"])
    except (OSError, ValueError, RuntimeError):
        return False


def tls_context(server: bool = False) -> ssl.SSLContext:
    if server:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(SECURITY_DIR / "server.pem"))
        return context
    return ssl.create_default_context(cafile=str(SECURITY_DIR / "ca.pem"))


def origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("destino HTTP inválido")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise ValueError("redirección a otro origen rechazada")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BoundedResponse:
    def __init__(self, response, streaming: bool = False):
        self.response = response
        self.remaining = 20 * 1024**3 if streaming else MAX_REPLY

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.response.close()

    def read(self, size: int = -1):
        requested = min(self.remaining + 1, MAX_REPLY + 1 if size < 0 else size)
        data = self.response.read(requested)
        self.remaining -= len(data)
        if self.remaining < 0 or (size < 0 and len(data) > MAX_REPLY):
            self.response.close()
            raise ValueError("respuesta del par demasiado grande")
        return data


def open_http(request, registry_origins: set, panel_origins: set, **kwargs):
    req = urllib.request.Request(request) if isinstance(request, str) else request
    target = origin(req.full_url)
    known_registries = {origin(value) for value in registry_origins}
    known_panels = {origin(value) for value in panel_origins}
    if target not in known_registries | known_panels:
        raise ValueError("destino fuera de los nodos autorizados")
    if target[0] != "https":
        raise ValueError("los nodos deben usar HTTPS con certificado verificado")
    if target in known_registries:
        username, token = machine_credentials()
        req.add_header("Authorization", "Basic " + base64.b64encode((username + ":" + token).encode()).decode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=tls_context()), SameOriginRedirect())
    response = opener.open(req, **kwargs)
    streaming = "/blobs/" in urllib.parse.urlsplit(req.full_url).path and req.get_method() == "GET"
    return BoundedResponse(response, streaming)


def session_register(nonce: str, expires: int) -> None:
    with _lock:
        path = SECURITY_DIR / "sessions.json"
        try:
            sessions = json.loads(private_read(path))
        except FileNotFoundError:
            sessions = {}
        sessions = {k: v for k, v in sessions.items() if isinstance(v, int) and v > time.time()}
        if len(sessions) >= 128:
            oldest = min(sessions, key=sessions.get)
            del sessions[oldest]
        sessions[nonce] = expires
        private_write(path, json.dumps(sessions).encode())


def session_valid(nonce: str, expires: int) -> bool:
    with _lock:
        try:
            sessions = json.loads(private_read(SECURITY_DIR / "sessions.json"))
        except FileNotFoundError:
            return False
        return sessions.get(nonce) == expires and expires > time.time()


def session_revoke(nonce: str) -> None:
    with _lock:
        path = SECURITY_DIR / "sessions.json"
        try:
            sessions = json.loads(private_read(path))
        except FileNotFoundError:
            return
        sessions.pop(nonce, None)
        private_write(path, json.dumps(sessions).encode())


if __name__ == "__main__":
    if sys.argv[1:] == ["prepare"]:
        prepare()
    elif sys.argv[1:] == ["docker-credentials"]:
        username, token = machine_credentials()
        print(json.dumps({"username": username, "password": token, "ca": private_read(SECURITY_DIR / "ca.pem").decode()}))
    elif sys.argv[1:] == ["docker-client"]:
        # Root of this container is already a trusted administrator. Issue a
        # scoped publisher for the local Docker engine, never the system key.
        import server
        cache = SECURITY_DIR / "launcher-client.json"
        try:
            saved = json.loads(private_read(cache))
            current = next((c for c in policy()["clients"] if c["username"] == saved["username"]), None)
            if current is None or current["token_hash"] != hashlib.sha256(saved["password"].encode()).hexdigest():
                raise ValueError("credencial revocada")
        except (FileNotFoundError, ValueError, KeyError):
            with contextlib.redirect_stdout(sys.stderr):
                issued = server._change_registry_client({"action": "create", "name": "Zero Launcher / " + os.environ.get("HOST_HOSTNAME", socket.gethostname()), "permissions": ["pull", "push"], "repositories": ["*"]})
            if issued.get("replication_errors"):
                raise SystemExit("No se ha confirmado la credencial en todos los nodos; revisa el grupo antes de continuar")
            saved = {"username": issued["username"], "password": issued["token"]}
            private_write(cache, json.dumps(saved).encode())
        print(json.dumps(saved))
    elif sys.argv[1:] == ["health"]:
        with urllib.request.urlopen("http://127.0.0.1:5050/v2/", timeout=3) as response:
            if response.status != 200:
                raise SystemExit(1)
        with open_http("https://127.0.0.1:5000/v2/", {"https://127.0.0.1:5000"}, set(), timeout=3) as response:
            if response.status != 200:
                raise SystemExit(1)
    elif sys.argv[1:] == ["panel-health"]:
        with urllib.request.urlopen("https://127.0.0.1:" + os.environ.get("PORT", "5001") + "/api/health", context=tls_context(), timeout=4) as response:
            print(response.read(MAX_REPLY).decode())
    elif sys.argv[1:] == ["catalog"]:
        base = os.environ.get("REGISTRY_URL", "https://127.0.0.1:5000")
        with open_http(base + "/v2/_catalog", {base}, set(), timeout=8) as response:
            print(response.read().decode())
    elif len(sys.argv) == 4 and sys.argv[1] == "digest":
        base = os.environ.get("REGISTRY_URL", "https://127.0.0.1:5000")
        repo, tag = sys.argv[2:]
        req = urllib.request.Request(base + "/v2/" + urllib.parse.quote(repo, safe='/') + "/manifests/" + urllib.parse.quote(tag, safe=''), method="HEAD")
        try:
            with open_http(req, {base}, set(), timeout=8) as response:
                print(response.headers.get('Docker-Content-Digest', ''))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    else:
        raise SystemExit("Uso: security.py prepare | docker-client | docker-credentials | health | panel-health | catalog | digest REPO TAG")
