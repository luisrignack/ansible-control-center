#!/usr/bin/env bash
#
# restaurar-datos.sh - Restaura en esta PC los datos exportados con
# herramientas/respaldo-datos.sh.
#
#   ./herramientas/restaurar-datos.sh respaldo/2026-11-16-120000/datos.tgz
#
# Restaura los volúmenes data, secrets y ssh. La configuración (.env.docker y
# certs/) se restaura aparte con las instrucciones de la guía 02, §7.
#
# Opciones:
#   --yes       no pide confirmación
#   --limpiar   vacía los volúmenes antes de restaurar (destructivo)
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
# Permite probar la herramienta desde el repositorio (docker/herramientas/)
# o desde el paquete de despliegue (herramientas/).
if [ ! -f "$BUNDLE_DIR/docker-compose.yml" ] \
   && [ -f "$SCRIPT_DIR/../../docker-compose.yml" ]; then
  BUNDLE_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
fi
cd "$BUNDLE_DIR"

log()  { printf '\033[1;36m[restaurar]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[restaurar]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[restaurar]\033[0m %s\n' "$*" >&2; exit 1; }

ASSUME_YES="no"
LIMPIAR="no"
ARCHIVE=""
for arg in "$@"; do
  case "$arg" in
    --yes)     ASSUME_YES="si" ;;
    --limpiar) LIMPIAR="si" ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -* )       die "Opción desconocida: $arg" ;;
    *)         ARCHIVE="$arg" ;;
  esac
done

[ -n "$ARCHIVE" ] || die "Indica el archivo datos.tgz. Uso: $0 respaldo/FECHA/datos.tgz"
[ -f "$ARCHIVE" ] || die "No existe el archivo $ARCHIVE"
ARCHIVE="$(cd -- "$(dirname -- "$ARCHIVE")" && pwd)/$(basename -- "$ARCHIVE")"
[ -f .env.docker ] || die "No existe .env.docker. Crea la configuración del paquete nuevo antes de restaurar."
docker info >/dev/null 2>&1 || die "El daemon de Docker no responde."

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose v2 no está disponible."
fi

IMAGE_TAG="$(sed -n 's/^ACC_IMAGE_TAG=//p' .env.docker | tail -n1)"
IMAGE_TAG="${IMAGE_TAG:-$(cat VERSION)}"
IMAGE="ansible-control-center:${IMAGE_TAG}"
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || die "No existe la imagen $IMAGE. Carga el paquete con: docker load -i imagenes/ansible-control-center-images.tar"

log "Este proceso SUSTITUYE el contenido de los volúmenes data, secrets y ssh."
if [ "$LIMPIAR" = "si" ]; then
  warn "Modo --limpiar: se borrarán los volúmenes actuales antes de restaurar."
fi
if [ "$ASSUME_YES" != "si" ]; then
  printf 'Escribe RESTAURAR para continuar: '
  read -r respuesta
  [ "$respuesta" = "RESTAURAR" ] || die "Cancelado."
fi

APP_CONTAINER="$("${COMPOSE[@]}" --env-file .env.docker ps -q app 2>/dev/null || true)"
if [ -n "$APP_CONTAINER" ]; then
  log "Deteniendo app ..."
  "${COMPOSE[@]}" --env-file .env.docker stop app >/dev/null
fi

if [ "$LIMPIAR" = "si" ]; then
  log "Eliminando contenedores (los volúmenes se conservan hasta el paso siguiente) ..."
  "${COMPOSE[@]}" --env-file .env.docker down >/dev/null
  for volume in hora-admin_acc_data hora-admin_acc_secrets hora-admin_acc_ssh; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      log "Eliminando volumen $volume ..."
      docker volume rm "$volume" >/dev/null
    fi
  done
fi

VOLUMES=(
  -v hora-admin_acc_data:/volumes/data
  -v hora-admin_acc_secrets:/volumes/secrets
  -v hora-admin_acc_ssh:/volumes/ssh
)

log "Restaurando $(basename "$ARCHIVE") ..."
docker run --rm --user 0:0 \
  "${VOLUMES[@]}" \
  -v "$(dirname "$ARCHIVE"):/backup:ro" \
  --entrypoint tar "$IMAGE" \
  xzf "/backup/$(basename "$ARCHIVE")" -C /volumes

# El tar conserva el UID/GID original (10001:10001). Si el archivo se generó
# con otro UID, se corrige para que la app pueda escribir.
docker run --rm --user 0:0 \
  "${VOLUMES[@]}" \
  --entrypoint sh "$IMAGE" \
  -c 'chown -R 10001:10001 /volumes/data /volumes/secrets /volumes/ssh && chmod 700 /volumes/secrets /volumes/ssh'

log "Arrancando la aplicación ..."
"${COMPOSE[@]}" --env-file .env.docker up -d --no-build >/dev/null

log "Restauración completada. Verifica el despliegue:"
printf '  ./herramientas/verificar-despliegue.sh\n\n'
warn "Recuerda restaurar también .env.docker y certs/ desde configuracion.tgz:"
printf '  tar xzf %s -C .\n  chmod 600 .env.docker\n' \
  "$(dirname "$ARCHIVE")/configuracion.tgz"
