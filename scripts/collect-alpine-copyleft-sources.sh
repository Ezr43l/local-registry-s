#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Uso: collect-alpine-copyleft-sources.sh OUTPUT_DIR PLATFORM=INSTALLED_DB [...]

Cada INSTALLED_DB es una copia exacta de /lib/apk/db/installed obtenida de una
arquitectura de la imagen. El directorio de salida debe existir y no puede
contener los dos artefactos que genera esta puerta.
EOF
  exit 2
}

[[ $# -ge 2 ]] || usage
OUTPUT_DIR="$1"
shift
[[ -d "$OUTPUT_DIR" && ! -L "$OUTPUT_DIR" ]] || {
  echo "El directorio de salida debe existir y no ser un enlace: $OUTPUT_DIR" >&2
  exit 2
}

ARCHIVE="$OUTPUT_DIR/alpine-copyleft-sources.tar.gz"
MANIFEST="$OUTPUT_DIR/alpine-copyleft-sources-manifest.tsv"
[[ ! -e "$ARCHIVE" && ! -e "$MANIFEST" ]] || {
  echo "La puerta no sobrescribe artefactos existentes" >&2
  exit 2
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST_AWK="$REPO_ROOT/scripts/apk-installed-manifest.awk"
[[ -f "$MANIFEST_AWK" ]] || {
  echo "Falta el parser de la base APK: $MANIFEST_AWK" >&2
  exit 2
}
for command_name in abuild awk git gzip sha512sum sort tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Falta el comando requerido: $command_name" >&2
    exit 2
  }
done

WORK_ROOT="$(mktemp -d)"
published=0
cleanup() {
  if [[ $published -eq 0 ]]; then
    # Ambos nombres estaban ausentes al entrar; retirar una publicación parcial
    # hace que un reintento sea seguro y nunca borra artefactos preexistentes.
    rm -f -- "$ARCHIVE" "$MANIFEST"
  fi
  rm -rf -- "$WORK_ROOT"
}
trap cleanup EXIT

STAGE="$WORK_ROOT/stage"
RECIPES="$STAGE/aports"
DISTFILES="$STAGE/distfiles"
RAW_MANIFEST="$WORK_ROOT/copyleft-raw.tsv"
INTERNAL_MANIFEST="$STAGE/alpine-copyleft-sources-manifest.tsv"
STAGED_ARCHIVE="$WORK_ROOT/alpine-copyleft-sources.tar.gz"
mkdir -p "$RECIPES" "$DISTFILES"

header_written=0
for source_arg in "$@"; do
  [[ "$source_arg" == *=* ]] || usage
  platform="${source_arg%%=*}"
  installed_db="${source_arg#*=}"
  [[ "$platform" =~ ^[a-z0-9][a-z0-9_-]{0,31}$ \
      && -f "$installed_db" && ! -L "$installed_db" ]] || {
    echo "Entrada de plataforma/base APK inválida: $source_arg" >&2
    exit 2
  }
  parsed="$WORK_ROOT/$platform.tsv"
  awk -v platform="$platform" -v copyleft=1 -f "$MANIFEST_AWK" \
    "$installed_db" > "$parsed"
  if [[ $header_written -eq 0 ]]; then
    cat "$parsed" > "$RAW_MANIFEST"
    header_written=1
  else
    tail -n +2 "$parsed" >> "$RAW_MANIFEST"
  fi
done

{
  head -n 1 "$RAW_MANIFEST"
  tail -n +2 "$RAW_MANIFEST" | LC_ALL=C sort -u
} > "$INTERNAL_MANIFEST"
[[ "$(wc -l < "$INTERNAL_MANIFEST" | tr -d ' ')" -gt 1 ]] || {
  echo "La imagen no produjo ningún origen Alpine copyleft; se rechaza el resultado" >&2
  exit 1
}

GIT_DIR="$WORK_ROOT/aports.git"
git -c init.defaultBranch=main init --bare "$GIT_DIR" >/dev/null
git -C "$GIT_DIR" remote add origin https://gitlab.alpinelinux.org/alpine/aports.git
git -C "$GIT_DIR" config remote.origin.promisor true
git -C "$GIT_DIR" config remote.origin.partialclonefilter blob:none
git -C "$GIT_DIR" config fetch.fsckObjects true
git -C "$GIT_DIR" config transfer.fsckObjects true
mapfile -t aports_commits < <(
  cut -f7 "$INTERNAL_MANIFEST" | tail -n +2 | LC_ALL=C sort -u
)
[[ ${#aports_commits[@]} -gt 0 ]]
for commit in "${aports_commits[@]}"; do
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || {
    echo "Commit aports inválido: $commit" >&2
    exit 1
  }
done
fetch_complete=0
for attempt in 1 2 3; do
  if git -C "$GIT_DIR" fetch --quiet --no-tags --filter=blob:none --depth=1 \
      origin "${aports_commits[@]}"; then
    fetch_complete=1
    break
  fi
  rm -f -- "$GIT_DIR/shallow.lock"
  if [[ $attempt -lt 3 ]]; then
    printf 'Descarga de aports interrumpida; reintento %s de 3.\n' "$((attempt + 1))" >&2
    sleep "$attempt"
  fi
done
[[ $fetch_complete -eq 1 ]] || {
  echo "No se pudieron obtener los commits exactos de aports tras 3 intentos" >&2
  exit 1
}

while IFS=$'\t' read -r origin commit; do
      [[ "$origin" =~ ^[a-z0-9][a-z0-9+._-]{0,127}$ \
          && "$commit" =~ ^[0-9a-f]{40}$ ]] || {
        echo "Origen o commit aports inválido: $origin $commit" >&2
        exit 1
      }
      git -C "$GIT_DIR" cat-file -e "$commit^{commit}"
      mapfile -t recipe_paths < <(
        git -C "$GIT_DIR" ls-tree -r --name-only "$commit" \
          | awk -v origin="$origin" \
              '$0 == "main/" origin "/APKBUILD" ||
               $0 == "community/" origin "/APKBUILD" { print }'
      )
      [[ ${#recipe_paths[@]} -eq 1 ]] || {
        echo "Se esperaba un único APKBUILD para $origin@$commit; encontrados: ${#recipe_paths[@]}" >&2
        exit 1
      }
      recipe_path="${recipe_paths[0]%/APKBUILD}"
      destination="$RECIPES/$commit"
      mkdir -p "$destination"
      git -C "$GIT_DIR" archive "$commit" "$recipe_path" \
        | tar -C "$destination" -xf -
      printf '%s\t%s\t%s\n' "$origin" "$commit" "$recipe_path"
done < <(cut -f6,7 "$INTERNAL_MANIFEST" | tail -n +2 | LC_ALL=C sort -u) \
  > "$STAGE/APORTS-RECIPES.tsv"

chown -R source-builder:source-builder "$WORK_ROOT"
while IFS=$'\t' read -r origin commit recipe_path; do
  recipe_dir="$RECIPES/$commit/$recipe_path"
  [[ -f "$recipe_dir/APKBUILD" ]]
  verify_dir="$WORK_ROOT/verify/$commit/$recipe_path"
  mkdir -p "$(dirname "$verify_dir")"
  cp -a "$recipe_dir" "$verify_dir"
  chown -R source-builder:source-builder "$verify_dir"
  su source-builder -s /bin/sh -c \
    "cd '$verify_dir' && SRCDEST='$DISTFILES' abuild fetch verify"
  printf 'Fuentes verificadas: %s@%s\n' "$origin" "$commit"
done < "$STAGE/APORTS-RECIPES.tsv"

(cd "$STAGE" \
  && find distfiles -type f -print0 | LC_ALL=C sort -z \
    | xargs -0 sha512sum) > "$STAGE/DISTFILES.sha512"
[[ -s "$STAGE/DISTFILES.sha512" ]] || {
  echo "La puerta no descargó ningún distfile" >&2
  exit 1
}

cp "$REPO_ROOT/THIRD_PARTY_NOTICES.md" "$STAGE/THIRD_PARTY_NOTICES.md"
cat > "$STAGE/README.txt" <<'EOF'
Este archivo acompaña las partes copyleft de los paquetes Alpine presentes en
la imagen. Cada receta procede del commit aports registrado por el propio APK;
`abuild fetch verify` verificó todos los distfiles contra los hashes de ese
APKBUILD. APORTS-RECIPES.tsv vincula origen, commit y receta. DISTFILES.sha512
permite comprobar de nuevo el contenido descargado.
EOF

tar --sort=name --format=posix \
  --pax-option=delete=atime,delete=ctime \
  --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
  -C "$STAGE" -cf - . | gzip -n -9 > "$STAGED_ARCHIVE"

gzip -t "$STAGED_ARCHIVE"
tar -tzf "$STAGED_ARCHIVE" >/dev/null
cp "$INTERNAL_MANIFEST" "$MANIFEST"
cp "$STAGED_ARCHIVE" "$ARCHIVE"
published=1
printf 'Archivo de fuentes copyleft: %s (%s bytes)\n' \
  "$ARCHIVE" "$(wc -c < "$ARCHIVE" | tr -d ' ')"
