# Guía de despliegue — Ansible Control Center

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
>
> ¿Prefieres contenedores? Consulta [docs/DOCKER.md](docs/DOCKER.md) para el
> despliegue con Docker Compose y HTTPS.

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
3. Crea `/etc/ansible-control-center/secrets` (0700), `/var/lib/ansible-control-center` y
   `/var/log/ansible-control-center`.
4. Genera `.env` con una `ACC_SECRET_KEY` aleatoria (0600).
5. Ejecuta `flask --app app init-db` (esquema v6, migración no destructiva).
6. **No crea el usuario administrador**: ejecuta después
   `flask --app app init-admin`.

> **Usuario de servicio:** `install.sh` crea los directorios y `.env` para el
> usuario que lo invoca. Si el unit de systemd usa `User=ansible-control-center`, crea ese
> usuario antes y ejecuta la instalación con él, o ajusta la propiedad:
>
> ```bash
> sudo useradd --system --home /opt/ansible-control-center --shell /usr/sbin/nologin ansible-control-center
> sudo chown -R ansible-control-center:ansible-control-center /opt/ansible-control-center /etc/ansible-control-center \
>   /var/lib/ansible-control-center /var/log/ansible-control-center
> sudo chmod 600 /opt/ansible-control-center/.env
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

sudo install -d -m 0750 /etc/ansible-control-center
sudo install -d -m 0700 /etc/ansible-control-center/secrets
sudo install -d -m 0750 /var/lib/ansible-control-center /var/log/ansible-control-center
sudo chown -R "$USER:$USER" /etc/ansible-control-center /var/lib/ansible-control-center /var/log/ansible-control-center

cp .env.example .env
# Edita .env y define ACC_SECRET_KEY con: openssl rand -hex 32
chmod 600 .env

flask --app app init-db
flask --app app init-admin
```

---

## 3. Configuración

### Variables principales

