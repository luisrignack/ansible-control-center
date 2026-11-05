#!/usr/bin/env bash
#
# export-images.sh - Empaqueta Ansible Control Center para desplegarlo en otra PC sin
# necesidad de registro de imágenes ni de acceso a Docker Hub.
#
#   * Construye (o reutiliza) las imágenes ansible-control-center y ansible-control-center-nginx.
#   * Las guarda con "docker save" junto con el compose, la configuración de
#     ejemplo, la guía de despliegue y las sumas de verificación.
#   * Deja una carpeta autocontenida lista para copiar a la PC de destino.
#
# Uso:
#   ./docker/export-images.sh                  # reconstruye desde HEAD
#   ./docker/export-images.sh --no-build       # usa las imágenes locales
#   ./docker/export-images.sh --output /ruta   # cambia la carpeta de salida
#
# Por defecto se reconstruyen SIEMPRE las imágenes desde el árbol de trabajo
# actual (HEAD). Así el paquete no puede contener una imagen anterior al
# código, como ocurría con el tar de 177 MB de versiones previas.
#
# La arquitectura de las imágenes debe coincidir con la de la PC de destino.
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

APP_IMAGE="ansible-control-center:latest"
NGINX_IMAGE="ansible-control-center-nginx:latest"
BUNDLE_NAME="hora-admin-despliegue"
BUILD_MODE="auto"
OUTPUT_DIR=""
GIT_COMMIT="$(git rev-parse --short=12 HEAD 2>/dev/null || printf 'desconocido')"
GIT_DIRTY="no"
if ! git diff --quiet --ignore-submodules HEAD 2>/dev/null; then
  GIT_DIRTY="sí (árbol de trabajo con cambios sin confirmar)"
fi

log()  { printf '\033[1;36m[ansible-control-center]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ansible-control-center]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[ansible-control-center]\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --build)    BUILD_MODE="force" ;;
    --no-build) BUILD_MODE="never" ;;
    --output)   shift; OUTPUT_DIR="${1:-}" ;;
    -h|--help)  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "Opción desconocida: $1 (usa --help)." ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# 1. Requisitos
# ---------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker no está instalado."
docker info >/dev/null 2>&1 || die "El daemon de Docker no responde."
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose v2 no está disponible."
fi

ENV_FILE_ARGS=()
if [ -f ".env.docker" ]; then
  ENV_FILE_ARGS=(--env-file .env.docker)
fi

# ---------------------------------------------------------------------------
# 2. Imágenes
# ---------------------------------------------------------------------------
have_images() {
  docker image inspect "$APP_IMAGE" >/dev/null 2>&1 \
    && docker image inspect "$NGINX_IMAGE" >/dev/null 2>&1
}

case "$BUILD_MODE" in
  force)
    log "Construyendo imágenes con --pull ..."
    "${COMPOSE[@]}" "${ENV_FILE_ARGS[@]}" build --pull
    ;;
  never)
    have_images || die "Faltan $APP_IMAGE o $NGINX_IMAGE (quita --no-build)."
    log "Usando las imágenes locales."
    ;;
  auto)
    log "Construyendo imágenes desde HEAD ($GIT_COMMIT) ..."
    "${COMPOSE[@]}" "${ENV_FILE_ARGS[@]}" build
    ;;
esac

# ---------------------------------------------------------------------------
# 3. Carpeta de salida
# ---------------------------------------------------------------------------
DEST="${OUTPUT_DIR:-$ROOT_DIR/dist/$BUNDLE_NAME}"
mkdir -p "$DEST/imagenes" "$DEST/docker/nginx" "$DEST/certs"

log "Exportando imágenes a $DEST/imagenes/ansible-control-center-images.tar ..."
docker save "$APP_IMAGE" "$NGINX_IMAGE" -o "$DEST/imagenes/ansible-control-center-images.tar"

log "Copiando archivos de despliegue ..."
cp -a docker-compose.yml Dockerfile .env.docker.example "$DEST/"
cp -a docker/entrypoint.sh "$DEST/docker/"
cp -a docker/nginx/Dockerfile docker/nginx/default.conf docker/nginx/self-signed.sh \
  "$DEST/docker/nginx/"
cp -a docker/despliegue-portable.md "$DEST/DESPLIEGUE.md"
: > "$DEST/certs/.gitkeep"

cat > "$DEST/LEEME.txt" <<'EOF'
Ansible Control Center - paquete de despliegue para otra PC
===============================================

