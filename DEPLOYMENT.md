# Guía de despliegue — Hora Admin

Aplicación web Flask para configurar hora y zona horaria en equipos Ubuntu
mediante Ansible.

> **Estado (fases P0-P4):** estructura unificada, arranque, login,
> validación de formularios, páginas de error, progreso SSE, integridad de
> datos SQLite (migraciones, WAL y concurrencia), vault real con
> `ansible-vault`, ejecución Ansible con inyección segura de credenciales,
> modo temporal `.txt`, CSRF, rate limiting, límites CIDR/hosts, cabeceras
> de seguridad, ejecución best-effort en todos los hosts, lotes con
> resultado por host, vista previa, verificación posterior, idempotencia y
> política NTP explícita. El modo `DRY_RUN` sigue activo por defecto.

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
5. Ejecuta `flask --app app init-db` (esquema v4, migración no destructiva).
6. **No crea el usuario administrador**: ejecuta después
   `flask --app app init-admin`.

> **Usuario de servicio:** `install.sh` crea los directorios y `.env` para el
> usuario que lo invoca. Si el unit de systemd usa `User=hora-admin`, crea ese
> usuario antes y ejecuta la instalación con él, o ajusta la propiedad:
>
> ```bash
> sudo useradd --system --home /opt/hora-admin --shell /usr/sbin/nologin hora-admin
> sudo chown -R hora-admin:hora-admin /opt/hora-admin /etc/hora-admin \
>   /var/lib/hora-admin /var/log/hora-admin
> sudo chmod 600 /opt/hora-admin/.env
> ```
>
> Si no se alinean propietario y `User=`, el servicio no podrá leer `.env` ni
> escribir SQLite/vault.

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
| `HORA_ADMIN_DRY_RUN` | `1` | Simula Ansible (no toca hosts) |
| `HORA_ADMIN_HOST_KEY_CHECKING` | `1` | Verificación estricta de host SSH |
| `HORA_ADMIN_DEBUG` | `0` | Depurador Flask |
| `HORA_ADMIN_COOKIE_SECURE` | `0` | `1` detrás de HTTPS |
| `HORA_ADMIN_TRUST_PROXY` | `0` | `1` solo tras proxy de confianza |
| `HORA_ADMIN_HSTS` | según cookie | Cabecera HSTS |
| `HORA_ADMIN_HSTS_MAX_AGE` | `31536000` | `max-age` de HSTS |
| `HORA_ADMIN_SESSION_HOURS` | `8` | Duración de sesión (1–72 h) |
| `HORA_ADMIN_MAX_UPLOAD_BYTES` | `2097152` | Tamaño máximo de subida |
| `HORA_ADMIN_CSRF_ENABLED` | `1` | Protección CSRF |
| `HORA_ADMIN_CSRF_TIME_LIMIT` | `28800` | Caducidad del token CSRF (s) |
| `HORA_ADMIN_RATE_LIMIT_ENABLED` | `1` | Rate limiting global |
| `HORA_ADMIN_LOGIN_RATE_LIMIT` | `10` | Intentos de login por ventana |
| `HORA_ADMIN_LOGIN_RATE_WINDOW` | `300` | Ventana de login (s) |
| `HORA_ADMIN_EXEC_RATE_LIMIT` | `10` | Ejecuciones por ventana |
| `HORA_ADMIN_EXEC_RATE_WINDOW` | `60` | Ventana de ejecución (s) |
| `HORA_ADMIN_VAULT_RATE_LIMIT` | `20` | Operaciones de vault por ventana |
| `HORA_ADMIN_VAULT_RATE_WINDOW` | `60` | Ventana de vault (s) |
| `HORA_ADMIN_SECURITY_HEADERS` | `1` | Cabeceras de seguridad |
| `HORA_ADMIN_CSP_ENABLED` | `1` | CSP con nonce |
| `HORA_ADMIN_ADMIN_PASSWORD_MIN_LENGTH` | `12` | Longitud mínima del admin |
| `HORA_ADMIN_MAX_HOSTS` | `256` | Hosts máximos por ejecución |
| `HORA_ADMIN_MIN_CIDR_PREFIX` | `22` | Prefijo IPv4 mínimo |
| `HORA_ADMIN_ALLOWED_NETWORKS` | vacío | Allowlist de redes (vacío = sin restricción) |
| `HORA_ADMIN_MAX_PASSWORDS` | `200` | Máximo de candidatas |
| `HORA_ADMIN_MAX_PASSWORD_LENGTH` | `512` | Longitud máxima de candidata |
| `HORA_ADMIN_PING_TIMEOUT` | `30` | Timeout de ping (s) |
| `HORA_ADMIN_PLAYBOOK_TIMEOUT` | `300` | Timeout de playbook (s) |
| `HORA_ADMIN_CONNECT_TIMEOUT` | `10` | Timeout de conexión SSH (s) |
| `HORA_ADMIN_TIME_TOLERANCE` | `120` | Tolerancia de verificación de hora (s) |
| `HORA_ADMIN_DEFAULT_NTP_SERVERS` | `pool.ntp.org` | Servidores NTP por defecto |
| `HORA_ADMIN_MAX_NTP_SERVERS` | `8` | Máximo de servidores NTP |
| `HORA_ADMIN_LOG_LEVEL` | `INFO` | Nivel de log |
| `HORA_ADMIN_LOG_TO_FILE` | `1` | Log rotativo en fichero |
| `HORA_ADMIN_LOG_MAX_BYTES` | `5242880` | Tamaño máximo por fichero |
| `HORA_ADMIN_LOG_BACKUPS` | `5` | Número de backups |
| `HORA_ADMIN_ANSIBLE_BIN` | autodetectado | Ruta de `ansible` |
| `HORA_ADMIN_ANSIBLE_PLAYBOOK_BIN` | autodetectado | Ruta de `ansible-playbook` |
| `HORA_ADMIN_ANSIBLE_VAULT_BIN` | autodetectado | Ruta de `ansible-vault` |
| `HORA_ADMIN_ANSIBLE_GALAXY_BIN` | autodetectado | Ruta de `ansible-galaxy` |
| `HORA_ADMIN_HISTORY_DAYS` | `180` | **Reservada**: retención sin job de purga |
| `HORA_ADMIN_JOB_RETENTION_HOURS` | `24` | **Reservada**: tareas en memoria |

