#!/usr/bin/env bash
# Los valores entre comillas simples se expanden en el shell local que construye
# el bloque remoto; esas comillas se envían literalmente por SSH.
# shellcheck disable=SC2016
# deploy-registry.sh — instala el registro en los nodos configurados.
#
# Uso:  NODES_CONFIG="name:host:key,..." ./deploy-registry.sh [--dry-run] [--force-recreate] [--force-templates] [nodo...]
#         --dry-run          enseña lo que haría, sin tocar nada
#         --force-recreate   recrea el contenedor aunque ya esté correcto
#         --force-templates  reescribe las plantillas de Unraid con las del repo.
#                            Respeta los puertos que hayas puesto por pantalla (se
#                            leen antes de reescribir), pero pierde otros ajustes.
#         nodo...            limita a ciertos nodos definidos en NODES_CONFIG
#
# Hace esto por nodo, todo idempotente:
#   1. copia docker-registry.sh a BOOT_DIR (la declaración de registros
#      inseguros, que se aplica en cada ARRANQUE),
#   2. engancha ese guion en /boot/config/go, ANTES de emhttp,
#   3. instala la plantilla de Unraid si falta o si está desfasada,
#   4. asegura el contenedor Local-Registry — que lleva DENTRO el registro y su
#      panel: una imagen, un volumen, una versión.
#
# LO QUE ESTE GUION NO HACE, A PROPÓSITO: recargar ni reiniciar dockerd.
# `insecure-registries` solo entra en vigor cuando dockerd relee su configuración,
# y eso en Unraid significa reiniciar el demonio — que se lleva por delante TODOS
# los contenedores del nodo. No es algo que deba pasar de refilón durante un
# despliegue. Por eso el fichero se coloca y se deja listo para el próximo
# arranque; si detecta que el docker vivo tiene una configuración distinta a la
# del fichero, AVISA de que queda un reinicio pendiente y sigue. La decisión de
# cuándo reiniciar es tuya.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# Las claves SSH viven fuera de este repo, que no guarda secretos. Se puede
# apuntar a otra ubicación con SECRETS_DIR.
SECRETS_DIR="${SECRETS_DIR:-$REPO_ROOT/.secrets}"

# Los secretos sólo entran desde ficheros locales privados. El despliegue los
# envía por stdin a ficheros root-only del nodo; nunca aparecen en argv, en la
# plantilla de Unraid ni en `docker inspect`. El backend conserva las variables
# directas únicamente para instalaciones antiguas gestionadas fuera de este
# guion, pero esta ruta pública las rechaza de forma explícita.
if [[ -n "${MAINTENANCE_ADMIN_TOKEN:-}" || -n "${MAINTENANCE_CLUSTER_TOKEN:-}" ]]; then
  echo "❌ no pase tokens directos; use ADMIN_TOKEN_FILE y CLUSTER_TOKEN_FILE" >&2
  exit 1
fi

load_secret_file() { # variable de ruta, variable de destino, nombre legible
  local path_var="$1" destination="$2" label="$3"
  local path="${!path_var:-}" mode mode_value size line_count value expected_size
  printf -v "$destination" '%s' ""
  [[ -n "$path" ]] || return 0
  [[ ! -L "$path" && -f "$path" ]] || {
    echo "❌ $label debe ser un fichero regular y no un enlace simbólico: $path" >&2
    exit 1
  }
  mode="$(stat -c '%a' -- "$path")"
  mode_value=$((8#$mode))
  (( (mode_value & 077) == 0 )) || {
    echo "❌ $label debe ser privado para su propietario (chmod 600 o 400): $path" >&2
    exit 1
  }
  size="$(wc -c < "$path")"
  line_count="$(awk 'END { print NR }' "$path")"
  [[ "$size" -ge 32 && "$size" -le 258 && "$line_count" -eq 1 ]] || {
    echo "❌ $label debe contener una única línea de 32 a 256 caracteres" >&2
    exit 1
  }
  IFS= read -r value < "$path" || [[ -n "$value" ]]
  value="${value%$'\r'}"
  [[ ${#value} -ge 32 && ${#value} -le 256 && "$value" =~ ^[-a-zA-Z0-9._~]+$ ]] || {
    echo "❌ $label contiene una longitud o caracteres no permitidos" >&2
    exit 1
  }
  expected_size=${#value}
  (( size == expected_size || size == expected_size + 1 || size == expected_size + 2 )) || {
    echo "❌ $label contiene bytes ocultos o más de un salto final" >&2
    exit 1
  }
  printf -v "$destination" '%s' "$value"
}

load_secret_file ADMIN_TOKEN_FILE ADMIN_TOKEN "ADMIN_TOKEN_FILE"
load_secret_file CLUSTER_TOKEN_FILE CLUSTER_TOKEN "CLUSTER_TOKEN_FILE"
trap 'unset ADMIN_TOKEN CLUSTER_TOKEN' EXIT

# nodo : ip : clave-ssh (relativa a SECRETS_DIR)
# El orden de NODES_CONFIG sólo determina el orden del despliegue.
NODES=()
if [[ -n "${NODES_CONFIG:-}" ]]; then
  IFS=',' read -r -a NODES <<< "$NODES_CONFIG"
fi

CONTAINER="${CONTAINER_NAME:-Local-Registry}"
[[ "$CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]] || {
  echo "❌ CONTAINER_NAME no es válido" >&2
  exit 2
}
# Si se proporciona DATA_DIR se fuerza una ruta concreta (por ejemplo, durante
# una migración planificada). Si no, cada nodo conserva primero el bind del
# contenedor que ya está funcionando y sólo usa este valor para instalaciones
# nuevas. Así una actualización nunca puede arrancar accidentalmente con un
# registro vacío por cambiar la ruta de datos por defecto.
DATA_DIR_OVERRIDE="${DATA_DIR:-}"
DEFAULT_DATA_DIR="/mnt/user/appdata/local-registry"
REMOTE_SECRETS_DIR_OVERRIDE="${REMOTE_SECRETS_DIR:-}"
DEFAULT_REMOTE_SECRETS_DIR="/mnt/user/appdata/local-registry-secrets"
REMOTE_SECRETS_DIR=""
SECRET_MOUNT="/run/secrets/local-registry"
ADMIN_SECRET_NAME="admin-token"
CLUSTER_SECRET_NAME="cluster-token"
DEPLOY_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BOOT_DIR="${BOOT_DIR:-/boot/config/local-registry}"
BOOT_SCRIPT="docker-registry.sh"
RENDERER="render-unraid-template.py"
ICON="https://www.docker.com/wp-content/uploads/2022/03/Moby-logo.png"
TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"

# Un solo número para el proyecto y para la imagen:
# el VERSION de la raíz ES la etiqueta que se despliega.
IMG_VER="$(tr -d ' \r\n' < VERSION)"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-ghcr.io/ezr43l/local-registry-s}"
if [[ ! "$IMG_VER" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9._-]+)?$ \
      || ${#IMAGE_REPOSITORY} -gt 512 \
      || ! "$IMAGE_REPOSITORY" =~ ^[a-zA-Z0-9][a-zA-Z0-9._/:@-]*$ ]]; then
  echo "❌ VERSION o IMAGE_REPOSITORY no forman una referencia OCI segura" >&2
  exit 2
fi
IMAGE="${IMAGE_REPOSITORY}:$IMG_VER"

# Los dos puertos son elegibles. Lo normal es hacerlo desde Unraid: si hay
# plantilla instalada MANDAN SUS PUERTOS y estos valores se ignoran, porque un
# guion no debe deshacer lo que el usuario acaba de elegir por pantalla.
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
STATS_PORT="${STATS_PORT:-5001}"

if [[ -n "$REMOTE_SECRETS_DIR_OVERRIDE" \
      && ( ! "$REMOTE_SECRETS_DIR_OVERRIDE" =~ ^/[a-zA-Z0-9._/@+-]+(/[a-zA-Z0-9._/@+-]+)*$ \
        || "$REMOTE_SECRETS_DIR_OVERRIDE" == *"/../"* \
        || "$REMOTE_SECRETS_DIR_OVERRIDE" == *"/.." ) ]]; then
  echo "❌ REMOTE_SECRETS_DIR no es una ruta absoluta segura" >&2
  exit 2
fi
if [[ ! "$BOOT_DIR" =~ ^/boot/config/[a-zA-Z0-9._+-]+$ ]]; then
  echo "❌ BOOT_DIR debe ser un subdirectorio directo y seguro de /boot/config" >&2
  exit 2
fi

# El contenedor del panel de la etapa anterior, que este despliegue retira.
OLD_STATS="Registry-Stats"

DRY=0
FORCE=0
FORCE_TPL=0
FILTER=()
for arg in "$@"; do
  case "$arg" in
    --dry-run)         DRY=1 ;;
    --force-recreate)  FORCE=1 ;;
    --force-templates) FORCE_TPL=1 ;;
    -h|--help)         sed -n '2,11p' "$0"; exit 0 ;;
    -*)                echo "❌ opción desconocida: $arg" >&2; exit 1 ;;
    *)                 FILTER+=("$arg") ;;
  esac
done