Guía completa: DESPLIEGUE.md
Imágenes:      imagenes/ansible-control-center-images.tar

Pasos mínimos
-------------
1. Requisitos: Docker Engine 24+ y Docker Compose v2.

2. Verificar y cargar las imágenes:

     sha256sum -c sha256sums.txt
     docker load -i imagenes/ansible-control-center-images.tar

3. Crear la configuración:

     cp .env.docker.example .env.docker
     chmod 600 .env.docker
     openssl rand -hex 32        # pégalo en ACC_SECRET_KEY

   Define también ACC_ADMIN_PASSWORD (mínimo 12 caracteres) y ajusta
   TLS_CN/TLS_SAN al nombre o IP real de acceso.

   Con ACC_ENV=production (valor por defecto) debes rellenar
   ACC_ALLOWED_NETWORKS; si está vacía, la aplicación no arranca.
   Ejemplo: ACC_ALLOWED_NETWORKS=10.0.0.0/8,192.168.1.0/24

4. Arrancar (no reconstruye: no hace falta el código fuente):

     docker compose --env-file .env.docker up -d --no-build

5. Comprobar:

     docker compose ps
     curl -fk https://localhost/healthz

   Entra en https://localhost (o el nombre/IP configurado) y acepta el aviso
   del certificado autofirmado.

Notas
-----
- Mantén ACC_DRY_RUN=1 hasta validar el flujo en laboratorio. Después, un
  administrador puede activar o desactivar la simulación con el interruptor
  de la barra superior; el estado se guarda en SQLite.
- ACC_HSTS=0 es intencionado con certificado autofirmado.
- ACC_ENV=production exige ACC_ALLOWED_NETWORKS no vacía.
- El primer arranque genera ./certs y aplica las migraciones de SQLite.
- Acceso por LDAPS (opcional): configura las ACC_LDAP_* de .env.docker con
  la contraseña del bind en el volumen de secretos
  (acc_ldap_bind_password) y actívalo en la web de Usuarios con el botón
  "Activar acceso LDAPS" (solo el admin local). La tarjeta
  "Zona horaria" aplica America/Caracas (UTC-4) permanente en Linux y
  Windows y guarda copia del /etc/timezone anterior en el volumen de datos.
- No copies volúmenes de otra instalación salvo que quieras restaurar datos.
EOF

# ---------------------------------------------------------------------------
# 4. Metadatos e integridad
# ---------------------------------------------------------------------------
ARCH="$(docker image inspect "$APP_IMAGE" --format '{{.Os}}/{{.Architecture}}')"
APP_DIGEST="$(docker image inspect "$APP_IMAGE" --format '{{.Id}}')"
NGINX_DIGEST="$(docker image inspect "$NGINX_IMAGE" --format '{{.Id}}')"
SIZE="$(du -h "$DEST/imagenes/ansible-control-center-images.tar" | cut -f1)"

cat > "$DEST/imagenes/IMAGENES_INFO.txt" <<EOF
Ansible Control Center - imágenes incluidas
===============================

Empaquetado : $(date -u '+%Y-%m-%d %H:%M UTC')
Commit      : $GIT_COMMIT
Sin confirmar: $GIT_DIRTY
Arquitectura: $ARCH
Tamaño      : $SIZE

$APP_IMAGE
  id: $APP_DIGEST
  app Flask con Gunicorn, Ansible, sshpass y Tini (puerto interno 8000)

$NGINX_IMAGE
  id: $NGINX_DIGEST
  Nginx 1.27 con TLS y generación de certificado autofirmado

Cargar con:
  docker load -i imagenes/ansible-control-center-images.tar

Las imágenes deben coincidir con la arquitectura de la PC de destino:
  docker info --format '{{.Architecture}}'
EOF

( cd "$DEST" && find . -type f ! -name sha256sums.txt -print0 \
    | sort -z | xargs -0 sha256sum > sha256sums.txt )

# ---------------------------------------------------------------------------
# 5. Resumen
# ---------------------------------------------------------------------------
log "Paquete listo: $DEST"
cat <<EOF

  Arquitectura : $ARCH
  Imagen .tar  : $SIZE
  Punto de entrada: $DEST/LEEME.txt
  Guía completa   : $DEST/DESPLIEGUE.md

  Copia toda la carpeta a la otra PC y sigue el LEEME.txt. Allí no hace falta
  ni el código fuente ni acceso a Docker Hub.

EOF