Variables específicas de Gunicorn (`gunicorn.conf.py`, no de `config.py`):

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_BIND` | `127.0.0.1:5000` | Dirección de escucha |
| `HORA_ADMIN_WORKERS` | `1` | Workers (el estado SSE es por proceso) |
| `HORA_ADMIN_GUNICORN_WORKER_CLASS` | `gthread` | Clase de worker |
| `HORA_ADMIN_GUNICORN_THREADS` | `4` | Hilos por worker |
| `HORA_ADMIN_GUNICORN_TIMEOUT` | `330` | Timeout del worker (s) |
| `HORA_ADMIN_GUNICORN_GRACEFUL_TIMEOUT` | `30` | Parada ordenada (s) |

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

### Esquema de datos y migración

El esquema actual es `PRAGMA user_version = 3`. `flask --app app init-db`
aplica migraciones **no destructivas** al arrancar y es idempotente.

Tablas principales desde P4:

- `execution_batches`: un registro por operación (lote) con `batch_id`,
  operador, objetivo, acción, valor, `time_mode`, `ntp_policy`, estado global,
  contadores y fechas.
- `execution_hosts`: un resultado por host con estado previo/posterior, error,
  duración y hash corto de la contraseña usada.
- `executions`: historial heredado por host; se conserva por compatibilidad.

Haz copia de seguridad antes de actualizar. Si una migración falla, la
aplicación no debe continuar; revisa el log y restaura la copia.

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

> **SSE y worker `gthread`:** se usa un solo worker con 4 hilos y un `timeout`
> de 330 s (mayor que el `max_wait` de 300 s del SSE). Así una conexión SSE no
> bloquea el resto de peticiones. Aun así, el estado de las tareas vive en
> memoria: no aumentes `HORA_ADMIN_WORKERS` sin moverlo antes a un almacén
> compartido.
>
> Si el proceso se reinicia, `_tasks` se pierde. Al arrancar, los lotes que
> quedaron `running` se marcan automáticamente como `error`
> (`flask --app app reconcile-batches` permite forzarlo).

### Servicio systemd

Antes de instalar el unit, crea el usuario de servicio y alinea la propiedad de
las rutas (ver §2):

```bash
sudo useradd --system --home /opt/hora-admin --shell /usr/sbin/nologin hora-admin
sudo chown -R hora-admin:hora-admin /opt/hora-admin /etc/hora-admin \
  /var/lib/hora-admin /var/log/hora-admin
```

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
TimeoutStopSec=30

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/hora-admin /var/log/hora-admin /etc/hora-admin/secrets
UMask=0077

[Install]
WantedBy=multi-user.target
```