| Variable | Por defecto | Descripción |
|---|---|---|
| `ACC_SECRET_KEY` | generada | Clave de sesión Flask |
| `ACC_SECRETS_DIR` | `/etc/ansible-control-center/secrets` | Vault y secretos |
| `ACC_DATA_DIR` | `/var/lib/ansible-control-center` | SQLite y clave local |
| `ACC_LOG_DIR` | `/var/log/ansible-control-center` | Logs |
| `ACC_DRY_RUN` | `1` | Modo simulación inicial (luego se controla desde la interfaz) |
| `ACC_HOST_KEY_CHECKING` | `1` | Verificación estricta de host SSH |
| `ACC_SSH_HOST_KEYS_AUTO` | `0` | Rellenar `known_hosts` con `ssh-keyscan` (debilita TOFU; solo redes de confianza) |
| `ACC_KNOWN_HOSTS_FILE` | vacío | Ruta alternativa de `known_hosts`; vacío usa `$HOME/.ssh/known_hosts` |
| `ACC_DEBUG` | `0` | Depurador Flask |
| `ACC_COOKIE_SECURE` | `0` | `1` detrás de HTTPS |
| `ACC_TRUST_PROXY` | `0` | `1` solo tras proxy de confianza |
| `ACC_HSTS` | según cookie | Cabecera HSTS |
| `ACC_HSTS_MAX_AGE` | `31536000` | `max-age` de HSTS |
| `ACC_SESSION_HOURS` | `8` | Duración de sesión (1–72 h) |
| `ACC_MAX_UPLOAD_BYTES` | `2097152` | Tamaño máximo de subida |
| `ACC_CSRF_ENABLED` | `1` | Protección CSRF |
| `ACC_CSRF_TIME_LIMIT` | `28800` | Caducidad del token CSRF (s) |
| `ACC_RATE_LIMIT_ENABLED` | `1` | Rate limiting global |
| `ACC_LOGIN_RATE_LIMIT` | `10` | Intentos de login por ventana |
| `ACC_LOGIN_RATE_WINDOW` | `300` | Ventana de login (s) |
| `ACC_EXEC_RATE_LIMIT` | `10` | Ejecuciones por ventana |
| `ACC_EXEC_RATE_WINDOW` | `60` | Ventana de ejecución (s) |
| `ACC_VAULT_RATE_LIMIT` | `20` | Operaciones de vault por ventana |
| `ACC_VAULT_RATE_WINDOW` | `60` | Ventana de vault (s) |
| `ACC_SECURITY_HEADERS` | `1` | Cabeceras de seguridad |
| `ACC_CSP_ENABLED` | `1` | CSP con nonce |
| `ACC_ADMIN_PASSWORD_MIN_LENGTH` | `12` | Longitud mínima del admin |
| `ACC_MAX_HOSTS` | `256` | Hosts máximos por ejecución |
| `ACC_MIN_CIDR_PREFIX` | `22` | Prefijo IPv4 mínimo |
| `ACC_ALLOWED_NETWORKS` | vacío | Allowlist de redes. **Obligatoria en producción** (vacío solo en desarrollo) |
| `ACC_ALLOW_LOOPBACK` | `1` en desarrollo | Permite loopback fuera de producción; en producción siempre se bloquea |
| `ACC_MAX_PASSWORDS` | `200` | Máximo de candidatas |
| `ACC_MAX_PASSWORD_LENGTH` | `512` | Longitud máxima de candidata |
| `ACC_PING_TIMEOUT` | `30` | Timeout de ping (s) |
| `ACC_PLAYBOOK_TIMEOUT` | `300` | Timeout de playbook (s) |
| `ACC_CONNECT_TIMEOUT` | `10` | Timeout de conexión SSH (s) |
| `ACC_TIME_TOLERANCE` | `2` | Tolerancia de verificación de hora (s) |
| `ACC_TIME_REQUIRE_NTP_OFF` | `1` | Bloquea `TIME` + NTP `keep` en ejecución |
| `ACC_NTP_SETTLE_SECONDS` | `3` | Espera tras reactivar NTP antes de verificar (s) |
| `ACC_MAX_PASSWORD_ATTEMPTS` | `3` | Candidatas máximas probadas por host |
| `ACC_BATCH_TIME_BUDGET` | `3600` | Presupuesto de tiempo del lote (s) |
| `ACC_JOB_WORKERS` | `2` | Lotes concurrentes |
| `ACC_JOB_QUEUE_SIZE` | `50` | Tamaño máximo de la cola |
| `ACC_JOB_SHUTDOWN_GRACE` | `10` | Espera de apagado ordenado (s) |
| `ACC_MAX_MEMORY_TASKS` | `200` | Instantáneas de tarea en memoria |
| `ACC_MAX_TASK_STATE_ROWS` | `500` | Filas máximas de `task_state` |
| `ACC_HISTORY_DAYS` | `180` | Retención del historial (días) |
| `ACC_AUTH_EVENT_DAYS` | `365` | Retención de auditoría de acceso (días) |
| `ACC_LOGIN_LOCKOUT_THRESHOLD` | `5` | Fallos antes de bloquear la cuenta |
| `ACC_LOGIN_LOCKOUT_MINUTES` | `15` | Duración del bloqueo (min) |
| `ACC_DEFAULT_NTP_SERVERS` | `pool.ntp.org` | Servidores NTP por defecto |
| `ACC_MAX_NTP_SERVERS` | `8` | Máximo de servidores NTP |
| `ACC_LOG_LEVEL` | `INFO` | Nivel de log |
| `ACC_LOG_TO_FILE` | `1` | Log rotativo en fichero |
| `ACC_LOG_MAX_BYTES` | `5242880` | Tamaño máximo por fichero |
| `ACC_LOG_BACKUPS` | `5` | Número de backups |
| `ACC_ANSIBLE_BIN` | autodetectado | Ruta de `ansible` |
| `ACC_ANSIBLE_PLAYBOOK_BIN` | autodetectado | Ruta de `ansible-playbook` |
| `ACC_ANSIBLE_VAULT_BIN` | autodetectado | Ruta de `ansible-vault` |
| `ACC_ANSIBLE_GALAXY_BIN` | autodetectado | Ruta de `ansible-galaxy` |
| `ACC_HISTORY_DAYS` | `180` | Retención del historial de lotes (purga periódica) |
| `ACC_AUTH_EVENT_DAYS` | `365` | Retención de eventos de autenticación |
| `ACC_MAX_TASK_STATE_ROWS` | `500` | Filas máximas de `task_state` |
| `ACC_JOB_RETENTION_HOURS` | `24` | Retención de instantáneas de tareas |

Variables específicas de Gunicorn (`gunicorn.conf.py`, no de `config.py`):

| Variable | Por defecto | Descripción |
|---|---|---|
| `ACC_BIND` | `127.0.0.1:5000` | Dirección de escucha |
| `ACC_WORKERS` | `1` | Workers (el estado SSE es por proceso) |
| `ACC_GUNICORN_WORKER_CLASS` | `gthread` | Clase de worker |
| `ACC_GUNICORN_THREADS` | `4` | Hilos por worker |
| `ACC_GUNICORN_TIMEOUT` | `330` | Timeout del worker (s) |
| `ACC_GUNICORN_GRACEFUL_TIMEOUT` | `30` | Parada ordenada (s) |

