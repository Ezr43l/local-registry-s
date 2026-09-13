#!/bin/sh
set -eu

case "${0##*/}" in
  curl)
    # El caso por defecto fuerza rollback; una marca permite probar el commit.
    [ -f /remote/curl-success ] && exit 0
    exit 1
    ;;
  sleep)
    # Mantiene el test rápido sin cambiar los 30 intentos del código real.
    exit 0
    ;;
  rm)
    if [ -f /remote/fail-cleanup ]; then
      printf '%s\n' "cleanup-failure $*" >> /remote/docker.log
      exit 1
    fi
    exec /bin/rm "$@"
    ;;
  docker)
    ;;
  *)
    exit 2
    ;;
esac

mkdir -p /remote/containers
printf '%s\n' "$*" >> /remote/docker.log
command_name="${1:-}"
shift || true

case "$command_name" in
  info)
    ;;
  inspect)
    name="${1:-}"
    test -f "/remote/containers/$name"
    shift || true
    if [ "${1:-}" = "--format" ]; then
      format="${2:-}"
      case "$format" in
        *'.State.Running}}') printf '%s\n' true ;;
        *'.Config.Image}}') printf '%s\n' 'local-registry:v1.2.15' ;;
        *'.HostConfig.RestartPolicy.Name}}') printf '%s\n' 'unless-stopped' ;;
        *'.HostConfig.ReadonlyRootfs}}|'*) printf '%s\n' 'true|false|256|true' ;;
        *'json .HostConfig.CapDrop'*) printf '%s\n' '["ALL"]' ;;
        *'json .HostConfig.SecurityOpt'*) printf '%s\n' '["no-new-privileges"]' ;;
        *'index .HostConfig.Tmpfs "/run"'*)
          printf '%s\n' 'rw,nosuid,noexec,size=16m,mode=0755|rw,nosuid,noexec,size=32m,mode=1777'
          ;;
        *'PortBindings "5000/tcp"'*) printf '%s\n' '5000' ;;
        *'PortBindings "5001/tcp"'*) printf '%s\n' '5001' ;;
        *'range .Config.Env'*)
          printf '%s\n' \
            'MAINTENANCE_ADMIN_TOKEN_FILE=/run/secrets/local-registry/admin-token' \
            'MAINTENANCE_CLUSTER_TOKEN_FILE=' \
            'APP_VERSION=v1.2.15' \
            'NODE_NAME=node-a' \
            'PEERS=' \
            'PANEL_PEERS=' \
            'CACHE_TTL=300' \
            'STATS_ENABLED=1' \
            'MAINTENANCE_ENABLED=1' \
            'KEEP_LAST=0' \
            'PROTECTED_TAGS=latest' \
            'MAINTENANCE_HOUR=' \
            'MAINTENANCE_GC=1' \
            'MAINTENANCE_COORDINATOR=node-a' \
            'MAINTENANCE_LEASE_TTL=7200' \
            'RETENTION_REQUIRE_ALL_NODES=1' \
            'RETENTION_BRANCH_PATTERN=' \
            'SYNC_SOURCE=' \
            'CORS_ALLOWED_ORIGINS=' \
            'TZ=UTC'
          ;;
        *'range .HostConfig.Binds'*)
          printf '%s\n' \
            '/remote/data:/var/lib/registry' \
            '/remote/secrets:/run/secrets/local-registry:ro'
          ;;
      esac
    fi
    ;;
  rename)
    old_name="${1:-}"
    new_name="${2:-}"
    test -f "/remote/containers/$old_name"
    test ! -e "/remote/containers/$new_name"
    mv "/remote/containers/$old_name" "/remote/containers/$new_name"
    ;;
  stop|start)
    test -f "/remote/containers/${1:-}"
    ;;
  rm)
    if [ "${1:-}" = "-f" ]; then
      shift
    fi
    /bin/rm -f "/remote/containers/${1:-}"
    ;;
  run)
    new_name=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--name" ]; then
        new_name="${2:-}"
        break
      fi
      shift
    done
    test -n "$new_name"
    test ! -e "/remote/containers/$new_name"
    : > "/remote/containers/$new_name"
    printf '%s\n' fake-container-id
    ;;
  *)
    echo "Comando Docker remoto inesperado: $command_name" >&2
    exit 2
    ;;
esac
