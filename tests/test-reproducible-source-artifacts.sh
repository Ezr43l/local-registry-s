#!/usr/bin/env bash
set -euo pipefail

ARTIFACT_ROOT="${1:?Uso: $0 DIRECTORIO [NUMERO_DE_GENERACIONES]}"
EXPECTED_COUNT="${2:-2}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ -d "$ARTIFACT_ROOT" && ! -L "$ARTIFACT_ROOT" ]]
[[ "$EXPECTED_COUNT" =~ ^[1-9][0-9]*$ ]]

mapfile -d '' archives < <(
  find "$ARTIFACT_ROOT" -type f -name alpine-copyleft-sources.tar.gz \
    -print0 | sort -z
)
[[ "${#archives[@]}" -eq "$EXPECTED_COUNT" ]]

reference_archive=""
reference_manifest=""
for archive in "${archives[@]}"; do
  manifest="$(dirname "$archive")/alpine-copyleft-sources-manifest.tsv"
  bash "$REPO_ROOT/scripts/verify-alpine-copyleft-archive.sh" \
    "$archive" "$manifest"
  if [[ -z "$reference_archive" ]]; then
    reference_archive="$archive"
    reference_manifest="$manifest"
  else
    cmp "$reference_archive" "$archive"
    cmp "$reference_manifest" "$manifest"
  fi
done

echo "$EXPECTED_COUNT generaciones de fuentes son válidas y byte a byte idénticas."
