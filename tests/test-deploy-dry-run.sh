#!/usr/bin/env bash
# Los patrones comparan texto de comandos remotos; los `$dir` son literales.
# shellcheck disable=SC2016
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

mkdir -p "$TEST_ROOT/ssh"
: > "$TEST_ROOT/ssh/key-a"
: > "$TEST_ROOT/ssh/key-b"
chmod 0600 "$TEST_ROOT/ssh/key-a"
chmod 0600 "$TEST_ROOT/ssh/key-b"
ADMIN_VALUE="dry-run-admin-token-0000000000000000000000000000"
CLUSTER_VALUE="dry-run-cluster-token-11111111111111111111111111"
printf '%s\n' "$ADMIN_VALUE" > "$TEST_ROOT/admin-token"
printf '%s\n' "$CLUSTER_VALUE" > "$TEST_ROOT/cluster-token"
chmod 0600 "$TEST_ROOT/admin-token"
chmod 0600 "$TEST_ROOT/cluster-token"
FAKE_SSH_LOG="$TEST_ROOT/ssh.log"
export FAKE_SSH_LOG

ssh() {
  local invocation="$*"
  local remote="${*: -1}"
  printf '%s\n--\n' "$remote" >> "$FAKE_SSH_LOG"
  case "$remote" in
    true) return 0 ;;
    *'Name="Puerto del registro"'*)
      [[ "$invocation" == *'root@node-a.example'* ]] && printf '%s\n' 5100
      [[ "$invocation" == *'root@node-b.example'* ]] && printf '%s\n' 5200
      return 0
      ;;
    *'Name="Puerto del panel"'*)
      [[ "$invocation" == *'root@node-a.example'* ]] && printf '%s\n' 5101
      [[ "$invocation" == *'root@node-b.example'* ]] && printf '%s\n' 5201
      return 0
      ;;
    *'Name="Directorio de secretos"'*)
      [[ "$invocation" == *'root@node-a.example'* ]] && printf '%s\n' /srv/node-a-secrets
      [[ "$invocation" == *'root@node-b.example'* ]] && printf '%s\n' /srv/node-b-secrets
      return 0
      ;;
    *'my-Local-Registry.xml'*'wc -c'*)
      if [[ "$invocation" == *'root@node-a.example'* ]]; then
        printf '%s\n' '<Container><Repository>registry.example/old:v0</Repository><Config Target="/var/lib/registry">/srv/node-a-data</Config><Config Target="/run/secrets/local-registry">/srv/node-a-secrets</Config><Config Target="RETENTION_BRANCH_PATTERN">(?P&lt;branch&gt;main)&amp;safe</Config></Container>'
      else
        printf '%s\n' '<Container><Repository>registry.example/old:v0</Repository><Config Target="/var/lib/registry">/srv/node-b-data</Config><Config Target="/run/secrets/local-registry">/srv/node-b-secrets</Config><Config Target="RETENTION_BRANCH_PATTERN">(?P&lt;branch&gt;main)&amp;safe</Config></Container>'
      fi
      return 0
      ;;
    *'file="$dir/admin-token"'*) printf '%s\n' missing ; return 0 ;;
    *'file="$dir/cluster-token"'*) printf '%s\n' missing ; return 0 ;;
    *) return 1 ;;
  esac
}

# En dry-run no se ejecuta el renderizador, pero sí se analiza el XML existente.
export -f ssh

output="$({
  cd "$REPO_ROOT"
  NODES_CONFIG='node-a:node-a.example:key-a' \
  SECRETS_DIR="$TEST_ROOT/ssh" \
  ADMIN_TOKEN_FILE="$TEST_ROOT/admin-token" \
  MAINTENANCE_ENABLED=1 \
    ./deploy-registry.sh --dry-run
} 2>&1)"

grep -Fq 'secretos: SE ACTUALIZARÍAN por stdin y con rollback' <<< "$output"
if grep -Fq "$ADMIN_VALUE" <<< "$output" || grep -Fq "$ADMIN_VALUE" "$FAKE_SSH_LOG"; then
  echo "El secreto apareció en salida o en argv remoto" >&2
  exit 1
fi

topology_output="$({
  cd "$REPO_ROOT"
  NODES_CONFIG='node-a:node-a.example:key-a,node-b:node-b.example:key-b' \
  SECRETS_DIR="$TEST_ROOT/ssh" \
  ADMIN_TOKEN_FILE="$TEST_ROOT/admin-token" \
  CLUSTER_TOKEN_FILE="$TEST_ROOT/cluster-token" \
  MAINTENANCE_ENABLED=1 \
    ./deploy-registry.sh --dry-run
} 2>&1)"
grep -Fq 'registro 5100, panel 5101' <<< "$topology_output"
grep -Fq 'registro 5200, panel 5201' <<< "$topology_output"
grep -Fq 'directorio de secretos: /srv/node-a-secrets (según la plantilla existente)' <<< "$topology_output"
grep -Fq 'directorio de secretos: /srv/node-b-secrets (según la plantilla existente)' <<< "$topology_output"
grep -Fq 'RETENTION_BRANCH_PATTERN=(?P<branch>main)&safe' "$FAKE_SSH_LOG"
if grep -Fq 'RETENTION_BRANCH_PATTERN=(?P&lt;branch&gt;main)&amp;safe' "$FAKE_SSH_LOG"; then
  echo "La lectura de la plantilla no decodificó sus entidades XML" >&2
  exit 1
