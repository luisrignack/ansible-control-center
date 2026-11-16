#!/usr/bin/env bash
#
# export-images.sh - Empaqueta Ansible Control Center para desplegarlo en otra PC sin
# necesidad de registro de imágenes ni de acceso a Docker Hub.
#
#   * Construye (o reutiliza) las imágenes ansible-control-center y ansible-control-center-nginx.
#   * Las etiqueta con la versión de VERSION, su minor, latest y sha-<commit>.
#   * Las guarda con "docker save" junto con el compose, la configuración de
#     ejemplo, la guía de despliegue y las sumas de verificación.
#   * Deja una carpeta autocontenida lista para copiar a la PC de destino.
#
# Uso:
#   ./docker/export-images.sh                  # reconstruye desde HEAD
#   ./docker/export-images.sh --no-build       # usa las imágenes locales
#   ./docker/export-images.sh --output /ruta   # cambia la carpeta de salida
#   ./docker/export-images.sh --allow-dirty    # permite empaquetar cambios
#                                              # sin confirmar (no recomendado)
#
# Por defecto se reconstruyen SIEMPRE las imágenes desde el árbol de trabajo
# actual (HEAD). Así el paquete no puede contener una imagen anterior al
# código, como ocurría con el tar de 177 MB de versiones previas. Si el árbol
# tiene cambios sin confirmar, el script se detiene salvo con --allow-dirty.
#
# La arquitectura de las imágenes debe coincidir con la de la PC de destino.
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

APP_IMAGE_NAME="ansible-control-center"
NGINX_IMAGE_NAME="ansible-control-center-nginx"
BUNDLE_NAME="hora-admin-despliegue"
BUILD_MODE="auto"
OUTPUT_DIR=""
ALLOW_DIRTY="no"
GIT_COMMIT="$(git rev-parse --short=12 HEAD 2>/dev/null || printf 'desconocido')"
GIT_DIRTY="no"
if ! git diff --quiet --ignore-submodules HEAD 2>/dev/null; then
  GIT_DIRTY="sí (árbol de trabajo con cambios sin confirmar)"
fi

log()  { printf '\033[1;36m[ansible-control-center]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ansible-control-center]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[ansible-control-center]\033[0m %s\n' "$*" >&2; exit 1; }

# La versión sale del archivo VERSION: es la misma que muestra la aplicación
# y la que se usa para el tag de Git y para los tags de las imágenes.
VERSION="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION" 2>/dev/null || true)"
[ -n "$VERSION" ] || die "Falta VERSION en $ROOT_DIR o está vacío."
MAJOR_MINOR="${VERSION%.*}"
APP_IMAGE="$APP_IMAGE_NAME:$VERSION"
NGINX_IMAGE="$NGINX_IMAGE_NAME:$VERSION"

while [ $# -gt 0 ]; do
  case "$1" in
    --build)    BUILD_MODE="force" ;;
    --no-build) BUILD_MODE="never" ;;
    --output)   shift; OUTPUT_DIR="${1:-}" ;;
    --allow-dirty) ALLOW_DIRTY="si" ;;
    -h|--help)  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

if [ "$GIT_DIRTY" != "no" ] && [ "$ALLOW_DIRTY" != "si" ]; then
  die "El árbol de trabajo tiene cambios sin confirmar. Confirma o descarta los cambios, o usa --allow-dirty para empaquetar una versión no reproducible."
fi

# ---------------------------------------------------------------------------
# 2. Imágenes
# ---------------------------------------------------------------------------
have_images() {
  docker image inspect "$APP_IMAGE" >/dev/null 2>&1 \
    && docker image inspect "$NGINX_IMAGE" >/dev/null 2>&1
}

# Los argumentos de build alimentan las labels OCI de las imágenes. Compose
# también usa ACC_IMAGE_TAG para etiquetar directamente la versión exacta.
export APP_VERSION="$VERSION"
export APP_COMMIT="$GIT_COMMIT"
export ACC_IMAGE_TAG="$VERSION"

case "$BUILD_MODE" in
  force)
    log "Construyendo imágenes $VERSION con --pull ..."
    "${COMPOSE[@]}" "${ENV_FILE_ARGS[@]}" build --pull
    ;;
  never)
    if ! have_images; then
      if docker image inspect "$APP_IMAGE_NAME:latest" >/dev/null 2>&1 \
        && docker image inspect "$NGINX_IMAGE_NAME:latest" >/dev/null 2>&1; then
        warn "No existen las imágenes $VERSION; se etiquetan desde latest."
        docker tag "$APP_IMAGE_NAME:latest" "$APP_IMAGE"
        docker tag "$NGINX_IMAGE_NAME:latest" "$NGINX_IMAGE"
      else
        die "Faltan $APP_IMAGE o $NGINX_IMAGE (quita --no-build)."
      fi
    fi
    log "Usando las imágenes locales $VERSION."
    ;;
  auto)
    log "Construyendo imágenes $VERSION desde HEAD ($GIT_COMMIT) ..."
    "${COMPOSE[@]}" "${ENV_FILE_ARGS[@]}" build
    ;;