Ajusta `ReadWritePaths` si usas rutas distintas. `ProtectHome=true` impide leer
`~/.ssh` del usuario de servicio: si usas `known_hosts` en el home, monta una
ruta propia o elimina esa directiva de forma consciente.

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
   (`TZ`/`TIME`), valor, semántica UTC/local, política NTP y opciones de RTC.
   En modo temporal se sube un `.txt` con una contraseña por línea.
4. **Vista previa (opcional)** → inspecciona todos los hosts sin cambiar nada
   y muestra su estado actual, el plan y si haría falta cambiar algo.
5. **Ejecución** → aplica el cambio en todos los hosts (best-effort), verifica
   el estado final y registra un lote con el resultado de cada host.
6. **Progreso** → la ejecución redirige a `/progress/<task_id>` con SSE y, al
   terminar, enlaza al detalle del lote en `/batches/<batch_id>`.
7. **Historial** → `/history` muestra los lotes con contadores, operador,
   duración y enlace al detalle por host.

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

Elementos a guardar:

- SQLite: `/var/lib/hora-admin/hora_admin.db` (o `instance/hora_admin.db` en
  desarrollo), junto con `-wal` y `-shm` si copias en caliente.
- Vault cifrado: `/etc/hora-admin/secrets/passwords_vault.yml`.
- `.env` (contiene la clave de sesión): trátalo como secreto.

Copia consistente con el servicio detenido:

```bash
sudo systemctl stop hora-admin
sudo install -d -m 0700 /var/backups/hora-admin
sudo cp -a /var/lib/hora-admin/hora_admin.db* /var/backups/hora-admin/
sudo cp -a /etc/hora-admin/secrets/passwords_vault.yml /var/backups/hora-admin/
sudo cp -a /opt/hora-admin/.env /var/backups/hora-admin/
sudo systemctl start hora-admin
```

En caliente, usa la API de backup de SQLite:

```bash
sudo sqlite3 /var/lib/hora-admin/hora_admin.db \
  ".backup '/var/backups/hora-admin/hora_admin-$(date +%F).db'"
```

