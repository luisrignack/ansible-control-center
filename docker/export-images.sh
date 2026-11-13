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

# La carpeta queda en orden de lectura (01 → 04). Si ya existía un paquete
# anterior con los nombres sin numerar, se retiran para no confundir.
rm -f "$DEST/LEEME.txt" "$DEST/DESPLIEGUE.md" \
      "$DEST/imagenes/IMAGENES_INFO.txt"

cat > "$DEST/01-INICIO-RAPIDO.txt" <<'EOF'
Ansible Control Center — despliegue en otra PC (arranque rápido)
================================================================

ORDEN DE LECTURA DE ESTE PAQUETE
  01-INICIO-RAPIDO.txt        este archivo: los 5 pasos mínimos
  02-GUIA-COMPLETA-DESPLIEGUE.md   guía completa (configuración, TLS,
                              SSH/WinRM, LDAPS, criticidad y ventanas,
                              volúmenes, actualización, diagnóstico y
                              seguridad)
  imagenes/03-INFORMACION-IMAGENES.txt  arquitectura, digests y fecha
                              de empaquetado de las imágenes

CONTENIDO DE LA CARPETA
  imagenes/ansible-control-center-images.tar  las imágenes listas para
                              cargar (aplicación + Nginx, sin código)
  docker-compose.yml          estructura del stack (app + Nginx)
  .env.docker.example         plantilla de configuración
  Dockerfile                  referencia de la imagen; con el tar de
                              imágenes no hace falta (reconstruir requiere
                              el código fuente y Docker Hub)
  certs/                      vacío: el certificado se genera al arrancar
  sha256sums.txt              para verificar la integridad del paquete

ARRANQUE EN 5 PASOS (detalles y comprobaciones: guía 02, §3)

  1) Verificar el paquete y cargar las imágenes:
       sha256sum -c sha256sums.txt
       docker load -i imagenes/ansible-control-center-images.tar

  2) Crear la configuración:
       cp .env.docker.example .env.docker && chmod 600 .env.docker
       openssl rand -hex 32        # pégalo en ACC_SECRET_KEY

  3) Editar .env.docker. Mínimo obligatorio (tabla completa: guía 02, §4):
       ACC_SECRET_KEY=<paso 2>
       ACC_ADMIN_PASSWORD=/contraseña/mínimo/12/
       ACC_ALLOWED_NETWORKS=10.0.0.0/8,192.168.1.0/24   # redes a administrar
       TLS_SAN=DNS:servidor.local,IP:10.0.0.5           # cómo se accederá

  4) Arrancar (no es necesario código fuente ni internet):
       docker compose --env-file .env.docker up -d --no-build

  5) Comprobar y entrar:
       curl -fk https://localhost/healthz
       https://localhost  (acepta el aviso del certificado autofirmado)
       Usuario: ACC_ADMIN_USERNAME / ACC_ADMIN_PASSWORD

DESPUÉS DEL ARRANQUE
  - La tarjeta «Zona horaria» aplica America/Caracas (UTC-4) permanente en
    Linux y Windows; guarda copia del /etc/timezone anterior en el volumen
    de datos.
  - La tarjeta «Tapiz oficial» permite subir una imagen (PNG/JPEG/WebP/GIF/
    BMP, máximo 2 MB) y la aplica como fondo en estaciones Linux con GNOME.
    El fondo se publica como valor predeterminado del sistema: sobrevive al
    reinicio y se aplica a los usuarios que inicien sesión. La tarjeta
    incluye un informe PDF con estación, fecha/hora y causa del error.
  - Estaciones Windows: requieren WinRM (5985/tcp con NTLM o 5986/tcp con
    TLS) y una cuenta de administrador local (guía 02, §6).
  - Inventario: asigna criticidad y ventana de mantenimiento en UTC. Los
    hosts «critical» se bloquean fuera de ventana; la vista previa y el
    DRY RUN no se bloquean (guía 02, §10).
  - La tarjeta «Actualización del sistema» está en preparación. Mientras
    tanto, usa el procedimiento apt-get de la guía 02, §10.
  - Acceso corporativo LDAPS (opcional): pasos en la guía 02, §4.1.
  - Usuarios: en /usuarios (admin) puedes crear cuentas locales con su
    contraseña y rol, cambiar contraseñas o eliminar la cuenta. Las cuentas
    LDAPS se validan en el directorio y solo se activan/desactivan aquí
    (guía 02, §4).
  - El modo Simulación cambia desde la barra superior de la interfaz.
  - Vistas: /inventario, /programaciones, /aprobaciones, /usuarios,
    /history, /audit y /metricas (admin).
EOF

cp -a docker-compose.yml Dockerfile .env.docker.example "$DEST/"
cp -a docker/entrypoint.sh "$DEST/docker/"
cp -a docker/nginx/Dockerfile docker/nginx/default.conf docker/nginx/self-signed.sh \
  "$DEST/docker/nginx/"
# Guía completa, numerada como 02 en el orden de lectura.
cp -a docker/despliegue-portable.md "$DEST/02-GUIA-COMPLETA-DESPLIEGUE.md"
: > "$DEST/certs/.gitkeep"

# ---------------------------------------------------------------------------
# 4. Metadatos e integridad
# ---------------------------------------------------------------------------
ARCH="$(docker image inspect "$APP_IMAGE" --format '{{.Os}}/{{.Architecture}}')"
APP_DIGEST="$(docker image inspect "$APP_IMAGE" --format '{{.Id}}')"
NGINX_DIGEST="$(docker image inspect "$NGINX_IMAGE" --format '{{.Id}}')"
SIZE="$(du -h "$DEST/imagenes/ansible-control-center-images.tar" | cut -f1)"

cat > "$DEST/imagenes/03-INFORMACION-IMAGENES.txt" <<EOF
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
  Punto de entrada: $DEST/01-INICIO-RAPIDO.txt
  Guía completa   : $DEST/02-GUIA-COMPLETA-DESPLIEGUE.md

  Copia toda la carpeta a la otra PC y empieza por 01-INICIO-RAPIDO.txt. Allí
  no hace falta ni el código fuente ni acceso a Docker Hub.

EOF