esac

# Tags reproducibles: la versión exacta es el tag que usa el paquete; los
# demás son alias de consulta. ``sha-<commit>`` permite rastrear el build.
tag_release() {
  local name="$1"
  docker tag "$name:$VERSION" "$name:$MAJOR_MINOR"
  docker tag "$name:$VERSION" "$name:latest"
  if [ "$GIT_COMMIT" != "desconocido" ]; then
    docker tag "$name:$VERSION" "$name:sha-$GIT_COMMIT"
  fi
}
tag_release "$APP_IMAGE_NAME"
tag_release "$NGINX_IMAGE_NAME"

# El paquete de producción no puede contener una imagen de otra versión o de
# otro commit. Sin esta comprobación es fácil exportar una imagen antigua con
# el tag nuevo (fue el fallo del paquete 0.9.6).
IMAGE_VERSION="$(docker image inspect "$APP_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')"
IMAGE_REVISION="$(docker image inspect "$APP_IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' 2>/dev/null || true)"
[ "$IMAGE_VERSION" = "$VERSION" ] \
  || die "La imagen $APP_IMAGE declara versión '$IMAGE_VERSION' y VERSION es '$VERSION'. Reconstruye con --build."
if [ "$GIT_COMMIT" != "desconocido" ] && [ -n "$IMAGE_REVISION" ]; then
  case "$IMAGE_REVISION" in
    "$GIT_COMMIT"*) : ;;
    *) die "La imagen $APP_IMAGE se construyó desde '$IMAGE_REVISION' y HEAD es '$GIT_COMMIT'. Reconstruye con --build." ;;
  esac
fi
log "Imágenes verificadas: versión $IMAGE_VERSION, commit ${IMAGE_REVISION:-desconocido}."

# ---------------------------------------------------------------------------
# 3. Carpeta de salida
# ---------------------------------------------------------------------------
DEST="${OUTPUT_DIR:-$ROOT_DIR/dist/$BUNDLE_NAME}"

# Limpia artefactos de una exportación anterior para no mezclar versiones,
# CA antiguas o certificados del equipo de origen. Se conserva respaldo/ si el
# operador lo dejó dentro de la carpeta, pero certs/ se vacía: el certificado
# debe generarse en la PC de destino.
rm -rf "$DEST/imagenes" "$DEST/docker" "$DEST/herramientas" "$DEST/certs-ca"
rm -f "$DEST"/01-* "$DEST"/02-* "$DEST"/03-* "$DEST"/04-* \
      "$DEST/LEEME.txt" "$DEST/DESPLIEGUE.md" \
      "$DEST/docker-compose.yml" "$DEST/Dockerfile" \
      "$DEST/.env.docker.example" "$DEST/VERSION" "$DEST/sha256sums.txt"
rm -f "$DEST/certs/"*.pem

mkdir -p "$DEST/imagenes" "$DEST/docker/nginx" "$DEST/herramientas" \
  "$DEST/certs" "$DEST/certs-ca"

log "Exportando imágenes $VERSION a $DEST/imagenes/ansible-control-center-images.tar ..."
SAVE_REFS=(
  "$APP_IMAGE" "$APP_IMAGE_NAME:$MAJOR_MINOR" "$APP_IMAGE_NAME:latest"
  "$NGINX_IMAGE" "$NGINX_IMAGE_NAME:$MAJOR_MINOR" "$NGINX_IMAGE_NAME:latest"
)
if [ "$GIT_COMMIT" != "desconocido" ]; then
  SAVE_REFS+=(
    "$APP_IMAGE_NAME:sha-$GIT_COMMIT"
    "$NGINX_IMAGE_NAME:sha-$GIT_COMMIT"
  )
fi
docker save "${SAVE_REFS[@]}" -o "$DEST/imagenes/ansible-control-center-images.tar"

log "Copiando archivos de despliegue ..."

# La carpeta queda en orden de lectura (01 → 04). Si ya existía un paquete
# anterior con los nombres sin numerar, se retiran para no confundir.
rm -f "$DEST/LEEME.txt" "$DEST/DESPLIEGUE.md" \
      "$DEST/imagenes/IMAGENES_INFO.txt"

