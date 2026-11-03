#!/usr/bin/env bash
#
# install.sh - Prepara el entorno de Hora Admin.
#
#   * Comprueba Python >= 3.11.
#   * Crea el virtualenv .venv e instala requirements.txt.
#   * Comprueba sshpass (necesario para SSH con contraseña).
#   * Crea /etc/hora-admin/secrets (0700), /var/lib/hora-admin y
#     /var/log/hora-admin con permisos correctos.
#   * Genera .env con una clave de sesión aleatoria (modo 0600).
#
# Uso:
#   ./install.sh
#   PYTHON_BIN=python3.12 ./install.sh
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
SECRETS_DIR="${HORA_ADMIN_SECRETS_DIR:-/etc/hora-admin/secrets}"
DATA_DIR="${HORA_ADMIN_DATA_DIR:-/var/lib/hora-admin}"
LOG_DIR="${HORA_ADMIN_LOG_DIR:-/var/log/hora-admin}"

log()  { printf '\033[1;36m[hora-admin]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[hora-admin]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[hora-admin]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Requisitos del sistema
# ---------------------------------------------------------------------------
command -v "$PYTHON_BIN" >/dev/null 2>&1 \
  || die "No se encontró '$PYTHON_BIN'. Instala Python 3.11 o superior."

"$PYTHON_BIN" - <<'PY' || die "Se requiere Python 3.11 o superior."
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python detectado: {sys.version.split()[0]}. Se requiere >= 3.11.")
PY

command -v sshpass >/dev/null 2>&1 \
  || warn "sshpass no está instalado; Ansible no podrá autenticarse por contraseña SSH (apt install sshpass)."

# ---------------------------------------------------------------------------
# 2. Virtualenv y dependencias Python
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
  log "Creando virtualenv en $VENV_DIR ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  log "Reutilizando virtualenv existente: $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

log "Actualizando pip/wheel ..."
python -m pip install --upgrade pip wheel >/dev/null

log "Instalando dependencias de requirements.txt ..."
python -m pip install -r requirements.txt

# ---------------------------------------------------------------------------
# 3. Directorios de datos y secretos
# ---------------------------------------------------------------------------
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  command -v sudo >/dev/null 2>&1 \
    || die "Se necesita sudo (o ejecutar como root) para crear $SECRETS_DIR."
  SUDO="sudo"
fi

OWNER="$(id -un)"
GROUP="$(id -gn)"

log "Creando rutas seguras ..."
# El padre de SECRETS_DIR permite el paso, pero secrets/ queda en 0700.
$SUDO install -d -m 0750 "$(dirname "$SECRETS_DIR")"
$SUDO install -d -m 0700 "$SECRETS_DIR"
$SUDO install -d -m 0750 "$DATA_DIR" "$LOG_DIR"
$SUDO chown -R "$OWNER:$GROUP" "$(dirname "$SECRETS_DIR")" "$DATA_DIR" "$LOG_DIR"

# ---------------------------------------------------------------------------
# 4. Archivo .env con clave de sesión aleatoria
# ---------------------------------------------------------------------------
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  log "Creando .env a partir de .env.example ..."
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  SESSION_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  # sed portable (Linux): reemplaza la línea de la clave.
  sed -i "s|^HORA_ADMIN_SECRET_KEY=.*|HORA_ADMIN_SECRET_KEY=${SESSION_KEY}|" "$SCRIPT_DIR/.env"
  chmod 600 "$SCRIPT_DIR/.env"
else
  log "Conservando .env existente (no se sobrescribe)."
fi

# ---------------------------------------------------------------------------
# 5. Inicialización de la base de datos (cuando app.py ya existe)
# ---------------------------------------------------------------------------
if [ -f "$SCRIPT_DIR/app.py" ]; then
  log "Inicializando base de datos ..."
  python -m flask --app app init-db || warn "No se pudo inicializar la BD automáticamente."
  log "Crea el administrador con: flask --app app init-admin"
else
  warn "app.py no encontrado; revisa la estructura del proyecto."
fi

# ---------------------------------------------------------------------------
# 6. Resumen
# ---------------------------------------------------------------------------
log "Entorno preparado correctamente."
cat <<EOF

  Virtualenv : $VENV_DIR
  Secretos   : $SECRETS_DIR  (0700)
  Datos      : $DATA_DIR
  Logs       : $LOG_DIR
  DRY RUN    : activado por defecto (HORA_ADMIN_DRY_RUN=1)
  Ejecución  : pon HORA_ADMIN_DRY_RUN=0 para tocar equipos reales

  Siguientes pasos:
    source .venv/bin/activate
    python app.py                 # desarrollo (http://127.0.0.1:5000)
    gunicorn -c gunicorn.conf.py app:app   # producción

  Recuerda publicar la app SOLO detrás de un proxy inverso con HTTPS
  (ver README.md, sección "Despliegue seguro").

EOF
