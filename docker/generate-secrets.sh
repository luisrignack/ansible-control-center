#!/usr/bin/env bash
#
# generate-secrets.sh - Crea los ficheros de secretos que consume
# docker-compose.secrets.yml.
#
#   ./docker/generate-secrets.sh              # crea secrets.d/ y sus ficheros
#   ./docker/generate-secrets.sh /ruta/dir    # otra ubicación
#
# Características:
#   * No sobrescribe ficheros existentes: se puede ejecutar en cada despliegue
#     sin invalidar sesiones ni rotar contraseñas por accidente.
#   * Funciona sin conexión (solo usa openssl del sistema).
#   * Directorio 0700 y ficheros 0600.
#
# Después:
#   docker compose -f docker-compose.yml -f docker-compose.secrets.yml up -d
#
set -Eeuo pipefail

SECRETS_DIR="${1:-${ACC_SECRETS_FILES_DIR:-./secrets.d}}"

umask 077
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

log() { printf '[generate-secrets] %s\n' "$*"; }

_create() {
    local name="$1"
    local generator="$2"
    local target="$SECRETS_DIR/$name"

    if [ -s "$target" ]; then
        log "Ya existe $name; se conserva."
        return
    fi
    if [ -e "$target" ]; then
        # Existe pero vacío (por ejemplo, el marcador de LDAP): se respeta.
        log "$name existe vacío; se conserva."
        chmod 600 "$target"
        return
    fi
    "$generator" >"$target"
    chmod 600 "$target"
    log "Creado $name (0600)."
}

_gen_hex() { openssl rand -hex 32; }
_gen_password() { openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | cut -c1-24; }

_create acc_secret_key _gen_hex
_create acc_admin_password _gen_password

# Marcador para LDAP: si no se usa LDAPS, el fichero vacío mantiene el
# compose válido. Si se activa LDAP, escribe aquí la contraseña del bind.
if [ ! -e "$SECRETS_DIR/acc_ldap_bind_password" ]; then
    : >"$SECRETS_DIR/acc_ldap_bind_password"
    chmod 600 "$SECRETS_DIR/acc_ldap_bind_password"
    log "Creado acc_ldap_bind_password vacío (rellénalo solo si usas LDAPS)."
fi

log "Listo. Directorio: $SECRETS_DIR"
log "Arranque: docker compose -f docker-compose.yml -f docker-compose.secrets.yml up -d"
log "La contraseña del administrador está en $SECRETS_DIR/acc_admin_password"
