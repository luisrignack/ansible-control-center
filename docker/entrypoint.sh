#!/bin/sh
# Entrypoint de la imagen de Hora Admin.
#
# Prepara directorios, aplica migraciones, crea el administrador inicial si se
# proporcionó HORA_ADMIN_ADMIN_PASSWORD y arranca Gunicorn.
set -eu

DATA_DIR="${HORA_ADMIN_DATA_DIR:-/data}"
SECRETS_DIR="${HORA_ADMIN_SECRETS_DIR:-/secrets}"
LOG_DIR="${HORA_ADMIN_LOG_DIR:-/logs}"

mkdir -p "$DATA_DIR" "$SECRETS_DIR" "$LOG_DIR" "$HOME/.ssh"
chmod 700 "$SECRETS_DIR" "$HOME/.ssh" 2>/dev/null || true

# known_hosts persistente. Se puede poblar con:
#   docker compose exec app ssh-keyscan 10.0.0.10 >> ~/.ssh/known_hosts
if [ -n "${HORA_ADMIN_KNOWN_HOSTS:-}" ]; then
    printf '%s\n' "$HORA_ADMIN_KNOWN_HOSTS" > "$HOME/.ssh/known_hosts"
    chmod 600 "$HOME/.ssh/known_hosts"
elif [ ! -f "$HOME/.ssh/known_hosts" ]; then
    : > "$HOME/.ssh/known_hosts"
    chmod 600 "$HOME/.ssh/known_hosts"
fi

echo "[hora-admin] Aplicando migraciones de SQLite…"
python -m flask --app app init-db

echo "[hora-admin] Comprobando usuario administrador…"
python - <<'PY'
import os
import sys

from config import ADMIN_PASSWORD_MIN_LENGTH, ADMIN_USERNAME, resolve_db_path
from models.user import User

db_path = str(resolve_db_path())
password = os.environ.get("HORA_ADMIN_ADMIN_PASSWORD", "")
username = os.environ.get("HORA_ADMIN_ADMIN_USERNAME", ADMIN_USERNAME)

if password:
    if len(password) < ADMIN_PASSWORD_MIN_LENGTH:
        sys.exit(
            "[hora-admin] HORA_ADMIN_ADMIN_PASSWORD es demasiado corta "
            f"(mínimo {ADMIN_PASSWORD_MIN_LENGTH} caracteres)."
        )
    if User.count(db_path=db_path) == 0:
        User.create_or_update(username, password, db_path=db_path)
        print(f"[hora-admin] Administrador '{username}' creado.")
    else:
        print(
            "[hora-admin] Ya existen usuarios; no se modifica el administrador. "
            "Usa 'flask --app app init-admin' para rotar la contraseña."
        )
elif User.count(db_path=db_path) == 0:
    print(
        "[hora-admin] AVISO: no hay usuarios. Define "
        "HORA_ADMIN_ADMIN_PASSWORD o ejecuta "
        "'docker compose exec app flask --app app init-admin'."
    )
PY

echo "[hora-admin] Iniciando Gunicorn en ${HORA_ADMIN_BIND:-0.0.0.0:8000}…"
exec gunicorn -c /app/gunicorn.conf.py app:app
