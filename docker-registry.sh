#!/bin/sh
# Configura los endpoints HTTP del registro para Docker.
#
# REGISTRY_ENDPOINTS es una lista separada por comas de host:puerto. El script
# se puede instalar en un hook de arranque o ejecutar manualmente. Si no se
# configura ningún endpoint, no toca la configuración existente.

set -eu

CONFIG="${DOCKER_DAEMON_CONFIG:-/etc/docker/daemon.json}"
ENDPOINTS="${REGISTRY_ENDPOINTS:-__REGISTRY_ENDPOINTS__}"

if [ "$ENDPOINTS" = "__REGISTRY_ENDPOINTS__" ] || [ -z "$ENDPOINTS" ]; then
  echo "[docker-registry] no hay endpoints HTTP configurados; no se modifica $CONFIG" >&2
  exit 0
fi

invalid_endpoint() {
  echo "[docker-registry] endpoint inválido: $1" >&2
  exit 2
}

valid_port() {
  case "$1" in
    ""|*[!0-9]*) return 1 ;;
  esac
  [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

valid_ipv6() {
  # Forma hexadecimal entre corchetes, con una sola compresión `::`. Se
  # rechazan aquí zonas e IPv4 incrustado para no aceptar una sintaxis que
  # Docker pueda interpretar de forma distinta al hook.
  printf '%s\n' "$1" | awk '
    {
      value = $0
      if (value == "" || value !~ /^[0-9A-Fa-f:]+$/ || value ~ /:::/) exit 1
      copy = value
      compressed = gsub(/::/, "::", copy)
      if (compressed > 1) exit 1
      if (compressed == 0 && (substr(value, 1, 1) == ":" || substr(value, length(value), 1) == ":")) exit 1
      count = split(value, groups, ":")
      present = 0
      for (idx = 1; idx <= count; idx++) {
        if (groups[idx] == "") continue
        if (length(groups[idx]) > 4 || groups[idx] !~ /^[0-9A-Fa-f]+$/) exit 1
        present++
      }
      if (compressed == 0) exit present == 8 ? 0 : 1
      exit present < 8 ? 0 : 1
    }
  '
}

# Las entradas vacías suelen proceder de una coma accidental. Rechazarlas evita
# escribir un daemon.json distinto de lo que el operador creyó configurar.
case "$ENDPOINTS" in
  ,*|*,,*|*,) invalid_endpoint "$ENDPOINTS" ;;
esac

json='['
old_ifs=$IFS
IFS=,
for endpoint in $ENDPOINTS; do
  endpoint=$(printf '%s' "$endpoint" | sed 's/^ *//;s/ *$//')
  case "$endpoint" in
    \[*\]:*)
      address="${endpoint#\[}"
      address="${address%%\]*}"
      suffix="${endpoint#*\]}"
      port="${suffix#:}"
      if ! valid_ipv6 "$address" \
          || [ "$suffix" != ":$port" ] \
          || ! valid_port "$port"; then
        invalid_endpoint "$endpoint"
      fi
      ;;
    *:*)
      host="${endpoint%:*}"
      port="${endpoint##*:}"
      case "$host" in
        ""|*[!A-Za-z0-9._-]*|[-._]*|*[-._]) invalid_endpoint "$endpoint" ;;
      esac
      valid_port "$port" || invalid_endpoint "$endpoint"
      ;;
    *) invalid_endpoint "$endpoint" ;;
  esac
  json="${json}\"${endpoint}\","
done
IFS=$old_ifs
json="${json%,}]"

mkdir -p "$(dirname "$CONFIG")"
tmp="${CONFIG}.tmp.$$"
trap 'rm -f "$tmp"' EXIT INT TERM

if [ -s "$CONFIG" ] && command -v jq >/dev/null 2>&1; then
  jq --argjson registries "$json" '. + {"insecure-registries": $registries}' \
    "$CONFIG" > "$tmp"
elif [ -s "$CONFIG" ]; then
  # En Unraid jq no siempre está instalado. No intentamos reescribir un JSON
  # existente sin poder conservar sus otras claves, pero tampoco fallamos en
  # cada arranque si ya contiene todos los endpoints que necesitamos.
  missing=0
  old_ifs=$IFS
  IFS=,
  for endpoint in $ENDPOINTS; do
    endpoint=$(printf '%s' "$endpoint" | sed 's/^ *//;s/ *$//')
    if ! grep -Fq "\"$endpoint\"" "$CONFIG"; then
      missing=1
      break
    fi
  done
  IFS=$old_ifs
  if [ "$missing" -eq 0 ]; then
    echo "[docker-registry] $CONFIG ya contiene los endpoints HTTP; no se modifica"
    exit 0
  fi
  echo "[docker-registry] $CONFIG ya existe y jq no está disponible; no se sobrescribe" >&2
  exit 1
else
  printf '{"insecure-registries":%s}\n' "$json" > "$tmp"
fi

mv "$tmp" "$CONFIG"
trap - EXIT INT TERM
echo "[docker-registry] endpoints HTTP configurados en $CONFIG"
