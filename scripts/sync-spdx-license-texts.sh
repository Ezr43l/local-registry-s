#!/usr/bin/env bash
set -euo pipefail

SPDX_COMMIT="c4a7237ec8f4654e867546f9f409749300f1bf4c"
SPDX_TAG="v3.28.0"
OUTPUT_DIR="${1:-third_party/spdx}"

[[ ! -e "$OUTPUT_DIR" ]] || {
  echo "El directorio de salida ya existe; no se sobrescribe: $OUTPUT_DIR" >&2
  exit 2
}

work_root="$(mktemp -d)"
cleanup() {
  rm -rf -- "$work_root"
}
trap cleanup EXIT

git -c init.defaultBranch=main init --bare "$work_root/license-list-data.git" >/dev/null
git -C "$work_root/license-list-data.git" remote add origin \
  https://github.com/spdx/license-list-data.git
git -C "$work_root/license-list-data.git" fetch --quiet --depth=1 origin "$SPDX_COMMIT"
[[ "$(git -C "$work_root/license-list-data.git" rev-parse FETCH_HEAD)" == "$SPDX_COMMIT" ]]

stage="$work_root/spdx"
mkdir -p "$stage"
license_ids=(
  0BSD
  Apache-2.0
  BSD-2-Clause
  BSD-3-Clause
  blessing
  bzip2-1.0.6
  CC-BY-4.0
  GPL-2.0-only
  GPL-2.0-or-later
  GPL-3.0-or-later
  ISC
  LGPL-2.0-or-later
  LGPL-2.1-or-later
  MIT
  MPL-2.0
  Python-2.0
  X11
  Zlib
)

for license_id in "${license_ids[@]}"; do
  git -C "$work_root/license-list-data.git" show \
    "FETCH_HEAD:text/$license_id.txt" > "$stage/$license_id.txt"
  [[ -s "$stage/$license_id.txt" ]]
done
git -C "$work_root/license-list-data.git" show FETCH_HEAD:README.md \
  > "$stage/UPSTREAM-README.md"
cat > "$stage/SOURCE.txt" <<EOF
SPDX license-list-data $SPDX_TAG
Repository: https://github.com/spdx/license-list-data
Commit: $SPDX_COMMIT

Estos textos canónicos complementan los avisos exactos conservados desde cada
módulo Go/npm. UPSTREAM-README.md describe el origen y proceso de publicación
de estos datos generados.
EOF

mkdir -p "$(dirname "$OUTPUT_DIR")"
mv "$stage" "$OUTPUT_DIR"
printf 'Textos SPDX sincronizados desde %s (%s).\n' "$SPDX_TAG" "$SPDX_COMMIT"
