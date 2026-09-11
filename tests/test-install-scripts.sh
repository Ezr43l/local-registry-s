#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

config="$TEST_ROOT/daemon.json"
REGISTRY_ENDPOINTS='registry-a.example:5000,[2001:db8::1]:5100,[::]:5200' \
DOCKER_DAEMON_CONFIG="$config" \
  "$REPO_ROOT/docker-registry.sh" >/dev/null
python3 - "$config" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload == {
    "insecure-registries": [
        "registry-a.example:5000",
        "[2001:db8::1]:5100",
        "[::]:5200",
    ]
}
PY

invalid_endpoints=(
  'host-only'
  'host:0'
  'host:65536'
  'host:not-a-port'
  'bad host:5000'
  'host:5000,'
  '[not-ip]:5000'
  '[a:::]:5000'
  '[1::2::3]:5000'
  '[12345::1]:5000'
  '[1:2:3:4:5:6:7]:5000'
  '[1:2:3:4:5:6:7:8:9]:5000'
  '[::1]5000'
)
for endpoint in "${invalid_endpoints[@]}"; do
  candidate="$TEST_ROOT/invalid-$RANDOM.json"
  if REGISTRY_ENDPOINTS="$endpoint" DOCKER_DAEMON_CONFIG="$candidate" \
      "$REPO_ROOT/docker-registry.sh" >/dev/null 2>&1; then
    echo "docker-registry.sh aceptó un endpoint inválido: $endpoint" >&2
    exit 1
  fi
  [[ ! -e "$candidate" ]]
done

key="$TEST_ROOT/peer-key"
: > "$key"
chmod 0600 "$key"

expect_mirror_rejection() {
  local label="$1"
  shift
  if env "$@" "$REPO_ROOT/mirror-registries.sh" app:v1 >/dev/null 2>&1; then
    echo "mirror-registries.sh aceptó $label" >&2
    exit 1
  fi
}

expect_mirror_rejection 'un host con metacaracteres' \
  MIRROR_NODES_CONFIG='node-a=host-a;id' PEER_KEY="$key"
expect_mirror_rejection 'un puerto fuera de rango' \
  MIRROR_NODES_CONFIG='node-a=host-a' PEER_KEY="$key" REGISTRY_PORT=70000

if MIRROR_NODES_CONFIG='node-a=host-a' PEER_KEY="$key" \
    "$REPO_ROOT/mirror-registries.sh" 'bad;repo:v1' >/dev/null 2>&1; then
  echo "mirror-registries.sh aceptó una referencia no OCI" >&2
  exit 1
fi

chmod 0644 "$key"
expect_mirror_rejection 'una clave SSH expuesta' \
  MIRROR_NODES_CONFIG='node-a=host-a' PEER_KEY="$key"
chmod 0600 "$key"
ln -s "$key" "$TEST_ROOT/peer-key-link"
expect_mirror_rejection 'una clave SSH enlazada' \
  MIRROR_NODES_CONFIG='node-a=host-a' PEER_KEY="$TEST_ROOT/peer-key-link"

mock_bin="$TEST_ROOT/bin"
ssh_log="$TEST_ROOT/ssh.log"
mkdir -p "$mock_bin"
cat > "$mock_bin/ssh" <<'MOCK'
#!/usr/bin/env sh
printf '%s\n' "$*" >> "$SSH_LOG"
exit 0
MOCK
cat > "$mock_bin/curl" <<'MOCK'
#!/usr/bin/env sh
exit 0
MOCK
chmod 0755 "$mock_bin/ssh" "$mock_bin/curl"
export SSH_LOG="$ssh_log"
if PATH="$mock_bin:$PATH" MIRROR_NODES_CONFIG='node-a=host-a' PEER_KEY="$key" \
    "$REPO_ROOT/mirror-registries.sh" namespace/app:v1 >/dev/null 2>&1; then
  echo "La prueba sin imagen origen debía terminar con estado de fallo" >&2
  exit 1
fi
grep -Fq 'StrictHostKeyChecking=yes' "$ssh_log"
if grep -Fq 'StrictHostKeyChecking=no' "$REPO_ROOT/mirror-registries.sh"; then
  echo "El espejo desactiva la comprobación de identidad SSH" >&2
  exit 1
fi

echo "Scripts de instalación: endpoints, referencias, claves e identidad SSH validados."
