#!/usr/bin/env bash
# Los patrones comparan texto de comandos remotos; sus expansiones son literales.
# shellcheck disable=SC2016
set -euo pipefail

TEST_IMAGE="$1"
[[ -n "$TEST_IMAGE" ]] || {
  echo "uso: test-deploy-post-commit.sh IMAGEN" >&2
  exit 2
}
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
PREFIX="registry-deploy-commit-$$-$RANDOM"
REMOTE_VOLUME="$PREFIX-remote"
SSH_LOG="$TEST_ROOT/ssh.log"
OLD_TOKEN="old-admin-token-000000000000000000000000000000"
NEW_TOKEN="new-admin-token-111111111111111111111111111111"

cleanup() {
  docker volume rm "$REMOTE_VOLUME" >/dev/null 2>&1 || true
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$TEST_ROOT/ssh"
: > "$TEST_ROOT/ssh/key-a"
chmod 0600 "$TEST_ROOT/ssh/key-a"
printf '%s\n' "$NEW_TOKEN" > "$TEST_ROOT/admin-token"
chmod 0600 "$TEST_ROOT/admin-token"

docker volume create "$REMOTE_VOLUME" >/dev/null
docker run --rm -i --entrypoint sh -v "$REMOTE_VOLUME:/remote" \
  "$TEST_IMAGE" -eu -c '
mkdir -p /remote/bin
cat > /remote/bin/remote-command
for command_name in docker curl sleep rm; do
  cp /remote/bin/remote-command "/remote/bin/$command_name"
  chmod 0755 "/remote/bin/$command_name"
done
mkdir -p /remote/config/plugins/dockerMan/templates-user
printf "%s\n" \
  "<Container><Repository>registry.example/legacy:v0</Repository></Container>" \
  > /remote/config/plugins/dockerMan/templates-user/my-Local-Registry.xml
' < "$REPO_ROOT/tests/fixtures/fake-remote-command.sh"
printf '%s\n' "$OLD_TOKEN" | docker run --rm -i --entrypoint sh \
  -v "$REMOTE_VOLUME:/remote" "$TEST_IMAGE" -eu -c '
install -d -o root -g root -m 0700 /remote/secrets
IFS= read -r old_token
printf "%s\n" "$old_token" > /remote/secrets/admin-token
chown root:root /remote/secrets/admin-token
chmod 0400 /remote/secrets/admin-token
mkdir -p /remote/data /remote/containers
: > /remote/containers/Local-Registry
: > /remote/curl-success
: > /remote/fail-cleanup
'

run_remote() {
  local remote="$1"
  docker run --rm -i --entrypoint sh \
    -e PATH=/remote/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    -v "$REMOTE_VOLUME:/remote" -v "$REMOTE_VOLUME:/boot" \
    "$TEST_IMAGE" -eu -c "$remote"
}

ssh() {
  local remote="${*: -1}"
  printf '%s\n--\n' "$remote" >> "$SSH_LOG"
  case "$remote" in
    true)
      return 0
      ;;
    *"rollback_container='Local-Registry-rollback-"*)
      run_remote "$remote"
      ;;
    *"grep -q 'docker-registry.sh' /boot/config/go"*|*"awk '/docker-registry.sh/"*)
      return 0
      ;;
    *'cleanup_boot_stage()'*)
      cat >/dev/null
      return 0
      ;;
    *"docker inspect 'Local-Registry' --format '{{range .HostConfig.Binds}}"*)
      printf '%s\n' '/remote/data:/var/lib/registry:rw'
      ;;
    *'my-Local-Registry.xml'*'wc -c < "$target"'*'cat "$target"'*)
      run_remote "$remote"
      ;;
    *'<Registry>https://github.com/Ezr43l/local-registry-s'*|\
    *'cat > "$stage"'*)
      run_remote "$remote"
      ;;
    *"sed -n 's|.*<Repository>"*)
      printf '%s\n' 'local-registry:v1.2.15'
      ;;
    *'file="$dir/admin-token"'*|*'file="$dir/cluster-token"'*|\
    *"sha256sum '/remote/secrets/"*)
      run_remote "$remote"
      ;;
    *"docker inspect 'Local-Registry' --format '{{.State.Running}}"*)
      printf '%s\n' 'true|local-registry:v1.2.15|unless-stopped|5000|5001'
      ;;
    *'envs="$(docker inspect'*)
      printf '%s\n' secure
      ;;
    *"docker pull -q 'local-registry:v1.2.15'"*)
      return 0
      ;;
    *'stage="$dir/.admin-token.'*|*'stage="$dir/.cluster-token.'*|\
    *"rm -f '/remote/secrets/.admin-token."*)
      run_remote "$remote"
      ;;
    *)
      return 1
      ;;
  esac
}