cat > "$DEST/01-INICIO-RAPIDO.txt" <<'EOF'
Ansible Control Center — despliegue en otra PC (arranque rápido)
================================================================

ORDEN DE LECTURA DE ESTE PAQUETE
  01-INICIO-RAPIDO.txt             este archivo: los pasos mínimos
  02-GUIA-COMPLETA-DESPLIEGUE.md   guía completa (configuración, TLS,
                                   SSH/WinRM, LDAPS, datos, diagnóstico)
  04-CHECKLIST-PRODUCCION.md       lista de comprobación para producción
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
  certs-ca/                   CA pública del directorio para LDAPS
  herramientas/               respaldo, restauración y verificación
  sha256sums.txt              para verificar la integridad del paquete

ARRANQUE EN 5 PASOS (detalles y comprobaciones: guía 02, §3)

  1) Verificar el paquete y cargar las imágenes:
       sha256sum -c sha256sums.txt
       docker load -i imagenes/ansible-control-center-images.tar

  2) Crear la configuración:
       cp .env.docker.example .env.docker && chmod 600 .env.docker
       openssl rand -hex 32        # pégalo en ACC_SECRET_KEY

  3) Editar .env.docker. Mínimo obligatorio (tabla completa: guía 02, §4):
       ACC_ENV=production
       ACC_SECRET_KEY=<paso 2>
       ACC_ADMIN_PASSWORD=<mínimo 12 caracteres>
       ACC_ALLOWED_NETWORKS=10.0.0.0/8,192.168.1.0/24   # redes a administrar
       TLS_SAN=DNS:servidor.local,IP=10.0.0.5           # cómo se accederá

  4) Arrancar (no es necesario código fuente ni internet):
       docker compose --env-file .env.docker up -d --no-build

  5) Comprobar y entrar:
       ./herramientas/verificar-despliegue.sh
       https://localhost  (acepta el aviso del certificado autofirmado)
       Usuario: ACC_ADMIN_USERNAME / ACC_ADMIN_PASSWORD

SI VIENES DE OTRA PC CON DATOS
  En el equipo de origen, dentro del paquete antiguo:
       ./herramientas/respaldo-datos.sh
  Copia la carpeta respaldo/ junto a este paquete y, en el destino:
       tar xzf respaldo/<fecha>/configuracion.tgz -C .
       chmod 600 .env.docker
       ./herramientas/restaurar-datos.sh respaldo/<fecha>/datos.tgz
  Detalles y advertencias: guía 02, §7.

DESPUÉS DEL ARRANQUE
  - La tarjeta «Zona horaria» aplica America/Caracas (UTC-4) permanente en
    Linux y Windows; guarda copia del /etc/timezone anterior en el volumen
    de datos.
  - La tarjeta «Tapiz oficial» permite subir una imagen (PNG/JPEG/WebP/GIF/
    BMP, máximo 2 MB) y la aplica como fondo en estaciones Linux con GNOME.
    El fondo se publica como valor predeterminado del sistema: sobrevive al
    reinicio y se aplica a los usuarios que inicien sesión. La tarjeta
    incluye un informe PDF con estación, fecha/hora y causa del error.
  - Estaciones Windows: requieren WinRM sobre HTTPS (5986/tcp; el 5985/tcp
    con NTLM queda para un laboratorio aislado) y una cuenta de
    administrador local (guía 02, §6).
  - Inventario: asigna criticidad y ventana de mantenimiento en UTC. Los
    hosts «critical» se bloquean fuera de ventana; la vista previa y el
    DRY RUN no se bloquean (guía 02, §10).
  - La tarjeta «Actualización del sistema» aplica una actualización segura
    (apt-get upgrade) sin eliminar paquetes ni reiniciar, omite Ubuntu 20.04
    y derivados y avisa si la estación requiere reinicio.
  - La tarjeta «Contraseña administrativa» cambia la contraseña de la cuenta
    de destino sin guardarla en claro (guía 02, §10).
  - Acceso corporativo LDAPS (opcional): pasos en la guía 02, §4.1.
  - Usuarios: en /usuarios (admin) puedes crear cuentas locales con su
    contraseña y rol, cambiar contraseñas o eliminar la cuenta. Las cuentas
    LDAPS se validan en el directorio y solo se activan/desactivan aquí
    (guía 02, §4).
  - El modo Simulación cambia desde la barra superior de la interfaz.
    Valida primero con DRY RUN y desactívalo después del host de prueba.
  - Vistas: /ejecuciones (lotes e historial), /solicitudes (aprobaciones),
    /credenciales, /inventario, /programaciones, /usuarios, /sistema
    (auditoría, rendimiento, ajustes y acerca de) y /cuenta.

