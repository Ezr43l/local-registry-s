#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

STAGE="$TEST_ROOT/stage"
ARCHIVE="$TEST_ROOT/sources.tar.gz"
MANIFEST="$TEST_ROOT/manifest.tsv"
mkdir -p "$STAGE/distfiles"

printf '%s\n' \
  $'platform\tpackage\tversion\tarchitecture\tlicense\torigin\taports_commit\tupstream_url' \
  $'amd64\tbusybox\t1-r0\tx86_64\tGPL-2.0-only\tbusybox\taaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\thttps://busybox.net/' \
  $'amd64\treadline\t8-r0\tx86_64\tGPL-3.0-or-later\treadline\tbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\thttps://tiswww.case.edu/php/chet/readline/rltop.html' \
  $'arm64\tca-certificates\t1-r0\taarch64\tMPL-2.0 AND MIT\tca-certificates\tcccccccccccccccccccccccccccccccccccccccc\thttps://www.mozilla.org/' \
  > "$STAGE/alpine-copyleft-sources-manifest.tsv"
cp "$STAGE/alpine-copyleft-sources-manifest.tsv" "$MANIFEST"

printf '%s\n' \
  $'busybox\taaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tmain/busybox' \
  $'readline\tbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tmain/readline' \
  $'ca-certificates\tcccccccccccccccccccccccccccccccccccccccc\tmain/ca-certificates' \
  > "$STAGE/APORTS-RECIPES.tsv"
while IFS=$'\t' read -r _origin commit recipe; do
  mkdir -p "$STAGE/aports/$commit/$recipe"
  printf '# fixture\n' > "$STAGE/aports/$commit/$recipe/APKBUILD"
done < "$STAGE/APORTS-RECIPES.tsv"

printf 'verified source\n' > "$STAGE/distfiles/source.txt"
(cd "$STAGE" && sha512sum distfiles/source.txt > DISTFILES.sha512)
printf 'fixture\n' > "$STAGE/README.txt"
printf 'fixture\n' > "$STAGE/THIRD_PARTY_NOTICES.md"
ln -s README.txt "$STAGE/safe-relative-link"
tar -czf "$ARCHIVE" -C "$STAGE" .

"$REPO_ROOT/scripts/verify-alpine-copyleft-archive.sh" "$ARCHIVE" "$MANIFEST"

cp "$MANIFEST" "$TEST_ROOT/manifest-good.tsv"
printf '# diferencia\n' >> "$MANIFEST"
if "$REPO_ROOT/scripts/verify-alpine-copyleft-archive.sh" "$ARCHIVE" "$MANIFEST" \
    >/dev/null 2>&1; then
  echo "El verificador aceptó un manifiesto externo distinto del interno" >&2
  exit 1
fi
cp "$TEST_ROOT/manifest-good.tsv" "$MANIFEST"

ln -s ../../outside "$STAGE/escape-link"
tar -czf "$TEST_ROOT/escape-link.tar.gz" -C "$STAGE" .
rm "$STAGE/escape-link"
if "$REPO_ROOT/scripts/verify-alpine-copyleft-archive.sh" \
    "$TEST_ROOT/escape-link.tar.gz" "$MANIFEST" >/dev/null 2>&1; then
  echo "El verificador aceptó un enlace que escapa del archivo" >&2
  exit 1
fi

printf 'tampered source\n' > "$STAGE/distfiles/source.txt"
tar -czf "$TEST_ROOT/tampered.tar.gz" -C "$STAGE" .
if "$REPO_ROOT/scripts/verify-alpine-copyleft-archive.sh" \
    "$TEST_ROOT/tampered.tar.gz" "$MANIFEST" >/dev/null 2>&1; then
  echo "El verificador aceptó un distfile cuyo SHA-512 no coincide" >&2
  exit 1
fi

echo "Verificador de fuentes copyleft: caso íntegro aceptado y manipulaciones rechazadas."