fi
if grep -Fq "$ADMIN_VALUE" <<< "$topology_output" \
    || grep -Fq "$CLUSTER_VALUE" <<< "$topology_output"; then
  echo "La topología multinodo expuso un secreto" >&2
  exit 1
fi

if {
  cd "$REPO_ROOT"
  MAINTENANCE_ADMIN_TOKEN="$ADMIN_VALUE" ./deploy-registry.sh --dry-run \
    >/dev/null 2>&1
}; then
  echo "El despliegue aceptó un token directo" >&2
  exit 1
fi

ln -s "$TEST_ROOT/admin-token" "$TEST_ROOT/admin-token-link"
if {
  cd "$REPO_ROOT"
  ADMIN_TOKEN_FILE="$TEST_ROOT/admin-token-link" ./deploy-registry.sh --dry-run \
    >/dev/null 2>&1
}; then
  echo "El despliegue aceptó un secreto mediante symlink" >&2
  exit 1
fi

printf '%s\n%s\n' \
  'multiline-admin-token-00000000000000000000000' \
  'second-line-must-never-be-accepted-0000000000' \
  > "$TEST_ROOT/admin-token-multiline"
chmod 0600 "$TEST_ROOT/admin-token-multiline"
if {
  cd "$REPO_ROOT"
  ADMIN_TOKEN_FILE="$TEST_ROOT/admin-token-multiline" ./deploy-registry.sh --dry-run \
    >/dev/null 2>&1
}; then
  echo "El despliegue aceptó un secreto con varias líneas" >&2
  exit 1
fi

printf '%257s' '' | tr ' ' x > "$TEST_ROOT/admin-token-oversize"
chmod 0600 "$TEST_ROOT/admin-token-oversize"
if {
  cd "$REPO_ROOT"
  ADMIN_TOKEN_FILE="$TEST_ROOT/admin-token-oversize" ./deploy-registry.sh --dry-run \
    >/dev/null 2>&1
}; then
  echo "El despliegue aceptó un secreto de más de 256 caracteres" >&2
  exit 1
fi

cp "$TEST_ROOT/admin-token" "$TEST_ROOT/admin-token-readable"
chmod 0640 "$TEST_ROOT/admin-token-readable"
if {
  cd "$REPO_ROOT"
  ADMIN_TOKEN_FILE="$TEST_ROOT/admin-token-readable" ./deploy-registry.sh --dry-run \
    >/dev/null 2>&1
}; then
  echo "El despliegue aceptó un fichero legible por el grupo" >&2
  exit 1
fi

if {
  cd "$REPO_ROOT"
  IMAGE_REPOSITORY='registry.example/repo;invalid' ./deploy-registry.sh --dry-run \
    >/dev/null 2>&1
}; then
  echo "El despliegue aceptó una referencia de imagen con metacaracteres" >&2
  exit 1
fi

set +e
unsafe_boot_output="$({
  cd "$REPO_ROOT"
  BOOT_DIR="/boot/config/registry';touch-injection" \
    ./deploy-registry.sh --dry-run
} 2>&1)"
unsafe_boot_status=$?
set -e
if [[ "$unsafe_boot_status" -eq 0 ]] || ! grep -Fq 'BOOT_DIR debe ser' <<< "$unsafe_boot_output"; then
  echo "El despliegue no rechazó de forma explícita un BOOT_DIR manipulable" >&2
  exit 1
fi

if {
  cd "$REPO_ROOT"
  NODES_CONFIG='node-a:node-a.example:key-a' \
  SECRETS_DIR="$TEST_ROOT/ssh" \
  DATA_DIR=/srv/local-registry \
  REMOTE_SECRETS_DIR=/srv/local-registry/secrets \
    ./deploy-registry.sh --dry-run >/dev/null 2>&1
}; then
  echo "El despliegue aceptó el directorio de secretos dentro del volumen" >&2
  exit 1
fi

if {
  cd "$REPO_ROOT"
  NODES_CONFIG='node-a:node-a.example:key-a' \
  SECRETS_DIR="$TEST_ROOT/ssh" \
  DATA_DIR=/srv/local-registry-secrets/data \
  REMOTE_SECRETS_DIR=/srv/local-registry-secrets \
    ./deploy-registry.sh --dry-run >/dev/null 2>&1
}; then
  echo "El despliegue aceptó el volumen dentro del directorio de secretos" >&2
  exit 1
fi

echo "Dry-run seguro: fichero privado de una línea validado, secreto ausente de argv/salida y entradas inseguras rechazadas."
