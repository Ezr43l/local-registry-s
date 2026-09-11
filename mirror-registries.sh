#!/usr/bin/env bash
# mirror-registries.sh — deja la misma imagen en los registros configurados.
#
# Uso:  ./mirror-registries.sh <repo:etiqueta> [repo:etiqueta ...]
# Ej.:  MIRROR_NODES_CONFIG="node-a=host-a,node-b=host-b" ./mirror-registries.sh app:stable
#       ./mirror-registries.sh mi-app:v1.2 mi-app:stable
#
# Por qué existe: con un registro por servidor, tener la imagen en uno solo no
# basta. Si vive únicamente en un nodo, su caída significa no poder desplegar en
# ningún sitio — justo el escenario para el que existe la alta disponibilidad.
# Este guion la deja en todos los nodos configurados.
#
# QUÉ imágenes replicar es decisión de quien lo llama, no de este guion. Aquí no
# hay lista por omisión a propósito: este repo es infraestructura compartida y no
# debe saber qué aplicaciones existen ni cómo se llaman sus etiquetas. Cada
# proyecto llama a esto con las suyas.
#
# Requisito previo: cada nodo debe declarar los registros configurados como inseguros,
# cosa que hace docker-registry.sh en cada arranque (ver README.md).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Las claves viven fuera de este repo. PEER_KEY puede ser absoluta o relativa a
# SECRETS_DIR. Los nodos se declaran como nombre=host, separados por comas.
SECRETS_DIR="${SECRETS_DIR:-$REPO_ROOT/.secrets}"
KEY="${PEER_KEY:-$SECRETS_DIR/registry-peer.key}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
if [[ ! "$REGISTRY_PORT" =~ ^[1-9][0-9]{0,4}$ \
      || "$REGISTRY_PORT" -gt 65535 ]]; then
  echo "❌ REGISTRY_PORT debe estar entre 1 y 65535" >&2
  exit 2
fi
NODES=()
if [[ -n "${MIRROR_NODES_CONFIG:-}" ]]; then
  IFS=',' read -r -a NODES <<< "$MIRROR_NODES_CONFIG"
else
  echo "❌ MIRROR_NODES_CONFIG es obligatorio (nombre=host[,nombre=host...])" >&2
  exit 2
fi

declare -A SEEN_NODES=()
declare -A SEEN_HOSTS=()
for entry in "${NODES[@]}"; do
  if [[ "$entry" != *=* || "${entry#*=}" == *"="* ]]; then
    echo "❌ MIRROR_NODES_CONFIG contiene una entrada inválida" >&2
    exit 2
  fi
  node="${entry%%=*}"
  host="${entry#*=}"
  if [[ ! "$node" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ \
        || ! "$host" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$ \
        || -n "${SEEN_NODES[$node]:-}" || -n "${SEEN_HOSTS[$host]:-}" ]]; then
    echo "❌ MIRROR_NODES_CONFIG contiene nombres/hosts inseguros o duplicados" >&2
    exit 2
  fi
  SEEN_NODES["$node"]=1
  SEEN_HOSTS["$host"]=1
done

