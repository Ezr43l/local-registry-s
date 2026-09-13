#!/usr/bin/env bash
set -euo pipefail

# Git Bash convierte argumentos que empiezan por `/` en rutas de Windows al
# invocar `docker.exe`. Los endpoints HTTP de este laboratorio deben llegar al
# contenedor literalmente; la variable no tiene efecto fuera de MSYS.
export MSYS_NO_PATHCONV=1

IMAGE="${1:-local-registry:v1.2.15}"
PREFIX="registry-cluster-it-$$"
NETWORK="$PREFIX"
TOKEN="integration-test-token-00000000000000000000000000000000"
SCHEDULE_HOUR="$(date -u -d '+2 minutes' +%H:%M)"
CONTAINERS=("$PREFIX-a" "$PREFIX-b" "$PREFIX-c")
VOLUMES=("$PREFIX-data-a" "$PREFIX-data-b" "$PREFIX-data-c")
SECRET_VOLUME="$PREFIX-secrets"

cleanup() {
  docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
  docker volume rm "${VOLUMES[@]}" "$SECRET_VOLUME" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$NETWORK" >/dev/null
docker volume create "$SECRET_VOLUME" >/dev/null
printf '%s\n' "$TOKEN" | docker run --rm -i \
  --entrypoint sh \
  -v "$SECRET_VOLUME:/run/secrets/local-registry" \
  "$IMAGE" -eu -c '
umask 077
IFS= read -r cluster_token
printf "%s\n" "$cluster_token" > /run/secrets/local-registry/cluster-token
chown root:root /run/secrets/local-registry/cluster-token
chmod 0400 /run/secrets/local-registry/cluster-token
'

start_node() {
  local suffix="$1" name="$2" peers="$3" panels="$4"
  docker run -d \
    --name "$PREFIX-$suffix" \
    --network "$NETWORK" \
    --network-alias "$name" \
    -v "$PREFIX-data-$suffix:/var/lib/registry" \
    -v "$SECRET_VOLUME:/run/secrets/local-registry:ro" \
    -e NODE_NAME="$name" \
    -e PEERS="$peers" \
    -e PANEL_PEERS="$panels" \
    -e MAINTENANCE_COORDINATOR=node-a \
    -e MAINTENANCE_ENABLED=1 \
    -e MAINTENANCE_CLUSTER_TOKEN_FILE=/run/secrets/local-registry/cluster-token \
    -e KEEP_LAST=5 \
    -e MAINTENANCE_HOUR="$SCHEDULE_HOUR" \
    "$IMAGE" >/dev/null
}

start_node a node-a \
  "node-b=http://node-b:5000,node-c=http://node-c:5000" \
  "node-b=http://node-b:5001,node-c=http://node-c:5001"
start_node b node-b \
  "node-a=http://node-a:5000,node-c=http://node-c:5000" \
  "node-a=http://node-a:5001,node-c=http://node-c:5001"
start_node c node-c \
  "node-a=http://node-a:5000,node-b=http://node-b:5000" \
  "node-a=http://node-a:5001,node-b=http://node-b:5001"

wait_ready() {
  local container="$1"
  for _ in $(seq 1 90); do
    if docker exec "$container" python3 -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/health', timeout=2)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "El panel $container no quedó disponible" >&2
  return 1
}

for container in "${CONTAINERS[@]}"; do
  wait_ready "$container"
  if docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' \
      | grep -Eq '^MAINTENANCE_(ADMIN|CLUSTER)_TOKEN='; then
    echo "El contenedor $container expone un secreto directo en docker inspect" >&2
    exit 1
  fi
  docker exec "$container" python3 -c '
import json, urllib.request
body = json.dumps({
    "username": "cluster-owner", "display_name": "Cluster Owner",
    "password": "correct-horse-battery-cluster-4821",
    "password_confirmation": "correct-horse-battery-cluster-4821",
}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:5001/api/auth/setup", data=body,
    headers={"Content-Type": "application/json"}, method="POST")
result = json.load(urllib.request.urlopen(request, timeout=10))
assert result["authenticated"]
'
  # Registry 2 considera inválido un almacenamiento totalmente virgen al
  # ejecutar GC. El directorio aparece con el primer push; aquí se crea la
  # estructura mínima para probar de forma legítima un registro vacío.
  docker exec "$container" mkdir -p \
    /var/lib/registry/docker/registry/v2/repositories \
    /var/lib/registry/docker/registry/v2/blobs/sha256
done

request() {
  local container="$1" path="$2" body="${3:-}" confirm="${4:-0}"
  docker exec -i "$container" python3 - "$path" "$body" "$confirm" <<'PY'
import http.cookiejar
import json
import sys
import urllib.request

path, raw, confirm = sys.argv[1:]
cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
login_body = json.dumps({
    "username": "cluster-owner", "password": "correct-horse-battery-cluster-4821",
}).encode()
login_request = urllib.request.Request(
    "http://127.0.0.1:5001/api/auth/session", data=login_body,
    headers={"Content-Type": "application/json"}, method="POST")
auth = json.load(opener.open(login_request, timeout=10))
headers = {
    "X-Registry-Maintenance": "1",
    "X-CSRF-Token": auth["session"]["csrf_token"],
}
data = raw.encode() if raw else None
if raw:
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(
    "http://127.0.0.1:5001" + path + ("?confirm=1" if confirm == "1" else ""),
    data=data,
    method="POST",
    headers=headers,
)
with opener.open(req, timeout=7500) as response:
    result = json.load(response)
if result.get("ok") is False:
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit(result.get("error") or "la operación devolvió ok=false")
print(json.dumps(result, ensure_ascii=False))
PY
}

assert_state() {
  local container="$1" coordinator="$2" expected="$3"
  docker exec -i "$container" python3 - "$coordinator" "$expected" <<'PY'
import http.cookiejar
import json
import sys
import urllib.request

coordinator, expected = sys.argv[1:]
cookies = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
login_body = json.dumps({
    "username": "cluster-owner", "password": "correct-horse-battery-cluster-4821",
}).encode()
login_request = urllib.request.Request(
    "http://127.0.0.1:5001/api/auth/session", data=login_body,
    headers={"Content-Type": "application/json"}, method="POST")
json.load(opener.open(login_request, timeout=10))
with opener.open("http://127.0.0.1:5001/api/maintenance", timeout=10) as response:
    state = json.load(response)
assert state["cluster_ready"], state.get("cluster_error")
assert state["coordinator"] == coordinator, state["coordinator"]
history = state["history"]
if expected == "empty":
    assert history["last_run"] is None and history["last_preview"] is None, history
elif expected == "preview":
    assert history["last_run"] is None, history
    assert history["last_preview"]["dry_run"] is True, history
    assert history["last_preview"]["coordinator"] == coordinator, history
elif expected == "real":
    assert history["last_run"]["dry_run"] is False, history
    assert history["last_run"]["finished_at"] >= history["last_run"]["started_at"], history
    assert history["last_run"]["coordinator"] == coordinator, history
    assert history["last_preview"]["dry_run"] is True, history
elif expected == "scheduled":
    assert history["last_run"]["dry_run"] is False, history
    assert history["last_run"]["trigger"] == "scheduled", history
    assert history["last_run"]["coordinator"] == coordinator, history
    assert history["last_preview"]["dry_run"] is True, history
else:
    raise AssertionError(expected)
PY
}

for container in "${CONTAINERS[@]}"; do
  assert_state "$container" node-a empty
done

# La orden nace en node-c, pero node-a coordina el cambio distribuido.
echo "· cambio de coordinador"
request "$PREFIX-c" /api/maintenance/coordinator '{"coordinator":"node-b"}' >/dev/null
for container in "${CONTAINERS[@]}"; do
  assert_state "$container" node-b empty
done

# La simulación no debe ocupar el lugar del último mantenimiento real.
echo "· previsualización coordinada"
request "$PREFIX-a" /api/maintenance/window >/dev/null
for container in "${CONTAINERS[@]}"; do
  assert_state "$container" node-b preview
done

# El mantenimiento real recorre los tres registros y persiste otro historial.
echo "· mantenimiento real coordinado"
request "$PREFIX-c" /api/maintenance/window '' 1 >/dev/null
for container in "${CONTAINERS[@]}"; do
  assert_state "$container" node-b real
done

# Aunque los tres nodos tienen la misma hora, sólo el coordinador elegido debe
# ejecutar la ventana y registrar el origen automático con un único run_id.
echo "· ventana programada en el coordinador dinámico ($SCHEDULE_HOUR UTC)"
for _ in $(seq 1 150); do
  if assert_state "$PREFIX-a" node-b scheduled >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
for container in "${CONTAINERS[@]}"; do
  assert_state "$container" node-b scheduled
done

# Un reinicio no puede borrar ni el coordinador elegido ni las dos ejecuciones.
docker restart "$PREFIX-c" >/dev/null
wait_ready "$PREFIX-c"
assert_state "$PREFIX-c" node-b scheduled

for container in "${CONTAINERS[@]}"; do
  docker exec "$container" python3 -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/v2/', timeout=3)" \
    >/dev/null
done

echo "Integración de clúster completada: coordinador dinámico, historial y reinicio verificados."
