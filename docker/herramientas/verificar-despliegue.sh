#!/usr/bin/env bash
#
# verificar-despliegue.sh - Comprueba que el paquete está sano antes y después
# de arrancar en la PC de producción.
#
#   ./herramientas/verificar-despliegue.sh
#
# No modifica nada. Devuelve 0 si todas las comprobaciones obligatorias pasan.
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

ok()   { printf '\033[1;32m[OK]\033[0m   %s\n' "$*"; }
warn() { printf '\033[1;33m[AVISO]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[FALLO]\033[0m %s\n' "$*"; FAILED=1; }

FAILED=0

env_value() {
  sed -n "s/^$1=//p" .env.docker 2>/dev/null | tail -n1 | tr -d '\r' \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

[ -f docker-compose.yml ] || { fail "Falta docker-compose.yml"; exit 1; }
[ -f .env.docker ] || { fail "Falta .env.docker (copia .env.docker.example)"; exit 1; }
[ -f VERSION ] || fail "Falta VERSION"

if docker info >/dev/null 2>&1; then
  ok "Docker responde"
else
  fail "Docker no responde"
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  fail "Docker Compose v2 no está disponible"
  exit 1
fi

if "${COMPOSE[@]}" --env-file .env.docker config --quiet 2>/dev/null; then
  ok "docker-compose.yml y .env.docker son válidos"
else
  fail "La configuración de Compose no es válida"
fi

VERSION_ACTUAL="$(cat VERSION 2>/dev/null || echo '?')"
IMAGE_TAG="$(env_value ACC_IMAGE_TAG)"
IMAGE_TAG="${IMAGE_TAG:-$VERSION_ACTUAL}"
IMAGE="ansible-control-center:${IMAGE_TAG}"
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  IMAGE_VERSION="$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')"
  if [ "$IMAGE_VERSION" = "$VERSION_ACTUAL" ]; then
    ok "Imagen $IMAGE presente y con versión $IMAGE_VERSION"
  else
    fail "La imagen $IMAGE declara versión '$IMAGE_VERSION' y el paquete es $VERSION_ACTUAL"
  fi
else
  fail "No existe la imagen $IMAGE (ejecuta docker load)"
fi

ARCH_HOST="$(docker info --format '{{.Architecture}}' 2>/dev/null || echo '?')"
case "$ARCH_HOST" in
  x86_64)  ARCH_HOST="amd64" ;;
  aarch64) ARCH_HOST="arm64" ;;
esac
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  ARCH_IMAGE="$(docker image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || echo '?')"
  case "$ARCH_IMAGE" in
    *"/$ARCH_HOST") ok "Arquitectura compatible: $ARCH_IMAGE" ;;
    *) warn "Arquitectura de la imagen $ARCH_IMAGE y del host $ARCH_HOST; confirma que es intencionado" ;;
  esac
fi

# --- Configuración de producción -----------------------------------------
ACC_ENV="$(env_value ACC_ENV)"
ACC_ENV="${ACC_ENV:-production}"
if [ "$ACC_ENV" = "production" ]; then
  ok "ACC_ENV=production"
  [ -n "$(env_value ACC_SECRET_KEY)" ] \
    && ok "ACC_SECRET_KEY definida" \
    || fail "ACC_SECRET_KEY vacía: la aplicación no arrancará en producción"
  [ -n "$(env_value ACC_ALLOWED_NETWORKS)" ] \
    && ok "ACC_ALLOWED_NETWORKS=$(env_value ACC_ALLOWED_NETWORKS)" \
    || fail "ACC_ALLOWED_NETWORKS vacía: la aplicación no arrancará en producción"
  [ "$(env_value ACC_HSTS)" = "1" ] \
    && ok "HSTS activado" \
    || warn "ACC_HSTS=0: actívalo cuando el certificado sea de una CA de confianza"
else
  warn "ACC_ENV=$ACC_ENV: es un entorno de desarrollo/laboratorio, no producción"
  [ -n "$(env_value ACC_SECRET_KEY)" ] || warn "ACC_SECRET_KEY vacía; se generará una clave local"
fi

[ "$(env_value ACC_HOST_KEY_CHECKING)" = "0" ] \
  && fail "ACC_HOST_KEY_CHECKING=0 no está permitido en producción" \
  || ok "Verificación de claves SSH activa"