Prueba la restauración periódicamente. El procedimiento completo está en
[docs/OPERACION.md](docs/OPERACION.md#6-copias-de-seguridad-y-restauración).

---

## 8. Seguridad y limitaciones conocidas (P0-P4)

- `DRY_RUN=1` por defecto: no se toca ningún equipo hasta cambiarlo. **La
  interfaz no muestra un banner de simulación y el lote no guarda `dry_run`**;
  comprueba `HORA_ADMIN_DRY_RUN` antes de dar un cambio por real.
- `DEBUG=0` por defecto; nunca actives el depurador en producción.
- Login con hash Werkzeug y sesión protegida por Flask-Login.
- **CSRF:** todos los `POST/PUT/PATCH/DELETE` exigen un token de sesión con
  caducidad (`HORA_ADMIN_CSRF_TIME_LIMIT`, 8 h por defecto). No desactives
  `HORA_ADMIN_CSRF_ENABLED` en producción.
- **Rate limiting:** login (10/5 min), ejecuciones (10/min) y operaciones de
  vault (20/min) por usuario/IP. Es en memoria y por proceso; con varios
  workers cada uno lleva su propia ventana. Para escalar, usa Redis.
- **Límites de red:** se rechazan rangos CIDR con prefijo menor que
  `HORA_ADMIN_MIN_CIDR_PREFIX` o con más de `HORA_ADMIN_MAX_HOSTS` hosts antes
  de enumerarlos. La validación usa `ipaddress` (rechaza octetos inválidos).
- **Cabeceras:** CSP con nonce (sin `unsafe-inline` para scripts),
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy` y HSTS opcional.
- **Proxy inverso:** activa `HORA_ADMIN_TRUST_PROXY=1` solo si nginx/apache es
  de confianza; así se leen `X-Forwarded-For/Proto` para la IP real y HTTPS.
- Las contraseñas candidatas se cifran con `ansible-vault`; SQLite solo guarda
  hashes cortos para la interfaz. Los hashes completos son SHA-256 sin sal.
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
  (`PRAGMA user_version`). Esquema actual: **v4**. Haz copia de seguridad antes
  de actualizar.
- El progreso SSE y las tareas viven en memoria: usa un worker o añade un
  almacén compartido antes de escalar. El worker `gthread` con `timeout=330`
  evita que el SSE bloquee o pierda el worker, pero no persiste las tareas.
- **Lotes:** cada ejecución crea un registro en `execution_batches` y uno por
  host en `execution_hosts` (estado previo/posterior, error y duración). El
  historial principal es el lote; `executions` se conserva como registro
  heredado por host.
- **Best-effort:** un host inaccesible o un playbook fallido no detiene el
  resto del lote. El estado global será `partial` si hubo éxitos y fallos.
- **Verificación:** el lote solo cuenta un host como éxito si el playbook
  confirma el estado final (`HORA_VERIFIED`). La tolerancia de hora se ajusta
  con `HORA_ADMIN_TIME_TOLERANCE`.
- **Idempotencia:** no se cambia la zona horaria ni la hora si ya coinciden;
  la casilla “Forzar” desactiva esa comprobación de forma consciente.
- **NTP:** la política por defecto es `keep` (no tocar). `off` deja NTP
  desactivado, `restore` lo desactiva/reactiva alrededor del cambio y
  `configure` escribe servidores NTP en chrony o systemd-timesyncd.
  - `TIME` + `keep` puede ser revertido por NTP: usa `off`, `restore` o
    `configure` para cambios de hora.
  - `restore` reactiva NTP aunque estuviera desactivado antes; no es una
    restauración exacta del estado previo.
- **Sin reconciliación automática:** si el proceso muere, el lote puede quedar
  `running`. Ciérralo manualmente
  ([docs/OPERACION.md](docs/OPERACION.md#41-el-lote-se-queda-en-running)).
- **Sin retención automática:** `HORA_ADMIN_HISTORY_DAYS` y
  `HORA_ADMIN_JOB_RETENTION_HOURS` están definidas pero no se aplican.
- **Sin rollback automático:** el estado previo se guarda, pero revertir es
  manual ([docs/OPERACION.md](docs/OPERACION.md#5-rollback)).
- **Sin RBAC/MFA/allowlist:** un único rol administrador y sin lista blanca de
  redes objetivo.

---

## 9. Verificación post-despliegue

```bash
# 1. Login disponible
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/login

# 2. Redirección de ruta protegida
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://127.0.0.1:5000/

# 3. Esquema y tablas P4
sqlite3 /var/lib/hora-admin/hora_admin.db \
  "PRAGMA user_version; SELECT name FROM sqlite_master
    WHERE type='table' AND name IN ('execution_batches','execution_hosts');"

# 4. El usuario del servicio puede leer/escribir sus rutas
sudo -u hora-admin test -r /opt/hora-admin/.env && echo ".env OK"
sudo -u hora-admin test -w /var/lib/hora-admin && echo "datos OK"
sudo -u hora-admin test -r /etc/hora-admin/secrets/passwords_vault.yml \
  && echo "vault OK"

# 5. Tests
source .venv/bin/activate
python -m pytest -q
```

---

## 10. Troubleshooting

| Síntoma | Causa probable | Acción |
|---|---|---|
| `No hay permisos para crear /var/lib/hora-admin` | Servicio con otro usuario o rutas sin `chown` | Alinea propietario y `User=`; revisa §2 y §5 |
| Login responde 500 | SQLite no escribible o `SECRET_KEY` inválida | Revisa permisos y `journalctl -u hora-admin` |
| `sshpass` no encontrado | Falta el paquete | `sudo apt install sshpass` |
| Primer SSH falla con host key | Host no está en `known_hosts` | `ssh-keyscan -H <host>` (verifica huella) |
| Todos los hosts fallan por autenticación | Contraseña maestra/vault incorrectos o usuario sin sudo | Prueba la vista previa y revisa el error por host |
| `HORA_VERIFIED=false` | NTP/VM revirtió la hora o no hay permisos | Revisa política NTP y `before_state`/`after_state` |
| `TIME` no se mantiene | Política `keep` con NTP activo | Usa `off`, `restore` o `configure` |
| Lote queda `running` | Proceso reiniciado o timeout de Gunicorn | Se marca `error` al arrancar; fuerza con `reconcile-batches` |
| SSE devuelve 404 | `_tasks` perdido tras reinicio | Abre `/batches/<batch_id>` o usa `/api/task-status` |
| Progreso bloqueado | SSE en worker sin hilos | Usa `gthread` (por defecto) o consulta por polling |
| `database is locked` | Escritura concurrente o copia en caliente | WAL y `busy_timeout` ya están activos; evita `VACUUM` en caliente |
| Vault no descifra | Contraseña maestra cambiada o fichero corrupto | Restaura la copia o usa `ansible-vault rekey` |
| `hwclock` falla | Contenedor o VM sin RTC accesible | No marques `sync_rtc`; documenta la limitación |

Para diagnóstico detallado, consulta
[docs/OPERACION.md](docs/OPERACION.md#4-diagnóstico-de-fallos).