ANTES DE PRODUCCIÓN
  - Revisa 04-CHECKLIST-PRODUCCION.md de principio a fin.
  - No uses ACC_ENV=development ni ACC_IMAGE_TAG=latest en producción.
  - No ejecutes "docker compose down -v": borra los volúmenes con los datos.
EOF

cp -a docker-compose.yml Dockerfile .env.docker.example VERSION "$DEST/"
cp -a docker/entrypoint.sh "$DEST/docker/"
cp -a docker/nginx/Dockerfile docker/nginx/default.conf docker/nginx/self-signed.sh \
  "$DEST/docker/nginx/"
# Guía completa y checklist, numeradas en el orden de lectura.
cp -a docker/despliegue-portable.md "$DEST/02-GUIA-COMPLETA-DESPLIEGUE.md"
cp -a docker/CHECKLIST-PRODUCCION.md "$DEST/04-CHECKLIST-PRODUCCION.md"
cp -a docker/herramientas/respaldo-datos.sh \
      docker/herramientas/restaurar-datos.sh \
      docker/herramientas/verificar-despliegue.sh "$DEST/herramientas/"
chmod +x "$DEST/herramientas/"*.sh
: > "$DEST/certs/.gitkeep"

# CA pública del directorio (solo certificados, nunca claves privadas).
cp -a docker/certs-ca/LEEME.txt "$DEST/certs-ca/LEEME.txt"
for ca in "$ROOT_DIR"/certs-ca/*.pem "$ROOT_DIR"/certs-ca/*.crt "$ROOT_DIR"/certs-ca/*.cer; do
  [ -f "$ca" ] && cp -a "$ca" "$DEST/certs-ca/"
done

# El paquete fija la etiqueta exacta: nunca latest para un despliegue
# reproducible. El usuario puede sobrescribirla en su .env.docker.
if grep -q '^ACC_IMAGE_TAG=' "$DEST/.env.docker.example"; then
  sed -i "s/^ACC_IMAGE_TAG=.*/ACC_IMAGE_TAG=$VERSION/" "$DEST/.env.docker.example"
else
  printf '\nACC_IMAGE_TAG=%s\n' "$VERSION" >> "$DEST/.env.docker.example"
fi

# La versión también queda visible al principio del arranque rápido.
{
  head -n 2 "$DEST/01-INICIO-RAPIDO.txt"
  printf '\nVersión del paquete: %s\n' "$VERSION"
  tail -n +3 "$DEST/01-INICIO-RAPIDO.txt"
} > "$DEST/01-INICIO-RAPIDO.txt.tmp"
mv "$DEST/01-INICIO-RAPIDO.txt.tmp" "$DEST/01-INICIO-RAPIDO.txt"

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

Versión     : $VERSION
Empaquetado : $(date -u '+%Y-%m-%d %H:%M UTC')
Commit      : $GIT_COMMIT
Sin confirmar: $GIT_DIRTY
Arquitectura: $ARCH
Tamaño      : $SIZE

Tags        : $VERSION, $MAJOR_MINOR, latest, sha-$GIT_COMMIT

$APP_IMAGE
  id: $APP_DIGEST
  app Flask con Gunicorn, Ansible, sshpass y Tini (puerto interno 8000)

$NGINX_IMAGE
  id: $NGINX_DIGEST
  Nginx 1.27 con TLS y generación de certificado autofirmado

El paquete fija ACC_IMAGE_TAG=$VERSION en .env.docker.example. Usa siempre
esa etiqueta exacta; latest es solo un alias de consulta.

Incluye además herramientas/respaldo-datos.sh, restaurar-datos.sh y
verificar-despliegue.sh, la guía 02 y la checklist 04 de producción.

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
  Versión      : $VERSION
  Imagen .tar  : $SIZE
  Punto de entrada: $DEST/01-INICIO-RAPIDO.txt
  Guía completa   : $DEST/02-GUIA-COMPLETA-DESPLIEGUE.md
  Checklist       : $DEST/04-CHECKLIST-PRODUCCION.md

  Copia toda la carpeta a la otra PC y empieza por 01-INICIO-RAPIDO.txt. Allí
  no hace falta ni el código fuente ni acceso a Docker Hub. Para migrar datos
  existentes usa herramientas/respaldo-datos.sh en origen y
  herramientas/restaurar-datos.sh en destino (guía 02, §7).

EOF
