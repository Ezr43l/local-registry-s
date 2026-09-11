#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || {
  echo "uso: verify-alpine-copyleft-archive.sh ARCHIVO MANIFIESTO" >&2
  exit 2
}
ARCHIVE="$1"
MANIFEST="$2"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for command_name in awk cmp cut gzip grep mktemp python3 sha512sum sort tail; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Falta el comando requerido: $command_name" >&2
    exit 2
  }
done
for input in "$ARCHIVE" "$MANIFEST"; do
  [[ -f "$input" && ! -L "$input" && -s "$input" ]] || {
    echo "El artefacto debe ser un fichero regular no vacío: $input" >&2
    exit 2
  }
done

VERIFY_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf -- "$VERIFY_ROOT"
}
trap cleanup EXIT HUP INT TERM

gzip -t "$ARCHIVE"
python3 "$REPO_ROOT/scripts/verify-tar-members.py" "$ARCHIVE" "$VERIFY_ROOT"
INTERNAL_MANIFEST="$VERIFY_ROOT/alpine-copyleft-sources-manifest.tsv"
for required in "$INTERNAL_MANIFEST" "$VERIFY_ROOT/APORTS-RECIPES.tsv" \
    "$VERIFY_ROOT/DISTFILES.sha512" "$VERIFY_ROOT/README.txt" \
    "$VERIFY_ROOT/THIRD_PARTY_NOTICES.md"; do
  [[ -f "$required" && ! -L "$required" && -s "$required" ]]
done
cmp "$MANIFEST" "$INTERNAL_MANIFEST"

awk -F '\t' '
  NR == 1 {
    if ($0 != "platform\tpackage\tversion\tarchitecture\tlicense\torigin\taports_commit\tupstream_url") {
      exit 1
    }
    next
  }
  NF != 8 || $1 !~ /^(amd64|arm64)$/ || $2 == "" || $3 == "" ||
      $4 !~ /^(x86_64|aarch64)$/ || $5 == "" || $6 == "" ||
      $7 !~ /^[0-9a-f]{40}$/ { exit 1 }
  {
    rows++
    if ($2 == "busybox") busybox = 1
    if ($2 == "readline") readline = 1
    if ($2 == "ca-certificates" && toupper($5) ~ /(^|[^A-Z0-9])MPL([^A-Z0-9]|$)/) {
      ca_mpl = 1
    }
  }
  END { exit !(rows > 0 && busybox && readline && ca_mpl) }
' "$INTERNAL_MANIFEST"

awk -F '\t' '
  NF != 3 || $1 !~ /^[a-z0-9][a-z0-9+._-]*$/ ||
      $2 !~ /^[0-9a-f]{40}$/ ||
      $3 !~ /^(main|community)\/[a-z0-9][a-z0-9+._-]*$/ { exit 1 }
  { rows++ }
  END { exit !(rows > 0) }
' "$VERIFY_ROOT/APORTS-RECIPES.tsv"
cut -f6,7 "$INTERNAL_MANIFEST" | tail -n +2 | LC_ALL=C sort -u \
  > "$VERIFY_ROOT/manifest-origins.tsv"
cut -f1,2 "$VERIFY_ROOT/APORTS-RECIPES.tsv" | LC_ALL=C sort -u \
  > "$VERIFY_ROOT/recipe-origins.tsv"
cmp "$VERIFY_ROOT/manifest-origins.tsv" "$VERIFY_ROOT/recipe-origins.tsv"
while IFS=$'\t' read -r _origin commit recipe_path; do
  recipe="$VERIFY_ROOT/aports/$commit/$recipe_path/APKBUILD"
  [[ -f "$recipe" && ! -L "$recipe" && -s "$recipe" ]]
done < "$VERIFY_ROOT/APORTS-RECIPES.tsv"

awk '
  length($1) != 128 || $1 !~ /^[0-9a-f]+$/ ||
      substr($0, 129, 2) != "  " { exit 1 }
  {
    path = substr($0, 131)
    if (path !~ /^distfiles\// || path ~ /(^|\/)\.\.(\/|$)/) exit 1
    rows++
  }
  END { exit !(rows > 0) }
' "$VERIFY_ROOT/DISTFILES.sha512"
(cd "$VERIFY_ROOT" && sha512sum -c DISTFILES.sha512)

echo "Archivo de fuentes copyleft verificado: manifiesto recíproco, recetas y distfiles íntegros."
