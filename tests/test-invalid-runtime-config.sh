#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:?Uso: $0 IMAGEN [PLATAFORMA]}"
PLATFORM="${2:-}"
platform_args=()
if [[ -n "$PLATFORM" ]]; then
  platform_args=(--platform "$PLATFORM")
fi

temporary_logs=()
cleanup() {
  if ((${#temporary_logs[@]})); then
    rm -f -- "${temporary_logs[@]}"
  fi
}
trap cleanup EXIT INT TERM

expect_invalid() {
  local variable="$1" value="$2" log status
  log="$(mktemp)"
  temporary_logs+=("$log")
  set +e
  docker run --rm "${platform_args[@]}" -e "$variable=$value" "$IMAGE" \
    >"$log" 2>&1
  status=$?
  set -e
  if [[ "$status" -ne 64 ]]; then
    cat "$log" >&2
    echo "$variable no cerró el contenedor con EX_USAGE (64): $status" >&2
    exit 1
  fi
  if ! grep -Fq "$variable" "$log"; then
    cat "$log" >&2
    echo "$variable falló sin un diagnóstico identificable" >&2
    exit 1
  fi
}

expect_invalid STATS_ENABLED invalid
expect_invalid TZ ../../etc/passwd
expect_invalid CACHE_TTL 86401
expect_invalid PROTECTED_TAGS bad/tag
expect_invalid PEERS 'node-b=http://127.0.0.1:5000,'
expect_invalid MAINTENANCE_COORDINATOR bad/name
expect_invalid CORS_ALLOWED_ORIGINS 'http://example.test,'

echo "Configuraciones inválidas rechazadas por $IMAGE${PLATFORM:+ ($PLATFORM)}"
