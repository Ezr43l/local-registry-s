#!/usr/bin/env python3
"""Valida y extrae de forma segura el archivo de fuentes de una release."""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath
import sys
import tarfile
from typing import NoReturn


MAX_MEMBERS = 10_000
MAX_ARCHIVE_SIZE = 512 * 1024 * 1024
MAX_MEMBER_SIZE = 256 * 1024 * 1024
MAX_TOTAL_SIZE = 512 * 1024 * 1024
MAX_PATH_LENGTH = 4_096
ALLOWED_TYPES = {
    tarfile.REGTYPE,
    tarfile.AREGTYPE,
    tarfile.DIRTYPE,
    tarfile.SYMTYPE,
}


def fail(message: str) -> NoReturn:
    raise ValueError(message)


def has_unsafe_text(value: str) -> bool:
    return (
        "\x00" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def normalize_member_name(name: str) -> str:
    if not name or len(name) > MAX_PATH_LENGTH or has_unsafe_text(name):
        fail("nombre de miembro vacío o no portable")
    path = PurePosixPath(name)
    if path.is_absolute():
        fail(f"ruta absoluta: {name!r}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if any(part == ".." for part in parts):
        fail(f"traversal en miembro: {name!r}")
    if parts and len(parts[0]) >= 2 and parts[0][1] == ":":
        fail(f"ruta con unidad: {name!r}")
    return "/".join(parts) or "."


def validate_link(member_name: str, link_name: str) -> None:
    if (
        not link_name
        or len(link_name) > MAX_PATH_LENGTH
        or has_unsafe_text(link_name)
        or posixpath.isabs(link_name)
    ):
        fail(f"destino de enlace inseguro en {member_name!r}")
    first = PurePosixPath(link_name).parts[:1]
    if first and len(first[0]) >= 2 and first[0][1] == ":":
        fail(f"destino con unidad en {member_name!r}")
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(member_name), link_name)
    )
    if resolved == ".." or resolved.startswith("../") or posixpath.isabs(resolved):
        fail(f"el enlace escapa del archivo: {member_name!r} -> {link_name!r}")


def validate_and_extract(archive_path: Path, extract_root: Path) -> None:
    if not archive_path.is_file() or archive_path.is_symlink():
        fail("el archivo debe ser regular y no puede ser un enlace")
    if archive_path.stat().st_size > MAX_ARCHIVE_SIZE:
        fail("el archivo comprimido supera el tamaño máximo")
    if not extract_root.is_dir() or extract_root.is_symlink():
        fail("el directorio de extracción debe existir y no ser un enlace")

    with tarfile.open(archive_path, mode="r:gz") as archive:
        members: list[tarfile.TarInfo] = []
        seen: set[str] = set()
        total_size = 0
        for member in archive:
            if len(members) >= MAX_MEMBERS:
                fail("cantidad de miembros desproporcionada")
            members.append(member)
            if member.type not in ALLOWED_TYPES:
                fail(f"tipo TAR no permitido para {member.name!r}")
            normalized = normalize_member_name(member.name)
            if normalized in seen:
                fail(f"miembro duplicado tras normalizar: {member.name!r}")
            seen.add(normalized)
            if member.mode & 0o6000:
                fail(f"bits setuid/setgid no permitidos en {member.name!r}")
            if member.size < 0 or member.size > MAX_MEMBER_SIZE:
                fail(f"miembro demasiado grande: {member.name!r}")
            total_size += member.size
            if total_size > MAX_TOTAL_SIZE:
                fail("tamaño expandido total desproporcionado")
            if member.issym():
                validate_link(normalized, member.linkname)

        if not members:
            fail("el archivo no contiene miembros")

        archive.extractall(path=extract_root, members=members, filter="data")


def main() -> int:
    if len(sys.argv) != 3:
        print("uso: verify-tar-members.py ARCHIVO DIRECTORIO", file=sys.stderr)
        return 2
    try:
        validate_and_extract(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, tarfile.TarError, ValueError) as error:
        print(f"Archivo TAR inseguro o inválido: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
