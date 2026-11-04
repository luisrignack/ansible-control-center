#!/bin/sh
# Entrypoint de la imagen de Ansible Control Center.
#
# Prepara directorios, aplica migraciones, crea el administrador inicial si se
# proporcionó ACC_ADMIN_PASSWORD y arranca Gunicorn.
set -eu

DATA_DIR="${ACC_DATA_DIR:-/data}"
SECRETS_DIR="${ACC_SECRETS_DIR:-/secrets}"
LOG_DIR="${ACC_LOG_DIR:-/logs}"

mkdir -p "$DATA_DIR" "$SECRETS_DIR" "$LOG_DIR" "$HOME/.ssh"
chmod 700 "$SECRETS_DIR" "$HOME/.ssh" 2>/dev/null || true

# known_hosts persistente. Se puede poblar con:
#   docker compose exec app ssh-keyscan 10.0.0.10 >> ~/.ssh/known_hosts
if [ -n "${ACC_KNOWN_HOSTS:-}" ]; then
    printf '%s\n' "$ACC_KNOWN_HOSTS" > "$HOME/.ssh/known_hosts"
    chmod 600 "$HOME/.ssh/known_hosts"
elif [ ! -f "$HOME/.ssh/known_hosts" ]; then
    : > "$HOME/.ssh/known_hosts"
    chmod 600 "$HOME/.ssh/known_hosts"
fi

echo "[ansible-control-center] Aplicando migraciones de SQLite…"
python -m flask --app app init-db

echo "[ansible-control-center] Comprobando usuario administrador…"
# Soporte de Docker secrets / credenciales montadas: si no hay contraseña en
# claro, se lee de ACC_ADMIN_PASSWORD_FILE para no exponerla en `docker inspect`.
if [ -z "${ACC_ADMIN_PASSWORD:-}" ] && [ -n "${ACC_ADMIN_PASSWORD_FILE:-}" ] && [ -r "${ACC_ADMIN_PASSWORD_FILE}" ]; then
    ACC_ADMIN_PASSWORD="$(cat "${ACC_ADMIN_PASSWORD_FILE}")"
    export ACC_ADMIN_PASSWORD
fi
python - <<'PY'
import os
import sys

from config import ADMIN_PASSWORD_MIN_LENGTH, ADMIN_USERNAME, resolve_db_path
from models.user import User

db_path = str(resolve_db_path())
password = os.environ.get("ACC_ADMIN_PASSWORD", "")
username = os.environ.get("ACC_ADMIN_USERNAME", ADMIN_USERNAME)

if password:
    if User.count(db_path=db_path) == 0:
        if len(password) < ADMIN_PASSWORD_MIN_LENGTH:
            sys.exit(
                "[ansible-control-center] ACC_ADMIN_PASSWORD es demasiado corta "
                f"(mínimo {ADMIN_PASSWORD_MIN_LENGTH} caracteres)."
            )
        User.create_or_update(username, password, db_path=db_path)
        print(f"[ansible-control-center] Administrador '{username}' creado.")
    else:
        print(
            "[ansible-control-center] Ya existen usuarios; no se modifica el administrador. "
            "Usa 'flask --app app init-admin' para rotar la contraseña."
        )
elif User.count(db_path=db_path) == 0:
    print(
        "[ansible-control-center] AVISO: no hay usuarios. Define "
        "ACC_ADMIN_PASSWORD o ejecuta "
        "'docker compose exec app flask --app app init-admin'."
    )
PY

echo "[ansible-control-center] Iniciando Gunicorn en ${ACC_BIND:-0.0.0.0:8000}…"
exec gunicorn -c /app/gunicorn.conf.py app:app