REFS=("$@")
if [[ ${#REFS[@]} -eq 0 ]]; then
  sed -n '2,7p' "$0" >&2
  echo "" >&2
  echo "❌ Dime qué imágenes replicar; este guion no asume ninguna." >&2
  exit 2
fi
[[ -f "$KEY" && ! -L "$KEY" ]] || {
  echo "❌ la clave SSH debe ser un fichero regular, no un enlace: $KEY" >&2
  exit 1
}
key_mode="$(stat -c '%a' -- "$KEY")"
key_mode_value=$((8#$key_mode))
if (( (key_mode_value & 077) != 0 )); then
  echo "❌ la clave SSH debe ser privada para su propietario (chmod 600 o 400)" >&2
  exit 1
fi

SSH=(
  ssh -i "$KEY"
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes
  -o ConnectTimeout=8
  -o BatchMode=yes
)

valid_repository() {
  local repository="$1" component
  [[ ${#repository} -le 255 && "$repository" != */ && "$repository" != /* ]] \
    || return 1
  IFS=/ read -r -a components <<< "$repository"
  for component in "${components[@]}"; do
    [[ "$component" =~ ^[a-z0-9]+(([.]|_|__|-+)[a-z0-9]+)*$ ]] || return 1
  done
}

# Se valida toda la petición antes de contactar con ningún nodo, evitando una
# replicación parcial si una referencia posterior está mal formada.
for ref in "${REFS[@]}"; do
  if [[ "$ref" != *:* ]]; then
    echo "❌ '$ref': falta la etiqueta (se espera repo:etiqueta)" >&2
    exit 2
  fi
  tag="${ref##*:}"
  repo="${ref%:*}"
  if ! valid_repository "$repo" \
      || [[ ! "$tag" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
    echo "❌ '$ref': repositorio o etiqueta OCI no válidos" >&2
    exit 2
  fi
done

# La identidad y el acceso de todos los hosts se comprueban antes del primer
# push. Un known_hosts incompleto detiene la operación sin dejar un espejo a
# medias.
for entry in "${NODES[@]}"; do
  node="${entry%%=*}"
  host="${entry#*=}"
  if ! "${SSH[@]}" "root@$host" true >/dev/null 2>&1; then
    echo "❌ $node: SSH no disponible o identidad de host no confiable" >&2
    exit 1
  fi
done

# El digest de una etiqueta, o vacío si ese registro no la tiene.
#
# Se compara el digest y no la mera presencia de la etiqueta a propósito: las
# etiquetas flotantes como «dev» o «stable» existen siempre y cambian de imagen en
# cada versión. Preguntar sólo si la etiqueta está haría que el espejo se declarara
# al día justo cuando acaba de quedarse viejo.
ACCEPT='application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.oci.image.index.v1+json'
digest_of() {  # <ip> <repo> <tag>
  curl -sI --max-time 8 -H "Accept: $ACCEPT" \
    "http://$1:$REGISTRY_PORT/v2/$2/manifests/$3" 2>/dev/null \
    | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}'
}

fallos=0

for ref in "${REFS[@]}"; do
  tag="${ref##*:}"
  repo="${ref%:*}"
  echo "▸ $repo:$tag"

  # De dónde copiarla: el primer registro accesible que ya la tenga.
  source_ip=""; source_digest=""
  for entry in "${NODES[@]}"; do
    ip="${entry#*=}"
    d=$(digest_of "$ip" "$repo" "$tag")
    if [[ -n "$d" ]]; then source_ip="$ip"; source_digest="$d"; break; fi
  done
  if [[ -z "$source_ip" ]]; then
    echo "  ⚠️  ningún registro la tiene; se omite" >&2
    fallos=1; continue
  fi
  echo "  origen: $source_ip"

  for entry in "${NODES[@]}"; do
    node="${entry%%=*}"; ip="${entry#*=}"
    if [[ "$ip" == "$source_ip" ]]; then
      echo "  · $node: es el origen"
      continue
    fi
    if [[ "$(digest_of "$ip" "$repo" "$tag")" == "$source_digest" ]]; then
      echo "  · $node: al día"
      continue
    fi
    # El destino tira de la imagen y la vuelve a empujar a su propio registro.
    # Se etiqueta contra 127.0.0.1 y no contra su IP para que el empujón salga
    # aunque el nodo aún no se fíe de sí mismo por red.
    #
    # Y al terminar se sueltan las dos referencias que este paso ha creado. Sin
    # eso, cada versión dejaba en el nodo la MISMA imagen nombrada de varias
    # formas distintas —una por cada manera de escribir el registro—, que
    # para Docker no está suelta y `prune` no limpia, pero Unraid enseña como
    # imágenes huérfanas. Lo que importa ya está guardado en su registro; el
    # despliegue la vuelve a traer por su nombre definitivo.
    if "${SSH[@]}" "root@$ip" "
        set -e
        docker pull -q '$source_ip:$REGISTRY_PORT/$repo:$tag' >/dev/null
        docker tag '$source_ip:$REGISTRY_PORT/$repo:$tag' '127.0.0.1:$REGISTRY_PORT/$repo:$tag'
        docker push -q '127.0.0.1:$REGISTRY_PORT/$repo:$tag' >/dev/null
        docker rmi '127.0.0.1:$REGISTRY_PORT/$repo:$tag' >/dev/null 2>&1 || true
        docker rmi '$source_ip:$REGISTRY_PORT/$repo:$tag' >/dev/null 2>&1 || true
      "; then
      echo "  · $node: copiada ✅"
    else
      echo "  · $node: falló ❌" >&2
      fallos=1
    fi
  done
done

echo
echo "Catálogos resultantes:"
for entry in "${NODES[@]}"; do
  node="${entry%%=*}"; ip="${entry#*=}"
  printf '  %-16s %s\n' "$node" "$(curl -s --max-time 6 "http://$ip:$REGISTRY_PORT/v2/_catalog" || echo 'sin respuesta')"
done

exit $fallos