Para desarrollo sin `sudo`, define rutas locales:

```env
ACC_SECRETS_DIR=./instance/secrets
ACC_DATA_DIR=./instance/data
ACC_LOG_DIR=./instance/logs
```

Si no se definen y el proceso no puede escribir en `/etc` o `/var`, la app
usa automáticamente `instance/` y muestra un aviso en el log.

### Usuario administrador

```bash
source .venv/bin/activate
flask --app app init-admin
# Usuario por defecto: admin (rol admin)
# Puedes indicar otro usuario y rol:
#   flask --app app init-admin --username operador --role operator
# Roles: viewer (solo lectura), operator (vista previa/ejecución/vault),
# admin (todo, incluida /audit).
```

La contraseña se guarda como hash Werkzeug en SQLite. Para cambiarla, vuelve a
ejecutar el comando.

### Esquema de datos y migración

El esquema actual es `PRAGMA user_version = 6`. `flask --app app init-db`
aplica migraciones **no destructivas** al arrancar y es idempotente. Si la base
tiene una versión **más nueva** que la soportada, el arranque se aborta para no
corromperla.

Tablas principales desde P4:

- `execution_batches`: un registro por operación (lote) con `batch_id`,
  operador, objetivo, acción, valor, `time_mode`, `ntp_policy`, `time_zone`,
  `local_fold`, `admin_user`, `password_origin`, `ntp_servers`, `sync_rtc`,
  `force`, estado global, contadores y fechas.
- `execution_hosts`: un resultado por host con estado previo/posterior, error,
  duración y hash corto de la contraseña usada.
- `users`: usuarios con `role` (`viewer`/`operator`/`admin`), contador de
  fallos y bloqueo.
- `auth_events`: auditoría de login correcto/fallido, bloqueo y accesos a
  cuentas bloqueadas.
- `task_state`: instantáneas de progreso; se recorta a
  `ACC_MAX_TASK_STATE_ROWS` y se purga según retención.
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
ACC_HOST=0.0.0.0 ACC_PORT=5000 python app.py
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
> memoria: no aumentes `ACC_WORKERS` sin moverlo antes a un almacén
> compartido.
>
> Si el proceso se reinicia, `_tasks` se pierde. Al arrancar, los lotes que
> quedaron `running` se marcan automáticamente como `error`
> (`flask --app app reconcile-batches` permite forzarlo).

### Servicio systemd

Antes de instalar el unit, crea el usuario de servicio y alinea la propiedad de
las rutas (ver §2):

```bash
sudo useradd --system --home /opt/ansible-control-center --shell /usr/sbin/nologin ansible-control-center
sudo chown -R ansible-control-center:ansible-control-center /opt/ansible-control-center /etc/ansible-control-center \
  /var/lib/ansible-control-center /var/log/ansible-control-center
```

`/etc/systemd/system/ansible-control-center.service`:

```ini
[Unit]
Description=Ansible Control Center
After=network.target

[Service]
User=ansible-control-center
Group=ansible-control-center
WorkingDirectory=/opt/ansible-control-center
EnvironmentFile=/opt/ansible-control-center/.env
ExecStart=/opt/ansible-control-center/.venv/bin/gunicorn -c /opt/ansible-control-center/gunicorn.conf.py app:app
Restart=always
RestartSec=5
TimeoutStopSec=30

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/ansible-control-center /var/log/ansible-control-center /etc/ansible-control-center/secrets
UMask=0077

[Install]
WantedBy=multi-user.target
```