[ "$(env_value ACC_WINRM_PORT)" = "5985" ] \
  && warn "ACC_WINRM_PORT=5985 (HTTP). Usa 5986/HTTPS en producción" \
  || ok "WinRM sobre HTTPS (5986)"

[ "$(env_value ACC_DRY_RUN)" = "1" ] \
  && warn "ACC_DRY_RUN=1: la aplicación no aplicará cambios reales" \
  || ok "DRY_RUN desactivado"

if [ "$(env_value ACC_LDAP_ENABLED)" = "1" ]; then
  CA_FILE="$(env_value ACC_LDAP_TLS_CA_FILE)"
  if [ -n "$CA_FILE" ]; then
    CA_REL="${CA_FILE#/certs/}"
    if [ -f "certs-ca/$CA_REL" ] || [ -f "$CA_FILE" ]; then
      ok "CA LDAPS presente: $CA_FILE"
    else
      fail "ACC_LDAP_TLS_CA_FILE=$CA_FILE no existe en certs-ca/"
    fi
  fi
fi

# --- Integridad y estado de los contenedores ------------------------------
if [ -f sha256sums.txt ]; then
  # Los certificados (certs/ y certs-ca/) se sustituyen a propósito en la PC
  # destino, así que no forman parte de la comprobación de integridad.
  CHECK_FILE="$(mktemp "${TMPDIR:-/tmp}/acc-sha256.XXXXXX")"
  awk '$2 !~ /^\.\/(certs|certs-ca)\//' sha256sums.txt > "$CHECK_FILE"
  if sha256sum -c "$CHECK_FILE" >/dev/null 2>&1; then
    ok "Sumas SHA-256 del paquete correctas"
  else
    fail "sha256sum -c ha detectado archivos modificados o corruptos"
  fi
  rm -f "$CHECK_FILE"
fi

RUNNING="$("${COMPOSE[@]}" --env-file .env.docker ps --status running --services 2>/dev/null || true)"
for service in app nginx; do
  if printf '%s\n' "$RUNNING" | grep -qx "$service"; then
    CONTAINER="$("${COMPOSE[@]}" --env-file .env.docker ps -q "$service" 2>/dev/null | head -n1)"
    HEALTH="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}sin-healthcheck{{end}}' "$CONTAINER" 2>/dev/null || echo desconocido)"
    if [ "$HEALTH" = "healthy" ] || [ "$HEALTH" = "sin-healthcheck" ]; then
      ok "Servicio $service arrancado ($HEALTH)"
    else
      fail "Servicio $service en estado $HEALTH"
    fi
  else
    fail "Servicio $service no está arrancado"
  fi
done

HTTP_PORT="$(env_value ACC_HTTP_PORT)"; HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="$(env_value ACC_HTTPS_PORT)"; HTTPS_PORT="${HTTPS_PORT:-443}"

HEALTH_JSON=""
if command -v curl >/dev/null 2>&1; then
  HEALTH_JSON="$(curl -fk --max-time 5 "https://127.0.0.1:${HTTPS_PORT}/healthz" 2>/dev/null || true)"
fi
if [ -z "$HEALTH_JSON" ] && printf '%s\n' "$RUNNING" | grep -qx nginx; then
  HEALTH_JSON="$("${COMPOSE[@]}" --env-file .env.docker exec -T nginx \
    curl -fk --max-time 5 https://127.0.0.1/healthz 2>/dev/null || true)"
fi

case "$HEALTH_JSON" in
  *'"status":"ok"'*|*'"status": "ok"'*)
    ok "HTTPS /healthz responde ok (https://127.0.0.1:${HTTPS_PORT})"
    ;;
  *)
    fail "HTTPS /healthz no responde correctamente"
    ;;
esac

printf '\n'
if [ "$FAILED" -eq 0 ]; then
  printf '\033[1;32mDespliegue verificado.\033[0m Abre https://127.0.0.1:%s (o la IP real).\n' "$HTTPS_PORT"
  exit 0
fi
printf '\033[1;31mHay comprobaciones fallidas.\033[0m Revisa docker compose logs app nginx.\n'
exit 1