curl() {
  case "$*" in
    *'/v2/_catalog'*) printf '%s\n' '{"repositories":[]}' ;;
    *'/api/health'*) return 0 ;;
    *) return 1 ;;
  esac
}

export TEST_IMAGE REMOTE_VOLUME SSH_LOG
export -f run_remote ssh curl

output="$({
  cd "$REPO_ROOT"
  NODES_CONFIG='node-a:node-a.example:key-a' \
  SECRETS_DIR="$TEST_ROOT/ssh" \
  ADMIN_TOKEN_FILE="$TEST_ROOT/admin-token" \
  IMAGE_REPOSITORY=local-registry \
  DATA_DIR=/remote/data \
  REMOTE_SECRETS_DIR=/remote/secrets \
  MAINTENANCE_ENABLED=1 \
    ./deploy-registry.sh
} 2>&1)"

grep -Fq 'quedó una copia privada de secretos' <<< "$output"
grep -Fq 'contenedor Local-Registry desplegado' <<< "$output"
if grep -Fq "$OLD_TOKEN" <<< "$output" || grep -Fq "$NEW_TOKEN" <<< "$output" \
    || grep -Fq "$OLD_TOKEN" "$SSH_LOG" || grep -Fq "$NEW_TOKEN" "$SSH_LOG"; then
  echo "Un secreto apareció en salida o argv durante el commit" >&2
  exit 1
fi

expected_hash="$(printf '%s\n' "$NEW_TOKEN" | sha256sum | cut -d' ' -f1)"
actual_hash="$(docker run --rm --entrypoint sha256sum \
  -v "$REMOTE_VOLUME:/remote" "$TEST_IMAGE" /remote/secrets/admin-token \
  | cut -d' ' -f1)"
[[ "$actual_hash" == "$expected_hash" ]]

docker run --rm --entrypoint sh -v "$REMOTE_VOLUME:/remote" \
  "$TEST_IMAGE" -eu -c '
test -f /remote/containers/Local-Registry
test -z "$(find /remote/containers -maxdepth 1 -name "Local-Registry-rollback-*" -print -quit)"
test -n "$(find /remote/secrets -maxdepth 1 -name ".rollback-*" -type d -print -quit)"
test "$(stat -c "%u:%g:%a" /remote/secrets)" = "0:0:700"
test "$(stat -c "%u:%g:%a" /remote/secrets/admin-token)" = "0:0:400"
template=/remote/config/plugins/dockerMan/templates-user/my-Local-Registry.xml
grep -Fq "<Repository>local-registry:v1.2.15</Repository>" "$template"
! grep -Fq "<Repository>registry.example/legacy:v0</Repository>" "$template"
test -n "$(find "$(dirname "$template")" -maxdepth 1 \
  -name "my-Local-Registry.xml.*.rollback" -type f -print -quit)"
! grep -q "^rename Local-Registry-rollback-.* Local-Registry$" /remote/docker.log
! grep -q "^start Local-Registry$" /remote/docker.log
'

echo "Commit seguro: un fallo de limpieza posterior no revierte un contenedor ya validado."
