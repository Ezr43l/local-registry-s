#!/usr/bin/env python3
"""Renderiza una copia de la plantilla pública sin guardar topología ni secretos."""

from __future__ import annotations

import argparse
import os
import stat
import sys
# La entrada se confina en read_template antes de llegar al parser.
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path


MAX_TEMPLATE_SIZE = 1024 * 1024
LEGACY_RUNTIME_TARGETS = {
    "/run/secrets/local-registry", "NODE_NAME", "PEERS", "PANEL_PEERS",
    "CACHE_TTL", "STATS_ENABLED", "MAINTENANCE_ENABLED",
    "MAINTENANCE_ADMIN_TOKEN_FILE", "KEEP_LAST", "PROTECTED_TAGS",
    "MAINTENANCE_HOUR", "MAINTENANCE_GC", "MAINTENANCE_COORDINATOR",
    "MAINTENANCE_CLUSTER_TOKEN_FILE", "MAINTENANCE_LEASE_TTL",
    "RETENTION_REQUIRE_ALL_NODES", "RETENTION_BRANCH_PATTERN", "SYNC_SOURCE",
    "CORS_ALLOWED_ORIGINS", "TZ",
}


def read_template(path: Path) -> ET.Element:
    if path.is_symlink():
        raise ValueError("la plantilla no puede ser un enlace simbólico")
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
            raise ValueError("la plantilla debe ser un fichero regular")
        if metadata.st_size <= 0 or metadata.st_size > MAX_TEMPLATE_SIZE:
            raise ValueError("la plantilla está vacía o supera 1 MiB")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_TEMPLATE_SIZE:
            chunk = os.read(descriptor, min(65536, MAX_TEMPLATE_SIZE + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > MAX_TEMPLATE_SIZE:
        raise ValueError("la plantilla supera 1 MiB")
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise ValueError("la plantilla no admite DTD ni entidades declaradas")
    # ElementTree ya no recibe XML arbitrario: sólo un fichero regular acotado,
    # abierto sin seguir enlaces y sin DTD/entidades.
    return ET.fromstring(payload)  # nosec B314


def parse_assignment(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("se esperaba TARGET=VALOR")
    return tuple(value.split("=", 1))  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--set", dest="values", action="append", default=[], type=parse_assignment)
    args = parser.parse_args()

    if (
        not args.repository
        or len(args.repository) > 512
        or "\\" in args.repository
        or any(character.isspace() or ord(character) < 32 for character in args.repository)
    ):
        parser.error("repository no es una referencia OCI portable")

    try:
        root = read_template(args.template)
    except (OSError, ValueError, ET.ParseError) as error:
        parser.error(f"plantilla no válida: {error}")
    if root.tag != "Container":
        parser.error("la raíz de la plantilla debe ser Container")
    repositories = root.findall("Repository")
    if len(repositories) != 1:
        parser.error("la plantilla debe contener un único Repository")
    repository = repositories[0]
    repository.text = args.repository

    config_items = root.findall("Config")
    targets = [item.get("Target") for item in config_items]
    if any(not target for target in targets) or len(targets) != len(set(targets)):
        parser.error("la plantilla contiene Target vacíos o duplicados")
    configs = {item.get("Target", ""): item for item in config_items}
    assigned: set[str] = set()
    for target, value in args.values:
        if target not in configs and target not in LEGACY_RUNTIME_TARGETS:
            parser.error(f"Target desconocido: {target}")
        if target in assigned:
            parser.error(f"Target repetido en --set: {target}")
        assigned.add(target)
        if target in configs:
            configs[target].text = value

    sys.stdout.buffer.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
    ET.ElementTree(root).write(sys.stdout.buffer, encoding="utf-8", xml_declaration=False)
    sys.stdout.buffer.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
