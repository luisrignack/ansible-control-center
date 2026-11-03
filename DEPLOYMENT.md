# Guía de despliegue — Hora Admin

Aplicación web Flask para configurar hora y zona horaria en equipos Ubuntu
mediante Ansible.

> **Estado (fases P0, P1 y P2):** estructura unificada, arranque, login,
> validación de formularios, páginas de error, progreso SSE, integridad de
> datos SQLite (migraciones, WAL y concurrencia), vault real con
> `ansible-vault`, ejecución Ansible con inyección segura de credenciales y
> modo temporal `.txt`. El modo `DRY_RUN` sigue activo por defecto.

---

## 1. Requisitos

| Componente | Versión mínima | Notas |
|---|---:|---|
| Ubuntu | 20.04+ | Equipos objetivo |
| Python | 3.11+ | Servidor Flask |
| Ansible | 2.16+ | `ansible`, `ansible-playbook`, `ansible-vault` |
| sshpass | — | SSH con contraseña (`apt install sshpass`) |
| Gunicorn | 22+ | Producción |
| Nginx/Apache | — | Proxy inverso con HTTPS |

Acceso SSH desde el servidor a los hosts objetivo. El usuario administrador
debe poder usar `sudo`/`become`. Los equipos objetivo deben ser Ubuntu con
`systemd`/`timedatectl`.

---

## 2. Instalación

Desde la raíz del proyecto:

```bash
sudo ./install.sh
```

El script:

1. Crea `.venv` e instala `requirements.txt` (`ansible-core`, Flask, etc.).
2. Comprueba `sshpass` y avisa si falta.
3. Crea `/etc/hora-admin/secrets` (0700), `/var/lib/hora-admin` y
   `/var/log/hora-admin`.
4. Genera `.env` con una `HORA_ADMIN_SECRET_KEY` aleatoria (0600).
5. Ejecuta `flask --app app init-db`.

> Si tu equipo usa un mirror corporativo en `/etc/pip.conf` y no estás en esa
> red, ejecuta la instalación de dependencias con
> `PIP_CONFIG_FILE=/dev/null python -m pip install -r requirements.txt`.

### Instalación manual

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo apt install sshpass

sudo install -d -m 0750 /etc/hora-admin
sudo install -d -m 0700 /etc/hora-admin/secrets
sudo install -d -m 0750 /var/lib/hora-admin /var/log/hora-admin
sudo chown -R "$USER:$USER" /etc/hora-admin /var/lib/hora-admin /var/log/hora-admin

cp .env.example .env
# Edita .env y define HORA_ADMIN_SECRET_KEY con: openssl rand -hex 32
chmod 600 .env

flask --app app init-db
flask --app app init-admin
```

---

## 3. Configuración

### Variables principales

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_SECRET_KEY` | generada | Clave de sesión Flask |
| `HORA_ADMIN_SECRETS_DIR` | `/etc/hora-admin/secrets` | Vault y secretos |
| `HORA_ADMIN_DATA_DIR` | `/var/lib/hora-admin` | SQLite y clave local |
| `HORA_ADMIN_LOG_DIR` | `/var/log/hora-admin` | Logs |
| `HORA_ADMIN_DRY_RUN` | `1` | Simula Ansible |
| `HORA_ADMIN_HOST_KEY_CHECKING` | `1` | Verificación estricta de host SSH |
| `HORA_ADMIN_DEBUG` | `0` | Depurador Flask |
| `HORA_ADMIN_COOKIE_SECURE` | `0` | `1` detrás de HTTPS |
| `HORA_ADMIN_SESSION_HOURS` | `8` | Duración de sesión |
| `HORA_ADMIN_MAX_UPLOAD_BYTES` | `2097152` | Tamaño máximo de subida |

Para desarrollo sin `sudo`, define rutas locales:

```env
HORA_ADMIN_SECRETS_DIR=./instance/secrets
HORA_ADMIN_DATA_DIR=./instance/data
HORA_ADMIN_LOG_DIR=./instance/logs
```

Si no se definen y el proceso no puede escribir en `/etc` o `/var`, la app
usa automáticamente `instance/` y muestra un aviso en el log.

### Usuario administrador

```bash
source .venv/bin/activate
flask --app app init-admin
# Usuario por defecto: admin
# Puedes indicar otro: flask --app app init-admin --username operador
```

La contraseña se guarda como hash Werkzeug en SQLite. Para cambiarla, vuelve a
ejecutar el comando.

---

## 4. Desarrollo

```bash
source .venv/bin/activate
python app.py
# http://127.0.0.1:5000
```

Por defecto escucha solo en `127.0.0.1`. Para exponerlo en la red local:

```bash
HORA_ADMIN_HOST=0.0.0.0 HORA_ADMIN_PORT=5000 python app.py
```

> No uses el servidor de desarrollo en producción.

---

## 5. Producción con Gunicorn

