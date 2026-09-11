#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VERSION="$(tr -d ' \r\n' < VERSION)"
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$ ]]; then
  echo "VERSION '$VERSION' no cumple vMAYOR.MENOR.PARCHE[-rcN]" >&2
  exit 2
fi

IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-local-registry}"
IMAGE="$IMAGE_REPOSITORY:$VERSION"
SOURCE_URL="${SOURCE_URL:-https://github.com/Ezr43l/local-registry-s}"
LICENSE_SPDX="${LICENSE_SPDX:-Apache-2.0}"
PUBLISH="${PUBLISH:-false}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PLATFORM="${PLATFORM:-}"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
VCS_REF="$(git rev-parse --short=12 HEAD 2>/dev/null || printf unknown)"

common=(
  --pull
  --file docker/local-registry/Dockerfile
  --build-arg "APP_VERSION=$VERSION"
  --build-arg "SOURCE_URL=$SOURCE_URL"
  --build-arg "BUILD_DATE=$BUILD_DATE"
  --build-arg "VCS_REF=$VCS_REF"
  --build-arg "LICENSE=$LICENSE_SPDX"
  --tag "$IMAGE"
)

if [[ "$PUBLISH" == "true" ]]; then
  if [[ "$IMAGE_REPOSITORY" != */* ]]; then
    echo "Para publicar, IMAGE_REPOSITORY debe incluir un registro o namespace" >&2
    exit 2
  fi
  docker buildx build "${common[@]}" --platform "$PLATFORMS" --push .
  echo "Imagen multi-arquitectura publicada: $IMAGE ($PLATFORMS)"
else
  platform_args=()
  [[ -n "$PLATFORM" ]] && platform_args=(--platform "$PLATFORM")
  docker buildx build "${common[@]}" "${platform_args[@]}" --load .
  echo "Imagen local construida y probada: $IMAGE"
fi

if [[ -n "${MIRROR_NODES_CONFIG:-}" ]]; then
  PEER_KEY="${PEER_KEY:-}" MIRROR_NODES_CONFIG="$MIRROR_NODES_CONFIG" \
    bash "$ROOT/mirror-registries.sh" "$IMAGE"
fi
