#!/usr/bin/env bash
set -euo pipefail

# Git Bash convierte rutas POSIX al invocar docker.exe. Los destinos de montaje
# y rutas internas deben llegar literalmente al motor Linux.
export MSYS_NO_PATHCONV=1

IMAGE="${1:?Uso: $0 IMAGEN [PLATAFORMA]}"
PLATFORM="${2:-}"
PREFIX="registry-runtime-contract-$$"
CONTAINER="$PREFIX"
DATA_VOLUME="$PREFIX-data"

platform_args=()
if [[ -n "$PLATFORM" ]]; then
  platform_args=(--platform "$PLATFORM")
fi

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$DATA_VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker volume create "$DATA_VOLUME" >/dev/null

docker run -d --name "$CONTAINER" "${platform_args[@]}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --init \
  --tmpfs /run:rw,nosuid,noexec,size=16m,mode=0755 \
  --tmpfs /tmp:rw,nosuid,noexec,size=32m,mode=1777 \
  -v "$DATA_VOLUME:/var/lib/registry" \
  "$IMAGE" >/dev/null

ready=0
for _attempt in $(seq 1 45); do
  if [[ "$(docker inspect "$CONTAINER" --format '{{.State.Health.Status}}' 2>/dev/null || true)" == healthy ]] \
    && docker exec "$CONTAINER" python3 -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/health', timeout=2).read()" \
      >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  docker logs "$CONTAINER" >&2
  exit 1
fi

docker exec "$CONTAINER" python3 -c '
import http.cookiejar, json, urllib.request
cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
owner = json.dumps({
    "username": "runtime-owner", "display_name": "Runtime Owner",
    "password": "correct-horse-battery-4821",  # gitleaks:allow -- credencial ficticia de laboratorio
    "password_confirmation": "correct-horse-battery-4821",  # gitleaks:allow -- misma confirmación ficticia
}).encode()
auth_request = urllib.request.Request(
    "http://127.0.0.1:5001/api/auth/setup", data=owner,
    headers={"Content-Type": "application/json"}, method="POST")
auth = json.load(opener.open(auth_request, timeout=3))
csrf = auth["session"]["csrf_token"]
payload = {
    "node": "node-a",
    "members": [{"name": "node-a", "registry_url": "http://127.0.0.1:5000", "panel_url": ""}],
    "timezone": "UTC", "enrollment_code": "", "cache_ttl": 30,
    "cors_allowed_origins": "",
    "maintenance": {"enabled": True, "keep_last": 2, "hour": "", "gc": True,
                    "coordinator": "node-a", "protected_tags": "latest", "sync_source": "node-a",
                    "lease_ttl": 900, "require_all_nodes": True, "branch_pattern": ""},
}
body = json.dumps(payload).encode()
request = urllib.request.Request("http://127.0.0.1:5001/api/setup", data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-CSRF-Token": csrf}, method="POST")
result = json.load(opener.open(request, timeout=3))
assert result["ok"] and result["enrollment_code"] and "admin_token" not in result
'

configured=0
for _attempt in $(seq 1 30); do
  if docker exec "$CONTAINER" python3 -c \
      "import json,urllib.request; assert not json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/setup',timeout=2))['required']" \
      >/dev/null 2>&1; then
    configured=1
    break
  fi
  sleep 1
done
[[ "$configured" == 1 ]]
runtime_logs="$(docker logs "$CONTAINER" 2>&1)"
if grep -Fq "Ignoring unrecognized environment variable" <<<"$runtime_logs"; then
  echo "Distribution recibió variables REGISTRY_* que pertenecen al panel" >&2
  exit 1
fi
if grep -Fq "traces export:" <<<"$runtime_logs"; then
  echo "Distribution intentó exportar telemetría sin configuración explícita" >&2
  exit 1
fi

docker exec "$CONTAINER" sh -ec '
  test "$(stat -c "%a" /var/lib/registry/.local-registry/secrets/cluster-token)" = "600"
  test "$(stat -c "%a" /var/lib/registry/.local-registry/auth.json)" = "600"
  test -s /var/lib/registry/.local-registry/config.json
  printf x > /var/lib/registry/.runtime-contract
  printf x > /run/.runtime-contract
  printf x > /tmp/.runtime-contract
'

if docker exec "$CONTAINER" touch /root-filesystem-must-be-readonly \
    >/dev/null 2>&1; then
  echo "El sistema de ficheros raíz admite escritura" >&2
  exit 1
fi
test "$(docker inspect "$CONTAINER" --format '{{.HostConfig.ReadonlyRootfs}}')" = true
test "$(docker inspect "$CONTAINER" --format '{{.HostConfig.Privileged}}')" = false
test "$(docker inspect "$CONTAINER" --format '{{.HostConfig.PidsLimit}}')" = 256
test "$(docker inspect "$CONTAINER" --format '{{.HostConfig.Init}}')" = true
docker inspect "$CONTAINER" --format '{{json .HostConfig.CapDrop}}' | grep -Fq 'ALL'
docker inspect "$CONTAINER" --format '{{json .HostConfig.SecurityOpt}}' | grep -Fq 'no-new-privileges'
tmpfs="$(docker inspect "$CONTAINER" --format '{{json .HostConfig.Tmpfs}}')"
grep -Fq '"/run":"rw,nosuid,noexec,size=16m,mode=0755"' <<<"$tmpfs"
grep -Fq '"/tmp":"rw,nosuid,noexec,size=32m,mode=1777"' <<<"$tmpfs"
test -z "$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/run/secrets/local-registry"}}{{.Destination}}{{end}}{{end}}')"
test "$(docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/registry"}}{{.RW}}{{end}}{{end}}')" = true
if docker inspect "$CONTAINER" --format '{{range .Mounts}}{{println .Destination}}{{end}}' \
    | grep -Fq '/var/run/docker.sock'; then
  echo "El contenedor no debe montar docker.sock" >&2
  exit 1
fi
if docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | grep -Eq '^MAINTENANCE_(ADMIN|CLUSTER)_TOKEN='; then
  echo "Los secretos no deben aparecer directamente en docker inspect" >&2
  exit 1
fi

docker restart "$CONTAINER" >/dev/null
persisted=0
for _attempt in $(seq 1 30); do
  if docker exec "$CONTAINER" test -f /var/lib/registry/.runtime-contract \
      >/dev/null 2>&1; then
    persisted=1
    break
  fi
  sleep 1
done
[[ "$persisted" == 1 ]]

echo "Contrato de ejecución correcto para $IMAGE${PLATFORM:+ ($PLATFORM)}"