Ajusta `ReadWritePaths` si usas rutas distintas. `ProtectHome=true` impide leer
`~/.ssh` del usuario de servicio: si usas `known_hosts` en el home, monta una
ruta propia o elimina esa directiva de forma consciente.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ansible-control-center
sudo systemctl status ansible-control-center
sudo journalctl -u ansible-control-center -f
```

### Proxy inverso con Nginx

```nginx
server {
    listen 443 ssl;
    server_name ansible-control-center.example.com;

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

Con HTTPS activa `ACC_COOKIE_SECURE=1` en `.env`.

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

Con `ACC_DRY_RUN=1` el playbook no se ejecuta: se simula el resultado.
Para ejecutar de verdad, un administrador desactiva el interruptor
**Simulación** de la barra superior; el estado se guarda en SQLite y se
mantiene tras reiniciar. `ACC_DRY_RUN` solo actúa como valor inicial la
primera vez.

### Vault

- El vault es un fichero cifrado con `ansible-vault` en
  `/etc/ansible-control-center/secrets/passwords_vault.yml` (permisos `0600`).
- Contiene una contraseña por línea; las líneas vacías y las que empiezan por
  `#` se ignoran.
- La contraseña maestra se pide en cada operación y no se guarda.
- Al borrar una contraseña desde la interfaz se actualiza también el vault.

```bash
# Inspección manual (pide la contraseña maestra)
ansible-vault view /etc/ansible-control-center/secrets/passwords_vault.yml
```

---

## 7. Copias de seguridad

Elementos a guardar:

- SQLite: `/var/lib/ansible-control-center/acc.db` (o `instance/acc.db` en
  desarrollo), junto con `-wal` y `-shm` si copias en caliente.
- Vault cifrado: `/etc/ansible-control-center/secrets/passwords_vault.yml`.
- `.env` (contiene la clave de sesión): trátalo como secreto.

Copia consistente con el servicio detenido:

```bash
sudo systemctl stop ansible-control-center
sudo install -d -m 0700 /var/backups/ansible-control-center
sudo cp -a /var/lib/ansible-control-center/acc.db* /var/backups/ansible-control-center/
sudo cp -a /etc/ansible-control-center/secrets/passwords_vault.yml /var/backups/ansible-control-center/
sudo cp -a /opt/ansible-control-center/.env /var/backups/ansible-control-center/
sudo systemctl start ansible-control-center
```

En caliente, usa la API de backup de SQLite:

```bash
sudo sqlite3 /var/lib/ansible-control-center/acc.db \
  ".backup '/var/backups/ansible-control-center/acc-$(date +%F).db'"
```

Prueba la restauración periódicamente. El procedimiento completo está en
[docs/OPERACION.md](docs/OPERACION.md#6-copias-de-seguridad-y-restauración).

---

## 8. Seguridad y limitaciones conocidas (P0-P4.2)

- `DRY_RUN=1` por defecto: no se aplica ningún cambio hasta cambiarlo. La
  interfaz muestra un banner, el lote guarda `dry_run` y el historial separa
  los hosts simulados de los reales. **La vista previa sí inspecciona los
  hosts de verdad** (playbook de solo lectura), incluso en simulación, para no
  ocultar hosts inalcanzables o credenciales inválidas.
- `DEBUG=0` por defecto; nunca actives el depurador en producción.
- Login con hash Werkzeug y sesión protegida por Flask-Login.
- **CSRF:** todos los `POST/PUT/PATCH/DELETE` exigen un token de sesión con
  caducidad (`ACC_CSRF_TIME_LIMIT`, 8 h por defecto). No desactives
  `ACC_CSRF_ENABLED` en producción.
- **Rate limiting:** login (10/5 min), ejecuciones (10/min) y operaciones de
  vault (20/min) por usuario/IP. Es en memoria y por proceso; con varios
  workers cada uno lleva su propia ventana. Para escalar, usa Redis.
- **Límites de red:** se rechazan rangos CIDR con prefijo menor que
  `ACC_MIN_CIDR_PREFIX` o con más de `ACC_MAX_HOSTS` hosts antes
  de enumerarlos. La validación usa `ipaddress` (rechaza octetos inválidos).
- **Cabeceras:** CSP con nonce (sin `unsafe-inline` para scripts),
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy` y HSTS opcional.
- **Proxy inverso:** activa `ACC_TRUST_PROXY=1` solo si nginx/apache es
  de confianza; así se leen `X-Forwarded-For/Proto` para la IP real y HTTPS.
- Las contraseñas candidatas se cifran con `ansible-vault`; SQLite solo guarda
  hashes cortos para la interfaz. Los hashes completos son HMAC-SHA256 con un
  pepper persistido en `DATA_DIR` (compatibilidad de lectura con SHA-256
  heredados).
- La contraseña de conexión de Ansible se pasa en un fichero temporal `0600`
  mediante `--extra-vars @fichero`; nunca va en la línea de comandos ni en los
  logs. El fichero se borra tras cada intento.
- Por defecto se verifica la clave SSH del host. Si los equipos no están en
  `known_hosts`, añádelos con `ssh-keyscan` o define
  `ACC_HOST_KEY_CHECKING=0` de forma consciente (menos seguro).
- El modo temporal `.txt` procesa el archivo en memoria; no se guarda en disco.
- SQLite en modo WAL: al copiar la base de datos en caliente, copia también
  `acc.db-wal` y `acc.db-shm`, o detén el servicio antes.
- Las migraciones son no destructivas y se aplican al arrancar
  (`PRAGMA user_version`). Esquema actual: **v10**. Haz copia de seguridad antes
  de actualizar. Una base creada por una versión más nueva aborta el arranque
  en lugar de degradarse.
- El SSE y la consola se sirven desde memoria, con instantáneas en SQLite para
  sobrevivir a reinicios. Usa un worker o añade un almacén compartido antes de
  escalar. El worker `gthread` con `timeout=330` evita bloqueos.
- **Lotes:** cada ejecución crea un registro en `execution_batches` y uno por
  host en `execution_hosts` (estado previo/posterior, error y duración). El
  historial principal es el lote; `executions` se conserva como registro
  heredado por host.
- **Best-effort acotado:** un host inaccesible o un playbook fallido no
  detiene el resto del lote. Se prueban como máximo
  `ACC_MAX_PASSWORD_ATTEMPTS` candidatas por host (3 por defecto) y el
  lote completo tiene un presupuesto de `ACC_BATCH_TIME_BUDGET` segundos
  (3600 por defecto); los hosts pendientes quedan como `skipped`. El estado
  global será `partial` si hubo éxitos y fallos.
- **Verificación en dos fases:** el lote solo cuenta un host como éxito si el
  playbook confirma el estado final (`HORA_VERIFIED`) antes y después de
  reactivar NTP. La tolerancia por defecto es de 2 s
  (`ACC_TIME_TOLERANCE`).
- **Idempotencia:** no se cambia la zona horaria ni la hora si ya coinciden;
  la casilla “Forzar” desactiva esa comprobación de forma consciente.
- **NTP:** la política por defecto es `keep` (no tocar). `off` deja NTP
  desactivado, `restore` lo desactiva/reactiva alrededor del cambio y
  `configure` escribe servidores NTP en chrony o systemd-timesyncd.
  - `TIME` + `keep` puede ser revertido por NTP: en ejecución se rechaza por
    defecto (`ACC_TIME_REQUIRE_NTP_OFF=1`); usa `off`, `restore` o
    `configure`. La vista previa solo avisa.
  - `restore` deja NTP como estaba: solo lo reactiva si estaba activo antes.
  - Tras reactivar NTP se espera `ACC_NTP_SETTLE_SECONDS` antes de la
    verificación final para detectar reversiones.
- **Cola y cancelación:** los lotes se encolan con concurrencia acotada
  (`ACC_JOB_WORKERS`, `ACC_JOB_QUEUE_SIZE`). Si la cola está
  llena se devuelve un error claro; cada lote se puede cancelar y el proceso
  espera `ACC_JOB_SHUTDOWN_GRACE` al apagarse.
- **Reconciliación al arrancar:** los lotes `running` se marcan como `error` y
  las tareas incompletas se cierran; también existe
  `flask --app app reconcile-batches`.
- **Retención:** instantáneas de tareas
  (`ACC_JOB_RETENTION_HOURS`), historial de lotes
  (`ACC_HISTORY_DAYS`), eventos de acceso
  (`ACC_AUTH_EVENT_DAYS`) y recorte de `task_state`
  (`ACC_MAX_TASK_STATE_ROWS`).
- **Sin rollback automático:** el estado previo se guarda, pero revertir es
  manual ([docs/OPERACION.md](docs/OPERACION.md#5-rollback)).
- **RBAC sin MFA:** roles `viewer`/`operator`/`admin`, bloqueo de cuenta y
  auditoría en `auth_events`; la autenticación sigue siendo solo
  usuario/contraseña. La allowlist de red es global.

---

## 9. Verificación post-despliegue

```bash
# 1. Login disponible
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/login

# 2. Redirección de ruta protegida
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" http://127.0.0.1:5000/

# 3. Esquema y tablas P4
sqlite3 /var/lib/ansible-control-center/acc.db \
  "PRAGMA user_version; SELECT name FROM sqlite_master
    WHERE type='table' AND name IN ('execution_batches','execution_hosts');"

# 4. El usuario del servicio puede leer/escribir sus rutas
sudo -u ansible-control-center test -r /opt/ansible-control-center/.env && echo ".env OK"
sudo -u ansible-control-center test -w /var/lib/ansible-control-center && echo "datos OK"
sudo -u ansible-control-center test -r /etc/ansible-control-center/secrets/passwords_vault.yml \
  && echo "vault OK"

# 5. Tests
source .venv/bin/activate
python -m pytest -q
```

---

## 10. Troubleshooting

| Síntoma | Causa probable | Acción |
|---|---|---|
| `No hay permisos para crear /var/lib/ansible-control-center` | Servicio con otro usuario o rutas sin `chown` | Alinea propietario y `User=`; revisa §2 y §5 |
| Login responde 500 | SQLite no escribible o `SECRET_KEY` inválida | Revisa permisos y `journalctl -u ansible-control-center` |
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
