#!/bin/bash
# Aplica la imagen de fondo a las sesiones GNOME activas del sistema.
#
# Uso: acc-aplicar-tapiz <file-uri>
#
# Recorre los directorios /home/*, localiza el bus de sesión de cada usuario
# y ejecuta gsettings con ese bus. No falla si un usuario no tiene sesión
# gráfica: solo se cuentan los fallos reales de gsettings en sesiones activas.
set -u

uri="${1:-}"
if [ -z "$uri" ]; then
    echo "Falta la URI del tapiz." >&2
    exit 2
fi

applied=0
failures=0

for home in /home/*; do
    [ -d "$home" ] || continue
    user="$(basename "$home")"
    uid="$(id -u "$user" 2>/dev/null || true)"
    [ -n "$uid" ] || continue
    bus="/run/user/$uid/bus"
    [ -S "$bus" ] || continue

    current="$(sudo -u "$user" \
        env DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" \
        gsettings get org.gnome.desktop.background picture-uri \
        2>/dev/null || true)"
    current="$(printf '%s' "$current" | tr -d "\"'")"

    if [ "$current" != "$uri" ]; then
        if sudo -u "$user" \
            env DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" \
            gsettings set org.gnome.desktop.background picture-uri "$uri"; then
            applied=$((applied + 1))
        else
            failures=$((failures + 1))
        fi
    fi

    # Modo oscuro y ajuste de la imagen: best-effort, no cuentan como fallo
    # porque no todas las versiones de GNOME publican esas claves.
    sudo -u "$user" \
        env DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" \
        gsettings set org.gnome.desktop.background picture-uri-dark "$uri" \
        >/dev/null 2>&1 || true
    sudo -u "$user" \
        env DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" \
        gsettings set org.gnome.desktop.background picture-options zoom \
        >/dev/null 2>&1 || true
done

echo "TAPIZ_APPLIED_USERS=$applied"
echo "TAPIZ_SESSION_FAILURES=$failures"
exit 0
