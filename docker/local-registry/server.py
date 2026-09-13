#!/usr/bin/env python3
"""panel del registro — qué guarda, cuánto ocupa de verdad y dónde no cuadra.

Sirve dos cosas en el mismo puerto: la API JSON bajo /api/ y la página que la
consume. La página es un consumidor puro —aquí no se renderiza HTML— para que
cambiar de tecnología en el navegador algún día no obligue a tocar nada de esto.

De dónde salen los datos:

  · El DISCO. El panel vive DENTRO del contenedor del registro, así que lee sus
    ficheros directamente. Es la única fuente que sabe lo que se ocupa DE VERDAD
    y la única que ve los blobs huérfanos: la API del registro no sabe enumerar
    lo que hay guardado, solo lo que está referenciado por alguna etiqueta.
  · La API del registro (/v2/...). Da el catálogo, las etiquetas y, por cada
    etiqueta, qué blobs usa y cuánto pesa cada uno.
  · Los nodos pares configurados, por HTTP, para comparar digests y detectar deriva.

La cuenta que importa: las capas SE COMPARTEN entre etiquetas. Muchas versiones
de una misma aplicación comparten casi toda su base, así que sumar el tamaño que
declara cada manifiesto cuenta la misma capa una y otra vez y da cifras
absurdas. Aquí se cuenta por digest único y se separa en dos:

  · exclusivo  — blobs que solo usa ese proyecto. Es lo que recuperarías de
                 verdad si lo borraras entero.
  · compartido — blobs que comparte con otros proyectos. Borrarlo no los libera.
"""
import hashlib
import hmac
import base64
import binascii
import json
import os
import re
import secrets
import signal
import stat
# Subprocess is restricted to fixed argv, no shell and an absolute executable.
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} debe ser un booleano explícito (0/1)")


def _env_int(
    name: str,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} debe ser un entero") from None
    if value < minimum or (maximum is not None and value > maximum):
        interval = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
        raise RuntimeError(f"{name} está fuera de rango ({interval})")
    return value


def _validated_secret(name: str, value: str) -> str:
    if not value:
        return ""
    if not (32 <= len(value) <= 256):
        raise RuntimeError(f"{name} debe tener entre 32 y 256 caracteres")
    if re.fullmatch(r"[-a-zA-Z0-9._~]+", value) is None:
        raise RuntimeError(f"{name} contiene caracteres no permitidos")
    return value


NODE_NAME_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}")
OCI_TAG_RE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}")


def _validated_node_name(name: str, value: str, *, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if allow_empty and not normalized:
        return ""
    if NODE_NAME_RE.fullmatch(normalized) is None:
        raise RuntimeError(f"{name} no es un nombre de nodo válido")
    return normalized


def _secret_from_env(name: str) -> str:
    """Carga un secreto directo o desde fichero, nunca desde ambos a la vez."""
    direct = os.environ.get(name, "").strip()
    secret_file = os.environ.get(f"{name}_FILE", "").strip()
    if direct and secret_file:
        raise RuntimeError(f"{name} y {name}_FILE son excluyentes")
    if not secret_file:
        return _validated_secret(name, direct)
    path = Path(secret_file)
    if path.is_symlink():
        raise RuntimeError(f"{name}_FILE no puede ser un enlace simbólico")
    descriptor: int | None = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"{name}_FILE debe ser un fichero regular")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError(f"{name}_FILE no puede ser legible por grupo u otros")
        if metadata.st_size > 258:
            raise RuntimeError(f"{name}_FILE supera 256 caracteres y su salto final")
        raw = os.read(descriptor, 259)
    except OSError as error:
        raise RuntimeError(f"no se puede leer {name}_FILE: {type(error).__name__}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > 258:
        raise RuntimeError(f"{name}_FILE supera 256 caracteres y su salto final")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"{name}_FILE no contiene UTF-8 válido") from error
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if "\r" in value or "\n" in value:
        raise RuntimeError(f"{name}_FILE debe contener una sola línea")
    if len(value) > 256:
        raise RuntimeError(f"{name}_FILE supera 256 caracteres")
    return _validated_secret(name, value)


def _validated_http_base(name: str, value: str, *, origin_only: bool = False) -> str:
    normalized = value.strip().rstrip("/")
    if (
        not normalized
        or len(normalized) > 2048
        or "\\" in normalized
        or any(character.isspace() for character in normalized)
    ):
        raise RuntimeError(f"{name} contiene una URL vacía, larga o no portable")
    try:
        parsed = urllib.parse.urlsplit(normalized)
        parsed_port = parsed.port
    except ValueError as error:
        raise RuntimeError(f"{name} contiene una URL inválida") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"{name} debe usar una URL HTTP o HTTPS absoluta")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} no admite credenciales, query ni fragmento")
    if parsed_port is not None and not (1 <= parsed_port <= 65535):
        raise RuntimeError(f"{name} contiene un puerto fuera de rango")
    decoded_path = urllib.parse.unquote(parsed.path)
    if re.search(r"(^|/)\.\.(/|$)", decoded_path):
        raise RuntimeError(f"{name} contiene traversal de ruta")
    if origin_only and parsed.path not in {"", "/"}:
        raise RuntimeError(f"{name} debe ser un origen sin ruta")
    return normalized


def _parse_peers(name: str) -> dict[str, str]:
    raw_value = os.environ.get(name, "")
    if not raw_value.strip():
        return {}
    if len(raw_value) > 8192:
        raise RuntimeError(f"{name} supera la longitud máxima")
    raw_peers = raw_value.split(",")
    if len(raw_peers) > 64 or any(not raw_peer.strip() for raw_peer in raw_peers):
        raise RuntimeError(f"{name} contiene demasiados nodos o una entrada vacía")
    peers: dict[str, str] = {}
    endpoints: set[str] = set()
    for raw_peer in raw_peers:
        if "=" not in raw_peer:
            raise RuntimeError(f"{name} debe usar nombre=http(s)://host:puerto")
        peer_name, raw_url = (part.strip() for part in raw_peer.split("=", 1))
        peer_name = _validated_node_name(name, peer_name)
        if peer_name in peers:
            raise RuntimeError(f"{name} contiene el nodo duplicado {peer_name}")
        endpoint = _validated_http_base(name, raw_url)
        if endpoint in endpoints:
            raise RuntimeError(f"{name} contiene el endpoint duplicado {endpoint}")
        endpoints.add(endpoint)
        peers[peer_name] = endpoint
    return peers


def _parse_http_origins(name: str) -> set[str]:
    raw_value = os.environ.get(name, "")
    if not raw_value.strip():
        return set()
    if len(raw_value) > 2048:
        raise RuntimeError(f"{name} supera la longitud máxima")
    raw_origins = raw_value.split(",")
    if len(raw_origins) > 32 or any(not origin.strip() for origin in raw_origins):
        raise RuntimeError(f"{name} contiene demasiados orígenes o una entrada vacía")
    origins = {
        _validated_http_base(name, origin, origin_only=True)
        for origin in raw_origins
    }
    if len(origins) != len(raw_origins):
        raise RuntimeError(f"{name} contiene orígenes duplicados")
    return origins


def _parse_protected_tags(name: str, default: str) -> set[str]:
    raw_value = os.environ.get(name, default)
    if not raw_value.strip():
        return set()
    if len(raw_value) > 8192:
        raise RuntimeError(f"{name} supera la longitud máxima")
    raw_tags = raw_value.split(",")
    if len(raw_tags) > 64 or any(not tag.strip() for tag in raw_tags):
        raise RuntimeError(f"{name} contiene demasiadas etiquetas o una entrada vacía")
    tags = {tag.strip() for tag in raw_tags}
    if len(tags) != len(raw_tags) or any(OCI_TAG_RE.fullmatch(tag) is None for tag in tags):
        raise RuntimeError(f"{name} contiene etiquetas OCI inválidas o duplicadas")
    return tags


PERSISTED_CONFIG = Path(os.environ.get(
    "LOCAL_REGISTRY_CONFIG_FILE",
    "/var/lib/registry/.local-registry/config.json",
))
PERSISTED_SECRET_DIR = PERSISTED_CONFIG.parent / "secrets"
PERSISTED_CLUSTER_TOKEN = PERSISTED_SECRET_DIR / "cluster-token"
PERSISTED_AUTH = PERSISTED_CONFIG.parent / "auth.json"
PERSISTED_ENV_KEYS = {
    "NODE_NAME", "PEERS", "PANEL_PEERS", "CACHE_TTL",
    "MAINTENANCE_ENABLED", "KEEP_LAST", "PROTECTED_TAGS",
    "MAINTENANCE_HOUR", "MAINTENANCE_GC", "MAINTENANCE_COORDINATOR",
    "MAINTENANCE_LEASE_TTL", "RETENTION_REQUIRE_ALL_NODES",
    "RETENTION_BRANCH_PATTERN", "SYNC_SOURCE", "CORS_ALLOWED_ORIGINS", "TZ",
}


def _load_persisted_environment() -> bool:
    """Carga la configuración del panel antes de fijar sus constantes."""
    if not PERSISTED_CONFIG.exists():
        return False
    try:
        metadata = PERSISTED_CONFIG.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("LOCAL_REGISTRY_CONFIG_FILE debe ser un fichero regular")
        if metadata.st_size > 128 * 1024:
            raise RuntimeError("LOCAL_REGISTRY_CONFIG_FILE supera el tamaño permitido")
        document = json.loads(PERSISTED_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise RuntimeError(f"no se puede leer la configuración persistente: {error}") from error
    if not isinstance(document, dict) or set(document) != {"schema", "environment"}:
        raise RuntimeError("la configuración persistente tiene un contrato desconocido")
    environment = document.get("environment")
    if document.get("schema") != 1 or not isinstance(environment, dict):
        raise RuntimeError("la configuración persistente usa un esquema no compatible")
    if set(environment) != PERSISTED_ENV_KEYS or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()):
        raise RuntimeError("la configuración persistente está incompleta")
    # En cuanto existe configuración gestionada por la WebUI, ésta es la fuente
    # de verdad. Así, las variables heredadas de una plantilla antigua no
    # impiden cambiar posteriormente los valores desde la aplicación.
    for key, value in environment.items():
        os.environ[key] = value
    # El código de incorporación también se administra desde la WebUI. Al
    # migrar una instalación antigua no debe seguir ganando un secreto montado
    # por la plantilla, pues haría que el formulario pareciese guardar el cambio
    # pero el siguiente arranque recuperase el valor anterior.
    os.environ.pop("MAINTENANCE_CLUSTER_TOKEN", None)
    os.environ["MAINTENANCE_CLUSTER_TOKEN_FILE"] = str(PERSISTED_CLUSTER_TOKEN)
    return True


PERSISTED_CONFIG_LOADED = _load_persisted_environment()
if PERSISTED_CONFIG_LOADED and hasattr(time, "tzset"):
    time.tzset()
LEGACY_CONFIGURATION = not PERSISTED_CONFIG_LOADED and (
    "NODE_NAME" in os.environ or "PEERS" in os.environ
)
SETUP_REQUIRED = not PERSISTED_CONFIG_LOADED and not LEGACY_CONFIGURATION


REGISTRY_URL = _validated_http_base(
    "REGISTRY_URL", os.environ.get("REGISTRY_URL", "http://127.0.0.1:5000")
)
REGISTRY_DATA = Path(os.environ.get("REGISTRY_DATA", "/var/lib/registry"))
NODE_NAME = _validated_node_name("NODE_NAME", os.environ.get("NODE_NAME", "local"))
APP_VERSION = os.environ.get("APP_VERSION", "v1.2.14").strip() or "v1.2.14"
PORT = _env_int("PORT", 5001, 1, 65535)
CACHE_TTL = _env_int("CACHE_TTL", 300, 0, 86400)
WEB_ROOT = Path(os.environ.get("WEB_ROOT", "/app/web"))
CORS_ORIGINS = _parse_http_origins("CORS_ALLOWED_ORIGINS")

PEERS = _parse_peers("PEERS")

# Los registros y los paneles usan puertos distintos. PEERS sigue describiendo
# exclusivamente la API OCI; PANEL_PEERS permite que los paneles coordinen las
# tareas locales que no se pueden ejecutar a través de esa API (sobre todo GC).
PANEL_PEERS = _parse_peers("PANEL_PEERS")

# Se piden todos los formatos que un registro puede devolver. Sin esto, el
# registro contesta con el esquema 1 (obsoleto), que no trae los tamaños.
ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _header(headers, name: str, default: str = "") -> str:
    """Lee una cabecera HTTP sin depender de cómo escriba sus mayúsculas."""
    wanted = name.lower()
    for key, value in (headers or {}).items():
        if key.lower() == wanted:
            return value
    return default


def _repo_url(base: str, repo: str, suffix: str) -> str:
    """Construye rutas de la API sin interpretar un nombre como una URL."""
    return f"{base}/v2/{urllib.parse.quote(repo, safe='/')}/{suffix}"


def _ref_url(base: str, repo: str, ref: str) -> str:
    return _repo_url(base, repo, f"manifests/{urllib.parse.quote(ref, safe='')}")