`gunicorn.conf.py` ya está incluido. Usa **un solo worker** por defecto porque
el progreso de tareas vive en memoria; para varios workers habrá que mover ese
estado a un almacén compartido.

```bash
source .venv/bin/activate
gunicorn -c gunicorn.conf.py app:app
```

### Servicio systemd

`/etc/systemd/system/hora-admin.service`:

```ini
[Unit]
Description=Hora Admin
After=network.target

[Service]
User=hora-admin
Group=hora-admin
WorkingDirectory=/opt/hora-admin
EnvironmentFile=/opt/hora-admin/.env
ExecStart=/opt/hora-admin/.venv/bin/gunicorn -c /opt/hora-admin/gunicorn.conf.py app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hora-admin
sudo systemctl status hora-admin
sudo journalctl -u hora-admin -f
```

### Proxy inverso con Nginx

```nginx
server {
    listen 443 ssl;
    server_name hora-admin.example.com;

    # ssl_certificate ...; ssl_certificate_key ...;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE (progreso en tiempo real)
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 360s;
    }
}
```

Con HTTPS activa `HORA_ADMIN_COOKIE_SECURE=1` en `.env`.

---

## 6. Flujo de uso

1. **Crear administrador** (`flask --app app init-admin`).
2. **Contraseñas** → introducir candidatas y la contraseña maestra del vault.
   También puedes importar un `.txt` o un vault `.yml/.yaml` ya cifrado.
3. **Panel** → IP o CIDR, usuario admin, origen de contraseñas, acción
   (`TZ`/`TIME`) y valor. En modo temporal se sube un `.txt` con una
   contraseña por línea.
4. **Progreso** → la ejecución redirige a `/progress/<task_id>` con SSE.
5. **Historial** → `/history` con las ejecuciones registradas (éxitos y fallos).

Con `HORA_ADMIN_DRY_RUN=1` el playbook no se ejecuta: se simula el resultado.
Para ejecutar de verdad, define `HORA_ADMIN_DRY_RUN=0` y reinicia el servicio.

### Vault

- El vault es un fichero cifrado con `ansible-vault` en
  `/etc/hora-admin/secrets/passwords_vault.yml` (permisos `0600`).
- Contiene una contraseña por línea; las líneas vacías y las que empiezan por
  `#` se ignoran.
- La contraseña maestra se pide en cada operación y no se guarda.
- Al borrar una contraseña desde la interfaz se actualiza también el vault.

```bash
# Inspección manual (pide la contraseña maestra)
ansible-vault view /etc/hora-admin/secrets/passwords_vault.yml
```

---

## 7. Copias de seguridad

- SQLite: `/var/lib/hora-admin/hora_admin.db` (o `instance/hora_admin.db` en
  desarrollo).
- Vault cifrado: `/etc/hora-admin/secrets/passwords_vault.yml`.
- `.env` (contiene la clave de sesión): trátalo como secreto.

```bash
# Con el servicio detenido (recomendado), o copiando también los ficheros WAL:
sudo systemctl stop hora-admin
sudo cp /var/lib/hora-admin/hora_admin.db /var/backups/hora_admin.db
sudo cp /etc/hora-admin/secrets/passwords_vault.yml /var/backups/
sudo systemctl start hora-admin
```

---

## 8. Seguridad y limitaciones conocidas (P0/P1/P2)

- `DRY_RUN=1` por defecto: no se toca ningún equipo hasta cambiarlo.
- `DEBUG=0` por defecto; nunca actives el depurador en producción.
- Login con hash Werkzeug y sesión protegida por Flask-Login.
- Las contraseñas candidatas se cifran con `ansible-vault`; SQLite solo guarda
  hashes cortos para la interfaz.
- La contraseña de conexión de Ansible se pasa en un fichero temporal `0600`
  mediante `--extra-vars @fichero`; nunca va en la línea de comandos ni en los
  logs. El fichero se borra tras cada intento.
- Por defecto se verifica la clave SSH del host. Si los equipos no están en
  `known_hosts`, añádelos con `ssh-keyscan` o define
  `HORA_ADMIN_HOST_KEY_CHECKING=0` de forma consciente (menos seguro).
- El modo temporal `.txt` procesa el archivo en memoria; no se guarda en disco.
- SQLite en modo WAL: al copiar la base de datos en caliente, copia también
  `hora_admin.db-wal` y `hora_admin.db-shm`, o detén el servicio antes.
- Las migraciones son no destructivas y se aplican al arrancar
  (`PRAGMA user_version`). Haz copia de seguridad antes de actualizar.
- **Pendiente P3:** CSRF, rate limiting, límites de CIDR/hosts y cabeceras de
  seguridad.
- El progreso SSE y las tareas viven en memoria: usa un worker o añade un
  almacén compartido antes de escalar.

---

## 9. Verificación post-despliegue

```bash
# 1. Login disponible
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/login

# 2. Redirección de ruta protegida
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://127.0.0.1:5000/

# 3. Tests
source .venv/bin/activate
python -m pytest -q
```
