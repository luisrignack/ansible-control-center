#!/usr/bin/env bash
#
# respaldo-datos.sh - Exporta los datos de Ansible Control Center para
# llevarlos a otra PC o conservar una copia antes de actualizar.
#
# Debe ejecutarse desde la carpeta del paquete (junto a docker-compose.yml y
# .env.docker), no desde el repositorio de desarrollo.
#
#   ./herramientas/respaldo-datos.sh
#
# Genera en respaldo/:
#   datos-AAAA-MM-DD-HHMMSS.tgz          volúmenes data, secrets y ssh
#   configuracion-AAAA-MM-DD-HHMMSS.tgz  .env.docker, certs y VERSION
#   MANIFIESTO-AAAA-MM-DD-HHMMSS.txt      versión, imagen, fecha y origen
#
# El contenedor app se detiene durante la copia para no capturar un WAL de
# SQLite a medias; si estaba arrancado, se vuelve a arrancar al terminar.
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

log()  { printf '\033[1;36m[respaldo]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[respaldo]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[respaldo]\033[0m %s\n' "$*" >&2; exit 1; }

[ -f docker-compose.yml ] || die "No encuentro docker-compose.yml. Ejecuta este script dentro del paquete de despliegue."
[ -f .env.docker ] || die "No existe .env.docker. Créalo a partir de .env.docker.example antes de respaldar."
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
VERSION_ACTUAL="$(cat VERSION)"
STAMP="$(date +%F-%H%M%S)"
DEST="$BUNDLE_DIR/respaldo/$STAMP"
mkdir -p "$DEST"
chmod 700 "$BUNDLE_DIR/respaldo" "$DEST"

docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || die "No existe la imagen $IMAGE. Carga el paquete con: docker load -i imagenes/ansible-control-center-images.tar"

APP_CONTAINER="$("${COMPOSE[@]}" --env-file .env.docker ps -q app 2>/dev/null || true)"
WAS_RUNNING="false"
if [ -n "$APP_CONTAINER" ]; then
  WAS_RUNNING="$(docker inspect -f '{{.State.Running}}' "$APP_CONTAINER" 2>/dev/null || echo false)"
fi

restore_app() {
  if [ "$WAS_RUNNING" = "true" ]; then
    log "Volviendo a arrancar la aplicación ..."
    "${COMPOSE[@]}" --env-file .env.docker start app >/dev/null 2>&1 || true
  fi
}
trap restore_app EXIT

if [ "$WAS_RUNNING" = "true" ]; then
  log "Deteniendo app para copiar SQLite de forma consistente ..."
  "${COMPOSE[@]}" --env-file .env.docker stop app >/dev/null
fi

VOLUMES=()
for volume in hora-admin_acc_data hora-admin_acc_secrets hora-admin_acc_ssh; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    case "$volume" in
      hora-admin_acc_data)    target="data" ;;
      hora-admin_acc_secrets) target="secrets" ;;
      hora-admin_acc_ssh)     target="ssh" ;;
    esac
    VOLUMES+=(-v "$volume:/volumes/$target:ro")
  else
    warn "El volumen $volume no existe; se omite."
  fi
done

if [ ${#VOLUMES[@]} -eq 0 ]; then
  die "No hay volúmenes que respaldar. ¿Se ha arrancado alguna vez la aplicación?"
fi

log "Empaquetando volúmenes ..."
docker run --rm --user 0:0 \
  "${VOLUMES[@]}" \
  -v "$DEST:/backup" \
  --entrypoint sh "$IMAGE" -c \
  "tar czf /backup/datos.tgz -C /volumes . \
   && chown $(id -u):$(id -g) /backup/datos.tgz"

log "Empaquetando configuración y certificados ..."
# Se hace dentro del contenedor porque certs/privkey.pem suele ser root:root
# 0600 y el usuario que lanza el respaldo no puede leerlo. El chown final
# devuelve el archivo al usuario actual para poder aplicar 0600.
CONFIG_ITEMS=".env.docker VERSION certs"
[ -d "$BUNDLE_DIR/certs-ca" ] && CONFIG_ITEMS="$CONFIG_ITEMS certs-ca"
docker run --rm --user 0:0 \
  -v "$BUNDLE_DIR:/bundle:ro" \
  -v "$DEST:/backup" \
  --entrypoint sh "$IMAGE" -c \
  "tar czf /backup/configuracion.tgz -C /bundle $CONFIG_ITEMS \
   && chown $(id -u):$(id -g) /backup/configuracion.tgz"

cat > "$DEST/MANIFIESTO.txt" <<EOF
Respaldo de Ansible Control Center
==================================

Fecha (UTC)     : $(date -u '+%Y-%m-%d %H:%M:%S')
Equipo origen   : $(hostname)
Versión         : $VERSION_ACTUAL
Imagen          : $IMAGE
Commit          : $(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' 2>/dev/null || echo desconocido)
Volúmenes       : ${VOLUMES[*]:-ninguno}

Contenido
---------
datos.tgz          data/ (SQLite, pepper, copias de zona horaria)
                   secrets/ (vault cifrado y secretos LDAPS)
                   ssh/ (known_hosts)
configuracion.tgz  .env.docker, VERSION, certs/ y certs-ca/

Restaurar en la PC destino
--------------------------
  1. Copia esta carpeta respaldo/$STAMP junto con el paquete de despliegue.
  2. Carga las imágenes y prepara .env.docker del paquete nuevo.
  3. Ejecuta:
       ./herramientas/restaurar-datos.sh respaldo/$STAMP/datos.tgz
  4. Restaura la configuración con:
       tar xzf respaldo/$STAMP/configuracion.tgz -C .
       chmod 600 .env.docker
  5. Arranca y verifica:
       ./herramientas/verificar-despliegue.sh

Seguridad
---------
Estos archivos contienen secretos (vault, pepper, contraseña de bind LDAPS).
Guárdalos cifrados, con permisos 0600, y no los subas a Git ni a un recurso
compartido sin cifrar.
EOF

chmod 600 "$DEST"/*.tgz "$DEST/MANIFIESTO.txt"

log "Respaldo listo en: $DEST"
ls -lh "$DEST"