def _urlopen_http(request: str | urllib.request.Request, **kwargs):
    """Open only an explicit HTTP(S) request.

    Registry peers are administrator-supplied, but validating again at the
    transport boundary prevents urllib from accepting file or custom schemes.
    """
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("la URL del registro debe usar HTTP o HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("la URL del registro no admite credenciales ni fragmentos")
    return urllib.request.urlopen(request, **kwargs)  # nosec B310


# ─────────────────────────── utilidades de red ───────────────────────────────

def _fetch(url: str, method: str = "GET", timeout: int = 15):
    """Devuelve (cuerpo_json_o_None, cabeceras) o (None, None) si falla."""
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", ACCEPT)
    try:
        with _urlopen_http(req, timeout=timeout) as r:
            headers = dict(r.headers)
            if method == "HEAD":
                return None, headers
            return json.loads(r.read() or b"null"), headers
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None, None


def _paginado(base: str, ruta: str, clave: str) -> list[str]:
    """Recorre un listado del registro siguiendo la cabecera Link.

    NO se pide un tamaño de página: el registro rechaza los valores grandes con
    un 400 (probado: n=1000 pasa, n=5000 no), y sin el parámetro devuelve el
    listado entero de una vez. Aun así se sigue 'Link: rel=next' si aparece,
    porque el día que el catálogo crezca y el registro empiece a paginar, no
    hacerlo truncaría los datos sin avisar de nada.
    """
    out: list[str] = []
    url = f"{base}{ruta}"
    visto: set[str] = set()
    while url and url not in visto:
        visto.add(url)
        body, headers = _fetch(url)
        if body is None:
            break
        out.extend(body.get(clave) or [])
        enlace = _header(headers, "Link")
        m = re.search(r'<([^>]+)>;\s*rel="next"', enlace)
        url = urllib.parse.urljoin(base, m.group(1)) if m else None
    return sorted(out)


def catalog(base: str) -> list[str]:
    return _paginado(base, "/v2/_catalog", "repositories")


def tags_of(base: str, repo: str) -> list[str]:
    return _paginado(base, _repo_url(base, repo, "tags/list").removeprefix(base), "tags")


def manifest_of(base: str, repo: str, tag: str, vistos: set[str] | None = None):
    """(digest_del_manifiesto, lista_de_blobs) donde cada blob es (digest, bytes).

    Si la etiqueta es multiarquitectura, el manifiesto es un índice que apunta a
    otros manifiestos; se entra en cada uno para no perder sus capas.
    """
    if vistos is None:
        vistos = set()
    ref_key = f"{repo}:{tag}"
    if ref_key in vistos:
        return None, []
    vistos.add(ref_key)
    body, headers = _fetch(_ref_url(base, repo, tag))
    if body is None or headers is None:
        return None, []
    digest = _header(headers, "Docker-Content-Digest")
    blobs: list[tuple[str, int]] = []

    # El propio manifiesto ocupa disco y es un blob más. Sin contarlo, todos los
    # manifiestos parecerían huérfanos al cruzar con el disco.
    if DIGEST_RE.match(digest):
        blobs.append((digest, 0))

    hijos = body.get("manifests")
    if hijos:  # índice multiarquitectura
        for h in hijos:
            d = h.get("digest", "")
            if DIGEST_RE.match(d):
                _, sub = manifest_of(base, repo, d, vistos)
                blobs.extend(sub)
        return digest, blobs

    cfg = body.get("config") or {}
    if DIGEST_RE.match(cfg.get("digest", "")):
        blobs.append((cfg["digest"], int(cfg.get("size") or 0)))
    for capa in body.get("layers") or []:
        if DIGEST_RE.match(capa.get("digest", "")):
            blobs.append((capa["digest"], int(capa.get("size") or 0)))
    return digest, blobs


# ─────────────────────────── lectura del disco ───────────────────────────────

def blobs_en_disco() -> dict[str, int]:
    """digest → bytes reales. Vacío si el volumen no está montado.

    Ruta: <raiz>/docker/registry/v2/blobs/sha256/<2 primeros>/<digest>/data
    El nombre del directorio ES el digest, así que no hay que abrir nada.
    """
    raiz = REGISTRY_DATA / "docker" / "registry" / "v2" / "blobs" / "sha256"
    if not raiz.is_dir():
        return {}
    out: dict[str, int] = {}
    for prefijo in raiz.iterdir():
        if not prefijo.is_dir():
            continue
        for dir_blob in prefijo.iterdir():
            data = dir_blob / "data"
            try:
                out[f"sha256:{dir_blob.name}"] = data.stat().st_size
            except OSError:
                continue
    return out


# ─────────────────────────── el cálculo ──────────────────────────────────────

def recolectar() -> dict:
    inicio = time.time()
    disco = blobs_en_disco()
    proyectos = catalog(REGISTRY_URL)

    # repo → {tag: digest_manifiesto} y digest_blob → {repos que lo usan}
    tags_por_repo: dict[str, dict[str, str]] = {}
    refs: dict[str, set[str]] = {}
    tam_declarado: dict[str, int] = {}

    def trabajo(par):
        repo, tag = par
        return repo, tag, manifest_of(REGISTRY_URL, repo, tag)

    pares = []
    for repo in proyectos:
        tags_por_repo[repo] = {}
        for tag in tags_of(REGISTRY_URL, repo):
            pares.append((repo, tag))

    with ThreadPoolExecutor(max_workers=16) as pool:
        for repo, tag, (digest, blobs) in pool.map(trabajo, pares):
            tags_por_repo[repo][tag] = digest or ""
            for d, size in blobs:
                refs.setdefault(d, set()).add(repo)
                if size:
                    tam_declarado[d] = size

    def bytes_de(d: str) -> int:
        # El disco manda; el tamaño declarado solo se usa si el volumen no está.
        return disco.get(d, tam_declarado.get(d, 0))

    salida_proyectos = []
    for repo in proyectos:
        exclusivo = compartido = 0
        n_blobs = 0
        for d, quien in refs.items():
            if repo not in quien:
                continue
            n_blobs += 1
            if len(quien) == 1:
                exclusivo += bytes_de(d)
            else:
                compartido += bytes_de(d)
        tags = tags_por_repo.get(repo, {})
        salida_proyectos.append({
            "name": repo,
            "tags": len(tags),
            "blobs": n_blobs,
            "exclusive_bytes": exclusivo,
            "shared_bytes": compartido,
            "total_bytes": exclusivo + compartido,
            "tag_names": sorted(tags.keys()),
        })
    salida_proyectos.sort(key=lambda p: p["exclusive_bytes"], reverse=True)

    referenciados = set(refs.keys())
    en_disco = set(disco.keys())
    huerfanos = en_disco - referenciados
    # Referenciado pero ausente del disco: no debería pasar nunca. Si pasa, el
    # registro está corrupto o alguien ha borrado a mano, y hay que saberlo.
    faltantes = referenciados - en_disco if disco else set()

    return {
        "node": NODE_NAME,
        "registry": REGISTRY_URL,
        "generated_at": int(time.time()),
        "took_ms": int((time.time() - inicio) * 1000),
        "disk_available": bool(disco),
        "summary": {
            "projects": len(proyectos),
            "tags": sum(len(t) for t in tags_por_repo.values()),
            "blobs_on_disk": len(en_disco),
            "blobs_referenced": len(referenciados),
            "total_bytes": sum(disco.values()) if disco else sum(tam_declarado.values()),
            "orphan_blobs": len(huerfanos),
            "orphan_bytes": sum(disco.get(d, 0) for d in huerfanos),
            "missing_blobs": len(faltantes),
        },
        "projects": salida_proyectos,
        "_tags": tags_por_repo,  # interno, para el cálculo de deriva
    }


def calcular_deriva(local: dict) -> dict:
    """Compara este nodo con sus pares: qué falta y qué difiere.

    Se compara el DIGEST, no la mera presencia de la etiqueta: 'stable' y 'dev'
    son flotantes, existen siempre y cambian de imagen en cada versión. Mirar
    solo si la etiqueta está daría por sincronizado justo lo que acaba de
    quedarse viejo.
    """
    nodos: dict[str, dict[str, dict[str, str]]] = {local["node"]: local["_tags"]}

    # Primero los catálogos y listas de etiquetas de cada par: son pocas
    # llamadas. Después TODAS las consultas de digest a la vez: son cientos, y en
    # serie tardaban lo suyo aunque cada una sea rápida.
    tareas: list[tuple[str, str, str, str]] = []
    for nombre, base in PEERS.items():
        datos: dict[str, dict[str, str]] = {}
        for repo in catalog(base):
            datos[repo] = {}
            for tag in tags_of(base, repo):
                datos[repo][tag] = ""
                tareas.append((nombre, base, repo, tag))
        nodos[nombre] = datos

    def digest_remoto(t):
        nombre, base, repo, tag = t
        _, headers = _fetch(_ref_url(base, repo, tag), method="HEAD")
        return nombre, repo, tag, _header(headers, "Docker-Content-Digest")

    if tareas:
        with ThreadPoolExecutor(max_workers=24) as pool:
            for nombre, repo, tag, dg in pool.map(digest_remoto, tareas):
                nodos[nombre][repo][tag] = dg

    # Se separan dos cosas que NO son igual de graves, y mezclarlas hace inútil
    # el resultado:
    #
    #   · discrepancia (mismatch) — la misma etiqueta apunta a imágenes DISTINTAS
    #     según el nodo. Eso siempre es un problema: desplegar en un servidor u
    #     otro te da cosas diferentes.
    #   · ausencia (missing) — la etiqueta solo está en algunos nodos. Suele ser
    #     lo esperado: el histórico de versiones se queda donde se construye y no
    #     se replica a propósito. Es información, no una avería.
    #
    # Si se cuentan juntas, cientos de ausencias normales entierran la única
    # discrepancia que sí importaba.
    todos_repos = sorted({r for d in nodos.values() for r in d})
    discrepancias, ausencias = [], []
    for repo in todos_repos:
        etiquetas = sorted({t for d in nodos.values() for t in d.get(repo, {})})
        for tag in etiquetas:
            presentes = {n: d.get(repo, {}).get(tag, "") for n, d in nodos.items()}
            con_valor = {n: v for n, v in presentes.items() if v}
            distintos = len(set(con_valor.values())) > 1
            if len(con_valor) == len(nodos) and not distintos:
                continue  # todos iguales: nada que decir
            entrada = {
                "project": repo,
                "tag": tag,
                "nodes": {n: (v[:19] if v else None) for n, v in presentes.items()},
            }
            (discrepancias if distintos else ausencias).append(entrada)

    return {
        "nodes": sorted(nodos.keys()),
        # «Coherente» = ninguna etiqueta significa algo distinto según el nodo.
        # Es la señal de salud de verdad; las ausencias van aparte.
        "consistent": len(discrepancias) == 0,
        "mismatch_count": len(discrepancias),
        "missing_count": len(ausencias),
        "mismatches": discrepancias,
        "missing": ausencias,
    }


# ─────────────────────────── caché ───────────────────────────────────────────
#
# Recorrer todas las etiquetas son varios cientos de peticiones. Rápido, pero no
# para repetirlo en cada refresco del navegador. Se guarda unos minutos y se
# puede forzar con ?refresh=1.

# RLock y no Lock: el cálculo de deriva necesita los datos locales, así que
# entra en la caché estando ya dentro de ella. Con un Lock normal eso es un
# interbloqueo —el mismo hilo esperándose a sí mismo— y la petición se cuelga
# para siempre sin dar error ni dejar rastro en los logs. Pasó, y costó verlo
# justo porque no falla: se queda quieta.
_lock = threading.RLock()
_cache: dict[str, tuple[float, dict]] = {}


def cacheado(clave: str, fn, refrescar: bool):
    with _lock:
        if not refrescar and clave in _cache:
            ts, valor = _cache[clave]
            if time.time() - ts < CACHE_TTL:
                return valor
        valor = fn()
        _cache[clave] = (time.time(), valor)
        return valor


def datos_locales(refrescar: bool) -> dict:
    return cacheado("local", recolectar, refrescar)


def datos_deriva(refrescar: bool) -> dict:
    return cacheado("drift", lambda: calcular_deriva(datos_locales(refrescar)), refrescar)


# ─────────────────────────── mantenimiento ───────────────────────────────────
#
# Aquí es donde esto deja de solo mirar y empieza a borrar. Tres reglas que no se
# negocian:
#
#   1. NADA se borra sin que lo pidas explícitamente. Todo es simulación por
#      omisión; para que borre de verdad hay que mandar confirm=1.
#   2. La recolección para el registro mientras corre. Si alguien empuja una
#      imagen mientras el recolector decide que un blob no lo usa nadie, ese blob
#      desaparece y la imagen queda rota. Es el único fallo de esto que no tiene
#      arreglo, así que se evita por diseño y no por suerte.
#   3. La retención nunca toca las etiquetas protegidas ni corre sin un
#      KEEP_LAST explícito. Borrar versiones es irreversible.

MAINT_FLAG = Path(os.environ.get("MAINTENANCE_FLAG", "/run/registry-maintenance.flag"))
MAINT_ENABLED = _env_bool("MAINTENANCE_ENABLED", False)
REGISTRY_CONFIG = os.environ.get("REGISTRY_CONFIG", "/etc/docker/registry/config.yml")
KEEP_LAST = _env_int("KEEP_LAST", 0, 0, 100000)
PROTECTED = _parse_protected_tags("PROTECTED_TAGS", "latest")
RETENTION_REQUIRE_ALL_NODES = _env_bool("RETENTION_REQUIRE_ALL_NODES", True)
RETENTION_BRANCH_PATTERN = os.environ.get("RETENTION_BRANCH_PATTERN", "").strip()
if len(RETENTION_BRANCH_PATTERN) > 512:
    raise RuntimeError("RETENTION_BRANCH_PATTERN supera 512 caracteres")
SYNC_SOURCE = _validated_node_name(
    "SYNC_SOURCE", os.environ.get("SYNC_SOURCE", ""), allow_empty=True
)


MAINTENANCE_CLUSTER_TOKEN = _secret_from_env("MAINTENANCE_CLUSTER_TOKEN")
MAINTENANCE_COORDINATOR = _validated_node_name(
    "MAINTENANCE_COORDINATOR",
    os.environ.get("MAINTENANCE_COORDINATOR", ""),
    allow_empty=True,
)
MAINTENANCE_LEASE_TTL = _env_int(
    "MAINTENANCE_LEASE_TTL", 7200, 300, 31536000
)
CLUSTER_REQUEST_MAX_AGE = _env_int("CLUSTER_REQUEST_MAX_AGE", 90, 30, 3600)
APP_STATE_FILE = Path(os.environ.get(
    "APP_STATE_FILE", str(REGISTRY_DATA / ".local-registry" / "state.json"),
))


def _setup_text(value, name: str, maximum: int = 2048, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise RuntimeError(f"{name} debe ser texto")
    normalized = value.strip()
    if required and not normalized:
        raise RuntimeError(f"{name} es obligatorio")
    if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise RuntimeError(f"{name} contiene caracteres no permitidos")
    return normalized


def _setup_bool(value, name: str) -> bool:
    if type(value) is not bool:
        raise RuntimeError(f"{name} debe ser booleano")
    return value


def _setup_int(value, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{name} no es válido")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} no es válido") from error
    if not minimum <= number <= maximum:
        raise RuntimeError(f"{name} está fuera de rango ({minimum}..{maximum})")
    return number


def _setup_members(raw_members, local_node: str) -> tuple[list[dict], str, str]:
    if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= 64:
        raise RuntimeError("declara entre 1 y 64 nodos")
    members: list[dict] = []
    names: set[str] = set()
    registry_endpoints: set[str] = set()
    panel_endpoints: set[str] = set()
    for raw in raw_members:
        if not isinstance(raw, dict) or set(raw) != {"name", "registry_url", "panel_url"}:
            raise RuntimeError("cada nodo debe incluir name, registry_url y panel_url")
        name = _validated_node_name("nombre de nodo", _setup_text(raw["name"], "nombre de nodo", 64, True))
        if name in names:
            raise RuntimeError(f"el nodo {name} está repetido")
        names.add(name)
        registry_url = _setup_text(
            raw["registry_url"], "URL del registro", required=name != local_node,
        )
        if registry_url:
            registry_url = _validated_http_base("URL del registro", registry_url)
        elif name == local_node:
            # La dirección pública del propio nodo no se consume localmente.
            # Los demás miembros sí deben declararla en sus respectivas listas.
            registry_url = REGISTRY_URL
        panel_url = _setup_text(raw["panel_url"], "URL del panel")
        if name != local_node:
            panel_url = _validated_http_base("URL del panel", panel_url)
        elif panel_url:
            panel_url = _validated_http_base("URL del panel", panel_url)
        if registry_url in registry_endpoints or (panel_url and panel_url in panel_endpoints):
            raise RuntimeError("las URLs de los nodos no pueden repetirse")
        registry_endpoints.add(registry_url)
        if panel_url:
            panel_endpoints.add(panel_url)
        members.append({"name": name, "registry_url": registry_url, "panel_url": panel_url})
    if local_node not in names:
        raise RuntimeError("la lista de nodos debe incluir este nodo")
    peers = ",".join(
        f'{member["name"]}={member["registry_url"]}'
        for member in members if member["name"] != local_node
    )
    panel_peers = ",".join(
        f'{member["name"]}={member["panel_url"]}'
        for member in members if member["name"] != local_node
    )
    return members, peers, panel_peers


def _setup_origins(value) -> str:
    raw = _setup_text(value, "orígenes CORS", 2048)
    if not raw:
        return ""
    parts = raw.split(",")
    if len(parts) > 32 or any(not part.strip() for part in parts):
        raise RuntimeError("los orígenes CORS contienen una entrada vacía o demasiados valores")
    normalized = [
        _validated_http_base("origen CORS", part, origin_only=True)
        for part in parts
    ]
    if len(set(normalized)) != len(normalized):
        raise RuntimeError("los orígenes CORS están repetidos")
    return ",".join(normalized)


def _decode_enrollment(code: str) -> str:
    try:
        padding = "=" * (-len(code) % 4)
        document = json.loads(base64.urlsafe_b64decode((code + padding).encode()).decode())
    except (binascii.Error, ValueError, UnicodeError) as error:
        raise RuntimeError("el código de incorporación no es válido") from error
    if not isinstance(document, dict):
        raise RuntimeError("el código de incorporación no pertenece a Local Registry")
    if document.get("schema") == 2 and set(document) == {"schema", "cluster"}:
        return _validated_secret("token de clúster", str(document.get("cluster") or ""))
    if document.get("schema") != 1 or set(document) != {"schema", "admin", "cluster"}:
        raise RuntimeError("el código de incorporación usa otro esquema")
    # Compatibilidad de incorporación con códigos generados antes del portal.
    _validated_secret("token administrativo heredado", str(document.get("admin") or ""))
    cluster = _validated_secret("token de clúster", str(document.get("cluster") or ""))
    return cluster


def _encode_enrollment(cluster: str) -> str:
    raw = json.dumps({"schema": 2, "cluster": cluster},
                     separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


AUTH_COOKIE = "local_registry_session"
AUTH_SESSION_SECONDS = 12 * 60 * 60
AUTH_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{2,63}")
_auth_lock = threading.RLock()
_auth_attempts: dict[str, list[float]] = {}


class AuthError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    if not re.fullmatch(r"[-_A-Za-z0-9]+", value or ""):
        raise ValueError("base64 no válido")
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def _normalize_username(value) -> str:
    username = str(value or "").strip().lower()
    if AUTH_USERNAME_RE.fullmatch(username) is None:
        raise AuthError("El usuario debe tener entre 3 y 64 caracteres válidos")
    return username


def _validate_password(password, username: str, confirmation=None) -> str:
    value = str(password or "")
    if confirmation is not None and value != str(confirmation or ""):
        raise AuthError("Las contraseñas no coinciden")
    if not 12 <= len(value) <= 128 or any(ord(character) < 32 for character in value):
        raise AuthError("La contraseña debe tener entre 12 y 128 caracteres")
    if username.casefold() in value.casefold():
        raise AuthError("La contraseña no puede contener el nombre de usuario")
    return value


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1,
        maxmem=64 * 1024 * 1024, dklen=32,
    )
    return f"scrypt$16384$8$1${_urlsafe_encode(salt)}${_urlsafe_encode(digest)}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        salt, expected = _urlsafe_decode(raw_salt), _urlsafe_decode(raw_digest)
        if algorithm != "scrypt" or (n, r, p) != (16384, 8, 1):
            return False
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
            maxmem=64 * 1024 * 1024, dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, binascii.Error, UnicodeError):
        return False


def _password_hash_valid(encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        return (
            algorithm == "scrypt"
            and (int(raw_n), int(raw_r), int(raw_p)) == (16384, 8, 1)
            and len(_urlsafe_decode(raw_salt)) == 16
            and len(_urlsafe_decode(raw_digest)) == 32
        )
    except (TypeError, ValueError, binascii.Error):
        return False


def _validate_auth_document(document) -> dict:
    required = {
        "schema", "username", "display_name", "password_hash",
        "session_secret", "session_version",
    }
    if not isinstance(document, dict) or set(document) != required or document.get("schema") != 1:
        raise RuntimeError("la cuenta de acceso tiene un contrato desconocido")
    username = _normalize_username(document.get("username"))
    display_name = str(document.get("display_name") or "").strip()
    password_hash = str(document.get("password_hash") or "")
    session_secret = str(document.get("session_secret") or "")
    session_version = document.get("session_version")
    if (
        not display_name or len(display_name) > 120
        or not _password_hash_valid(password_hash)
        or not 32 <= len(session_secret) <= 256
        or not isinstance(session_version, int) or session_version < 1
    ):
        raise RuntimeError("la cuenta de acceso está incompleta")
    try:
        if len(_urlsafe_decode(session_secret)) < 32:
            raise ValueError("secreto corto")
    except (ValueError, binascii.Error) as error:
        raise RuntimeError("el secreto de sesión no es válido") from error
    return {
        **document,
        "username": username,
        "display_name": display_name,
        "password_hash": password_hash,
        "session_secret": session_secret,
        "session_version": session_version,
    }


def _read_auth() -> dict | None:
    if not PERSISTED_AUTH.exists():
        return None
    try:
        metadata = PERSISTED_AUTH.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("el fichero de acceso debe ser regular")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("el fichero de acceso tiene permisos inseguros")
        if metadata.st_size > 16384:
            raise RuntimeError("el fichero de acceso supera el tamaño permitido")
        document = json.loads(PERSISTED_AUTH.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise RuntimeError(f"no se puede leer la cuenta de acceso: {error}") from error
    return _validate_auth_document(document)


def _shared_auth_document(document) -> dict:
    """Devuelve la identidad replicable sin el secreto local de las sesiones."""
    validated = _validate_auth_document(document)
    return {
        "schema": 1,
        "username": validated["username"],
        "display_name": validated["display_name"],
        "password_hash": validated["password_hash"],
        "session_version": validated["session_version"],
    }


def _validate_shared_auth(document) -> dict:
    required = {"schema", "username", "display_name", "password_hash", "session_version"}
    if not isinstance(document, dict) or set(document) != required or document.get("schema") != 1:
        raise AuthError("La identidad compartida tiene un contrato desconocido")
    username = _normalize_username(document.get("username"))
    display_name = str(document.get("display_name") or "").strip()
    password_hash = str(document.get("password_hash") or "")
    session_version = document.get("session_version")
    if (
        not display_name or len(display_name) > 120
        or not _password_hash_valid(password_hash)
        or not isinstance(session_version, int) or session_version < 1
    ):
        raise AuthError("La identidad compartida está incompleta")
    return {
        "schema": 1,
        "username": username,
        "display_name": display_name,
        "password_hash": password_hash,
        "session_version": session_version,
    }


def _auth_fingerprint(document) -> str:
    shared = _validate_shared_auth(document)
    canonical = json.dumps(
        shared, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _cluster_auth_snapshot() -> dict:
    document = _read_auth()
    if not document:
        return {"ok": True, "node": NODE_NAME, "configured": False}
    shared = _shared_auth_document(document)
    return {
        "ok": True,
        "node": NODE_NAME,
        "configured": True,
        "fingerprint": _auth_fingerprint(shared),
        "account": shared,
    }


def _install_shared_auth(document, fingerprint: str) -> bool:
    shared = _validate_shared_auth(document)
    expected = _auth_fingerprint(shared)
    if not re.fullmatch(r"[0-9a-f]{64}", str(fingerprint or "")) or not hmac.compare_digest(
            expected, str(fingerprint)):
        raise AuthError("La huella de la identidad compartida no es válida")
    with _auth_lock:
        current = _read_auth()
        if current:
            if not hmac.compare_digest(
                    _auth_fingerprint(_shared_auth_document(current)), expected):
                raise AuthError(
                    "Existe otra cuenta propietaria en este nodo; no se ha sobrescrito",
                    409,
                )
            return False
        local_document = {
            **shared,
            # Cada nodo firma sus propias cookies. Sólo la cuenta y su clave
            # derivada forman parte de la identidad común del clúster.
            "session_secret": secrets.token_urlsafe(48),
        }
        _atomic_write(PERSISTED_AUTH, (
            json.dumps(local_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"))
        return True


def _import_auth_from_cluster() -> dict:
    """Adopta la única identidad existente; nunca decide entre dos distintas."""
    if _read_auth() or not PANEL_PEERS or len(MAINTENANCE_CLUSTER_TOKEN) < 32:
        return {"imported": False, "conflict": False, "sources": []}
    candidates: dict[str, tuple[dict, list[str]]] = {}
    for name in sorted(PANEL_PEERS):
        response, error = _solicitud_panel(
            name, "/api/internal/cluster/auth", timeout=4,
        )
        if error or not isinstance(response, dict) or not response.get("configured"):
            continue
        try:
            shared = _validate_shared_auth(response.get("account"))
            fingerprint = str(response.get("fingerprint") or "")
            if not hmac.compare_digest(_auth_fingerprint(shared), fingerprint):
                continue
        except (AuthError, TypeError):
            continue
        if fingerprint not in candidates:
            candidates[fingerprint] = (shared, [])
        candidates[fingerprint][1].append(name)
    if len(candidates) > 1:
        return {
            "imported": False,
            "conflict": True,
            "sources": sorted(name for _, names in candidates.values() for name in names),
        }
    if not candidates:
        return {"imported": False, "conflict": False, "sources": []}
    fingerprint, (shared, sources) = next(iter(candidates.items()))
    imported = _install_shared_auth(shared, fingerprint)
    return {"imported": imported, "conflict": False, "sources": sorted(sources)}


def _create_session(document: dict) -> tuple[str, dict]:
    now = int(time.time())
    public = {
        "username": document["username"],
        "display_name": document["display_name"],
        "csrf_token": secrets.token_urlsafe(24),
        "expires_at": now + AUTH_SESSION_SECONDS,
    }
    body = {
        "sub": public["username"], "name": public["display_name"],
        "csrf": public["csrf_token"], "iat": now, "exp": public["expires_at"],
        "sv": document["session_version"], "nonce": secrets.token_hex(12),
    }
    encoded = _urlsafe_encode(json.dumps(
        body, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    signature = _urlsafe_encode(hmac.new(
        _urlsafe_decode(document["session_secret"]), encoded.encode("ascii"), hashlib.sha256,
    ).digest())
    return f"{encoded}.{signature}", public


def _read_session(token: str) -> dict | None:
    if not token or len(token) > 4096 or token.count(".") != 1:
        return None
    document = _read_auth()
    if not document:
        return None
    encoded, signature = token.split(".", 1)
    expected = _urlsafe_encode(hmac.new(
        _urlsafe_decode(document["session_secret"]), encoded.encode("ascii"), hashlib.sha256,
    ).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        body = json.loads(_urlsafe_decode(encoded).decode("utf-8"))
        username = str(body["sub"])
        display_name = str(body["name"])
        csrf = str(body["csrf"])
        expires_at = int(body["exp"])
        session_version = int(body["sv"])
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        expires_at <= int(time.time())
        or not hmac.compare_digest(username, document["username"])
        or session_version != document["session_version"]
    ):
        return None
    return {
        "username": username, "display_name": display_name,
        "csrf_token": csrf, "expires_at": expires_at,
    }


def _auth_status(token: str = "") -> dict:  # nosec B107: ausencia explícita de sesión
    document = _read_auth()
    if not document:
        imported = _import_auth_from_cluster()
        if imported["conflict"]:
            raise RuntimeError(
                "hay identidades propietarias distintas en el clúster; no se ha sobrescrito ninguna"
            )
        document = _read_auth()
    if not document:
        return {"setup_required": True, "authenticated": False, "session": None}
    session = _read_session(token)
    return {
        "setup_required": False,
        "authenticated": session is not None,
        "session": session,
    }


def _auth_setup(payload) -> tuple[str, dict]:
    if not isinstance(payload, dict) or set(payload) != {
            "username", "display_name", "password", "password_confirmation"}:
        raise AuthError("El formulario de acceso está incompleto")
    username = _normalize_username(payload.get("username"))
    password = _validate_password(
        payload.get("password"), username, payload.get("password_confirmation"),
    )
    display_name = " ".join(str(payload.get("display_name") or username).split())
    if not display_name or len(display_name) > 120:
        raise AuthError("El nombre visible no es válido")
    with _auth_lock:
        if PERSISTED_AUTH.exists():
            raise AuthError("La cuenta propietaria ya está configurada", 409)
        document = {
            "schema": 1, "username": username, "display_name": display_name,
            "password_hash": _hash_password(password),
            "session_secret": secrets.token_urlsafe(48), "session_version": 1,
        }
        _atomic_write(PERSISTED_AUTH, (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"))
        return _create_session(document)


def _auth_login(payload, origin: str) -> tuple[str, dict]:
    if not isinstance(payload, dict) or set(payload) != {"username", "password"}:
        raise AuthError("El formulario de acceso está incompleto")
    document = _read_auth()
    if not document:
        raise AuthError("Primero debes crear la cuenta propietaria", 409)
    try:
        username = _normalize_username(payload.get("username"))
    except AuthError:
        username = ""
    key = f"{origin}|{username or '<invalid>'}"
    now = time.monotonic()
    with _auth_lock:
        attempts = [instant for instant in _auth_attempts.get(key, []) if now - instant < 300]
        if len(attempts) >= 5:
            _auth_attempts[key] = attempts
            raise AuthError("Demasiados intentos. Espera cinco minutos", 429)
        password_valid = _verify_password(
            str(payload.get("password") or ""), document["password_hash"],
        )
        valid = bool(username) and hmac.compare_digest(
            username, document["username"],
        ) and password_valid
        if not valid:
            attempts.append(now)
            _auth_attempts[key] = attempts
            raise AuthError("Usuario o contraseña no válidos", 401)
        _auth_attempts.pop(key, None)
    return _create_session(document)


def _validate_setup(payload) -> tuple[dict, str]:
    if not isinstance(payload, dict) or set(payload) != {
            "node", "members", "maintenance", "timezone", "enrollment_code",
            "cache_ttl", "cors_allowed_origins"}:
        raise RuntimeError("el formulario está incompleto o contiene campos desconocidos")
    node = _validated_node_name(
        "nombre de este nodo", _setup_text(payload["node"], "nombre de este nodo", 64, True),
    )
    members, peers, panel_peers = _setup_members(payload["members"], node)
    maintenance = payload["maintenance"]
    if not isinstance(maintenance, dict) or set(maintenance) != {
            "enabled", "keep_last", "hour", "gc", "coordinator",
            "protected_tags", "sync_source", "lease_ttl",
            "require_all_nodes", "branch_pattern"}:
        raise RuntimeError("la configuración de mantenimiento está incompleta")
    enabled = _setup_bool(maintenance["enabled"], "mantenimiento")
    gc_enabled = _setup_bool(maintenance["gc"], "recolección")
    require_all_nodes = _setup_bool(
        maintenance["require_all_nodes"], "retención segura con pares",
    )
    keep_last = _setup_int(maintenance["keep_last"], "versiones a conservar", 0, 100000)
    cache_ttl = _setup_int(payload["cache_ttl"], "segundos de caché", 0, 86400)
    lease_ttl = _setup_int(
        maintenance["lease_ttl"], "caducidad de la reserva", 300, 31536000,
    )
    hour = _setup_text(maintenance["hour"], "hora de mantenimiento", 5)
    if hour and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", hour):
        raise RuntimeError("la hora de mantenimiento debe usar HH:MM")
    tags = _setup_text(maintenance["protected_tags"], "etiquetas protegidas", 8192)
    protected_tags = (tags or "latest").split(",")
    normalized_tags = [tag.strip() for tag in protected_tags]
    if (len(protected_tags) > 64 or any(not tag for tag in normalized_tags)
            or len(set(normalized_tags)) != len(normalized_tags)
            or any(OCI_TAG_RE.fullmatch(tag) is None for tag in normalized_tags)):
        raise RuntimeError("las etiquetas protegidas no son válidas o están repetidas")
    coordinator = _setup_text(maintenance["coordinator"], "coordinador", 64)
    coordinator = _validated_node_name("coordinador", coordinator or sorted(m["name"] for m in members)[0])
    if coordinator not in {member["name"] for member in members}:
        raise RuntimeError("el coordinador debe pertenecer al clúster")
    source = _setup_text(maintenance["sync_source"], "origen de sincronización", 64)
    if source:
        source = _validated_node_name("origen de sincronización", source)
        if source not in {member["name"] for member in members}:
            raise RuntimeError("el origen de sincronización debe pertenecer al clúster")
    branch_pattern = _setup_text(
        maintenance["branch_pattern"], "patrón de ramas", 512,
    )
    if branch_pattern:
        try:
            re.compile(branch_pattern)
        except re.error as error:
            raise RuntimeError("el patrón de ramas no es una expresión regular válida") from error
    timezone = _setup_text(payload["timezone"], "zona horaria", 128, True)
    if timezone.startswith("/") or ".." in timezone or not Path("/usr/share/zoneinfo", timezone).is_file():
        raise RuntimeError("la zona horaria no existe en la imagen")
    enrollment = _setup_text(payload["enrollment_code"], "código de incorporación", 4096)
    cors_allowed_origins = _setup_origins(payload["cors_allowed_origins"])
    environment = {
        "NODE_NAME": node,
        "PEERS": peers,
        "PANEL_PEERS": panel_peers,
        "CACHE_TTL": str(cache_ttl),
        "MAINTENANCE_ENABLED": "1" if enabled else "0",
        "KEEP_LAST": str(keep_last),
        "PROTECTED_TAGS": ",".join(normalized_tags),
        "MAINTENANCE_HOUR": hour,
        "MAINTENANCE_GC": "1" if gc_enabled else "0",
        "MAINTENANCE_COORDINATOR": coordinator,
        "MAINTENANCE_LEASE_TTL": str(lease_ttl),
        "RETENTION_REQUIRE_ALL_NODES": "1" if require_all_nodes else "0",
        "RETENTION_BRANCH_PATTERN": branch_pattern,
        "SYNC_SOURCE": source,
        "CORS_ALLOWED_ORIGINS": cors_allowed_origins,
        "TZ": timezone,
    }
    return {"schema": 1, "environment": environment}, enrollment


def _save_initial_setup(payload) -> dict:
    if not SETUP_REQUIRED or PERSISTED_CONFIG.exists():
        raise RuntimeError("este registro ya está configurado")
    document, enrollment = _validate_setup(payload)
    if enrollment:
        cluster = _decode_enrollment(enrollment)
        generated_code = ""
    else:
        cluster = secrets.token_urlsafe(48)
        generated_code = _encode_enrollment(cluster)
    PERSISTED_SECRET_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PERSISTED_SECRET_DIR, 0o700)
    _atomic_write(PERSISTED_CLUSTER_TOKEN, (cluster + "\n").encode())
    _atomic_write(PERSISTED_CONFIG, (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode())
    return {"ok": True, "restarting": True, "enrollment_code": generated_code}


def _settings_payload() -> dict:
    members = [{"name": NODE_NAME, "registry_url": "", "panel_url": ""}]
    for name in sorted(set(PEERS) | set(PANEL_PEERS)):
        members.append({
            "name": name,
            "registry_url": PEERS.get(name, ""),
            "panel_url": PANEL_PEERS.get(name, ""),
        })
    enrollment = (
        _encode_enrollment(MAINTENANCE_CLUSTER_TOKEN)
        if len(MAINTENANCE_CLUSTER_TOKEN) >= 32 else ""
    )
    return {
        "node": NODE_NAME,
        "members": members,
        "timezone": os.environ.get("TZ", "UTC"),
        "enrollment_code": enrollment,
        "cache_ttl": CACHE_TTL,
        "cors_allowed_origins": ",".join(sorted(CORS_ORIGINS)),
        "maintenance": {
            "enabled": MAINT_ENABLED,
            "keep_last": KEEP_LAST,
            "hour": MAINTENANCE_HOUR,
            "gc": MAINTENANCE_GC,
            # La configuración muestra el coordinador efectivo, no el valor
            # inicial que pudo quedar en el entorno. La única edición del
            # coordinador se realiza desde el panel de mantenimiento.
            "coordinator": _config_cluster()["coordinator"],
            "protected_tags": ",".join(sorted(PROTECTED)),
            "sync_source": SYNC_SOURCE,
            "lease_ttl": MAINTENANCE_LEASE_TTL,
            "require_all_nodes": RETENTION_REQUIRE_ALL_NODES,
            "branch_pattern": RETENTION_BRANCH_PATTERN,
        },
    }


def _save_settings(payload) -> dict:
    document, enrollment = _validate_setup(payload)
    if enrollment:
        cluster = _decode_enrollment(enrollment)
    elif len(MAINTENANCE_CLUSTER_TOKEN) >= 32:
        cluster = MAINTENANCE_CLUSTER_TOKEN
    else:
        cluster = secrets.token_urlsafe(48)
    PERSISTED_SECRET_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(PERSISTED_SECRET_DIR, 0o700)
    _atomic_write(PERSISTED_CLUSTER_TOKEN, (cluster + "\n").encode())
    _atomic_write(PERSISTED_CONFIG, (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode())
    return {"ok": True, "restarting": True}


def _setup_status() -> dict:
    return {"required": SETUP_REQUIRED, "version": APP_VERSION}
try:
    _custom_branch_re = re.compile(RETENTION_BRANCH_PATTERN) if RETENTION_BRANCH_PATTERN else None
except re.error as error:
    raise RuntimeError(
        f"RETENTION_BRANCH_PATTERN no es una expresión regular válida: {error}"
    ) from error

_maint_lock = threading.RLock()
_window_lock = threading.Lock()
_progress_lock = threading.RLock()
_cluster_lease_lock = threading.RLock()
_cluster_nonce_lock = threading.Lock()
_state_lock = threading.RLock()
_setup_lock = threading.Lock()
_ultimo: dict = {}
_app_state: dict = {
    "coordinator": None,
    "last_run": None,
    "last_preview": None,
}
_cluster_lease: dict = {
    "run_id": None,
    "owner": None,
    "purpose": None,
    "acquired_at": None,
    "expires_at": 0,
}
_cluster_nonces: dict[str, int] = {}
_ventana_progreso: dict = {
    "running": False,
    "status": "idle",
    "current": None,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "steps": [],
}


def _progreso_snapshot() -> dict:
    with _progress_lock:
        return json.loads(json.dumps(_ventana_progreso))


def _iniciar_progreso_ventana() -> bool:
    ahora = int(time.time())
    nodos_gc = [
        {"name": nombre, "status": "pending", "detail": "", "freed_bytes": 0}
        for nombre in sorted(_nodos())
    ]
    pasos = [
        {"id": "retention", "number": 1, "label": "Retención de versiones",
         "status": "pending", "detail": "", "started_at": None, "finished_at": None},
        {"id": "gc", "number": 2, "label": "Liberar espacio",
         "status": "pending", "detail": "", "started_at": None, "finished_at": None,
         "nodes": nodos_gc},
        {"id": "sync", "number": 3, "label": "Sincronizar registros",
         "status": "pending", "detail": "", "started_at": None, "finished_at": None},
    ]
    with _progress_lock:
        if _ventana_progreso["running"]:
            return False
        _ventana_progreso.update({
            "running": True,
            "status": "running",
            "current": "retention",
            "message": "Preparando mantenimiento…",
            "started_at": ahora,
            "finished_at": None,
            "steps": pasos,
        })
        pasos[0]["status"] = "running"
        pasos[0]["detail"] = "Comprobando y reservando todos los nodos…"
        pasos[0]["started_at"] = ahora
    return True


def _actualizar_progreso_paso(step_id: str, status: str, detail: str = "") -> None:
    ahora = int(time.time())
    with _progress_lock:
        for paso in _ventana_progreso["steps"]:
            if paso["id"] != step_id:
                continue
            paso["status"] = status
            if detail:
                paso["detail"] = detail
            if status == "running":
                paso["started_at"] = paso.get("started_at") or ahora
                _ventana_progreso["current"] = step_id
            elif status in {"completed", "skipped", "failed", "blocked"}:
                paso["finished_at"] = ahora
            break


def _actualizar_progreso_nodo(
    nombre: str, status: str, detail: str = "", freed_bytes: int = 0,
) -> None:
    with _progress_lock:
        paso_gc = next(
            (paso for paso in _ventana_progreso["steps"] if paso["id"] == "gc"), None
        )
        if not paso_gc:
            return
        for nodo in paso_gc.get("nodes", []):
            if nodo["name"] != nombre:
                continue
            nodo["status"] = status
            nodo["detail"] = detail
            nodo["freed_bytes"] = max(0, int(freed_bytes or 0))
            break


def _finalizar_progreso_ventana(status: str, message: str) -> dict:
    with _progress_lock:
        if status == "failed":
            for paso in _ventana_progreso["steps"]:
                if paso["status"] == "running":
                    paso["status"] = "failed"
                    paso["detail"] = paso.get("detail") or "El paso no pudo completarse."
                    paso["finished_at"] = int(time.time())
        _ventana_progreso["running"] = False
        _ventana_progreso["status"] = status
        _ventana_progreso["current"] = None
        _ventana_progreso["message"] = message
        _ventana_progreso["finished_at"] = int(time.time())
        return _progreso_snapshot()


def _pid_registro() -> int | None:
    """El PID del registro, buscándolo en /proc. Comparten contenedor."""
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if "registry" in cmd and "serve" in cmd:
            return int(d.name)
    return None


def _esperar_registro(vivo: bool, limite: int = 60) -> bool:
    """Espera a que el registro esté sirviendo (vivo=True) o parado."""
    for _ in range(limite * 2):
        _, h = _fetch(f"{REGISTRY_URL}/v2/", timeout=2)
        if (h is not None) == vivo:
            return True
        time.sleep(0.5)
    return False


def _bytes_en_disco() -> int:
    return sum(blobs_en_disco().values())


def recolectar_basura(dry: bool) -> dict:
    """Recolección de basura: libera los blobs que ninguna etiqueta referencia.

    En simulación no se para nada: `--dry-run` no escribe, así que puede correr
    con el registro sirviendo. En serio, el registro se para —ver regla 2— y el
    entrypoint lo vuelve a levantar al quitar la señal.
    """
    if not MAINT_ENABLED:
        return {"error": "mantenimiento desactivado (MAINTENANCE_ENABLED=0)"}

    with _maint_lock:
        antes = _bytes_en_disco()
        cmd = ["/bin/registry", "garbage-collect", "--delete-untagged"]
        if dry:
            cmd.append("--dry-run")
        cmd.append(REGISTRY_CONFIG)

        parado = False
        try:
            if not dry:
                # Si no podemos demostrar qué proceso sirve el registro, no se
                # ejecuta una operación destructiva. Ejecutar GC con el registro
                # vivo puede eliminar un blob que acaba de empezar a usarse.
                pid = _pid_registro()
                if not pid:
                    return {
                        "action": "gc", "dry_run": dry, "ok": False,
                        "bytes_before": antes, "bytes_after": antes,
                        "freed_bytes": 0, "registry_back": False,
                        "output": "no se localizó el proceso del registro; no se toca nada",
                        "at": int(time.time()),
                    }
                MAINT_FLAG.parent.mkdir(parents=True, exist_ok=True)
                MAINT_FLAG.touch()
                os.kill(pid, 15)
                parado = _esperar_registro(vivo=False, limite=30)
                if not parado:
                    MAINT_FLAG.unlink(missing_ok=True)
                    return {"error": "el registro no se detuvo; no se toca nada"}

            # The command is assembled from fixed arguments and uses no shell.
            r = subprocess.run(  # nosec B603
                cmd, capture_output=True, text=True, timeout=3600,
            )
            salida = (r.stdout or "") + (r.stderr or "")
            ok = r.returncode == 0
        except Exception as e:
            salida, ok = f"{type(e).__name__}: {e}", False
        finally:
            # Pase lo que pase, se quita la señal: el entrypoint está esperándola
            # para volver a levantar el registro. Dejarla puesta por un error
            # sería dejar el registro caído.
            if not dry:
                MAINT_FLAG.unlink(missing_ok=True)
                if parado:
                    _esperar_registro(vivo=True, limite=60)

        _cache.pop("local", None)
        _cache.pop("drift", None)
        despues = _bytes_en_disco()
        res = {
            "action": "gc", "dry_run": dry, "ok": ok,
            "bytes_before": antes, "bytes_after": despues,
            "freed_bytes": max(0, antes - despues),
            "registry_back": _esperar_registro(vivo=True, limite=10) if not dry else True,
            "output": salida.strip()[-4000:],
            "at": int(time.time()),
        }
        _ultimo["gc"] = res
        return res


def _a_epoch(iso: str) -> float:
    """Fecha ISO-8601 del config de una imagen → epoch.

    Docker escribe hasta nanosegundos ('2026-01-01T12:00:00.123456789Z') y
    fromisoformat no traga más de seis decimales, así que se recortan.
    """
    if not iso:
        return 0.0
    s = iso.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*\.\d{1,6})\d*(\+.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def fecha_de_imagen(base: str, repo: str, tag: str) -> float:
    """Cuándo se CONSTRUYÓ esa imagen, según ella misma.

    Antes esto miraba la fecha del fichero de la etiqueta en disco, y estaba
    mal: esa fecha es la del último `push`, no la de la versión. Replicar una
    imagen entre nodos, o volver a publicar una etiqueta, la rejuvenece — así
    que el registro creía que una versión vieja era la más nueva.
    Ahora se lee el campo `created` del config de la imagen, que lo escribe
    quien la construyó, viaja dentro de ella y no cambia al copiarla.
    """
    body, _ = _fetch(_ref_url(base, repo, tag))
    if not body:
        return 0.0
    cfg = (body.get("config") or {}).get("digest")
    if not cfg:
        # Índice multiarquitectura: vale la fecha de cualquiera de sus hijos.
        for h in body.get("manifests") or []:
            if DIGEST_RE.match(h.get("digest", "")):
                return fecha_de_imagen(base, repo, h["digest"])
        return 0.0
    blob, _ = _fetch(_repo_url(base, repo, f"blobs/{urllib.parse.quote(cfg, safe=':')}"))
    return _a_epoch((blob or {}).get("created", ""))


# El sufijo de la etiqueta es la rama. Con el formato predeterminado
# (vMAYOR.MENOR.PARCHE[-dN|-rcN]): 'v0.9.2' es estable, 'v0.9.2-d3' es
# desarrollo y 'v0.9.2-rc1' es candidata. Cualquier otro sufijo con letras es su
# propia rama; sin sufijo, estable.
RAMA_RE = re.compile(r"^.+-(?P<sufijo>[A-Za-z]+)\d*$")
NOMBRE_RAMA = {"d": "dev", "rc": "rc"}


def rama_de_etiqueta(tag: str) -> str:
    if _custom_branch_re:
        m = _custom_branch_re.match(tag)
        if m:
            branch = m.groupdict().get("branch") if m.groupdict() else None
            if branch is None and m.groups():
                branch = m.group(1)
            if branch:
                return branch.lower()
    m = RAMA_RE.match(tag)
    if not m:
        return "stable"
    s = m.group("sufijo").lower()
    return NOMBRE_RAMA.get(s, s)


def _nodos() -> dict[str, str]:
    n = {NODE_NAME: REGISTRY_URL}
    n.update(PEERS)
    return n


def _estado_persistente_snapshot() -> dict:
    with _state_lock:
        return json.loads(json.dumps(_app_state))


def _cargar_estado_persistente() -> None:
    """Carga sólo las claves conocidas; un fichero roto no impide arrancar."""
    try:
        datos = json.loads(APP_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(datos, dict):
        return
    with _state_lock:
        coordinador = datos.get("coordinator")
        _app_state["coordinator"] = coordinador if isinstance(coordinador, str) else None
        for clave in ("last_run", "last_preview"):
            valor = datos.get(clave)
            _app_state[clave] = valor if isinstance(valor, dict) else None


def _guardar_estado_persistente() -> str | None:
    """Escribe el estado de forma atómica dentro del volumen del registro."""
    temporal: Path | None = None
    try:
        with _state_lock:
            APP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporal = APP_STATE_FILE.with_name(
                f".{APP_STATE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporal.write_text(
                json.dumps(_app_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporal, 0o600)
            os.replace(temporal, APP_STATE_FILE)
        return None
    except OSError as e:
        if temporal:
            try:
                temporal.unlink(missing_ok=True)
            except OSError:
                pass
        return f"no se pudo guardar el estado persistente ({type(e).__name__})"


def _actualizar_estado_persistente(clave: str, valor) -> str | None:
    if clave not in _app_state:
        return "clave de estado desconocida"
    with _state_lock:
        anterior = _app_state[clave]
        _app_state[clave] = json.loads(json.dumps(valor)) if valor is not None else None
        error = _guardar_estado_persistente()
        if error:
            _app_state[clave] = anterior
        return error


_cargar_estado_persistente()


def _config_cluster() -> dict:
    nodos = sorted(_nodos())
    coordinador_guardado = _estado_persistente_snapshot().get("coordinator")
    coordinador = coordinador_guardado or MAINTENANCE_COORDINATOR or nodos[0]
    problemas: list[str] = []
    modo_cluster = len(nodos) > 1

    if NODE_NAME in PEERS:
        problemas.append("PEERS no puede incluir el propio NODE_NAME")
    if coordinador not in nodos:
        problemas.append(
            f"el coordinador {coordinador!r} no pertenece a los nodos configurados"
        )
    if SYNC_SOURCE and SYNC_SOURCE not in nodos:
        problemas.append(
            f"el origen de sincronización {SYNC_SOURCE!r} no pertenece a los nodos configurados"
        )
    paneles_extra = sorted(set(PANEL_PEERS) - set(PEERS))
    if paneles_extra:
        problemas.append(
            "hay paneles sin registro par: " + ", ".join(paneles_extra)
        )
    if modo_cluster:
        faltan_paneles = sorted(set(nodos) - {NODE_NAME} - set(PANEL_PEERS))
        if faltan_paneles:
            problemas.append(
                "faltan paneles para: " + ", ".join(faltan_paneles)
            )
        invalidos = sorted(
            nombre for nombre, url in PANEL_PEERS.items()
            if nombre in nodos
            and urllib.parse.urlsplit(url).scheme not in {"http", "https"}
        )
        if invalidos:
            problemas.append(
                "las URL de panel no son válidas para: " + ", ".join(invalidos)
            )
        if len(MAINTENANCE_CLUSTER_TOKEN) < 32:
            problemas.append(
                "MAINTENANCE_CLUSTER_TOKEN debe tener al menos 32 caracteres"
            )

    return {
        "mode": modo_cluster,
        "nodes": nodos,
        "coordinator": coordinador,
        "coordinator_source": "interface" if coordinador_guardado else (
            "environment" if MAINTENANCE_COORDINATOR else "default"
        ),
        "is_coordinator": NODE_NAME == coordinador,
        "ready": not problemas,
        "problems": problemas,
    }


def _firma_cluster(
    method: str, path: str, timestamp: str, nonce: str, sender: str, body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    mensaje = "\n".join([
        method.upper(), path, timestamp, nonce, sender, body_hash,
    ]).encode("utf-8")
    return hmac.new(
        MAINTENANCE_CLUSTER_TOKEN.encode("utf-8"), mensaje, hashlib.sha256,
    ).hexdigest()


def _cabeceras_cluster(method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    return {
        "Content-Type": "application/json",
        "X-Registry-Cluster-Node": NODE_NAME,
        "X-Registry-Cluster-Timestamp": timestamp,
        "X-Registry-Cluster-Nonce": nonce,
        "X-Registry-Cluster-Signature": _firma_cluster(
            method, path, timestamp, nonce, NODE_NAME, body,
        ),
    }


def _solicitud_panel(
    nombre: str, path: str, method: str = "GET", payload: dict | None = None,
    timeout: int = 30,
) -> tuple[dict | None, str | None]:
    base = PANEL_PEERS.get(nombre)
    if not base:
        return None, f"no hay URL de panel para {nombre}"
    body = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if payload is not None else b""
    )
    req = urllib.request.Request(
        f"{base}{path}", method=method, data=body if method != "GET" else None,
    )
    for clave, valor in _cabeceras_cluster(method, path, body).items():
        req.add_header(clave, valor)
    try:
        with _urlopen_http(req, timeout=timeout) as respuesta:
            datos = json.loads(respuesta.read() or b"{}")
            return datos if isinstance(datos, dict) else None, None
    except urllib.error.HTTPError as e:
        try:
            detalle = json.loads(e.read() or b"{}").get("error")
        except (ValueError, AttributeError):
            detalle = None
        return None, detalle or f"HTTP {e.code} desde {nombre}"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return None, f"{nombre} no responde ({type(e).__name__})"


def _peticion_cluster_autorizada(
    method: str, path: str, headers, body: bytes,
) -> tuple[bool, str]:
    if len(MAINTENANCE_CLUSTER_TOKEN) < 32:
        return False, "secreto de clúster no configurado"
    sender = _header(headers, "X-Registry-Cluster-Node")
    timestamp = _header(headers, "X-Registry-Cluster-Timestamp")
    nonce = _header(headers, "X-Registry-Cluster-Nonce")
    signature = _header(headers, "X-Registry-Cluster-Signature")
    if sender not in _nodos():
        return False, "nodo emisor desconocido"
    try:
        instante = int(timestamp)
    except ValueError:
        return False, "fecha de firma inválida"
    ahora = int(time.time())
    if abs(ahora - instante) > CLUSTER_REQUEST_MAX_AGE:
        return False, "firma caducada"
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return False, "nonce inválido"
    esperado = _firma_cluster(method, path, timestamp, nonce, sender, body)
    if not hmac.compare_digest(signature, esperado):
        return False, "firma inválida"

    with _cluster_nonce_lock:
        limite = ahora - CLUSTER_REQUEST_MAX_AGE
        for usado, momento in list(_cluster_nonces.items()):
            if momento < limite:
                _cluster_nonces.pop(usado, None)
        if nonce in _cluster_nonces:
            return False, "petición repetida"
        _cluster_nonces[nonce] = ahora
    return True, sender


def _lease_snapshot() -> dict:
    with _cluster_lease_lock:
        if _cluster_lease["run_id"] and _cluster_lease["expires_at"] <= time.time():
            _cluster_lease.update({
                "run_id": None, "owner": None, "purpose": None,
                "acquired_at": None, "expires_at": 0,
            })
        return dict(_cluster_lease)


def _adquirir_lease_local(run_id: str, owner: str, purpose: str, ttl: int) -> bool:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        return False
    config = _config_cluster()
    if owner != config["coordinator"]:
        return False
    with _cluster_lease_lock:
        actual = _lease_snapshot()
        if actual["run_id"] not in {None, run_id}:
            return False
        ahora = int(time.time())
        _cluster_lease.update({
            "run_id": run_id,
            "owner": owner,
            "purpose": purpose[:40],
            "acquired_at": actual["acquired_at"] or ahora,
            "expires_at": ahora + min(max(300, ttl), MAINTENANCE_LEASE_TTL),
        })
        return True


def _lease_local_valida(run_id: str, owner: str) -> bool:
    actual = _lease_snapshot()
    return actual["run_id"] == run_id and actual["owner"] == owner


def _liberar_lease_local(run_id: str, owner: str) -> bool:
    with _cluster_lease_lock:
        actual = _lease_snapshot()
        if actual["run_id"] != run_id or actual["owner"] != owner:
            return False
        _cluster_lease.update({
            "run_id": None, "owner": None, "purpose": None,
            "acquired_at": None, "expires_at": 0,
        })
        return True


def _preflight_cluster(require_maintenance: bool = True) -> list[str]:
    config = _config_cluster()
    errores = list(config["problems"])
    if errores:
        return errores
    if require_maintenance and not MAINT_ENABLED:
        errores.append(f"el mantenimiento está desactivado en {NODE_NAME}")

    for nombre in config["nodes"]:
        if nombre != NODE_NAME:
            estado, error = _solicitud_panel(
                nombre, "/api/internal/cluster/status", timeout=10,
            )
            if error or not estado:
                errores.append(error or f"el panel de {nombre} no responde")
                continue
            remoto = estado.get("cluster") or {}
            if remoto.get("coordinator") != config["coordinator"]:
                errores.append(f"{nombre} tiene otro coordinador configurado")
            if remoto.get("nodes") != config["nodes"]:
                errores.append(f"{nombre} tiene una topología distinta")
            if not remoto.get("ready"):
                errores.append(f"{nombre} no tiene completa la configuración del clúster")
            mantenimiento_remoto = estado.get("maintenance") or {}
            if require_maintenance and not mantenimiento_remoto.get("enabled"):
                errores.append(f"el mantenimiento está desactivado en {nombre}")

        base = _nodos()[nombre]
        if _fetch(f"{base}/v2/", timeout=8)[1] is None:
            errores.append(f"el registro de {nombre} no responde")
    return errores


def _liberar_leases(
    run_id: str, adquiridos: list[str], owner: str | None = None,
) -> None:
    owner = owner or _config_cluster()["coordinator"]
    for nombre in reversed(adquiridos):
        if nombre == NODE_NAME:
            _liberar_lease_local(run_id, owner)
        else:
            _solicitud_panel(
                nombre, "/api/internal/cluster/lease/release", "POST",
                {"run_id": run_id}, timeout=10,
            )


def _adquirir_leases(
    run_id: str, purpose: str, require_maintenance: bool = True,
) -> tuple[list[str], list[str]]:
    errores = _preflight_cluster(require_maintenance=require_maintenance)
    if errores:
        return [], errores
    config = _config_cluster()
    adquiridos: list[str] = []
    for nombre in config["nodes"]:
        if nombre == NODE_NAME:
            ok = _adquirir_lease_local(
                run_id, config["coordinator"], purpose, MAINTENANCE_LEASE_TTL,
            )
            error = None if ok else f"{nombre} ya tiene otro mantenimiento activo"
        else:
            respuesta, error = _solicitud_panel(
                nombre, "/api/internal/cluster/lease/acquire", "POST",
                {
                    "run_id": run_id,
                    "owner": config["coordinator"],
                    "purpose": purpose,
                    "ttl": MAINTENANCE_LEASE_TTL,
                },
                timeout=10,
            )
            ok = bool(respuesta and respuesta.get("ok"))
            if not ok and not error:
                error = f"{nombre} ya tiene otro mantenimiento activo"
        if not ok:
            _liberar_leases(run_id, adquiridos, config["coordinator"])
            return [], [error or f"no se pudo reservar {nombre}"]
        adquiridos.append(nombre)
    return adquiridos, []


def _renovar_leases(run_id: str, adquiridos: list[str], purpose: str) -> list[str]:
    config = _config_cluster()
    errores: list[str] = []
    for nombre in adquiridos:
        if nombre == NODE_NAME:
            ok = _adquirir_lease_local(
                run_id, config["coordinator"], purpose, MAINTENANCE_LEASE_TTL,
            )
        else:
            respuesta, error = _solicitud_panel(
                nombre, "/api/internal/cluster/lease/acquire", "POST",
                {
                    "run_id": run_id,
                    "owner": config["coordinator"],
                    "purpose": purpose,
                    "ttl": MAINTENANCE_LEASE_TTL,
                },
                timeout=10,
            )
            ok = bool(respuesta and respuesta.get("ok"))
            if error:
                errores.append(error)
                continue
        if not ok:
            errores.append(f"no se pudo renovar la reserva de {nombre}")
    return errores


def inventario_global() -> tuple[dict[str, dict[str, dict]], list[str]]:
    """repo → etiqueta → nodos/digests, y la lista de nodos caídos.

    Se mira el conjunto de los nodos configurados y no solo el propio. Es lo que permite que
    retención y sincronización hablen del mismo mundo: si cada una razonara con
    lo que ve su nodo, una borraría lo que la otra acaba de traer.
    """
    global_inv: dict[str, dict[str, dict]] = {}
    caidos = []
    for nombre, base in _nodos().items():
        if _fetch(f"{base}/v2/", timeout=8)[1] is None:
            caidos.append(nombre)
            continue
        incompleto = False
        for repo in catalog(base):
            for tag in tags_of(base, repo):
                digest = _header(
                    _cabeza(_ref_url(base, repo, tag)),
                    "Docker-Content-Digest",
                )
                if not DIGEST_RE.match(digest):
                    incompleto = True
                    break
                entry = global_inv.setdefault(repo, {}).setdefault(
                    tag, {"nodes": [], "digests": {}}
                )
                entry["nodes"].append(nombre)
                entry["digests"][nombre] = digest
            if incompleto:
                break
        if incompleto and nombre not in caidos:
            caidos.append(nombre)
    return global_inv, caidos


def plan_retencion() -> dict:
    """Qué etiquetas sobran, mirando los registros configurados. No borra nada.

    Dos reglas que salieron de sendos incidentes:

      · Se conservan las N últimas DE CADA RAMA, no las N últimas en total. Con
        un solo montón, las versiones de producción —más nuevas— echan fuera a
        TODAS las de desarrollo y esa rama desaparece entera.
      · Se decide sobre la unión de los nodos. Si se decidiera solo con lo
        que hay en el nodo local, la sincronización volvería a traer desde un par justo
        lo que se acaba de borrar, y el disco no bajaría nunca.
    """
    if KEEP_LAST <= 0:
        return {"enabled": False, "keep_last": KEEP_LAST,
                "protected": sorted(PROTECTED), "candidates": [], "reason":
                "KEEP_LAST no está puesto: la retención está apagada"}

    inv, caidos = inventario_global()
    if caidos and RETENTION_REQUIRE_ALL_NODES:
        return {
            "enabled": True,
            "blocked": True,
            "keep_last": KEEP_LAST,
            "protected": sorted(PROTECTED),
            "unreachable": caidos,
            "candidates": [],
            "total_tags_to_remove": 0,
            "reason": "retención bloqueada: no responden todos los nodos configurados",
        }
    plan = []
    for repo, etiquetas in sorted(inv.items()):
        # Las protegidas salen del reparto: son flotantes y siempre apuntan a lo
        # que está desplegado. Borrarlas dejaría sin imagen a algo que corre.
        candidatas = [t for t in etiquetas if t not in PROTECTED]
        if not candidatas:
            continue

        def fechar(t):
            # Da igual a qué nodo se pregunte: la fecha va dentro de la imagen.
            for n in etiquetas[t]["nodes"]:
                f = fecha_de_imagen(_nodos()[n], repo, t)
                if f:
                    return f
            return 0.0

        with ThreadPoolExecutor(max_workers=16) as pool:
            fechas = dict(zip(candidatas, pool.map(fechar, candidatas)))

        # Varias etiquetas pueden apuntar al mismo manifiesto (por ejemplo, una
        # versión y un alias). La retención cuenta versiones únicas, no alias.
        # Si un digest difiere entre nodos, se trata como una etiqueta propia para
        # no ocultar una deriva ni borrar una copia inesperada.
        grupos: dict[tuple[str, str], dict] = {}
        for tag in candidatas:
            info = etiquetas[tag]
            digests = set(info["digests"].values())
            clave_digest = next(iter(digests)) if len(digests) == 1 else f"tag:{tag}"
            clave = (rama_de_etiqueta(tag), clave_digest)
            grupo = grupos.setdefault(clave, {
                "tags": [],
                "date": fechas.get(tag, 0.0),
            })
            grupo["tags"].append(tag)
            grupo["date"] = max(grupo["date"], fechas.get(tag, 0.0))

        ramas: dict[str, list[str]] = {}
        for (rama, _), grupo in grupos.items():
            ramas.setdefault(rama, []).append(grupo)

        detalle = []
        for rama, grupos_rama in sorted(ramas.items()):
            # Una fecha desconocida no es evidencia de que una versión sea vieja;
            # se conserva por seguridad. Las conocidas se ordenan de más nueva a
            # más antigua y, a igualdad, de forma determinista.
            grupos_rama.sort(
                key=lambda g: (g["date"] > 0, g["date"], sorted(g["tags"])),
                reverse=True,
            )
            conservar = grupos_rama[:KEEP_LAST]
            conservar += [g for g in grupos_rama[KEEP_LAST:] if g["date"] <= 0]
            eliminar = [g for g in grupos_rama if g not in conservar and g["date"] > 0]
            keeping = sorted(t for g in conservar for t in g["tags"])
            removing = sorted(t for g in eliminar for t in g["tags"])
            detalle.append({
                "branch": rama,
                "keeping": keeping,
                "removing": removing,
                "dates": {t: int(fechas.get(t, 0)) for t in keeping + removing},
            })

        # El API de Registry borra manifiestos, no aliases individuales. Si un
        # tag que sobra comparte digest con otro tag que se conserva (o con uno
        # protegido), eliminarlo quitaría también ese alias retenido. En ese caso
        # se conserva el tag sobrante: no ocupa una versión única adicional y es
        # mucho más seguro que borrar un manifiesto compartido.
        inicialmente_sobran = {t for d in detalle for t in d["removing"]}
        conservadas = set(etiquetas) - inicialmente_sobran
        digests_conservados = {
            d for t in conservadas for d in etiquetas[t]["digests"].values()
        }
        sobran = []
        for d in detalle:
            seguras = []
            for tag in d["removing"]:
                digests = set(etiquetas[tag]["digests"].values())
                if digests & digests_conservados:
                    d["keeping"].append(tag)
                else:
                    seguras.append(tag)
            d["removing"] = sorted(seguras)
            d["keeping"] = sorted(set(d["keeping"]))
            sobran.extend(seguras)
        if sobran:
            plan.append({"project": repo, "tags": len(etiquetas),
                         "branches": detalle, "removing": sobran,
                         "nodes": {t: etiquetas[t]["nodes"] for t in sobran},
                         "digests": {t: etiquetas[t]["digests"] for t in sobran}})

    return {"enabled": True, "keep_last": KEEP_LAST,
            "protected": sorted(PROTECTED),
            "unreachable": caidos,
            "candidates": plan,
            "total_tags_to_remove": sum(len(p["removing"]) for p in plan)}


def _a_descartar(plan: dict | None = None) -> set[tuple[str, str]]:
    """{(repo, etiqueta)} que la retención condena. La sincronización lo usa
    para no repartir lo que está a punto de borrarse."""
    if KEEP_LAST <= 0:
        return set()
    p = plan or plan_retencion()
    return {(c["project"], t) for c in p.get("candidates", []) for t in c["removing"]}


# ─────────────────────────── sincronización ──────────────────────────────────
#
# Replica etiquetas entre los registros configurados hablando su API, sin Docker de por
# medio: el contenedor no tiene cliente de Docker ni debe tenerlo.
#
# Va en las DOS direcciones. Si solo tirase de los pares, un nodo con el
# contenedor parado se quedaría descolgado para siempre; así, cualquier instancia
# viva deja los nodos configurados iguales, que es de lo que se trata: que perder un nodo no
# sea perder imágenes.

def _cabeza(url: str, accept: str = ACCEPT) -> dict | None:
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("Accept", accept)
    try:
        with _urlopen_http(req, timeout=20) as r:
            return dict(r.headers)
    except Exception:
        return None


def _manifiesto_crudo(base: str, repo: str, ref: str):
    """(bytes, content-type, digest). En crudo porque reserializar el JSON
    cambiaría el digest y el destino rechazaría el manifiesto."""
    req = urllib.request.Request(_ref_url(base, repo, ref))
    req.add_header("Accept", ACCEPT)
    try:
        with _urlopen_http(req, timeout=30) as r:
            return r.read(), _header(r.headers, "Content-Type"), \
                   _header(r.headers, "Docker-Content-Digest")
    except Exception:
        return None, None, None


def _copiar_blob(origen: str, destino: str, repo: str, digest: str, size: int) -> bool:
    blob_url = _repo_url(destino, repo, f"blobs/{urllib.parse.quote(digest, safe=':')}")
    if _cabeza(blob_url, "*/*"):
        return True  # ya lo tiene
    try:
        req = urllib.request.Request(
            _repo_url(destino, repo, "blobs/uploads/"), method="POST", data=b"")
        req.add_header("Content-Length", "0")
        with _urlopen_http(req, timeout=30) as r:
            location = _header(r.headers, "Location")
        if not location:
            return False
        if not location.startswith("http"):
            location = destino + location
        url = location + ("&" if "?" in location else "?") + "digest=" + digest

        # El blob se reenvía en streaming: una capa puede pesar cientos de MB y
        # cargarla en memoria dentro de un contenedor pequeño es pedir problemas.
        with _urlopen_http(
            _repo_url(origen, repo, f"blobs/{urllib.parse.quote(digest, safe=':')}"),
            timeout=120,
        ) as src:
            put = urllib.request.Request(url, method="PUT", data=src)
            put.add_header("Content-Length", str(size))
            put.add_header("Content-Type", "application/octet-stream")
            with _urlopen_http(put, timeout=1800) as resp:
                return resp.status in (200, 201, 202)
    except Exception:
        return False


def _copiar_etiqueta(origen: str, destino: str, repo: str, tag: str) -> tuple[bool, str]:
    crudo, ctype, _ = _manifiesto_crudo(origen, repo, tag)
    if not crudo:
        return False, "no se pudo leer el manifiesto en el origen"
    try:
        doc = json.loads(crudo)
    except ValueError:
        return False, "manifiesto ilegible"

    # Primero los blobs; el manifiesto va el ÚLTIMO. Si se subiera antes, el
    # destino tendría una etiqueta que apunta a capas que aún no están, y una
    # imagen rota es peor que una imagen ausente.
    piezas = []
    for hijo in doc.get("manifests") or []:      # índice multiarquitectura
        ok, msg = _copiar_etiqueta(origen, destino, repo, hijo.get("digest", ""))
        if not ok:
            return False, f"submanifiesto: {msg}"
    cfg = doc.get("config") or {}
    if cfg.get("digest"):
        piezas.append((cfg["digest"], int(cfg.get("size") or 0)))
    for capa in doc.get("layers") or []:
        piezas.append((capa["digest"], int(capa.get("size") or 0)))

    for d, s in piezas:
        if not _copiar_blob(origen, destino, repo, d, s):
            return False, f"fallo copiando blob {d[:19]}"

    try:
        put = urllib.request.Request(
            _ref_url(destino, repo, tag), method="PUT", data=crudo)
        put.add_header("Content-Type", ctype or
                       "application/vnd.docker.distribution.manifest.v2+json")
        with _urlopen_http(put, timeout=120) as r:
            return r.status in (200, 201, 202), ""
    except Exception as e:
        return False, f"{type(e).__name__} al publicar el manifiesto"


def _inventario(base: str) -> dict[str, dict[str, str]]:
    """repo → {etiqueta: digest} de un registro."""
    out: dict[str, dict[str, str]] = {}
    for repo in catalog(base):
        tags = tags_of(base, repo)
        if not tags:
            continue
        def dg(t):
            h = _cabeza(_ref_url(base, repo, t))
            return t, _header(h, "Docker-Content-Digest")
        with ThreadPoolExecutor(max_workers=16) as pool:
            out[repo] = {t: d for t, d in pool.map(dg, tags) if d}
    return out


def sincronizar(dry: bool) -> dict:
    """Deja las mismas etiquetas en los registros configurados."""
    if not MAINT_ENABLED:
        return {"error": "mantenimiento desactivado (MAINTENANCE_ENABLED=0)"}

    with _maint_lock:
        nodos = {NODE_NAME: REGISTRY_URL}
        nodos.update(PEERS)
        inventarios: dict[str, dict[str, dict[str, str]]] = {}
        caidos = []
        for nombre, base in nodos.items():
            if _fetch(f"{base}/v2/", timeout=8)[1] is None:
                caidos.append(nombre)
                continue
            inventarios[nombre] = _inventario(base)

        # Lo que la retención va a borrar NO se reparte. Sin esto, sincronizar
        # deshacía la limpieza: el par todavía tenía la versión vieja y la
        # devolvía al nodo que acababa de borrarla. Se probó y pasaba.
        condenadas = _a_descartar()

        # Qué falta o qué difiere en cada nodo. La sincronización necesita
        # corregir también un tag existente que apunta a otro digest; limitarse
        # a ausencias dejaba la deriva detectada pero sin forma de repararla.
        cambios: list[tuple[str, str, str, str]] = []   # destino, repo, tag, origen
        omitidas = 0
        coordinador = _config_cluster()["coordinator"]
        repos = {r for inv in inventarios.values() for r in inv}
        for repo in sorted(repos):
            etiquetas = {t for inv in inventarios.values() for t in inv.get(repo, {})}
            for tag in sorted(etiquetas):
                if (repo, tag) in condenadas:
                    omitidas += 1
                    continue
                quien = {n: inv.get(repo, {}).get(tag) for n, inv in inventarios.items()}
                # Sin una excepción explícita, el coordinador es también la
                # fuente autoritativa cuando una etiqueta tiene varios
                # digests. Si no posee esa etiqueta se usa la primera copia
                # disponible para poder propagar imágenes que le falten.
                preferidas = [SYNC_SOURCE, coordinador]
                fuente = next((
                    nombre for nombre in preferidas
                    if nombre and nombre in quien and quien[nombre]
                ), None) or next((n for n in sorted(quien) if quien[n]), None)
                if not fuente:
                    continue
                digest_fuente = quien[fuente]
                for n, d in quien.items():
                    if n != fuente and d != digest_fuente:
                        cambios.append((n, repo, tag, fuente))

        copiadas, fallos = [], []
        if not dry:
            for destino, repo, tag, fuente in cambios:
                ok, msg = _copiar_etiqueta(nodos[fuente], nodos[destino], repo, tag)
                if ok:
                    copiadas.append(f"{repo}:{tag} → {destino}")
                else:
                    fallos.append(f"{repo}:{tag} → {destino} ({msg})")
            _cache.pop("local", None)
            _cache.pop("drift", None)

        res = {
            "action": "sync", "dry_run": dry,
            "nodes": sorted(inventarios), "unreachable": caidos,
            "skipped_by_retention": omitidas,
            "missing_count": sum(
                1 for d, r, t, f in cambios
                if not inventarios.get(d, {}).get(r, {}).get(t)
            ),
            "mismatch_count": sum(
                1 for d, r, t, f in cambios
                if inventarios.get(d, {}).get(r, {}).get(t)
            ),
            "missing": [
                f"{r}:{t} falta en {d}" if not inventarios.get(d, {}).get(r, {}).get(t)
                else f"{r}:{t} difiere en {d}"
                for d, r, t, _ in cambios[:200]
            ],
            "copied": copiadas, "failed": fallos,
            "at": int(time.time()),
        }
        _ultimo["sync"] = res
        return res


def aplicar_retencion(dry: bool) -> dict:
    """Borra las etiquetas que sobran. El espacio no vuelve hasta recolectar.

    Borrar una etiqueta solo quita la referencia; los blobs siguen ocupando hasta
    que pase el recolector. Por eso esto se hace en dos pasos y no en uno.
    """
    with _maint_lock:
        if not MAINT_ENABLED:
            return {"error": "mantenimiento desactivado (MAINTENANCE_ENABLED=0)"}
        plan = plan_retencion()
        if not plan.get("enabled") or plan.get("blocked"):
            return {"action": "retention", "dry_run": dry, **plan}

        borradas, fallos = [], []
        if not dry:
            nodos = _nodos()
            manifiestos_borrados: set[tuple[str, str]] = set()
            for entrada in plan["candidates"]:
                repo = entrada["project"]
                for tag in entrada["removing"]:
                    # Se borra en TODOS los nodos que la tengan. El digest se
                    # captura en el plan y se comprueba otra vez antes del DELETE:
                    # si una etiqueta cambió mientras calculábamos, no se borra
                    # accidentalmente la versión nueva.
                    for nombre in entrada.get("nodes", {}).get(tag, [NODE_NAME]):
                        base = nodos.get(nombre)
                        expected = entrada.get("digests", {}).get(tag, {}).get(nombre)
                        if not base or not DIGEST_RE.match(expected or ""):
                            fallos.append(f"{repo}:{tag}@{nombre} (digest esperado inválido)")
                            continue
                        manifiesto = (nombre, expected)
                        if manifiesto in manifiestos_borrados:
                            borradas.append(f"{repo}:{tag}@{nombre} (mismo manifiesto)")
                            continue
                        _, h = _fetch(_ref_url(base, repo, tag), method="HEAD")
                        current = _header(h, "Docker-Content-Digest")
                        if current != expected:
                            fallos.append(f"{repo}:{tag}@{nombre} (cambió durante el plan; no se borra)")
                            continue
                        req = urllib.request.Request(
                            _ref_url(base, repo, expected), method="DELETE")
                        try:
                            with _urlopen_http(req, timeout=30) as r:
                                if r.status in (200, 202):
                                    borradas.append(f"{repo}:{tag}@{nombre}")
                                    manifiestos_borrados.add(manifiesto)
                                else:
                                    fallos.append(f"{repo}:{tag}@{nombre} (HTTP {r.status})")
                        except urllib.error.HTTPError as e:
                            # 405 = ese registro no tiene el borrado activado.
                            fallos.append(f"{repo}:{tag}@{nombre} (HTTP {e.code})")
                        except Exception as e:
                            fallos.append(f"{repo}:{tag}@{nombre} ({type(e).__name__})")
            _cache.pop("local", None)
            _cache.pop("drift", None)

        res = {"action": "retention", "dry_run": dry, "keep_last": KEEP_LAST,
               "protected": sorted(PROTECTED),
               "would_remove": plan["total_tags_to_remove"],
               "removed": borradas, "failed": fallos,
               "candidates": plan["candidates"],
               "note": "el espacio no vuelve hasta pasar la recolección de basura",
               "at": int(time.time())}
        _ultimo["retention"] = res
        return res


def _recolectar_cluster(dry: bool, run_id: str, adquiridos: list[str]) -> dict:
    """Ejecuta GC nodo a nodo y nunca continúa si uno no vuelve a servir."""
    config = _config_cluster()
    resultados: dict[str, dict] = {}
    abortado = False

    for nombre in config["nodes"]:
        if abortado:
            resultados[nombre] = {
                "action": "gc", "dry_run": dry, "ok": False,
                "skipped": True,
                "reason": "un registro anterior no volvió a estar disponible",
                "registry_back": False, "freed_bytes": 0,
            }
            _actualizar_progreso_nodo(nombre, "skipped", "Omitido por seguridad.")
            continue

        errores_renovacion = _renovar_leases(run_id, adquiridos, "gc")
        if errores_renovacion:
            resultados[nombre] = {
                "action": "gc", "dry_run": dry, "ok": False,
                "error": "; ".join(errores_renovacion),
                "registry_back": False, "freed_bytes": 0,
            }
            _actualizar_progreso_nodo(
                nombre, "failed", "No se pudo renovar la reserva del clúster.",
            )
            abortado = True
            continue

        _actualizar_progreso_nodo(
            nombre, "running",
            "Analizando blobs…" if dry else "Registro detenido; liberando espacio…",
        )
        if nombre == NODE_NAME:
            if not _lease_local_valida(run_id, config["coordinator"]):
                resultado = {
                    "action": "gc", "dry_run": dry, "ok": False,
                    "error": "la reserva local ha caducado",
                    "registry_back": False, "freed_bytes": 0,
                }
            else:
                resultado = recolectar_basura(dry)
        else:
            resultado, error = _solicitud_panel(
                nombre, "/api/internal/cluster/gc", "POST",
                {"run_id": run_id, "dry_run": dry}, timeout=3700,
            )
            if error or not resultado:
                resultado = {
                    "action": "gc", "dry_run": dry, "ok": False,
                    "error": error or f"{nombre} no devolvió un resultado",
                    "registry_back": False, "freed_bytes": 0,
                }

        if not dry and resultado.get("registry_back", False):
            base = _nodos()[nombre]
            if _fetch(f"{base}/v2/", timeout=8)[1] is None:
                resultado["registry_back"] = False
                resultado["error"] = "el nodo terminó GC pero su registro no responde"

        resultados[nombre] = resultado
        correcto = (
            not resultado.get("error")
            and resultado.get("ok", True)
            and resultado.get("registry_back", True)
        )
        detalle = (
            f"{resultado.get('freed_bytes', 0)} bytes liberados."
            if correcto and not dry else
            "Previsualización completada." if correcto else
            resultado.get("error") or "La recolección terminó con incidencias."
        )
        _actualizar_progreso_nodo(
            nombre, "completed" if correcto else "failed", detalle,
            resultado.get("freed_bytes", 0),
        )
        # Se puede continuar tras un fallo del comando si el registro volvió.
        # Lo que nunca se hace es detener el nodo siguiente sin disponibilidad.
        if not resultado.get("registry_back", True):
            abortado = True

    completados = [r for r in resultados.values() if not r.get("skipped")]
    lineas = []
    for nombre in config["nodes"]:
        r = resultados.get(nombre, {})
        if r.get("skipped"):
            lineas.append(f"{nombre}: omitido por seguridad")
        elif r.get("error") or not r.get("ok", True):
            lineas.append(f"{nombre}: incidencia")
        elif dry:
            lineas.append(f"{nombre}: previsualización completada")
        else:
            lineas.append(f"{nombre}: {r.get('freed_bytes', 0)} bytes liberados")

    resultado_cluster = {
        "action": "gc", "dry_run": dry, "cluster": config["mode"],
        "nodes": config["nodes"], "node_results": resultados,
        "ok": bool(completados) and all(
            not r.get("error") and r.get("ok", True) for r in completados
        ) and not any(r.get("skipped") for r in resultados.values()),
        "bytes_before": sum(int(r.get("bytes_before", 0)) for r in completados),
        "bytes_after": sum(int(r.get("bytes_after", 0)) for r in completados),
        "freed_bytes": sum(int(r.get("freed_bytes", 0)) for r in completados),
        "registry_back": all(
            r.get("registry_back", True) for r in resultados.values()
        ) and not any(r.get("skipped") for r in resultados.values()),
        "output": "\n".join(lineas),
        "at": int(time.time()),
    }
    _ultimo["gc"] = resultado_cluster
    return resultado_cluster


def _verificar_sincronizacion() -> dict:
    anterior = _ultimo.get("sync")
    comprobacion = sincronizar(True)
    if anterior is None:
        _ultimo.pop("sync", None)
    else:
        _ultimo["sync"] = anterior
    return {
        "ok": (
            not comprobacion.get("unreachable")
            and not comprobacion.get("missing_count")
            and not comprobacion.get("mismatch_count")
            and not comprobacion.get("failed")
        ),
        "unreachable": comprobacion.get("unreachable", []),
        "missing_count": comprobacion.get("missing_count", 0),
        "mismatch_count": comprobacion.get("mismatch_count", 0),
    }


def _resultado_bloqueado(action: str, dry: bool, errores: list[str]) -> dict:
    return {
        "action": action, "dry_run": dry, "ok": False,
        "error": "; ".join(errores), "at": int(time.time()),
    }


def _resumen_ventana(resultado: dict, trigger: str) -> dict:
    progreso = resultado.get("progress") or {}
    estados_progreso = {
        paso.get("id"): paso.get("status")
        for paso in progreso.get("steps") or [] if isinstance(paso, dict)
    }
    pasos = resultado.get("steps") or {}
    retencion = pasos.get("retention") or {}
    gc = pasos.get("gc") or {}
    sync = pasos.get("sync") or {}
    verificacion = sync.get("verification") or {}
    inicio = int(progreso.get("started_at") or resultado.get("at") or time.time())
    fin = int(progreso.get("finished_at") or resultado.get("at") or time.time())
    return {
        "schema": 1,
        "run_id": resultado.get("run_id"),
        "dry_run": bool(resultado.get("dry_run")),
        "status": "completed" if resultado.get("ok") else "failed",
        "ok": bool(resultado.get("ok")),
        "trigger": trigger if trigger in {"manual", "scheduled"} else "manual",
        "coordinator": resultado.get("coordinator"),
        "nodes": resultado.get("nodes") or [],
        "started_at": inicio,
        "finished_at": fin,
        "duration_seconds": max(0, fin - inicio),
        "message": progreso.get("message") or "",
        "retention": {
            "status": estados_progreso.get("retention") or (
                "blocked" if retencion.get("blocked") else (
                "failed" if retencion.get("error") or retencion.get("failed")
                else "completed"
                )
            ),
            "count": int(
                retencion.get("would_remove", len(retencion.get("removed") or [])) or 0
            ),
        },
        "gc": {
            "status": estados_progreso.get("gc") or (
                "skipped" if gc.get("skipped") else (
                "failed" if gc.get("error") or not gc.get("ok", True)
                or not gc.get("registry_back", True) else "completed"
                )
            ),
            "freed_bytes": int(gc.get("freed_bytes", 0) or 0),
            "registry_back": bool(gc.get("registry_back", True)),
        },
        "sync": {
            "status": estados_progreso.get("sync") or (
                "skipped" if sync.get("skipped") else (
                "failed" if sync.get("error") or sync.get("failed")
                or sync.get("unreachable") or not verificacion.get("ok", True)
                else "completed"
                )
            ),
            "copied": len(sync.get("copied") or []),
            "missing_count": int(sync.get("missing_count", 0) or 0),
            "mismatch_count": int(sync.get("mismatch_count", 0) or 0),
            "verified": bool(verificacion.get("ok", resultado.get("dry_run", False))),
        },
    }


def _guardar_historial_cluster(
    resumen: dict, run_id: str, adquiridos: list[str], owner: str,
) -> list[str]:
    clave = "last_preview" if resumen.get("dry_run") else "last_run"
    errores: list[str] = []
    error_local = _actualizar_estado_persistente(clave, resumen)
    if error_local:
        errores.append(f"{NODE_NAME}: {error_local}")
    for nombre in adquiridos:
        if nombre == NODE_NAME:
            continue
        respuesta, error = _solicitud_panel(
            nombre, "/api/internal/cluster/history", "POST",
            {"run_id": run_id, "key": clave, "summary": resumen}, timeout=15,
        )
        if error or not respuesta or not respuesta.get("ok"):
            errores.append(error or f"{nombre}: no confirmó el historial")
    if errores:
        print(f"[mantenimiento] historial incompleto: {'; '.join(errores)}", flush=True)
    return errores


def _guardar_coordinador_configurado(coordinador: str) -> str | None:
    """Mantiene el valor de arranque alineado con la selección del panel."""
    if not PERSISTED_CONFIG.exists():
        # Compatibilidad con instalaciones antiguas gobernadas por variables.
        return None
    try:
        with _setup_lock:
            document = json.loads(PERSISTED_CONFIG.read_text(encoding="utf-8"))
            environment = document.get("environment") if isinstance(document, dict) else None
            if (not isinstance(document, dict) or document.get("schema") != 1
                    or not isinstance(environment, dict)
                    or set(environment) != PERSISTED_ENV_KEYS):
                return "la configuración persistida no tiene un formato válido"
            environment["MAINTENANCE_COORDINATOR"] = coordinador
            _atomic_write(PERSISTED_CONFIG, (
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode())
        return None
    except (OSError, ValueError, TypeError) as error:
        return f"no se pudo guardar el coordinador en la configuración ({type(error).__name__})"


def _aplicar_coordinador_local(coordinador: str) -> str | None:
    if coordinador not in _nodos():
        return "el coordinador elegido no pertenece al clúster"
    anterior = _estado_persistente_snapshot().get("coordinator")
    error_estado = _actualizar_estado_persistente("coordinator", coordinador)
    if error_estado:
        return error_estado
    error_config = _guardar_coordinador_configurado(coordinador)
    if not error_config:
        return None
    error_reversion = _actualizar_estado_persistente("coordinator", anterior)
    if error_reversion:
        return f"{error_config}; tampoco se pudo revertir el estado ({error_reversion})"
    return error_config


def _cambiar_coordinador_coordinado(coordinador: str) -> dict:
    config = _config_cluster()
    anterior = config["coordinator"]
    if coordinador not in config["nodes"]:
        return {"ok": False, "error": "el nodo elegido no pertenece al clúster"}
    if coordinador == anterior:
        return {"ok": True, "changed": False, "coordinator": anterior}
    if not _window_lock.acquire(blocking=False):
        return {"ok": False, "error": "hay un mantenimiento en curso"}

    run_id = secrets.token_hex(16)
    adquiridos: list[str] = []
    actualizados: list[str] = []
    try:
        # Cambiar el rol no ejecuta mantenimiento. Debe seguir siendo posible
        # con la función pausada en uno o varios nodos; conectividad,
        # topología y exclusión mediante lease sí continúan comprobándose.
        adquiridos, errores = _adquirir_leases(
            run_id, "coordinator-change", require_maintenance=False,
        )
        if errores:
            return {"ok": False, "error": "; ".join(errores)}

        for nombre in config["nodes"]:
            if nombre == NODE_NAME:
                continue
            respuesta, error = _solicitud_panel(
                nombre, "/api/internal/cluster/config/coordinator", "POST",
                {"run_id": run_id, "coordinator": coordinador}, timeout=15,
            )
            if error or not respuesta or not respuesta.get("ok"):
                # También se revierte el nodo cuya respuesta se perdió: pudo
                # guardar el cambio y cortarse la conexión antes de confirmarlo.
                for cambiado in reversed(actualizados + [nombre]):
                    _solicitud_panel(
                        cambiado, "/api/internal/cluster/config/coordinator", "POST",
                        {"run_id": run_id, "coordinator": anterior}, timeout=15,
                    )
                return {
                    "ok": False,
                    "error": error or f"{nombre} no aceptó el nuevo coordinador",
                }
            actualizados.append(nombre)

        error_local = _aplicar_coordinador_local(coordinador)
        if error_local:
            for cambiado in reversed(actualizados):
                _solicitud_panel(
                    cambiado, "/api/internal/cluster/config/coordinator", "POST",
                    {"run_id": run_id, "coordinator": anterior}, timeout=15,
                )
            return {"ok": False, "error": f"{NODE_NAME}: {error_local}"}

        return {
            "ok": True,
            "changed": True,
            "coordinator": coordinador,
            "previous_coordinator": anterior,
            "nodes": config["nodes"],
        }
    finally:
        if adquiridos:
            _liberar_leases(run_id, adquiridos, anterior)
        _window_lock.release()


def cambiar_coordinador(coordinador: str) -> dict:
    config = _config_cluster()
    if not config["ready"]:
        return {"ok": False, "error": "; ".join(config["problems"])}
    if coordinador not in config["nodes"]:
        return {"ok": False, "error": "el nodo elegido no pertenece al clúster"}
    if config["mode"] and not config["is_coordinator"]:
        resultado, error = _solicitud_panel(
            config["coordinator"],
            "/api/internal/cluster/coordinator/change", "POST",
            {"coordinator": coordinador}, timeout=90,
        )
        return resultado or {"ok": False, "error": error or "el coordinador no respondió"}
    return _cambiar_coordinador_coordinado(coordinador)


def _ejecutar_operacion_coordinada(action: str, dry: bool) -> dict:
    if not _window_lock.acquire(blocking=False):
        return _resultado_bloqueado(
            action, dry, ["ya hay una operación de mantenimiento en curso"],
        )
    run_id = secrets.token_hex(16)
    adquiridos: list[str] = []
    try:
        adquiridos, errores = _adquirir_leases(run_id, action)
        if errores:
            return _resultado_bloqueado(action, dry, errores)
        if action == "gc":
            return _recolectar_cluster(dry, run_id, adquiridos)
        if action == "retention":
            return aplicar_retencion(dry)
        if action == "sync":
            return sincronizar(dry)
        return _resultado_bloqueado(action, dry, ["acción desconocida"])
    finally:
        if adquiridos:
            _liberar_leases(run_id, adquiridos)
        _window_lock.release()


def ejecutar_accion_mantenimiento(
    action: str, dry: bool, trigger: str = "manual",
) -> dict:
    config = _config_cluster()
    if not config["ready"]:
        return _resultado_bloqueado(action, dry, config["problems"])
    if config["mode"] and not config["is_coordinator"]:
        resultado, error = _solicitud_panel(
            config["coordinator"], "/api/internal/cluster/action", "POST",
            {"action": action, "dry_run": dry, "trigger": trigger}, timeout=7400,
        )
        return resultado or _resultado_bloqueado(
            action, dry, [error or "el coordinador no devolvió un resultado"],
        )
    if action == "window":
        return ejecutar_ventana(dry, trigger)
    return _ejecutar_operacion_coordinada(action, dry)


# ─────────────────────────── ventana de mantenimiento ────────────────────────
#
# Una hora al día, en hora local (TZ del contenedor), el coordinador reserva
# todo el clúster y ejecuta una sola tanda. El orden NO es indiferente:
#
#   1. RETENCIÓN — borra las etiquetas que sobran de cada rama.
#   2. RECOLECCIÓN — libera en disco lo que la retención dejó sin referenciar.
#      Sin este paso, borrar etiquetas no devuelve un solo byte y el disco crece
#      igual: las versiones se acumulan aunque ya no se vean.
#   3. SINCRONIZACIÓN — iguala los registros configurados.
#
# Sincronizar antes de limpiar sería replicar a los nodos versiones que
# estamos a punto de borrar, para luego borrarlas varias veces. Primero se decide
# qué se guarda; después se reparte.

MAINTENANCE_HOUR = os.environ.get("MAINTENANCE_HOUR", "").strip()
MAINTENANCE_GC = _env_bool("MAINTENANCE_GC", True)
if MAINTENANCE_HOUR:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", MAINTENANCE_HOUR)
    if match is None or not (
        0 <= int(match.group(1)) < 24 and 0 <= int(match.group(2)) < 60
    ):
        raise RuntimeError("MAINTENANCE_HOUR debe usar una hora real en formato HH:MM")


def _proxima_ventana(desde: float | None = None) -> float | None:
    """Epoch de la próxima ejecución, u None si no hay hora configurada."""
    if not re.match(r"^\d{1,2}:\d{2}$", MAINTENANCE_HOUR):
        return None
    h, m = (int(x) for x in MAINTENANCE_HOUR.split(":"))
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    ahora = datetime.fromtimestamp(desde if desde else time.time())
    objetivo = ahora.replace(hour=h, minute=m, second=0, microsecond=0)
    ts = objetivo.timestamp()
    ahora_ts = desde or time.time()
    if ts <= ahora_ts:
        # timedelta respeta los cambios de horario local; sumar 86400 segundos
        # puede desplazar la ventana una hora durante el cambio DST.
        objetivo = objetivo + timedelta(days=1)
        ts = objetivo.timestamp()
    return ts


def ejecutar_ventana(dry: bool = False, trigger: str = "manual") -> dict:
    """Tanda completa del clúster, coordinada desde un único nodo."""
    if not _window_lock.acquire(blocking=False):
        return {
            "action": "window", "dry_run": dry, "ok": False,
            "error": "ya hay una ventana de mantenimiento en curso",
            "progress": _progreso_snapshot(),
        }
    if not _iniciar_progreso_ventana():
        _window_lock.release()
        return {
            "action": "window", "dry_run": dry, "ok": False,
            "error": "ya hay una ventana de mantenimiento en curso",
            "progress": _progreso_snapshot(),
        }

    pasos: dict = {}
    run_id = secrets.token_hex(16)
    adquiridos: list[str] = []
    config = _config_cluster()
    try:
        adquiridos, errores = _adquirir_leases(run_id, "window")
        if errores:
            motivo = "; ".join(errores)
            pasos["retention"] = {
                "action": "retention", "dry_run": dry, "blocked": True,
                "reason": motivo,
            }
            pasos["gc"] = {
                "action": "gc", "dry_run": dry, "skipped": True,
                "reason": "no se pudo reservar todo el clúster",
            }
            pasos["sync"] = {
                "action": "sync", "dry_run": dry, "skipped": True,
                "reason": "no se pudo reservar todo el clúster",
            }
            _actualizar_progreso_paso("retention", "blocked", motivo)
            _actualizar_progreso_paso("gc", "skipped", "No se ha detenido ningún registro.")
            _actualizar_progreso_paso("sync", "skipped", "No se ha modificado ningún registro.")
        else:
            _actualizar_progreso_paso(
                "retention", "running",
                "Clúster reservado. Calculando la retención por rama…",
            )
            pasos["retention"] = aplicar_retencion(dry)

        if pasos["retention"].get("blocked"):
            if "gc" not in pasos:
                pasos["gc"] = {
                    "action": "gc", "dry_run": dry, "skipped": True,
                    "reason": "se omitió porque la retención está bloqueada",
                }
                pasos["sync"] = {
                    "action": "sync", "dry_run": dry, "skipped": True,
                    "reason": "se omitió porque la retención está bloqueada",
                }
                _actualizar_progreso_paso(
                    "retention", "blocked",
                    pasos["retention"].get(
                        "reason", "Inventario incompleto; no se borra nada.",
                    ),
                )
                _actualizar_progreso_paso(
                    "gc", "skipped", "Omitido: la retención está bloqueada.",
                )
                _actualizar_progreso_paso(
                    "sync", "skipped", "Omitido: la retención está bloqueada.",
                )
        else:
            fallo_retencion = bool(
                pasos["retention"].get("error") or pasos["retention"].get("failed")
            )
            _actualizar_progreso_paso(
                "retention", "failed" if fallo_retencion else "completed",
                "La retención terminó con incidencias." if fallo_retencion
                else "Retención aplicada correctamente.",
            )
            gc_fatal = False
            if MAINTENANCE_GC:
                _actualizar_progreso_paso(
                    "gc", "running", "Liberando espacio nodo a nodo…",
                )
                pasos["gc"] = _recolectar_cluster(dry, run_id, adquiridos)
                fallo_gc = bool(
                    pasos["gc"].get("error")
                    or not pasos["gc"].get("ok", True)
                    or not pasos["gc"].get("registry_back", True)
                )
                gc_fatal = not pasos["gc"].get("registry_back", True)
                _actualizar_progreso_paso(
                    "gc", "failed" if fallo_gc else "completed",
                    "La liberación terminó con incidencias."
                    if fallo_gc else
                    f"{pasos['gc'].get('freed_bytes', 0)} bytes liberados en el clúster.",
                )
            else:
                pasos["gc"] = {
                    "action": "gc", "dry_run": dry, "skipped": True,
                    "reason": "MAINTENANCE_GC=0",
                }
                for nombre in config["nodes"]:
                    _actualizar_progreso_nodo(nombre, "skipped", "Omitido por configuración.")
                _actualizar_progreso_paso("gc", "skipped", "Omitido por configuración.")

            if gc_fatal:
                pasos["sync"] = {
                    "action": "sync", "dry_run": dry, "skipped": True,
                    "reason": "un registro no volvió tras la recolección",
                }
                _actualizar_progreso_paso(
                    "sync", "skipped",
                    "Omitido por seguridad: un registro no está disponible.",
                )
            else:
                _actualizar_progreso_paso(
                    "sync", "running", "Comparando los registros configurados…",
                )
                pasos["sync"] = sincronizar(dry)
                if not dry and not pasos["sync"].get("unreachable"):
                    pasos["sync"]["verification"] = _verificar_sincronizacion()
                verificacion = pasos["sync"].get("verification", {"ok": True})
                fallo_sync = bool(
                    pasos["sync"].get("error")
                    or pasos["sync"].get("failed")
                    or pasos["sync"].get("unreachable")
                    or not verificacion.get("ok", True)
                )
                _actualizar_progreso_paso(
                    "sync", "failed" if fallo_sync else "completed",
                    "La sincronización o su comprobación terminó con incidencias."
                    if fallo_sync else "Registros sincronizados y verificados.",
                )

        fallo = any(
            v.get("error") or v.get("blocked") or v.get("failed")
            or v.get("unreachable") or ("ok" in v and not v.get("ok"))
            for v in pasos.values()
            if not v.get("skipped")
        )
        estado = _finalizar_progreso_ventana(
            "failed" if fallo else "completed",
            "Mantenimiento terminado con incidencias." if fallo
            else "Mantenimiento completado correctamente.",
        )
        res = {"action": "window", "dry_run": dry, "ok": not fallo,
               "run_id": run_id, "coordinator": config["coordinator"],
               "nodes": config["nodes"],
               "trigger": trigger,
               "at": int(time.time()),
               "steps": {k: {kk: vv for kk, vv in v.items() if kk != "candidates"}
                         for k, v in pasos.items()},
               "progress": estado}
        _ultimo["window"] = res
        if adquiridos:
            resumen = _resumen_ventana(res, trigger)
            errores_historial = _guardar_historial_cluster(
                resumen, run_id, adquiridos, config["coordinator"],
            )
            res["history"] = resumen
            if errores_historial:
                res["history_errors"] = errores_historial
        return res
    except Exception as e:
        estado = _finalizar_progreso_ventana(
            "failed", f"Mantenimiento detenido por un error: {type(e).__name__}."
        )
        res = {
            "action": "window", "dry_run": dry, "ok": False,
            "run_id": run_id, "coordinator": config["coordinator"],
            "nodes": config["nodes"], "trigger": trigger,
            "at": int(time.time()), "error": f"{type(e).__name__}: {e}",
            "steps": {
                k: {kk: vv for kk, vv in v.items() if kk != "candidates"}
                for k, v in pasos.items()
            },
            "progress": estado,
        }
        _ultimo["window"] = res
        if adquiridos:
            resumen = _resumen_ventana(res, trigger)
            errores_historial = _guardar_historial_cluster(
                resumen, run_id, adquiridos, config["coordinator"],
            )
            res["history"] = resumen
            if errores_historial:
                res["history_errors"] = errores_historial
        return res
    finally:
        if adquiridos:
            _liberar_leases(run_id, adquiridos)
        _window_lock.release()


def _planificador():
    while True:
        siguiente = _proxima_ventana()
        if siguiente is None:
            time.sleep(300)          # sin hora puesta: se vuelve a mirar luego
            continue
        espera = max(30.0, siguiente - time.time())
        time.sleep(espera)
        if _proxima_ventana(time.time() - 60) is None:
            continue                 # la configuración cambió mientras dormía
        try:
            config = _config_cluster()
            if not config["ready"] or not config["is_coordinator"]:
                time.sleep(90)
                continue
            print(f"[mantenimiento] arrancando ventana de las {MAINTENANCE_HOUR}", flush=True)
            r = ejecutar_accion_mantenimiento("window", dry=False, trigger="scheduled")
            print(f"[mantenimiento] terminada: {json.dumps(r['steps'], ensure_ascii=False)[:500]}", flush=True)
        except Exception as e:
            print(f"[mantenimiento] la ventana fallo: {type(e).__name__}: {e}", flush=True)
        time.sleep(90)               # que no se dispare dos veces el mismo minuto


# ─────────────────────────── servidor ────────────────────────────────────────

TIPOS = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".woff2": "font/woff2",
    ".png": "image/png", ".map": "application/json",
}


class PanelHTTPServer(ThreadingHTTPServer):
    """No convierte cierres normales del navegador en trazas de error."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _estado_mantenimiento_local() -> dict:
    prox = _proxima_ventana()
    progreso = _progreso_snapshot()
    config = _config_cluster()
    lease = _lease_snapshot()
    return {
        "enabled": MAINT_ENABLED,
        "node": NODE_NAME,
        "keep_last": KEEP_LAST,
        "protected_tags": sorted(PROTECTED),
        "in_progress": MAINT_FLAG.exists() or progreso["running"] or bool(lease["run_id"]),
        "window_hour": MAINTENANCE_HOUR if prox else None,
        "window_gc": MAINTENANCE_GC,
        "window_next": int(prox) if prox else None,
        "timezone": time.tzname[0] if time.tzname else "?",
        "peers": sorted(PEERS),
        "cluster_mode": config["mode"],
        "cluster_nodes": config["nodes"],
        "cluster_ready": config["ready"],
        "cluster_error": "; ".join(config["problems"]) or None,
        "coordinator": config["coordinator"],
        "coordinator_source": config["coordinator_source"],
        "is_coordinator": config["is_coordinator"],
        "coordinator_reachable": config["is_coordinator"] or not config["mode"],
        "window_progress": progreso,
        "history": _estado_persistente_snapshot(),
        "last": _ultimo,
    }


def _estado_mantenimiento() -> dict:
    local = _estado_mantenimiento_local()
    config = _config_cluster()
    if not config["mode"] or config["is_coordinator"] or not config["ready"]:
        return local

    estado, error = _solicitud_panel(
        config["coordinator"], "/api/internal/cluster/status", timeout=10,
    )
    remoto = (estado or {}).get("maintenance")
    if not isinstance(remoto, dict):
        local["cluster_ready"] = False
        local["coordinator_reachable"] = False
        local["cluster_error"] = error or "no se puede consultar el coordinador"
        return local
    remoto = json.loads(json.dumps(remoto))
    remoto["node"] = NODE_NAME
    remoto["is_coordinator"] = False
    remoto["coordinator_reachable"] = True
    return remoto


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # el registro de accesos no aporta nada aquí y ensucia los logs

    def _responder(self, code: int, cuerpo: bytes, tipo: str, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and self._origin_permitido(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def _origin_permitido(self, origin: str) -> bool:
        if not origin:
            return True
        if "*" in CORS_ORIGINS or origin in CORS_ORIGINS:
            return True
        try:
            return urllib.parse.urlsplit(origin).netloc == self.headers.get("Host", "")
        except ValueError:
            return False

    def _json(self, datos, code: int = 200, headers=None):
        cuerpo = json.dumps(datos, ensure_ascii=False, indent=2).encode()
        self._responder(code, cuerpo, "application/json; charset=utf-8", headers)

    def _single_header(self, name: str) -> str:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            self.close_connection = True
            return ""
        return values[0] if values else ""

    def _session_token(self) -> str:
        raw = self._single_header("Cookie")
        if not raw:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return ""
        morsel = cookie.get(AUTH_COOKIE)
        return morsel.value if morsel else ""

    def _require_session(self, mutation: bool = False) -> dict | None:
        identity = _read_session(self._session_token())
        if not identity:
            self._json({"error": "Debes iniciar sesión", "code": "SESSION_REQUIRED"}, 401)
            return None
        if mutation:
            candidate = self._single_header("X-CSRF-Token")
            if not candidate or not hmac.compare_digest(candidate, identity["csrf_token"]):
                self._json({"error": "La verificación de la sesión no es válida", "code": "CSRF_INVALID"}, 403)
                return None
        return identity

    @staticmethod
    def _cookie(token: str, max_age: int) -> str:
        return "; ".join([
            f"{AUTH_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict",
            f"Max-Age={max(0, max_age)}",
        ])

    def _send_session(self, session: tuple[str, dict]):
        token, public = session
        self._json(
            {"authenticated": True, "session": public},
            headers={"Set-Cookie": self._cookie(
                token, public["expires_at"] - int(time.time()),
            )},
        )

    def _cuerpo_json_interno(self, ruta: str) -> tuple[dict | None, str | None]:
        try:
            longitud = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return None, "longitud de petición inválida"
        if longitud < 0 or longitud > 65536:
            return None, "petición demasiado grande"
        cuerpo = self.rfile.read(longitud) if longitud else b""
        autorizado, emisor = _peticion_cluster_autorizada(
            self.command, ruta, self.headers, cuerpo,
        )
        if not autorizado:
            return None, emisor
        try:
            datos = json.loads(cuerpo or b"{}")
        except ValueError:
            return None, "JSON inválido"
        if not isinstance(datos, dict):
            return None, "el cuerpo debe ser un objeto JSON"
        datos["_sender"] = emisor
        return datos, None

    def _cuerpo_publico(self) -> tuple[bytes | None, str | None]:
        """Consume el cuerpo antes de autenticar para mantener HTTP/1.1 alineado.

        Si la longitud no es segura se cierra la conexión: no podemos consumir
        de forma fiable un cuerpo desconocido y reutilizar el socket permitiría
        que esos bytes se interpretasen como la siguiente petición.
        """
        try:
            longitud = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.close_connection = True
            return None, "longitud de petición inválida"
        if longitud < 0 or longitud > 65536:
            self.close_connection = True
            return None, "petición demasiado grande"
        return self.rfile.read(longitud) if longitud else b"", None

    @staticmethod
    def _decodificar_json_publico(cuerpo: bytes) -> tuple[dict | None, str | None]:
        try:
            datos = json.loads(cuerpo or b"{}")
        except ValueError:
            return None, "JSON inválido"
        if not isinstance(datos, dict):
            return None, "el cuerpo debe ser un objeto JSON"
        return datos, None

    def _post_interno(self, ruta: str):
        datos, error = self._cuerpo_json_interno(ruta)
        if error or datos is None:
            return self._json({"error": error or "petición no autorizada"}, 403)
        sender = datos.pop("_sender")
        config = _config_cluster()

        if ruta == "/api/internal/cluster/action":
            action = str(datos.get("action", ""))
            if not config["is_coordinator"]:
                return self._json({"error": "este nodo no es el coordinador"}, 409)
            if action not in {"gc", "retention", "sync", "window"}:
                return self._json({"error": "acción desconocida"}, 400)
            return self._json(ejecutar_accion_mantenimiento(
                action, bool(datos.get("dry_run", True)),
                str(datos.get("trigger", "manual")),
            ))

        if ruta == "/api/internal/cluster/coordinator/change":
            if not config["is_coordinator"]:
                return self._json({"error": "este nodo no es el coordinador"}, 409)
            resultado = cambiar_coordinador(str(datos.get("coordinator", "")))
            return self._json(resultado, 200 if resultado.get("ok") else 409)

        run_id = str(datos.get("run_id", ""))
        if ruta == "/api/internal/cluster/lease/acquire":
            owner = str(datos.get("owner", ""))
            if sender != owner:
                return self._json({"error": "el emisor no coincide con el coordinador"}, 403)
            ok = _adquirir_lease_local(
                run_id, owner, str(datos.get("purpose", "maintenance")),
                int(datos.get("ttl", MAINTENANCE_LEASE_TTL)),
            )
            return self._json({"ok": ok, "node": NODE_NAME, "lease": _lease_snapshot()}, 200 if ok else 409)

        if ruta == "/api/internal/cluster/lease/release":
            lease = _lease_snapshot()
            if sender != lease.get("owner"):
                return self._json({"error": "sólo el propietario puede liberar la reserva"}, 403)
            ok = _liberar_lease_local(run_id, sender)
            return self._json({"ok": ok, "node": NODE_NAME}, 200 if ok else 409)

        if ruta == "/api/internal/cluster/gc":
            if not _lease_local_valida(run_id, sender):
                return self._json({"error": "reserva de clúster inválida"}, 409)
            return self._json(recolectar_basura(bool(datos.get("dry_run", True))))

        if ruta == "/api/internal/cluster/history":
            if not _lease_local_valida(run_id, sender):
                return self._json({"error": "reserva de clúster inválida"}, 409)
            clave = str(datos.get("key", ""))
            resumen = datos.get("summary")
            if clave not in {"last_run", "last_preview"} or not isinstance(resumen, dict):
                return self._json({"error": "historial inválido"}, 400)
            if resumen.get("run_id") != run_id:
                return self._json({"error": "el historial no pertenece a la reserva"}, 409)
            error_estado = _actualizar_estado_persistente(clave, resumen)
            return self._json(
                {"ok": not error_estado, "node": NODE_NAME, "error": error_estado},
                200 if not error_estado else 500,
            )

        if ruta == "/api/internal/cluster/config/coordinator":
            if not _lease_local_valida(run_id, sender):
                return self._json({"error": "reserva de clúster inválida"}, 409)
            error_estado = _aplicar_coordinador_local(str(datos.get("coordinator", "")))
            return self._json(
                {"ok": not error_estado, "node": NODE_NAME, "error": error_estado},
                200 if not error_estado else 409,
            )

        return self._json({"error": "no existe"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        """Las mutaciones públicas exigen login, CSRF y origen permitido."""
        ruta, _, consulta = self.path.partition("?")
        if ruta.startswith("/api/internal/cluster/"):
            try:
                return self._post_interno(ruta)
            except Exception as e:
                return self._json({"error": type(e).__name__, "detail": str(e)}, 500)

        cuerpo, error_cuerpo = self._cuerpo_publico()
        if error_cuerpo or cuerpo is None:
            return self._json({"error": error_cuerpo or "petición inválida"}, 400)

        origin = self.headers.get("Origin", "").rstrip("/")
        if ruta in {"/api/auth/setup", "/api/auth/session"}:
            if not self._origin_permitido(origin):
                return self._json({"error": "origen no permitido"}, 403)
            datos, error = self._decodificar_json_publico(cuerpo)
            if error or datos is None:
                return self._json({"error": error or "petición inválida"}, 400)
            try:
                session = (
                    _auth_setup(datos)
                    if ruta == "/api/auth/setup"
                    else _auth_login(datos, self.client_address[0])
                )
                return self._send_session(session)
            except AuthError as e:
                return self._json({"error": str(e)}, e.status)
            except RuntimeError as e:
                return self._json({"error": str(e)}, 503)

        if ruta in {"/api/setup", "/api/settings"}:
            if not self._origin_permitido(origin):
                return self._json({"error": "origen no permitido"}, 403)
            if not self._require_session(mutation=True):
                return None
            datos, error = self._decodificar_json_publico(cuerpo)
            if error or datos is None:
                return self._json({"error": error or "petición inválida"}, 400)
            try:
                with _setup_lock:
                    resultado = (
                        _save_initial_setup(datos)
                        if ruta == "/api/setup" else _save_settings(datos)
                    )
                self._json(resultado)
                reinicio = threading.Timer(0.5, os.kill, args=(os.getpid(), signal.SIGTERM))
                reinicio.daemon = True
                reinicio.start()
                return None
            except RuntimeError as e:
                return self._json({"error": str(e)}, 409)
        if (
            not self._origin_permitido(origin)
            or self.headers.get("X-Registry-Maintenance") != "1"
        ):
            return self._json({"error": "petición de mantenimiento no autorizada"}, 403)
        if not self._require_session(mutation=True):
            return None

        if ruta == "/api/maintenance/coordinator":
            datos, error = self._decodificar_json_publico(cuerpo)
            if error or datos is None:
                return self._json({"error": error or "petición inválida"}, 400)
            try:
                resultado = cambiar_coordinador(str(datos.get("coordinator", "")))
                return self._json(resultado, 200 if resultado.get("ok") else 409)
            except Exception as e:
                return self._json({"error": type(e).__name__, "detail": str(e)}, 500)

        parametros = urllib.parse.parse_qs(consulta, keep_blank_values=True)
        confirm = parametros.get("confirm") == ["1"]
        dry = not confirm

        try:
            if ruta == "/api/maintenance/gc":
                return self._json(ejecutar_accion_mantenimiento("gc", dry))
            if ruta == "/api/maintenance/retention":
                return self._json(ejecutar_accion_mantenimiento("retention", dry))
            if ruta == "/api/maintenance/sync":
                return self._json(ejecutar_accion_mantenimiento("sync", dry))
            if ruta == "/api/maintenance/window":
                # La tanda completa, la misma que corre sola cada noche.
                return self._json(ejecutar_accion_mantenimiento("window", dry))
            return self._json({"error": "no existe"}, 404)
        except Exception as e:
            return self._json({"error": type(e).__name__, "detail": str(e)}, 500)

    def do_GET(self):
        ruta, _, consulta = self.path.partition("?")
        parametros = urllib.parse.parse_qs(consulta, keep_blank_values=True)
        refrescar = parametros.get("refresh") == ["1"]

        try:
            if ruta == "/api/auth":
                return self._json(_auth_status(self._session_token()))
            if ruta == "/api/setup":
                return self._json(_setup_status())
            if ruta in {"/api/internal/cluster/status", "/api/internal/cluster/auth"}:
                datos, error = self._cuerpo_json_interno(ruta)
                if error or datos is None:
                    return self._json({"error": error or "petición no autorizada"}, 403)
                if ruta == "/api/internal/cluster/auth":
                    return self._json(_cluster_auth_snapshot())
                return self._json({
                    "ok": True,
                    "node": NODE_NAME,
                    "cluster": _config_cluster(),
                    "lease": _lease_snapshot(),
                    "maintenance": _estado_mantenimiento_local(),
                })

            if ruta == "/api/health":
                return self._json({"ok": True, "version": APP_VERSION})

            if ruta.startswith("/api/") and not self._require_session():
                return None

            if ruta == "/api/summary":
                d = datos_locales(refrescar)
                return self._json({k: v for k, v in d.items() if k != "_tags" and k != "projects"})

            if ruta == "/api/projects":
                d = datos_locales(refrescar)
                return self._json({
                    "node": d["node"], "generated_at": d["generated_at"],
                    "disk_available": d["disk_available"], "projects": d["projects"],
                })

            if ruta == "/api/drift":
                return self._json(datos_deriva(refrescar))

            if ruta == "/api/all":
                d = dict(datos_locales(refrescar))
                d.pop("_tags", None)
                d["drift"] = datos_deriva(refrescar)
                d["maintenance"] = _estado_mantenimiento()
                return self._json(d)

            if ruta == "/api/maintenance":
                return self._json(_estado_mantenimiento())

            if ruta == "/api/settings":
                return self._json(_settings_payload())

            if ruta == "/api/maintenance/retention":
                # Consultar el plan no borra nada, así que puede ir por GET.
                return self._json(plan_retencion())

            if ruta.startswith("/api/"):
                return self._json({"error": "no existe"}, 404)

            return self._estatico(ruta)
        except Exception as e:  # que un fallo no tumbe el servicio entero
            return self._json({"error": type(e).__name__, "detail": str(e)}, 500)

    def _estatico(self, ruta: str):
        rel = ruta.lstrip("/") or "index.html"
        root = WEB_ROOT.resolve()
        destino = (root / rel).resolve()
        # Que nadie salga del directorio con ../
        if destino != root and root not in destino.parents or not destino.is_file():
            destino = root / "index.html"  # rutas de la SPA
        if not destino.is_file():
            return self._responder(404, b"sin interfaz construida", "text/plain; charset=utf-8")
        tipo = TIPOS.get(destino.suffix, "application/octet-stream")
        self._responder(200, destino.read_bytes(), tipo)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and not self._origin_permitido(origin):
            return self._json({"error": "origen no permitido"}, 403)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-Registry-Maintenance, X-CSRF-Token",
            )
            self.send_header("Vary", "Origin")
        self.end_headers()

    def do_DELETE(self):
        ruta = self.path.split("?", 1)[0]
        origin = self.headers.get("Origin", "").rstrip("/")
        if ruta != "/api/auth/session":
            return self._json({"error": "no existe"}, 404)
        if not self._origin_permitido(origin):
            return self._json({"error": "origen no permitido"}, 403)
        return self._json(
            {"authenticated": False, "session": None},
            headers={"Set-Cookie": self._cookie("", 0)},
        )


if __name__ == "__main__" and sys.argv[1:] == ["--check-config"]:
    # Fuerza también las comprobaciones que combinan varias variables. El
    # entrypoint lo ejecuta antes de arrancar ningún proceso de larga duración.
    _config_cluster()
    raise SystemExit(0)


if __name__ == "__main__":
    prox = _proxima_ventana()
    config_cluster = _config_cluster()
    print(f"panel del registro · nodo={NODE_NAME} registro={REGISTRY_URL} "
          f"pares={list(PEERS) or 'ninguno'} puerto={PORT}", flush=True)
    print(f"  clúster: nodos={config_cluster['nodes']} · "
          f"coordinador={config_cluster['coordinator']} · "
          f"configuración={'correcta' if config_cluster['ready'] else 'INCOMPLETA'}", flush=True)
    if config_cluster["problems"]:
        print(f"  clúster bloqueado: {'; '.join(config_cluster['problems'])}", flush=True)
    print(f"  retención: {KEEP_LAST or 'apagada'} por rama · protegidas: {sorted(PROTECTED)}", flush=True)
    if not MAINT_ENABLED:
        print("  ventana de mantenimiento: desactivada (MAINTENANCE_ENABLED=0)", flush=True)
    elif prox and config_cluster["ready"]:
        print(f"  ventana de mantenimiento: {MAINTENANCE_HOUR} ({time.tzname[0]}), "
              f"próxima {datetime.fromtimestamp(prox):%d/%m %H:%M} · "
              f"{'este nodo coordina' if config_cluster['is_coordinator'] else 'en espera'}",
              flush=True)
        # Todos los nodos mantienen un planificador en espera. Sólo quien sea
        # coordinador en el momento de la ventana puede ejecutarla, de modo que
        # cambiarlo desde la interfaz no requiere reiniciar contenedores.
        threading.Thread(target=_planificador, daemon=True).start()
    elif prox and not config_cluster["ready"]:
        print("  ventana de mantenimiento: no se programa hasta completar el clúster", flush=True)
    else:
        print("  ventana de mantenimiento: sin programar (MAINTENANCE_HOUR vacío)", flush=True)
    PanelHTTPServer(("", PORT), Handler).serve_forever()
