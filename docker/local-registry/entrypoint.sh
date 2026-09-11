#!/bin/sh
# Arranca los dos procesos del contenedor: el registro y su panel.
#
# NO son igual de importantes, y el guion lo refleja:
#
#   · El REGISTRO es la razon de ser de esto. Si muere, el contenedor muere con
#     el, para que la politica de reinicio de Docker lo levante otra vez.
#   · El PANEL es accesorio. Si se cae, se vuelve a levantar solo y el registro
#     ni se entera. Nunca debe pasar que un fallo mirando estadisticas deje al
#     cluster sin poder desplegar.
#
# Se puede apagar el panel del todo con STATS_ENABLED=0 y el registro sigue
# funcionando igual, que es la garantia de que anadirlo no ha hecho el registro
# mas fragil.
#
# MANTENIMIENTO: la recoleccion de basura NO puede correr con el registro
# sirviendo. Si alguien empuja una imagen mientras el recolector decide que un
# blob no lo usa nadie, ese blob se pierde y la imagen queda rota. Asi que el
# mantenimiento para el registro un rato. Para que eso no se confunda con una
# caida, deja una senal en $FLAG: mientras exista, este guion NO da por muerto al
# registro, espera a que el mantenimiento termine y lo vuelve a levantar.

REGISTRY_CONFIG="${REGISTRY_CONFIG:-/etc/docker/registry/config.yml}"
STATS_ENABLED="${STATS_ENABLED:-1}"
FLAG="${MAINTENANCE_FLAG:-/run/registry-maintenance.flag}"
TZ="${TZ:-UTC}"

case "$STATS_ENABLED" in
  0|1) ;;
  *)
    echo "[entrypoint] STATS_ENABLED sólo admite 0 o 1" >&2
    exit 64
    ;;
esac
case "$TZ" in
  /*|*..*|"")
    echo "[entrypoint] TZ no es un identificador de zona seguro" >&2
    exit 64
    ;;
esac
if [ ! -f "/usr/share/zoneinfo/$TZ" ]; then
  echo "[entrypoint] TZ no existe en la base horaria instalada: $TZ" >&2
  exit 64
fi

mkdir -p "$(dirname "$FLAG")"

rm -f "$FLAG"

if [ "$STATS_ENABLED" = "1" ]; then
  if ! python3 /app/server.py --check-config; then
    echo "[entrypoint] la configuración del panel no es válida" >&2
    exit 64
  fi
fi

vigilar_panel() {
  PANEL_PID=""
  # Invocada indirectamente por trap.
  # shellcheck disable=SC2329
  parar_panel() {
    [ -n "$PANEL_PID" ] && kill "$PANEL_PID" 2>/dev/null
    exit 0
  }
  trap parar_panel TERM INT
  while true; do
    python3 /app/server.py &
    PANEL_PID=$!
    wait "$PANEL_PID"
    CODE=$?
    PANEL_PID=""
    echo "[entrypoint] el panel ha salido (codigo $CODE); reintento en 10s" >&2
    sleep 10
  done
}

STATS_PID=""
if [ "$STATS_ENABLED" = "1" ]; then
  vigilar_panel &
  STATS_PID=$!
else
  echo "[entrypoint] panel desactivado (STATS_ENABLED=0); solo registro" >&2
fi

# Al parar el contenedor, Docker manda TERM a este guion. Sin reenviarlo, el
# registro moriria por el KILL de los 10 segundos en vez de cerrar limpiamente.
REG_PID=""
terminar() {
  SALIENDO=1
  [ -n "$STATS_PID" ] && kill "$STATS_PID" 2>/dev/null
  [ -n "$REG_PID" ] && kill "$REG_PID" 2>/dev/null
  [ -n "$REG_PID" ] && wait "$REG_PID" 2>/dev/null
  exit 0
}
SALIENDO=0
trap terminar TERM INT

while true; do
  # Distribution interpreta cualquier REGISTRY_* como una sobreescritura de su
  # YAML. Estas tres variables pertenecen al panel/entrypoint, no al registro;
  # se eliminan sólo del proceso hijo para evitar avisos y ambigüedad.
  env -u REGISTRY_CONFIG -u REGISTRY_URL -u REGISTRY_DATA \
    registry serve "$REGISTRY_CONFIG" &
  REG_PID=$!
  wait "$REG_PID"
  CODE=$?
  [ "$SALIENDO" = "1" ] && exit 0

  if [ -f "$FLAG" ]; then
    echo "[entrypoint] registro detenido para mantenimiento; espero a que acabe" >&2
    # Sin limite de tiempo a proposito: recolectar 12 GB puede tardar. Quien
    # borra la senal es el propio mantenimiento, tanto si acaba bien como mal.
    while [ -f "$FLAG" ]; do sleep 1; done
    echo "[entrypoint] mantenimiento terminado; levanto el registro otra vez" >&2
    continue
  fi

  echo "[entrypoint] el registro ha terminado (codigo $CODE): se detiene el contenedor" >&2
  [ -n "$STATS_PID" ] && kill "$STATS_PID" 2>/dev/null
  exit "$CODE"
done
