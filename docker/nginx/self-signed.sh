#!/bin/sh
# Genera un certificado autofirmado si no existe ninguno en /etc/nginx/certs.
# Para usar un certificado real, monta fullchain.pem y privkey.pem en ese
# directorio y reinicia Nginx.
set -eu

CERT_DIR="${TLS_CERT_DIR:-/etc/nginx/certs}"
CERT_FILE="$CERT_DIR/fullchain.pem"
KEY_FILE="$CERT_DIR/privkey.pem"

mkdir -p "$CERT_DIR"

if [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
    echo "[nginx] Usando certificado existente en $CERT_DIR"
    exit 0
fi

TLS_CN="${TLS_CN:-localhost}"
TLS_SAN="${TLS_SAN:-DNS:localhost,IP:127.0.0.1}"
TLS_DAYS="${TLS_DAYS:-825}"
TLS_ORG="${TLS_ORG:-Hora Admin}"

echo "[nginx] Generando certificado autofirmado CN=$TLS_CN SAN=$TLS_SAN"
openssl req -x509 -nodes -newkey rsa:4096 -sha256 \
    -days "$TLS_DAYS" \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -subj "/C=${TLS_COUNTRY:-ES}/O=${TLS_ORG}/CN=${TLS_CN}" \
    -addext "subjectAltName=${TLS_SAN}"

chmod 600 "$KEY_FILE"
chmod 644 "$CERT_FILE"
echo "[nginx] Certificado generado. Sustitúyelo por uno real cuando puedas."