if [[ ${#NODES[@]} -eq 0 ]]; then
  echo "❌ NODES_CONFIG es obligatorio (nombre:host:clave[,nombre:host:clave...])" >&2
  exit 2
fi

IFS=: read -r DEFAULT_COORDINATOR _ _ <<< "${NODES[0]}"
if [[ ${#NODES[@]} -gt 1 ]]; then
  if [[ ${#CLUSTER_TOKEN} -lt 32 || ${#CLUSTER_TOKEN} -gt 256 \
        || "$CLUSTER_TOKEN" =~ [^-a-zA-Z0-9._~] ]]; then
    echo "❌ el secreto común debe tener entre 32 y 256 caracteres alfanuméricos (también . _ ~ -)" >&2
    exit 2
  fi
fi

[[ -f "$BOOT_SCRIPT" ]] || { echo "❌ no encuentro $BOOT_SCRIPT en $REPO_ROOT" >&2; exit 1; }
[[ -f "$RENDERER" ]] || { echo "❌ no encuentro $RENDERER en $REPO_ROOT" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "❌ python3 es necesario para renderizar la plantilla" >&2; exit 1; }
VER="$(tr -d ' \r\n' < VERSION)"

# Antes de modificar un solo nodo se descubre la topología efectiva completa.
# La plantilla manda; si falta, se conserva el puerto publicado del contenedor;
# sólo una instalación nueva usa los valores globales. Así los pares nunca
# anuncian por error 5000/5001 cuando otro nodo usa puertos personalizados.
declare -A NODE_REGISTRY_PORT=()
declare -A NODE_STATS_PORT=()
declare -A NODE_STATS_ENABLED=()
declare -A NODE_SEEN=()
for entry in "${NODES[@]}"; do
  IFS=: read -r probe_name probe_ip probe_key <<< "$entry"
  if [[ ! "$probe_name" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ \
        || ! "$probe_ip" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$ \
        || ! "$probe_key" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$ \
        || -n "${NODE_SEEN[$probe_name]:-}" ]]; then
    echo "❌ NODES_CONFIG contiene una entrada inválida o un nombre duplicado" >&2
    exit 2
  fi
  NODE_SEEN["$probe_name"]=1
  probe_key_path="$SECRETS_DIR/$probe_key"
  [[ -f "$probe_key_path" && ! -L "$probe_key_path" ]] || {
    echo "❌ $probe_name: no encuentro una clave SSH regular" >&2
    exit 2
  }
  probe_key_mode="$(stat -c '%a' -- "$probe_key_path")"
  probe_key_mode_value=$((8#$probe_key_mode))
  if (( (probe_key_mode_value & 077) != 0 )); then
    echo "❌ $probe_name: la clave SSH debe ser privada (chmod 600 o 400)" >&2
    exit 2
  fi
  probe_ssh=(
    ssh -i "$probe_key_path"
    -o IdentitiesOnly=yes
    -o StrictHostKeyChecking=yes
    -o ConnectTimeout=20
    -o BatchMode=yes
    "root@$probe_ip"
  )
  "${probe_ssh[@]}" true 2>/dev/null || {
    echo "❌ $probe_name: no responde por SSH; no se modifica ningún nodo" >&2
    exit 1
  }
  probe_tpl="$TEMPLATE_DIR/my-$CONTAINER.xml"
  probe_registry_tpl="$("${probe_ssh[@]}" \
    "sed -n 's/.*Name=\"Puerto del registro\".*>\([0-9]\{1,5\}\)<.*/\1/p' '$probe_tpl' 2>/dev/null" \
    | head -1 || true)"
  probe_stats_tpl="$("${probe_ssh[@]}" \
    "sed -n 's/.*Name=\"Puerto del panel\".*>\([0-9]\{1,5\}\)<.*/\1/p' '$probe_tpl' 2>/dev/null" \
    | head -1 || true)"
  probe_registry_runtime="$("${probe_ssh[@]}" \
    "docker inspect '$CONTAINER' --format '{{if index .HostConfig.PortBindings \"5000/tcp\"}}{{(index .HostConfig.PortBindings \"5000/tcp\" 0).HostPort}}{{end}}' 2>/dev/null" \
    || true)"
  probe_stats_runtime="$("${probe_ssh[@]}" \
    "docker inspect '$CONTAINER' --format '{{if index .HostConfig.PortBindings \"5001/tcp\"}}{{(index .HostConfig.PortBindings \"5001/tcp\" 0).HostPort}}{{end}}' 2>/dev/null" \
    || true)"
  probe_registry="${probe_registry_tpl:-${probe_registry_runtime:-$REGISTRY_PORT}}"
  probe_stats="${probe_stats_tpl:-${probe_stats_runtime:-$STATS_PORT}}"
  if [[ ! "$probe_registry" =~ ^[1-9][0-9]{0,4}$ || ! "$probe_stats" =~ ^[1-9][0-9]{0,4}$ \
        || "$probe_registry" -lt 1 || "$probe_registry" -gt 65535 \
        || "$probe_stats" -lt 1 || "$probe_stats" -gt 65535 ]]; then
    echo "❌ $probe_name: la topología contiene puertos fuera de rango" >&2
    exit 2
  fi
  NODE_REGISTRY_PORT["$probe_name"]="$probe_registry"
  NODE_STATS_PORT["$probe_name"]="$probe_stats"
done
for requested in "${FILTER[@]}"; do
  [[ -n "${NODE_SEEN[$requested]:-}" ]] || {
    echo "❌ el filtro solicita un nodo no definido: $requested" >&2
    exit 2
  }
done

echo "🐳 docker-registry $VER · imagen $IMAGE"
[[ $DRY == 1 ]] && echo "   (simulación: no se toca nada)"

fallos=0
pendiente_reinicio=()

for entry in "${NODES[@]}"; do
  IFS=: read -r name ip key <<< "$entry"

  if [[ ! "$name" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ \
        || ! "$ip" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$ \
        || ! "$key" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$ ]]; then
    echo "❌ entrada NODES_CONFIG inválida: se espera nombre:host:archivo-clave sin metacaracteres" >&2
    fallos=1
    continue
  fi

  if [[ ${#FILTER[@]} -gt 0 ]]; then
    encontrado=0
    for f in "${FILTER[@]}"; do [[ "$f" == "$name" ]] && encontrado=1; done
    [[ $encontrado == 1 ]] || continue
  fi

  SSH_KEY="$SECRETS_DIR/$key"
  if [[ ! -f "$SSH_KEY" ]]; then
    echo "❌ $name: no encuentro la clave $SSH_KEY" >&2
    fallos=1; continue
  fi
  ssh_node() {
    ssh -i "$SSH_KEY" \
      -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=yes \
      -o ConnectTimeout=20 \
      -o BatchMode=yes \
      "root@$ip" "$@"
  }

  remote_secret_state() { # nombre de fichero; sólo devuelve valid|missing|invalid
    local secret_name="$1"
    ssh_node "
set -eu
dir='$REMOTE_SECRETS_DIR'
file=\"\$dir/$secret_name\"
if [ -L \"\$dir\" ] || { [ -e \"\$dir\" ] && [ ! -d \"\$dir\" ]; }; then
  echo invalid
elif [ -d \"\$dir\" ] && [ \"\$(stat -c '%u:%g:%a' \"\$dir\")\" != '0:0:700' ]; then
  echo invalid
elif [ ! -e \"\$file\" ] && [ ! -L \"\$file\" ]; then
  echo missing
elif [ -L \"\$file\" ] || [ ! -f \"\$file\" ]; then
  echo invalid
elif [ \"\$(stat -c '%u:%g:%a' \"\$dir\")\" != '0:0:700' ] \
     || [ \"\$(stat -c '%u:%g:%a' \"\$file\")\" != '0:0:400' ]; then
  echo invalid
elif ! awk 'NR != 1 { exit 1 } END { exit !(NR == 1 && length(\$0) >= 32 && length(\$0) <= 256 && \$0 ~ /^[-a-zA-Z0-9._~]+\$/) }' \"\$file\"; then
  echo invalid
else
  echo valid
fi
"
  }

  remote_secret_hash() { # nombre de fichero validado
    ssh_node "sha256sum '$REMOTE_SECRETS_DIR/$1' | cut -d' ' -f1"
  }

  stage_remote_secret() { # nombre remoto, valor (siempre por stdin)
    local secret_name="$1" secret_value="$2"
    printf '%s\n' "$secret_value" | ssh_node "
set -eu
umask 077
dir='$REMOTE_SECRETS_DIR'
target=\"\$dir/$secret_name\"
stage=\"\$dir/.$secret_name.$DEPLOY_ID.new\"
[ ! -L \"\$dir\" ]
mkdir -p \"\$dir\"
chown root:root \"\$dir\"
chmod 0700 \"\$dir\"
[ ! -L \"\$target\" ]
[ ! -e \"\$stage\" ] && [ ! -L \"\$stage\" ]
cat > \"\$stage\"
chown root:root \"\$stage\"
chmod 0400 \"\$stage\"
[ \"\$(stat -c '%u:%g:%a' \"\$stage\")\" = '0:0:400' ]
awk 'NR != 1 { exit 1 } END { exit !(NR == 1 && length(\$0) >= 32 && length(\$0) <= 256 && \$0 ~ /^[-a-zA-Z0-9._~]+\$/) }' \"\$stage\"
"
  }

  cleanup_staged_secrets() {
    ssh_node "rm -f '$REMOTE_SECRETS_DIR/.$ADMIN_SECRET_NAME.$DEPLOY_ID.new' '$REMOTE_SECRETS_DIR/.$CLUSTER_SECRET_NAME.$DEPLOY_ID.new'" \
      >/dev/null 2>&1 || true
  }

  echo
  echo "── $name ($ip) ──────────────────────────────"

  endpoints=""
  for otro in "${NODES[@]}"; do
    IFS=: read -r _oname oip _key <<< "$otro"
    endpoints="${endpoints:+$endpoints,}$oip:${NODE_REGISTRY_PORT[$_oname]}"
  done

  if ! ssh_node true 2>/dev/null; then
    echo "❌ $name: no responde por SSH; me lo salto." >&2
    fallos=1; continue
  fi

  # ── 1. el guion de arranque ────────────────────────────────────────────────
  # Se instala con la lista concreta de endpoints del despliegue; el fichero
  # versionado sigue siendo genérico y no contiene IPs del entorno.
  script_source="$(sed "s|__REGISTRY_ENDPOINTS__|$endpoints|g" "$BOOT_SCRIPT")"
  script_hash="$(printf '%s\n' "$script_source" | sha256sum | cut -d' ' -f1)"
  boot_script_current=0
  if ssh_node "test -f '$BOOT_DIR/$BOOT_SCRIPT' && sha256sum '$BOOT_DIR/$BOOT_SCRIPT'" 2>/dev/null \
      | grep -Eq "^${script_hash}[[:space:]]"; then
    boot_script_current=1
  fi
  if [[ "$boot_script_current" == "1" ]]; then
    echo "· guion de arranque: ya está al día"
  elif [[ $DRY == 1 ]]; then
    echo "· guion de arranque: SE COPIARÍA a $BOOT_DIR/$BOOT_SCRIPT"
  else
    # Por stdin, no con scp: el USB de Unraid es FAT y scp arrastra permisos que
    # ahí no significan nada. Además así el fichero se escribe entero o no se
    # escribe (nada de medio guion si se corta la conexión).
    if ! printf '%s\n' "$script_source" | ssh_node "
set -eu
dir='$BOOT_DIR'
target=\"\$dir/$BOOT_SCRIPT\"
stage=\"\$dir/.$BOOT_SCRIPT.$DEPLOY_ID.new\"
[ ! -L /boot/config ]
if [ -e \"\$dir\" ] || [ -L \"\$dir\" ]; then
  [ -d \"\$dir\" ] && [ ! -L \"\$dir\" ]
else
  mkdir \"\$dir\"
fi
[ ! -L \"\$target\" ]
[ ! -e \"\$stage\" ] && [ ! -L \"\$stage\" ]
cleanup_boot_stage() { rm -f \"\$stage\"; }
trap cleanup_boot_stage EXIT HUP INT TERM
cat > \"\$stage\"
[ -s \"\$stage\" ] && [ ! -L \"\$stage\" ]
mv \"\$stage\" \"\$target\"
trap - EXIT HUP INT TERM
"; then
      echo "❌ $name: no se pudo instalar atómicamente el guion de arranque" >&2
      fallos=1
      continue
    fi
    echo "· guion de arranque colocado en $BOOT_DIR/$BOOT_SCRIPT"
  fi

  # ── 2. el enganche en /boot/config/go ──────────────────────────────────────
  #
  # Va ANTES de /usr/local/sbin/emhttp y no al final: emhttp arranca el array y
  # con él los contenedores, así que /etc/docker/daemon.json tiene que existir ya
  # cuando eso ocurre. Enganchado después, dockerd arrancaría sin conocer los
  # registros inseguros y los `pull` entre nodos fallarían hasta el reinicio.
  if ssh_node "grep -q '$BOOT_SCRIPT' /boot/config/go"; then
    if ssh_node "awk '/$BOOT_SCRIPT/{r=NR} /^[[:space:]]*\/usr\/local\/sbin\/emhttp/{e=NR} END{exit !(r && e && r < e)}' /boot/config/go"; then
      echo "· enganche en /boot/config/go: correcto (antes de emhttp)"
    else
      echo "⚠  $name: el enganche existe pero NO está antes de emhttp. Revísalo a mano." >&2
      fallos=1
      continue
    fi
  elif [[ $DRY == 1 ]]; then
    echo "· enganche en /boot/config/go: SE AÑADIRÍA antes de emhttp"
  else
    # El `go` es el fichero de arranque del servidor: copia de seguridad antes de
    # tocarlo, y escritura atómica. Un `go` a medias es un nodo que no arranca.
    if ! ssh_node "
set -eu
target=/boot/config/go
backup=/boot/config/go.bak-$DEPLOY_ID
stage=/boot/config/.go.$DEPLOY_ID.new
[ ! -L /boot/config ]
[ -f \"\$target\" ] && [ ! -L \"\$target\" ]
[ ! -e \"\$backup\" ] && [ ! -L \"\$backup\" ]
[ ! -e \"\$stage\" ] && [ ! -L \"\$stage\" ]
cleanup_go_stage() { rm -f \"\$stage\" \"\$backup\"; }
trap cleanup_go_stage EXIT HUP INT TERM
cp \"\$target\" \"\$backup\"
awk '
  !hecho && /^[[:space:]]*\/usr\/local\/sbin\/emhttp/ {
    print \"# Registro de imagenes: va ANTES de emhttp, que arranca el array y con el\"
    print \"# los contenedores, asi que el fichero debe existir ya entonces.\"
    print \"bash $BOOT_DIR/$BOOT_SCRIPT\"
    print \"\"
    hecho = 1
  }
  { print }
  END { if (!hecho) exit 3 }
' \"\$target\" > \"\$stage\"
[ -s \"\$stage\" ]
mv \"\$stage\" \"\$target\"
trap - EXIT HUP INT TERM
"; then
      echo "❌ $name: no encontré una línea emhttp segura; no se modificó /boot/config/go" >&2
      fallos=1
      continue
    fi
    echo "· enganche añadido a /boot/config/go (copia de seguridad guardada)"
  fi

  # ── 3. la plantilla de Unraid ──────────────────────────────────────────────
  #
  # Los puertos que mandan son los de la plantilla, si la hay: cambiarlos por
  # pantalla y que el siguiente despliegue los revirtiera sería un guion
  # peleándose con su propio usuario.
  TPL="$TEMPLATE_DIR/my-$CONTAINER.xml"
  TPL_STAGE="$TPL.$DEPLOY_ID.new"
  TPL_BACKUP="$TPL.$DEPLOY_ID.rollback"
  template_update=0
  cleanup_staged_template() {
    ssh_node "rm -f '$TPL_STAGE'" >/dev/null 2>&1 || true
  }
  activate_staged_template() {
    ssh_node "
set -eu
target='$TPL'
stage='$TPL_STAGE'
backup='$TPL_BACKUP'
had_old=0
touched=0
[ -f \"\$stage\" ] && [ ! -L \"\$stage\" ]
[ ! -L \"\$target\" ]
[ ! -e \"\$backup\" ] && [ ! -L \"\$backup\" ]
rollback_template() {
  code=\$?
  trap - EXIT HUP INT TERM
  set +e
  if [ \"\$touched\" = 1 ]; then
    rm -f \"\$target\"
    [ \"\$had_old\" = 1 ] && mv \"\$backup\" \"\$target\"
  fi
  rm -f \"\$stage\" \"\$backup\"
  exit \"\$code\"
}
trap rollback_template EXIT HUP INT TERM
if [ -e \"\$target\" ]; then
  [ -f \"\$target\" ] && [ ! -L \"\$target\" ]
  cp \"\$target\" \"\$backup\"
  had_old=1
fi
touched=1
mv \"\$stage\" \"\$target\"
grep -Fq '<Repository>$IMAGE</Repository>' \"\$target\"
test \"\$(grep -c '<Config ' \"\$target\")\" = 3
grep -q 'Target=\"5000\"' \"\$target\"
grep -q 'Target=\"5001\"' \"\$target\"
grep -q 'Target=\"/var/lib/registry\"' \"\$target\"
! grep -Fq 'Type=\"Variable\"' \"\$target\"
grep -q -- '--read-only' \"\$target\"
grep -q -- '--cap-drop=ALL' \"\$target\"
grep -q -- '--security-opt=no-new-privileges' \"\$target\"
! grep -q 'Target=\"MAINTENANCE_ADMIN_TOKEN\"' \"\$target\"
! grep -q 'Target=\"MAINTENANCE_CLUSTER_TOKEN\"' \"\$target\"
trap - EXIT HUP INT TERM
rm -f \"\$backup\" || echo 'AVISO: quedó una copia de plantilla para limpieza manual' >&2
"
  }
  template_xml=""
  if ! template_xml="$(ssh_node "
set -eu
target='$TPL'
[ ! -L \"\$target\" ]
if [ -e \"\$target\" ]; then
  [ -f \"\$target\" ] && [ -s \"\$target\" ]
  [ \"\$(wc -c < \"\$target\")\" -le 1048576 ]
  cat \"\$target\"
fi
")"; then
    echo "❌ $name: la plantilla existente no es un fichero XML regular y acotado" >&2
    fallos=1
    continue
  fi
  if [[ -n "$template_xml" ]] && ! printf '%s' "$template_xml" | python3 -c '
import sys
import xml.etree.ElementTree as ET

raw = sys.stdin.read()
upper = raw.upper()
if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
    raise SystemExit(1)
root = ET.fromstring(raw)
if root.tag != "Container":
    raise SystemExit(1)
targets = [item.get("Target") for item in root.findall("Config")]
if any(target is None for target in targets) or len(targets) != len(set(targets)):
    raise SystemExit(1)
' >/dev/null 2>&1; then
    echo "❌ $name: la plantilla existente es XML inválido o duplica destinos" >&2
    fallos=1
    continue
  fi
  var_plantilla() {   # <Target de la configuración>
    [[ -n "$template_xml" ]] || return 0
    printf '%s' "$template_xml" | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
matches = [item for item in root.findall("Config") if item.get("Target") == sys.argv[1]]
if len(matches) == 1:
    sys.stdout.write(matches[0].text or "")
' "$1"
  }
  elemento_plantilla() {   # <Nombre del elemento directo>
    [[ -n "$template_xml" ]] || return 0
    printf '%s' "$template_xml" | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
matches = root.findall(sys.argv[1])
if len(matches) == 1:
    sys.stdout.write(matches[0].text or "")
' "$1"
  }
  current_bind="$(ssh_node "docker inspect '$CONTAINER' --format '{{range .HostConfig.Binds}}{{println .}}{{end}}' 2>/dev/null | sed -n '\|:/var/lib/registry\(:rw\)\{0,1\}\$|p' | head -1" || true)"
  current_bind="${current_bind%:rw}"
  current_data_dir=""
  if [[ "$current_bind" == *":/var/lib/registry" ]]; then
    current_data_dir="${current_bind%:/var/lib/registry}"
  fi
  template_data_dir="$(var_plantilla /var/lib/registry)"
  if [[ -n "$DATA_DIR_OVERRIDE" ]]; then
    data_dir="$DATA_DIR_OVERRIDE"
    echo "· volumen de imágenes: $data_dir (forzado por DATA_DIR)"
  elif [[ -n "$current_data_dir" ]]; then
    data_dir="$current_data_dir"
    echo "· volumen de imágenes: $data_dir (se conserva el actual)"
  elif [[ -n "$template_data_dir" ]]; then
    data_dir="$template_data_dir"
    echo "· volumen de imágenes: $data_dir (según la plantilla existente)"
  else
    data_dir="$DEFAULT_DATA_DIR"
    echo "· volumen de imágenes: $data_dir (instalación nueva)"
  fi
  current_secret_bind="$(ssh_node "docker inspect '$CONTAINER' --format '{{range .HostConfig.Binds}}{{println .}}{{end}}' 2>/dev/null | sed -n '\|:$SECRET_MOUNT:ro\$|p' | head -1" || true)"
  current_secret_dir=""
  if [[ "$current_secret_bind" == *":$SECRET_MOUNT:ro" ]]; then
    current_secret_dir="${current_secret_bind%:"$SECRET_MOUNT":ro}"
  fi
  template_secret_dir="$(var_plantilla "$SECRET_MOUNT")"
  if [[ -n "$REMOTE_SECRETS_DIR_OVERRIDE" ]]; then
    REMOTE_SECRETS_DIR="$REMOTE_SECRETS_DIR_OVERRIDE"
    echo "· directorio de secretos: $REMOTE_SECRETS_DIR (forzado por REMOTE_SECRETS_DIR)"
  elif [[ -n "$current_secret_dir" ]]; then
    REMOTE_SECRETS_DIR="$current_secret_dir"
    echo "· directorio de secretos: $REMOTE_SECRETS_DIR (se conserva el actual)"
  elif [[ -n "$template_secret_dir" ]]; then
    REMOTE_SECRETS_DIR="$template_secret_dir"
    echo "· directorio de secretos: $REMOTE_SECRETS_DIR (según la plantilla existente)"
  else
    REMOTE_SECRETS_DIR="$DEFAULT_REMOTE_SECRETS_DIR"
    echo "· directorio de secretos: $REMOTE_SECRETS_DIR (instalación nueva)"
  fi
  puerto_reg="${NODE_REGISTRY_PORT[$name]}"
  puerto_web="${NODE_STATS_PORT[$name]}"
  if [[ ! "$puerto_reg" =~ ^[1-9][0-9]{0,4}$ || ! "$puerto_web" =~ ^[1-9][0-9]{0,4}$ \
        || "$puerto_reg" -lt 1 || "$puerto_reg" -gt 65535 \
        || "$puerto_web" -lt 1 || "$puerto_web" -gt 65535 ]]; then
    echo "❌ $name: puertos fuera de rango" >&2
    fallos=1
    continue
  fi
  if [[ ! "$data_dir" =~ ^/[a-zA-Z0-9._/@+-]+(/[a-zA-Z0-9._/@+-]+)*$ \
        || "$data_dir" == *"/../"* || "$data_dir" == *"/.." ]]; then
    echo "❌ $name: ruta de datos no segura: $data_dir" >&2
    fallos=1
    continue
  fi
  if [[ ! "$REMOTE_SECRETS_DIR" =~ ^/[a-zA-Z0-9._/@+-]+(/[a-zA-Z0-9._/@+-]+)*$ \
        || "$REMOTE_SECRETS_DIR" == *"/../"* || "$REMOTE_SECRETS_DIR" == *"/.." ]]; then
    echo "❌ $name: la ruta de secretos configurada no es segura" >&2
    fallos=1
    continue
  fi
  if [[ "$REMOTE_SECRETS_DIR" == "$data_dir" \
        || "$REMOTE_SECRETS_DIR" == "$data_dir/"* \
        || "$data_dir" == "$REMOTE_SECRETS_DIR/"* ]]; then
    echo "❌ $name: REMOTE_SECRETS_DIR y el volumen de imágenes no pueden solaparse" >&2
    fallos=1
    continue
  fi
  echo "· puertos efectivos verificados: registro $puerto_reg, panel $puerto_web"

  # Los ajustes que se tocan por pantalla se leen de la plantilla y se vuelven a
  # pasar al contenedor. Sin esto, redesplegar los devolvía al valor de fábrica
  # —retención apagada, sin ventana— sin decir nada, y el usuario descubriría
  # semanas después que su configuración llevaba tiempo sin aplicarse.
  keep_last="$(var_plantilla KEEP_LAST)"
  cache_ttl="$(var_plantilla CACHE_TTL)"
  stats_enabled="$(var_plantilla STATS_ENABLED)"
  hora_mant="$(var_plantilla MAINTENANCE_HOUR)"
  protegidas="$(var_plantilla PROTECTED_TAGS)"
  mant_gc="$(var_plantilla MAINTENANCE_GC)"
  tz="$(var_plantilla TZ)"
  require_all="$(var_plantilla RETENTION_REQUIRE_ALL_NODES)"
  branch_pattern="$(var_plantilla RETENTION_BRANCH_PATTERN)"
  sync_source="$(var_plantilla SYNC_SOURCE)"
  cors_origins="$(var_plantilla CORS_ALLOWED_ORIGINS)"
  maint_enabled="$(var_plantilla MAINTENANCE_ENABLED)"
  coordinator="$(var_plantilla MAINTENANCE_COORDINATOR)"
  lease_ttl="$(var_plantilla MAINTENANCE_LEASE_TTL)"
  maint_enabled="${MAINTENANCE_ENABLED:-${maint_enabled:-0}}"
  coordinator="${MAINTENANCE_COORDINATOR:-${coordinator:-$DEFAULT_COORDINATOR}}"
  keep_last="${keep_last:-0}"
  cache_ttl="${cache_ttl:-300}"
  stats_enabled="${STATS_ENABLED:-${stats_enabled:-1}}"
  protegidas="${protegidas:-latest}"
  mant_gc="${mant_gc:-1}"
  tz="${tz:-UTC}"
  require_all="${require_all:-1}"
  lease_ttl="${lease_ttl:-7200}"
  for env_value in "$keep_last" "$cache_ttl" "$stats_enabled" "$hora_mant" \
      "$protegidas" "$mant_gc" "$tz" \
      "$require_all" "$branch_pattern" "$sync_source" "$cors_origins" "$coordinator" \
      "$lease_ttl"; do
    if [[ "$env_value" == *"'"* || "$env_value" == *$'\n'* || "$env_value" == *$'\r'* ]]; then
      echo "❌ $name: un valor de plantilla contiene caracteres no seguros" >&2
      fallos=1
      continue 2
    fi
  done
  if [[ "$maint_enabled" != "0" && "$maint_enabled" != "1" \
        || "$stats_enabled" != "0" && "$stats_enabled" != "1" \
        || "$mant_gc" != "0" && "$mant_gc" != "1" \
        || "$require_all" != "0" && "$require_all" != "1" ]]; then
    echo "❌ $name: las opciones booleanas sólo admiten 0 o 1" >&2
    fallos=1
    continue
  fi
  if [[ ! "$keep_last" =~ ^(0|[1-9][0-9]{0,5})$ || "$keep_last" -gt 100000 \
        || ! "$cache_ttl" =~ ^(0|[1-9][0-9]{0,5})$ || "$cache_ttl" -gt 86400 \
        || ! "$lease_ttl" =~ ^[1-9][0-9]{2,7}$ || "$lease_ttl" -lt 300 \
        || "$lease_ttl" -gt 31536000 ]]; then
    echo "❌ $name: retención, caché o caducidad están fuera de rango" >&2
    fallos=1
    continue
  fi
  if [[ -n "$hora_mant" \
        && ! "$hora_mant" =~ ^([0-9]|[01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    echo "❌ $name: MAINTENANCE_HOUR no representa una hora válida" >&2
    fallos=1
    continue
  fi
  if [[ ! "$tz" =~ ^[a-zA-Z0-9_+-]+(/[a-zA-Z0-9_+-]+)*$ \
        || "$tz" == *"/../"* || "$tz" == *"/.." || ${#tz} -gt 128 ]]; then
    echo "❌ $name: TZ no es un identificador de zona seguro" >&2
    fallos=1
    continue
  fi
  if [[ -z "${NODE_SEEN[$coordinator]:-}" \
        || -n "$sync_source" && -z "${NODE_SEEN[$sync_source]:-}" ]]; then
    echo "❌ $name: coordinador u origen de sincronización no pertenece a NODES_CONFIG" >&2
    fallos=1
    continue
  fi
  if [[ ${#branch_pattern} -gt 512 ]] \
      || ! python3 -c 'import re,sys; re.compile(sys.argv[1])' "$branch_pattern" \
          >/dev/null 2>&1; then
    echo "❌ $name: RETENTION_BRANCH_PATTERN no es una regex válida" >&2
    fallos=1
    continue
  fi
  if [[ -n "$protegidas" ]]; then
    IFS=, read -r -a protected_values <<< "$protegidas"
    for protected_value in "${protected_values[@]}"; do
      if [[ ! "$protected_value" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]]; then
        echo "❌ $name: PROTECTED_TAGS contiene una etiqueta OCI inválida" >&2
        fallos=1
        continue 2
      fi
    done
  fi
  if [[ ${#cors_origins} -gt 2048 ]]; then
    echo "❌ $name: CORS_ALLOWED_ORIGINS es demasiado largo" >&2
    fallos=1
    continue
  fi
  NODE_STATS_ENABLED["$name"]="$stats_enabled"
  admin_state="$(remote_secret_state "$ADMIN_SECRET_NAME")"
  cluster_state="$(remote_secret_state "$CLUSTER_SECRET_NAME")"
  if [[ "$admin_state" == "invalid" || "$cluster_state" == "invalid" ]]; then
    echo "❌ $name: los secretos remotos deben ser ficheros root:root 0400, no enlaces" >&2
    fallos=1
    continue
  fi

  admin_configured=0
  cluster_configured=0
  update_admin=0
  update_cluster=0
  secret_changed=0
  admin_effective_hash=""
  cluster_effective_hash=""
  if [[ -n "$ADMIN_TOKEN" ]]; then
    admin_configured=1
    admin_effective_hash="$(printf '%s\n' "$ADMIN_TOKEN" | sha256sum | cut -d' ' -f1)"
    if [[ "$admin_state" != "valid" ]] \
        || [[ "$(remote_secret_hash "$ADMIN_SECRET_NAME")" != "$admin_effective_hash" ]]; then
      secret_changed=1
      update_admin=1
    fi
  elif [[ "$admin_state" == "valid" ]]; then
    admin_configured=1
    admin_effective_hash="$(remote_secret_hash "$ADMIN_SECRET_NAME")"
  fi
  if [[ -n "$CLUSTER_TOKEN" ]]; then
    cluster_configured=1
    cluster_effective_hash="$(printf '%s\n' "$CLUSTER_TOKEN" | sha256sum | cut -d' ' -f1)"
    if [[ "$cluster_state" != "valid" ]] \
        || [[ "$(remote_secret_hash "$CLUSTER_SECRET_NAME")" != "$cluster_effective_hash" ]]; then
      secret_changed=1
      update_cluster=1
    fi
  elif [[ "$cluster_state" == "valid" ]]; then
    cluster_configured=1
    cluster_effective_hash="$(remote_secret_hash "$CLUSTER_SECRET_NAME")"
  fi

  if [[ "$maint_enabled" == "1" && "$admin_configured" != "1" ]]; then
    echo "❌ $name: MAINTENANCE_ENABLED=1 exige ADMIN_TOKEN_FILE o un fichero remoto válido" >&2
    fallos=1
    continue
  fi
  if [[ ${#NODES[@]} -gt 1 && "$cluster_configured" != "1" ]]; then
    echo "❌ $name: el modo multinodo exige CLUSTER_TOKEN_FILE" >&2
    fallos=1
    continue
  fi
  if [[ -n "$admin_effective_hash" && "$admin_effective_hash" == "$cluster_effective_hash" ]]; then
    echo "❌ $name: los tokens administrativo y de clúster deben ser distintos" >&2
    fallos=1
    continue
  fi
  admin_file_value=""
  cluster_file_value=""
  [[ "$admin_configured" == "1" ]] && admin_file_value="$SECRET_MOUNT/$ADMIN_SECRET_NAME"
  [[ "$cluster_configured" == "1" ]] && cluster_file_value="$SECRET_MOUNT/$CLUSTER_SECRET_NAME"
  if [[ -n "${keep_last}${hora_mant}" ]]; then
    echo "· ajustes según la plantilla: retención ${keep_last:-0} por rama · ventana ${hora_mant:-sin programar}"
  fi

  pares=""
  paneles=""
  for otro in "${NODES[@]}"; do
    IFS=: read -r oname oip _ <<< "$otro"
    [[ "$oname" == "$name" ]] && continue
    pares="${pares:+$pares,}$oname=http://$oip:${NODE_REGISTRY_PORT[$oname]}"
    paneles="${paneles:+$paneles,}$oname=http://$oip:${NODE_STATS_PORT[$oname]}"
  done

  template_values_match=1
  [[ "$template_data_dir" == "$data_dir" ]] || template_values_match=0

  # Se reescribe si falta, si se fuerza, o si la instalada describe OTRA cosa (la
  # de la etapa de dos contenedores apuntaba a registry:2). Una plantilla que no
  # describe lo desplegado es peor que no tenerla: al pulsar «aplicar» en Unraid
  # recrearía el contenedor equivocado.
  tpl_img="$(elemento_plantilla Repository)"
  tpl_secret_contract="$(ssh_node "
test -f '$TPL' \
  && grep -Fq '<Registry>https://github.com/Ezr43l/local-registry-s/pkgs/container/local-registry-s</Registry>' '$TPL' \
  && grep -Fq '<Support>https://github.com/Ezr43l/local-registry-s/issues</Support>' '$TPL' \
  && grep -Fq '<Project>https://github.com/Ezr43l/local-registry-s</Project>' '$TPL' \
  && grep -Fq '<TemplateURL>https://raw.githubusercontent.com/Ezr43l/local-registry-s/main/unraid/my-Local-Registry.xml</TemplateURL>' '$TPL' \
  && grep -Fq '<Icon>https://www.docker.com/wp-content/uploads/2022/03/Moby-logo.png</Icon>' '$TPL' \
  && grep -q 'Target=\"5000\"' '$TPL' \
  && grep -q 'Target=\"5001\"' '$TPL' \
  && grep -q 'Target=\"/var/lib/registry\"' '$TPL' \
  && [ \"\$(grep -c '<Config ' '$TPL')\" = 3 ] \
  && ! grep -Fq 'Type=\"Variable\"' '$TPL' \
  && ! grep -q 'Target=\"MAINTENANCE_ADMIN_TOKEN\"' '$TPL' \
  && ! grep -q 'Target=\"MAINTENANCE_CLUSTER_TOKEN\"' '$TPL' \
  && grep -q '<Privileged>false</Privileged>' '$TPL' \
  && grep -q -- '--read-only' '$TPL' \
  && grep -Fq -- '--tmpfs /run:rw,nosuid,noexec,size=16m,mode=0755' '$TPL' \
  && grep -Fq -- '--tmpfs /tmp:rw,nosuid,noexec,size=32m,mode=1777' '$TPL' \
  && grep -q -- '--cap-drop=ALL' '$TPL' \
  && grep -q -- '--security-opt=no-new-privileges' '$TPL' \
  && grep -q -- '--pids-limit=256' '$TPL' \
  && echo secure
" 2>/dev/null || true)"
  if [[ "$tpl_img" == "$IMAGE" && "$tpl_secret_contract" == "secure" \
        && "$template_values_match" == "1" && $FORCE_TPL == 0 ]]; then
    echo "· plantilla de Unraid: al día (la gestiona Unraid)"
  elif [[ $DRY == 1 ]]; then
    if [[ -z "$tpl_img" ]]; then
      echo "· plantilla de Unraid: SE INSTALARÍA"
    else
      echo "· plantilla de Unraid: SE ACTUALIZARÍA (ahora describe '$tpl_img')"
    fi
  else
    template_update=1
    if ! python3 "$RENDERER" "unraid/my-$CONTAINER.xml" \
        --repository "$IMAGE" \
        --set "5000=$puerto_reg" \
        --set "5001=$puerto_web" \
        --set "/var/lib/registry=$data_dir" \
        --set "$SECRET_MOUNT=$REMOTE_SECRETS_DIR" \
        --set "NODE_NAME=$name" \
        --set "PEERS=$pares" \
        --set "PANEL_PEERS=$paneles" \
        --set "CACHE_TTL=$cache_ttl" \
        --set "STATS_ENABLED=$stats_enabled" \
        --set "MAINTENANCE_ENABLED=$maint_enabled" \
        --set "MAINTENANCE_ADMIN_TOKEN_FILE=$admin_file_value" \
        --set "KEEP_LAST=$keep_last" \
        --set "PROTECTED_TAGS=$protegidas" \
        --set "MAINTENANCE_HOUR=$hora_mant" \
        --set "MAINTENANCE_GC=$mant_gc" \
        --set "MAINTENANCE_COORDINATOR=$coordinator" \
        --set "MAINTENANCE_CLUSTER_TOKEN_FILE=$cluster_file_value" \
        --set "MAINTENANCE_LEASE_TTL=$lease_ttl" \
        --set "RETENTION_REQUIRE_ALL_NODES=$require_all" \
        --set "RETENTION_BRANCH_PATTERN=$branch_pattern" \
        --set "SYNC_SOURCE=$sync_source" \
        --set "CORS_ALLOWED_ORIGINS=$cors_origins" \
        --set "TZ=$tz" \
      | ssh_node "
set -eu
dir='$TEMPLATE_DIR'
target='$TPL'
stage='$TPL_STAGE'
for path in /boot/config /boot/config/plugins /boot/config/plugins/dockerMan \"\$dir\"; do
  [ ! -L \"\$path\" ]
done
mkdir -p \"\$dir\"
for path in /boot/config /boot/config/plugins /boot/config/plugins/dockerMan \"\$dir\"; do
  [ -d \"\$path\" ] && [ ! -L \"\$path\" ]
done
[ ! -L \"\$target\" ]
[ ! -e \"\$stage\" ] && [ ! -L \"\$stage\" ]
cleanup_template_stage() { rm -f \"\$stage\"; }
trap cleanup_template_stage EXIT HUP INT TERM
cat > \"\$stage\"
[ -s \"\$stage\" ] && [ ! -L \"\$stage\" ]
grep -Fq '<Repository>$IMAGE</Repository>' \"\$stage\"
test \"\$(grep -c '<Config ' \"\$stage\")\" = 3
grep -q 'Target=\"5000\"' \"\$stage\"
grep -q 'Target=\"5001\"' \"\$stage\"
grep -q 'Target=\"/var/lib/registry\"' \"\$stage\"
! grep -Fq 'Type=\"Variable\"' \"\$stage\"
grep -q -- '--read-only' \"\$stage\"
grep -q -- '--cap-drop=ALL' \"\$stage\"
grep -q -- '--security-opt=no-new-privileges' \"\$stage\"
! grep -q 'Target=\"MAINTENANCE_ADMIN_TOKEN\"' \"\$stage\"
! grep -q 'Target=\"MAINTENANCE_CLUSTER_TOKEN\"' \"\$stage\"
trap - EXIT HUP INT TERM
"; then
      echo "❌ $name: no se pudo preparar de forma segura la plantilla de Unraid" >&2
      cleanup_staged_template
      fallos=1
      continue
    fi
    echo "· plantilla de Unraid preparada; se activará al confirmar el contenedor"
  fi

  # ── 4. el contenedor ───────────────────────────────────────────────────────
  #
  # Solo se recrea si falta o si no coincide: recrear porque sí deja el registro
  # fuera de servicio unos segundos, y si justo entonces algo está tirando una
  # imagen, se lleva el fallo.
  # El `if` no sobra: `index` revienta si ese mapeo de puerto no existe, y el
  # contenedor viejo no tenía el del panel. Sin esto, el estado leído salía
  # corrupto en vez de simplemente «distinto».
  fmt='{{.State.Running}}|{{.Config.Image}}|{{.HostConfig.RestartPolicy.Name}}'
  fmt+='|{{if index .HostConfig.PortBindings "5000/tcp"}}{{(index .HostConfig.PortBindings "5000/tcp" 0).HostPort}}{{end}}'
  fmt+='|{{if index .HostConfig.PortBindings "5001/tcp"}}{{(index .HostConfig.PortBindings "5001/tcp" 0).HostPort}}{{end}}'
  estado="$(ssh_node "docker inspect '$CONTAINER' --format '$fmt' 2>/dev/null" || true)"
  container_exists=1
  if [[ -z "$estado" ]]; then
    estado="AUSENTE"
    container_exists=0
  else
    estado="${estado}|${current_bind:-sin-volumen}"
  fi
  deseado="true|$IMAGE|unless-stopped|$puerto_reg|$puerto_web|$data_dir:/var/lib/registry"

  runtime_contract="$(ssh_node "
set -eu
envs=\"\$(docker inspect '$CONTAINER' --format '{{range .Config.Env}}{{println .}}{{end}}')\"
binds=\"\$(docker inspect '$CONTAINER' --format '{{range .HostConfig.Binds}}{{println .}}{{end}}')\"
! printf '%s\n' \"\$envs\" | grep -Eq '^MAINTENANCE_(ADMIN|CLUSTER)_TOKEN='
printf '%s\n' \"\$binds\" | grep -Fxq '$REMOTE_SECRETS_DIR:$SECRET_MOUNT:ro'
printf '%s\n' \"\$binds\" | grep -Fxq '$data_dir:/var/lib/registry'
! printf '%s\n' \"\$binds\" | grep -Fq '/var/run/docker.sock'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_ADMIN_TOKEN_FILE=$admin_file_value'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_CLUSTER_TOKEN_FILE=$cluster_file_value'
printf '%s\n' \"\$envs\" | grep -Fxq 'APP_VERSION=$IMG_VER'
printf '%s\n' \"\$envs\" | grep -Fxq 'NODE_NAME=$name'
printf '%s\n' \"\$envs\" | grep -Fxq 'PEERS=$pares'
printf '%s\n' \"\$envs\" | grep -Fxq 'PANEL_PEERS=$paneles'
printf '%s\n' \"\$envs\" | grep -Fxq 'CACHE_TTL=$cache_ttl'
printf '%s\n' \"\$envs\" | grep -Fxq 'STATS_ENABLED=$stats_enabled'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_ENABLED=$maint_enabled'
printf '%s\n' \"\$envs\" | grep -Fxq 'KEEP_LAST=$keep_last'
printf '%s\n' \"\$envs\" | grep -Fxq 'PROTECTED_TAGS=$protegidas'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_HOUR=$hora_mant'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_GC=$mant_gc'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_COORDINATOR=$coordinator'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_LEASE_TTL=$lease_ttl'
printf '%s\n' \"\$envs\" | grep -Fxq 'RETENTION_REQUIRE_ALL_NODES=$require_all'
printf '%s\n' \"\$envs\" | grep -Fxq 'RETENTION_BRANCH_PATTERN=$branch_pattern'
printf '%s\n' \"\$envs\" | grep -Fxq 'SYNC_SOURCE=$sync_source'
printf '%s\n' \"\$envs\" | grep -Fxq 'CORS_ALLOWED_ORIGINS=$cors_origins'
printf '%s\n' \"\$envs\" | grep -Fxq 'TZ=$tz'
test \"\$(docker inspect '$CONTAINER' --format '{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.Privileged}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.Init}}')\" = 'true|false|256|true'
test \"\$(docker inspect '$CONTAINER' --format '{{json .HostConfig.CapDrop}}')\" = '[\"ALL\"]'
test \"\$(docker inspect '$CONTAINER' --format '{{json .HostConfig.SecurityOpt}}')\" = '[\"no-new-privileges\"]'
test \"\$(docker inspect '$CONTAINER' --format '{{index .HostConfig.Tmpfs \"/run\"}}|{{index .HostConfig.Tmpfs \"/tmp\"}}')\" = 'rw,nosuid,noexec,size=16m,mode=0755|rw,nosuid,noexec,size=32m,mode=1777'
curl -fsS --max-time 2 'http://127.0.0.1:$puerto_reg/v2/' >/dev/null
if [ '$stats_enabled' = 1 ]; then
  curl -fsS --max-time 2 'http://127.0.0.1:$puerto_web/api/health' >/dev/null
else
  ! curl -fsS --max-time 2 'http://127.0.0.1:$puerto_web/api/health' >/dev/null 2>&1
fi
echo secure
" 2>/dev/null || true)"

  if [[ "$estado" == "$deseado" && "$runtime_contract" == "secure" \
        && "$secret_changed" == "0" && $FORCE == 0 ]]; then
    echo "· contenedor $CONTAINER: correcto, no lo toco"
    if [[ "$template_update" == "1" ]]; then
      if ! activate_staged_template; then
        echo "❌ $name: el contenedor está sano, pero no se pudo activar la plantilla" >&2
        cleanup_staged_template
        fallos=1
        continue
      fi
      echo "· plantilla de Unraid activada atómicamente"
    fi
  elif [[ $DRY == 1 ]]; then
    echo "· contenedor $CONTAINER: SE RECREARÍA"
    [[ "$container_exists" == "1" ]] && echo "    ahora:  $estado"
    echo "    quiero: $deseado"
    [[ "$secret_changed" == "1" ]] && echo "    secretos: SE ACTUALIZARÍAN por stdin y con rollback"
  else
    if [[ "$container_exists" == "0" ]]; then
      echo "· contenedor $CONTAINER: no existe, lo creo"
    else
      echo "· contenedor $CONTAINER: no coincide, lo recreo"
    fi
    # La imagen pública se trae ANTES de tirar nada. Si el pull falla no se toca
    # el contenedor y el registro sigue funcionando con lo que tenía.
    if ! ssh_node "docker pull -q '$IMAGE' >/dev/null"; then
      echo "❌ $name: no pude traer $IMAGE del registro. NO toco el contenedor." >&2
      echo "   Construye y replica primero:  ./build-local-registry.sh" >&2
      cleanup_staged_template
      fallos=1
      continue
    else
      if [[ "$update_admin" == "1" ]] && ! stage_remote_secret "$ADMIN_SECRET_NAME" "$ADMIN_TOKEN"; then
        echo "❌ $name: no se pudo preparar el token administrativo" >&2
        cleanup_staged_secrets
        cleanup_staged_template
        fallos=1
        continue
      fi
      if [[ "$update_cluster" == "1" ]] && ! stage_remote_secret "$CLUSTER_SECRET_NAME" "$CLUSTER_TOKEN"; then
        echo "❌ $name: no se pudo preparar el secreto de clúster" >&2
        cleanup_staged_secrets
        cleanup_staged_template
        fallos=1
        continue
      fi

      # Las rutas vacías también se declaran: son configuración, no secretos, y
      # permiten comparar el contrato exacto sin recrear indefinidamente un nodo
      # de sólo lectura que todavía no use mantenimiento.
      admin_env_arg="-e MAINTENANCE_ADMIN_TOKEN_FILE=$admin_file_value"
      cluster_env_arg="-e MAINTENANCE_CLUSTER_TOKEN_FILE=$cluster_file_value"
      rollback_container="$CONTAINER-rollback-$DEPLOY_ID"

      # Se conserva el contenedor anterior y se activan secretos y plantilla de
      # forma atómica. Cualquier error anterior al commit restaura las tres cosas.
      if ! ssh_node "
set -eu
secret_dir='$REMOTE_SECRETS_DIR'
backup_dir=\"\$secret_dir/.rollback-$DEPLOY_ID\"
rollback_container='$rollback_container'
template_target='$TPL'
template_stage='$TPL_STAGE'
template_backup='$TPL_BACKUP'
old_present=0
secrets_activated=0
template_touched=0
template_had_old=0
# Distingue «contenedor ausente» de «daemon inaccesible»: ante lo segundo se
# aborta antes de activar secretos o tocar el contenedor que estaba sirviendo.
docker info >/dev/null
if docker inspect '$CONTAINER' >/dev/null 2>&1; then
  old_present=1
fi
# Un nombre de rollback ocupado indica una ejecución anterior sin resolver. No
# se adivina cuál es el contenedor correcto y aún no se ha activado ningún
# secreto, por lo que se aborta dejando ambos intactos.
! docker inspect \"\$rollback_container\" >/dev/null 2>&1
rollback() {
  code=\$?
  trap - EXIT HUP INT TERM
  set +e
  if docker inspect \"\$rollback_container\" >/dev/null 2>&1; then
    docker rm -f '$CONTAINER' >/dev/null 2>&1
    docker rename \"\$rollback_container\" '$CONTAINER' >/dev/null 2>&1
    docker start '$CONTAINER' >/dev/null 2>&1
  elif [ \"\$old_present\" = 0 ]; then
    # En una instalación nueva no hay contenedor anterior que preservar.
    docker rm -f '$CONTAINER' >/dev/null 2>&1
  fi
  if [ \"\$secrets_activated\" = 1 ]; then
    for secret_name in '$ADMIN_SECRET_NAME' '$CLUSTER_SECRET_NAME'; do
      target=\"\$secret_dir/\$secret_name\"
      if [ -f \"\$backup_dir/\$secret_name\" ]; then
        cp -p \"\$backup_dir/\$secret_name\" \"\$target\"
      elif [ -f \"\$backup_dir/.\$secret_name.absent\" ]; then
        rm -f \"\$target\"
      fi
    done
  fi
  if [ \"\$template_touched\" = 1 ]; then
    rm -f \"\$template_target\"
    if [ \"\$template_had_old\" = 1 ] && [ -f \"\$template_backup\" ]; then
      mv \"\$template_backup\" \"\$template_target\"
    fi
  fi
  rm -rf \"\$backup_dir\"
  rm -f \"\$secret_dir/.$ADMIN_SECRET_NAME.$DEPLOY_ID.new\" \
        \"\$secret_dir/.$CLUSTER_SECRET_NAME.$DEPLOY_ID.new\" \
        \"\$template_stage\" \"\$template_backup\"
  exit \"\$code\"
}
trap rollback EXIT HUP INT TERM

mkdir -p '$data_dir'
[ ! -L \"\$secret_dir\" ]
mkdir -p \"\$secret_dir\"
chown root:root \"\$secret_dir\"
chmod 0700 \"\$secret_dir\"
if [ '$template_update' = 1 ]; then
  [ -f \"\$template_stage\" ] && [ ! -L \"\$template_stage\" ]
  [ ! -L \"\$template_target\" ]
  [ ! -e \"\$template_backup\" ] && [ ! -L \"\$template_backup\" ]
fi
if [ '$update_admin' = 1 ] || [ '$update_cluster' = 1 ]; then
  [ ! -e \"\$backup_dir\" ]
  mkdir -m 0700 \"\$backup_dir\"
  chown root:root \"\$backup_dir\"
  # Se activa antes del primer mv: incluso si falla a mitad del bucle, todo lo
  # que ya se haya sustituido se restaura usando su copia o marcador de ausencia.
  secrets_activated=1
  for secret_name in '$ADMIN_SECRET_NAME' '$CLUSTER_SECRET_NAME'; do
    update=0
    [ \"\$secret_name\" = '$ADMIN_SECRET_NAME' ] && update='$update_admin'
    [ \"\$secret_name\" = '$CLUSTER_SECRET_NAME' ] && update='$update_cluster'
    [ \"\$update\" = 1 ] || continue
    target=\"\$secret_dir/\$secret_name\"
    stage=\"\$secret_dir/.\$secret_name.$DEPLOY_ID.new\"
    [ -f \"\$stage\" ] && [ ! -L \"\$stage\" ]
    if [ -e \"\$target\" ]; then
      [ -f \"\$target\" ] && [ ! -L \"\$target\" ]
      cp -p \"\$target\" \"\$backup_dir/\$secret_name\"
    else
      : > \"\$backup_dir/.\$secret_name.absent\"
    fi
    mv \"\$stage\" \"\$target\"
    chown root:root \"\$target\"
    chmod 0400 \"\$target\"
  done
fi

if [ '$admin_configured' = 1 ] && [ '$cluster_configured' = 1 ]; then
  ! cmp -s \"\$secret_dir/$ADMIN_SECRET_NAME\" \"\$secret_dir/$CLUSTER_SECRET_NAME\"
fi
if [ \"\$old_present\" = 1 ]; then
  docker rename '$CONTAINER' \"\$rollback_container\"
  docker stop \"\$rollback_container\" >/dev/null
fi

docker run -d --name '$CONTAINER' \
  --restart=unless-stopped \
  --init \
  --read-only \
  --tmpfs /run:rw,nosuid,noexec,size=16m,mode=0755 \
  --tmpfs /tmp:rw,nosuid,noexec,size=32m,mode=1777 \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --pids-limit=256 \
  -p $puerto_reg:5000 \
  -p $puerto_web:5001 \
  -v '$data_dir:/var/lib/registry' \
  -v '$REMOTE_SECRETS_DIR:$SECRET_MOUNT:ro' \
  -e NODE_NAME='$name' \
  -e PEERS='$pares' \
  -e PANEL_PEERS='$paneles' \
  -e APP_VERSION='$IMG_VER' \
  -e CACHE_TTL='$cache_ttl' \
  -e STATS_ENABLED='$stats_enabled' \
  -e MAINTENANCE_ENABLED='$maint_enabled' \
  $admin_env_arg \
  -e KEEP_LAST='$keep_last' \
  -e PROTECTED_TAGS='$protegidas' \
  -e MAINTENANCE_HOUR='$hora_mant' \
  -e MAINTENANCE_GC='$mant_gc' \
  -e MAINTENANCE_COORDINATOR='$coordinator' \
  $cluster_env_arg \
  -e MAINTENANCE_LEASE_TTL='$lease_ttl' \
  -e RETENTION_REQUIRE_ALL_NODES='$require_all' \
  -e RETENTION_BRANCH_PATTERN='$branch_pattern' \
  -e SYNC_SOURCE='$sync_source' \
  -e CORS_ALLOWED_ORIGINS='$cors_origins' \
  -e TZ='$tz' \
  --label net.unraid.docker.managed=dockerman \
  --label 'net.unraid.docker.webui=http://[IP]:[PORT:5001]/' \
  --label net.unraid.docker.icon=$ICON \
  '$IMAGE' >/dev/null

ready=0
for attempt in \$(seq 1 30); do
  panel_ready=1
  if [ '$stats_enabled' = 1 ]; then
    curl -fsS --max-time 2 'http://127.0.0.1:$puerto_web/api/health' >/dev/null 2>&1 \
      || panel_ready=0
  else
    if curl -fsS --max-time 2 'http://127.0.0.1:$puerto_web/api/health' >/dev/null 2>&1; then
      panel_ready=0
    fi
  fi
  if [ \"\$panel_ready\" = 1 ] \
      && curl -fsS --max-time 2 'http://127.0.0.1:$puerto_reg/v2/' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ \"\$ready\" = 1 ]

# Contrato exacto de la instancia antes del punto de no retorno. No basta con
# recibir HTTP: deben coincidir imagen, puertos, secretos, bind mounts y límites.
test \"\$(docker inspect '$CONTAINER' --format '{{.State.Running}}')\" = true
test \"\$(docker inspect '$CONTAINER' --format '{{.Config.Image}}')\" = '$IMAGE'
test \"\$(docker inspect '$CONTAINER' --format '{{.HostConfig.RestartPolicy.Name}}')\" = unless-stopped
test \"\$(docker inspect '$CONTAINER' --format '{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.Privileged}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.Init}}')\" = 'true|false|256|true'
test \"\$(docker inspect '$CONTAINER' --format '{{json .HostConfig.CapDrop}}')\" = '[\"ALL\"]'
test \"\$(docker inspect '$CONTAINER' --format '{{json .HostConfig.SecurityOpt}}')\" = '[\"no-new-privileges\"]'
test \"\$(docker inspect '$CONTAINER' --format '{{index .HostConfig.Tmpfs \"/run\"}}|{{index .HostConfig.Tmpfs \"/tmp\"}}')\" = 'rw,nosuid,noexec,size=16m,mode=0755|rw,nosuid,noexec,size=32m,mode=1777'
test \"\$(docker inspect '$CONTAINER' --format '{{(index .HostConfig.PortBindings \"5000/tcp\" 0).HostPort}}')\" = '$puerto_reg'
test \"\$(docker inspect '$CONTAINER' --format '{{(index .HostConfig.PortBindings \"5001/tcp\" 0).HostPort}}')\" = '$puerto_web'
envs=\"\$(docker inspect '$CONTAINER' --format '{{range .Config.Env}}{{println .}}{{end}}')\"
binds=\"\$(docker inspect '$CONTAINER' --format '{{range .HostConfig.Binds}}{{println .}}{{end}}')\"
! printf '%s\n' \"\$envs\" | grep -Eq '^MAINTENANCE_(ADMIN|CLUSTER)_TOKEN='
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_ADMIN_TOKEN_FILE=$admin_file_value'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_CLUSTER_TOKEN_FILE=$cluster_file_value'
printf '%s\n' \"\$envs\" | grep -Fxq 'APP_VERSION=$IMG_VER'
printf '%s\n' \"\$envs\" | grep -Fxq 'NODE_NAME=$name'
printf '%s\n' \"\$envs\" | grep -Fxq 'PEERS=$pares'
printf '%s\n' \"\$envs\" | grep -Fxq 'PANEL_PEERS=$paneles'
printf '%s\n' \"\$envs\" | grep -Fxq 'CACHE_TTL=$cache_ttl'
printf '%s\n' \"\$envs\" | grep -Fxq 'STATS_ENABLED=$stats_enabled'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_ENABLED=$maint_enabled'
printf '%s\n' \"\$envs\" | grep -Fxq 'KEEP_LAST=$keep_last'
printf '%s\n' \"\$envs\" | grep -Fxq 'PROTECTED_TAGS=$protegidas'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_HOUR=$hora_mant'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_GC=$mant_gc'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_COORDINATOR=$coordinator'
printf '%s\n' \"\$envs\" | grep -Fxq 'MAINTENANCE_LEASE_TTL=$lease_ttl'
printf '%s\n' \"\$envs\" | grep -Fxq 'RETENTION_REQUIRE_ALL_NODES=$require_all'
printf '%s\n' \"\$envs\" | grep -Fxq 'RETENTION_BRANCH_PATTERN=$branch_pattern'
printf '%s\n' \"\$envs\" | grep -Fxq 'SYNC_SOURCE=$sync_source'
printf '%s\n' \"\$envs\" | grep -Fxq 'CORS_ALLOWED_ORIGINS=$cors_origins'
printf '%s\n' \"\$envs\" | grep -Fxq 'TZ=$tz'
printf '%s\n' \"\$binds\" | grep -Fxq '$data_dir:/var/lib/registry'
printf '%s\n' \"\$binds\" | grep -Fxq '$REMOTE_SECRETS_DIR:$SECRET_MOUNT:ro'
! printf '%s\n' \"\$binds\" | grep -Fq '/var/run/docker.sock'

if [ '$template_update' = 1 ]; then
  if [ -e \"\$template_target\" ]; then
    [ -f \"\$template_target\" ] && [ ! -L \"\$template_target\" ]
    cp \"\$template_target\" \"\$template_backup\"
    template_had_old=1
  fi
  template_touched=1
  mv \"\$template_stage\" \"\$template_target\"
  grep -Fq '<Repository>$IMAGE</Repository>' \"\$template_target\"
  test \"\$(grep -c '<Config ' \"\$template_target\")\" = 3
  grep -q 'Target=\"5000\"' \"\$template_target\"
  grep -q 'Target=\"5001\"' \"\$template_target\"
  grep -q 'Target=\"/var/lib/registry\"' \"\$template_target\"
  ! grep -Fq 'Type=\"Variable\"' \"\$template_target\"
  grep -q -- '--read-only' \"\$template_target\"
  grep -q -- '--cap-drop=ALL' \"\$template_target\"
  grep -q -- '--security-opt=no-new-privileges' \"\$template_target\"
  ! grep -q 'Target=\"MAINTENANCE_ADMIN_TOKEN\"' \"\$template_target\"
  ! grep -q 'Target=\"MAINTENANCE_CLUSTER_TOKEN\"' \"\$template_target\"
fi

# Punto de commit: desde aquí el servicio y su contrato completo están sanos.
# La retirada de copias es limpieza best-effort y nunca provoca un rollback.
trap - EXIT HUP INT TERM
secrets_activated=0

if [ \"\$old_present\" = 1 ]; then
  docker rm -f \"\$rollback_container\" >/dev/null \
    || echo 'AVISO: quedó un contenedor de rollback detenido' >&2
fi
rm -rf \"\$backup_dir\" \
  || echo 'AVISO: quedó una copia privada de secretos para limpieza manual' >&2
rm -f \"\$secret_dir/.$ADMIN_SECRET_NAME.$DEPLOY_ID.new\" \
      \"\$secret_dir/.$CLUSTER_SECRET_NAME.$DEPLOY_ID.new\" \
      \"\$template_stage\" \"\$template_backup\" \
  || echo 'AVISO: quedaron ficheros transaccionales para limpieza manual' >&2
"; then
        cleanup_staged_secrets
        cleanup_staged_template
        echo "❌ $name: el despliegue falló; contenedor, secretos y plantilla anteriores restaurados" >&2
        fallos=1
        continue
      fi
      [[ "$template_update" == "1" ]] && echo "· plantilla de Unraid activada atómicamente"
      echo "· contenedor $CONTAINER desplegado → registro :$puerto_reg · panel http://$ip:$puerto_web/"
    fi
  fi

  # Restos de la etapa de dos contenedores.
  if [[ $DRY == 0 ]]; then
    if ssh_node "docker inspect $OLD_STATS >/dev/null 2>&1"; then
      ssh_node "docker rm -f $OLD_STATS >/dev/null 2>&1 || true"
      echo "· retirado el contenedor $OLD_STATS (ahora va dentro del registro)"
    fi
    if ssh_node "test -f $TEMPLATE_DIR/my-$OLD_STATS.xml"; then
      ssh_node "rm -f $TEMPLATE_DIR/my-$OLD_STATS.xml"
      echo "· retirada la plantilla de $OLD_STATS"
    fi
  fi

  # ── 5. ¿queda un reinicio pendiente? ───────────────────────────────────────
  #
  # El docker que está corriendo ahora leyó su daemon.json en el último arranque.
  # Si el guion que acabamos de colocar produce otra cosa, el nodo está sirviendo
  # con una lista de registros vieja. No lo arreglamos aquí (ver cabecera): se
  # avisa. Se compara ejecutando el guion contra un directorio de usar y tirar,
  # para no escribir en el /etc/docker de verdad.
  if [[ $DRY == 0 ]]; then
    if ! ssh_node "
T=\$(mktemp -d)
mkdir -p \$T/etc/docker
if [ -f /etc/docker/daemon.json ]; then
  cp /etc/docker/daemon.json \$T/etc/docker/daemon.json
fi
sed 's#/etc/docker#'\$T'/etc/docker#g' $BOOT_DIR/$BOOT_SCRIPT > \$T/check.sh
bash \$T/check.sh
cmp -s \$T/etc/docker/daemon.json /etc/docker/daemon.json
r=\$?
rm -rf \$T
exit \$r
" 2>/dev/null; then
      echo "⚠  el docker vivo tiene otra configuración de registros: queda REINICIO pendiente"
      pendiente_reinicio+=("$name")
    else
      echo "· configuración de registros: el docker vivo ya está al día"
    fi
  fi
done

# ── comprobación final ───────────────────────────────────────────────────────
#
# Comprobado, no proclamado: no basta con que los comandos no fallen; el registro
# tiene que contestar de verdad. Se pregunta desde el PC, que es como lo usamos.
if [[ $DRY == 1 ]]; then
  echo
  echo "(simulación terminada: no se ha tocado nada)"
  exit "$fallos"
fi

echo
echo "── comprobación ─────────────────────────────"
for entry in "${NODES[@]}"; do
  IFS=: read -r name ip key <<< "$entry"
  if [[ ${#FILTER[@]} -gt 0 ]]; then
    encontrado=0
    for f in "${FILTER[@]}"; do [[ "$f" == "$name" ]] && encontrado=1; done
    [[ $encontrado == 1 ]] || continue
  fi
  SSH_KEY="$SECRETS_DIR/$key"
  # Se preguntan los puertos reales al contenedor en vez de suponerlos: puede
  # haberlos cambiado el usuario desde Unraid.
  leer_puerto() {
    ssh -i "$SSH_KEY" \
      -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=yes \
      -o ConnectTimeout=10 \
      -o BatchMode=yes \
      "root@$ip" \
      "docker inspect $CONTAINER --format '{{if index .HostConfig.PortBindings \"$1/tcp\"}}{{(index .HostConfig.PortBindings \"$1/tcp\" 0).HostPort}}{{end}}' 2>/dev/null" || true
  }
  pr="$(leer_puerto 5000)"; pr="${pr:-${NODE_REGISTRY_PORT[$name]}}"
  pw="$(leer_puerto 5001)"; pw="${pw:-${NODE_STATS_PORT[$name]}}"

  if catalogo="$(curl -sf --max-time 8 "http://$ip:$pr/v2/_catalog" 2>/dev/null)"; then
    n="$(echo "$catalogo" | tr ',' '\n' | grep -c '"' || true)"
    echo "✅ $name: registro en http://$ip:$pr/v2/_catalog ($n imágenes)"
  else
    echo "❌ $name: el registro no contesta en http://$ip:$pr/v2/_catalog" >&2
    fallos=1
  fi
  if [[ "${NODE_STATS_ENABLED[$name]:-1}" == "0" ]]; then
    echo "   panel desactivado explícitamente (STATS_ENABLED=0)"
  elif curl -sf --max-time 8 "http://$ip:$pw/api/health" >/dev/null 2>&1; then
    echo "   panel en http://$ip:$pw/"
  else
    echo "❌ $name: STATS_ENABLED=1 pero el panel no contesta en http://$ip:$pw/" >&2
    fallos=1
  fi
done

if [[ ${#pendiente_reinicio[@]} -gt 0 ]]; then
  echo
  echo "⚠  Reinicio pendiente en: ${pendiente_reinicio[*]}"
  echo "   El fichero ya está colocado y entrará solo en el próximo arranque."
  echo "   Para aplicarlo antes hay que reiniciar dockerd en ese nodo, y eso"
  echo "   reinicia TODOS sus contenedores. Hazlo a mano, cuando toque."
fi

exit $fallos
