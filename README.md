# Hora Admin

Aplicación web Flask + Ansible para configurar **hora, zona horaria, NTP y RTC**
en grupos de equipos Ubuntu. Incluye autenticación, vault cifrado con
`ansible-vault`, ejecución best-effort en todos los hosts, vista previa,
verificación posterior, idempotencia, lotes auditables e historial en SQLite.

> **Uso responsable:** la aplicación prueba credenciales contra hosts y puede
> cambiar su reloj. Úsala únicamente sobre equipos para los que tengas
> autorización explícita.

---

## Estado actual

Fases **P0–P4, P4.1 y P4.2 completadas**. Suite de pruebas: **88 tests**.
Incluye empaquetado Docker con Nginx y TLS.

| Fase | Contenido |
|---|---|
| P0 | Estructura unificada, login, sesión, errores, SSE, arranque en desarrollo |
| P1 | SQLite centralizado, migraciones versionadas, WAL, concurrencia |
| P2 | Vault real, Ansible real, inyección segura de credenciales, historial |
| P3 | CSRF, rate limiting, límites CIDR/hosts, cabeceras de seguridad |
| P4 | Grupo completo, best-effort, lotes, vista previa, verificación, idempotencia, UTC/local, política NTP |
| P4.1 | DRY RUN visible y persistido, `restore` fiel al estado previo, aviso `TIME`+`keep`, JSON sin caché |
| P4.2 | Vista previa real en simulación, intentos y tiempo acotados, hostnames, verificación en ruta legacy |

---

## Características

### Operación

- **Objetivo único o CIDR**: IP, hostname resuelto o rango CIDR acotado.
- **Todos los hosts**: la operación recorre el grupo completo; un fallo no
  detiene el resto (best-effort).
- **Reintento de contraseñas acotado**: si una contraseña autentica pero el
  playbook falla, se prueban las siguientes candidatas hasta
  `HORA_ADMIN_MAX_PASSWORD_ATTEMPTS` (3 por defecto).
- **Vista previa**: inspecciona cada host sin cambiar nada y muestra su estado
  actual, el plan y si haría falta cambiar algo. La inspección es real aunque
  `DRY_RUN` esté activo: es de solo lectura y evita ocultar hosts caídos o
  credenciales inválidas.
- **Presupuesto de tiempo por lote**: al agotarse
  `HORA_ADMIN_BATCH_TIME_BUDGET` (1 h por defecto), los hosts pendientes se
  registran como omitidos en vez de dejar el lote abierto indefinidamente.
- **Consola en vivo**: muestra la salida de `ansible-playbook` línea a línea
  durante la ejecución, con auto-scroll, limpieza, descarga y redacción de
  secretos conocidos.
- **Verificación posterior**: `timedatectl show`, `date` y `hwclock`; el host
  solo cuenta como éxito si el estado final coincide con el objetivo.
- **Idempotencia**: no cambia zona horaria ni hora si ya son correctas, salvo
  que se marque “Forzar”.
- **Semántica explícita UTC/local** para la hora exacta.
- **Política NTP explícita**: `keep`, `off`, `restore` o `configure`;
  `restore` devuelve NTP a su estado previo.
- **RTC opcional**: `hwclock --systohc`.
- **Sesiones acotadas**: cookie permanente de 8 h por defecto, “recordarme”
  limitado a 7 días y cookies `HttpOnly`/`SameSite=Lax` (y `Secure` con
  `HORA_ADMIN_COOKIE_SECURE=1`).
- **Hashes con pepper**: los hashes de contraseñas candidatas usan
  HMAC-SHA256 con un pepper persistido en `DATA_DIR`, con compatibilidad de
  lectura para hashes heredados.
- **Reconciliación de lotes huérfanos**: al arrancar, los lotes `running` se
  marcan como `error`; también existe `flask --app app reconcile-batches`.
- **Confirmación e idempotencia**: la ejecución real pide confirmación con
  resumen y un UUID de operación evita lotes duplicados por doble clic.
- **DRY RUN** activo por defecto (`HORA_ADMIN_DRY_RUN=1`), visible en el panel,
  la página de progreso, el detalle del lote y el historial, y persistido en
  cada lote.

### Seguridad

- Login con Flask-Login y hash Werkzeug.
- CSRF en todos los métodos mutables, con caducidad configurable.
- Rate limiting en login, ejecuciones y operaciones del vault.
- Límites de CIDR/hosts antes de enumerar el rango.
- CSP con nonce, `X-Frame-Options`, `nosniff`, `Referrer-Policy`,
  `Permissions-Policy`, HSTS opcional y `Cache-Control: no-store`.
- Contraseñas candidatas cifradas con `ansible-vault`; la contraseña maestra
  nunca se persiste ni se registra.
- Credenciales de Ansible en ficheros temporales `0600` mediante
  `--extra-vars @fichero`, nunca en la línea de comandos.
- Verificación estricta de clave SSH (`known_hosts`) por defecto.
- Modo temporal `.txt`: el archivo se procesa en memoria y no se guarda.

### Registro y auditoría

- **Lote** (`execution_batches`): `batch_id`, operador, objetivo, acción,
  valor, semántica de hora, política NTP, estado global, contadores y duración.
- **Resultado por host** (`execution_hosts`): resultado, error, duración,
  estado previo, estado posterior, hash corto de la contraseña usada y origen.
- **Historial** (`/history`) con lotes y detalle por host
  (`/batches/<batch_id>`).
- Se conserva el registro heredado `executions` por compatibilidad.

### Interfaz

- Panel de configuración con vista previa y ejecución.
- Progreso en tiempo real por SSE con **consola de Ansible en vivo**, resumen
  de hitos y enlace al detalle del lote.
- Detalle de lote con tabla por host y estados antes/después.
- Gestión del vault: alta manual, importación `.txt`/`.yml`, borrado.
- Historial de lotes y estadísticas.

---

## Arquitectura

```
app.py                     Rutas, validación, tareas SSE y CLI
config.py                  Configuración central y rutas con fallback
gunicorn.conf.py           Configuración de Gunicorn
Dockerfile                 Imagen de la aplicación (Gunicorn + Tini)
docker-compose.yml         App + Nginx con TLS
docker/                    Entrypoint, Nginx y empaquetado para otra PC
certs/                     Certificado TLS (autofirmado o real)
models/
  db.py                    Conexiones SQLite, WAL, busy_timeout y migraciones
  user.py                  Usuarios administradores
  password.py              Hashes de contraseñas candidatas
  execution.py             Historial heredado por host
  batch.py                 Lotes y resultados por host (P4)
services/
  ansible_runner.py        inspect_host, apply_host y ejecución segura
  password_tester.py       Ping y prueba de contraseñas candidatas
  host_resolver.py         Validación y resolución de IP/CIDR
  vault_manager.py         Cifrado/descifrado con ansible-vault
  secure_temp.py           Ficheros y directorios temporales 0600/0700
  csrf.py                  Protección CSRF
  rate_limiter.py          Ventana deslizante en memoria
templates/                 Vistas Jinja2
ansible/
  cambiar_hora.yml         Playbook de cambio + verificación
  estado_hora.yml          Playbook de inspección (solo lectura)
  tasks/leer_estado.yml    Lectura de TZ, NTP, hora y RTC
tests/                     Pruebas pytest
docs/                      Documentación técnica y operativa
```

### Flujo de una ejecución

1. El formulario se valida en `_parse_operation_form`.
2. Se resuelve el objetivo con `resolve_target`.
3. Se descifra el vault o se usan las contraseñas temporales.
4. Se crea un lote en `execution_batches`.
5. Por cada host:
   - se prueba `ansible -m ping` con cada candidata;
   - si autentica, se ejecuta `cambiar_hora.yml` con `apply_host`;
   - si el playbook falla, se prueban las siguientes candidatas;
   - se guarda `execution_hosts` y el registro heredado `executions`.
6. `finish_batch` recalcula contadores y estado global.
7. El navegador sigue el progreso por SSE, muestra la salida de Ansible en
   vivo y enlaza al detalle del lote.

En **vista previa** se usa `estado_hora.yml` (`inspect_host`), se registra un
lote en estado `preview` y **nunca** se llama a `apply_host`.

---

## Requisitos

- Python 3.11+ (probado con 3.14).
- `ansible-core` (`ansible`, `ansible-playbook`, `ansible-vault`).
- `sshpass` en el servidor para SSH con contraseña.
- Gunicorn para producción.
- Equipos objetivo Ubuntu con `systemd`/`timedatectl`.

---

## Arranque rápido (desarrollo)

```bash
python3 -m venv .venv
source .venv/bin/activate

# Si tu equipo usa un mirror corporativo y no estás en esa red:
# PIP_CONFIG_FILE=/dev/null python -m pip install -r requirements.txt
python -m pip install -r requirements.txt

# Base de datos y usuario administrador
flask --app app init-db
flask --app app init-admin

# Servidor de desarrollo (solo 127.0.0.1 por defecto)
python app.py
```

Abre `http://127.0.0.1:5000` e inicia sesión con el usuario creado.

Si no hay ningún administrador, la pantalla de login lo indica y muestra el
comando `flask --app app init-admin`.

---

## Arranque rápido con Docker (HTTPS)

El despliegue publica solo **Nginx** (80/443); la app queda en una red interna.
El certificado es autofirmado y se genera en el primer arranque.

```bash
cp .env.docker.example .env.docker

# Genera una clave de sesión y edítala en .env.docker
openssl rand -hex 32

docker compose --env-file .env.docker up -d --build
curl -fk https://localhost/healthz
```

Para llevarlo a otra PC (con o sin acceso a Docker Hub) y arrancarlo allí sin
reconstruir nada:

```bash
./docker/export-images.sh
# genera dist/hora-admin-despliegue/ (LEEME.txt + DESPLIEGUE.md)

# en la PC de destino, dentro de esa carpeta:
docker load -i imagenes/hora-admin-images.tar
docker compose --env-file .env.docker up -d --no-build
```

Ajusta `TLS_CN`/`TLS_SAN` al nombre o IP real de acceso para evitar avisos del
navegador por el nombre del certificado. Guía completa, copias de seguridad,
certificado real y diagnóstico en [docs/DOCKER.md](docs/DOCKER.md).

---

## Configuración

Se lee de `.env` (opcional) o del entorno real. `install.sh` genera `.env` con
una `HORA_ADMIN_SECRET_KEY` aleatoria.

### Rutas y sesión

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_SECRET_KEY` | generada | Clave de sesión Flask |
| `HORA_ADMIN_SECRETS_DIR` | `/etc/hora-admin/secrets` | Secretos y vault |
| `HORA_ADMIN_DATA_DIR` | `/var/lib/hora-admin` | SQLite y clave de sesión |
| `HORA_ADMIN_LOG_DIR` | `/var/log/hora-admin` | Logs |
| `HORA_ADMIN_SESSION_HOURS` | `8` | Duración de sesión (1–72 h) |
| `HORA_ADMIN_JOB_RETENTION_HOURS` | `24` | Retención de instantáneas de tareas (mínimo 1 h) |
| `HORA_ADMIN_REMEMBER_DAYS` | `7` | Duración de la cookie “recordarme” (1–30 días) |
| `HORA_ADMIN_MAX_UPLOAD_BYTES` | `2097152` | Tamaño máximo de subida |
| `HORA_ADMIN_HOST` / `HORA_ADMIN_PORT` | `127.0.0.1` / `5000` | Servidor de desarrollo |

### Ejecución

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_DRY_RUN` | `1` | No ejecuta Ansible real |
| `HORA_ADMIN_HOST_KEY_CHECKING` | `1` | Verificación estricta de clave SSH |
| `HORA_ADMIN_MAX_HOSTS` | `256` | Máximo de hosts por ejecución |
| `HORA_ADMIN_MIN_CIDR_PREFIX` | `22` | Prefijo IPv4 mínimo aceptado |
| `HORA_ADMIN_ALLOWED_NETWORKS` | vacío | Allowlist de redes separadas por comas (vacío = sin restricción) |
| `HORA_ADMIN_MAX_PASSWORDS` | `200` | Máximo de contraseñas por vault/archivo |
| `HORA_ADMIN_MAX_PASSWORD_LENGTH` | `512` | Longitud máxima de una contraseña |
| `HORA_ADMIN_PING_TIMEOUT` | `30` | Timeout de `ansible -m ping` (s) |
| `HORA_ADMIN_PLAYBOOK_TIMEOUT` | `300` | Timeout de playbook (s) |
| `HORA_ADMIN_CONNECT_TIMEOUT` | `10` | Timeout de conexión SSH (s) |
| `HORA_ADMIN_TIME_TOLERANCE` | `120` | Desviación máxima al verificar hora (s) |
| `HORA_ADMIN_MAX_PASSWORD_ATTEMPTS` | `3` | Candidatas máximas probadas por host |
| `HORA_ADMIN_BATCH_TIME_BUDGET` | `3600` | Presupuesto de tiempo del lote (s) |
| `HORA_ADMIN_DEFAULT_NTP_SERVERS` | `pool.ntp.org` | Servidores NTP por defecto |
| `HORA_ADMIN_MAX_NTP_SERVERS` | `8` | Máximo de servidores NTP |

### Seguridad

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_DEBUG` | `0` | Depurador Flask (nunca en producción) |
| `HORA_ADMIN_COOKIE_SECURE` | `0` | Cookies solo por HTTPS |
| `HORA_ADMIN_PASSWORD_PEPPER` | generado | Pepper de los hashes de contraseñas (se persiste en `DATA_DIR`) |
| `HORA_ADMIN_TRUST_PROXY` | `0` | Leer `X-Forwarded-For/Proto` tras proxy |
| `HORA_ADMIN_HSTS` | según cookie | Cabecera HSTS |
| `HORA_ADMIN_HSTS_MAX_AGE` | `31536000` | `max-age` de HSTS |
| `HORA_ADMIN_SECURITY_HEADERS` | `1` | Cabeceras de seguridad |
| `HORA_ADMIN_CSP_ENABLED` | `1` | Content-Security-Policy con nonce |
| `HORA_ADMIN_CSRF_ENABLED` | `1` | Protección CSRF |
| `HORA_ADMIN_CSRF_TIME_LIMIT` | `28800` | Caducidad del token CSRF (s) |
| `HORA_ADMIN_RATE_LIMIT_ENABLED` | `1` | Rate limiting |
| `HORA_ADMIN_LOGIN_RATE_LIMIT` | `10` | Intentos de login por ventana |
| `HORA_ADMIN_LOGIN_RATE_WINDOW` | `300` | Ventana de login (s) |
| `HORA_ADMIN_EXEC_RATE_LIMIT` | `10` | Ejecuciones por ventana |
| `HORA_ADMIN_EXEC_RATE_WINDOW` | `60` | Ventana de ejecución (s) |
| `HORA_ADMIN_VAULT_RATE_LIMIT` | `20` | Operaciones de vault por ventana |
| `HORA_ADMIN_VAULT_RATE_WINDOW` | `60` | Ventana de vault (s) |
| `HORA_ADMIN_ADMIN_PASSWORD_MIN_LENGTH` | `12` | Longitud mínima del admin |
| `HORA_ADMIN_USERNAME` | `admin` | Usuario por defecto de `init-admin` |

### Logging

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_LOG_LEVEL` | `INFO` | Nivel de log |
| `HORA_ADMIN_LOG_TO_FILE` | `1` | Escribir en fichero rotativo |
| `HORA_ADMIN_LOG_MAX_BYTES` | `5242880` | Tamaño máximo por fichero |
| `HORA_ADMIN_LOG_BACKUPS` | `5` | Número de backups |

### Binarios de Ansible

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_ANSIBLE_BIN` | autodetectado | Ruta de `ansible` |
| `HORA_ADMIN_ANSIBLE_PLAYBOOK_BIN` | autodetectado | Ruta de `ansible-playbook` |
| `HORA_ADMIN_ANSIBLE_VAULT_BIN` | autodetectado | Ruta de `ansible-vault` |
| `HORA_ADMIN_ANSIBLE_GALAXY_BIN` | autodetectado | Ruta de `ansible-galaxy` |

### Gunicorn

Estas variables las lee `gunicorn.conf.py`, no `config.py`:

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_BIND` | `127.0.0.1:5000` | Dirección de escucha |
| `HORA_ADMIN_WORKERS` | `1` | Workers (el estado SSE es por proceso) |
| `HORA_ADMIN_GUNICORN_WORKER_CLASS` | `gthread` | Clase de worker (`gthread` evita bloquear con SSE) |
| `HORA_ADMIN_GUNICORN_THREADS` | `4` | Hilos por worker con `gthread` |
| `HORA_ADMIN_GUNICORN_TIMEOUT` | `330` | Timeout del worker (s), mayor que el SSE |
| `HORA_ADMIN_GUNICORN_GRACEFUL_TIMEOUT` | `30` | Espera de parada ordenada (s) |

### Reservadas (definidas, aún no aplicadas)

| Variable | Por defecto | Nota |
|---|---|---|
| `HORA_ADMIN_HISTORY_DAYS` | `180` | Retención de historial; pendiente de job de purga |

> Copia `.env.example` a `.env` y ajusta los valores. En producción usa HTTPS,
> `HORA_ADMIN_COOKIE_SECURE=1`, `HORA_ADMIN_HSTS=1` y un usuario de servicio
> sin privilegios.

---

## Flujo de uso

1. **Crear administrador**: `flask --app app init-admin`.
2. **Contraseñas**: alta manual, importación `.txt` o vault `.yml/.yaml` ya
   cifrado. Se requiere la contraseña maestra.
3. **Panel**: IP/CIDR, usuario admin, origen de contraseñas, acción
   (`TZ`/`TIME`), valor, semántica UTC/local, política NTP, servidores NTP,
   RTC y “Forzar”.
4. **Vista previa (recomendada)**: inspecciona todos los hosts sin cambiar
   nada y muestra estado actual, plan y si haría falta cambiar algo. Se ejecuta
   también con `DRY_RUN=1` porque es de solo lectura.
5. **Ejecución**: aplica el cambio en todos los hosts, verifica el estado final
   y registra un lote con el resultado de cada host.
6. **Progreso**: `/progress/<task_id>` con SSE y consola de Ansible en vivo;
   al terminar enlaza al detalle.
7. **Historial**: `/history` muestra lotes, contadores, operador y duración.

Con `HORA_ADMIN_DRY_RUN=1` el playbook de cambio no se ejecuta: se simula el
resultado y el lote queda marcado como simulación. La vista previa sí
inspecciona los hosts de verdad (solo lectura). Para ejecutar de verdad, define
`HORA_ADMIN_DRY_RUN=0` y reinicia el servicio.

### Acciones

- **TZ**: cambia la zona horaria con `timedatectl set-timezone`.
- **TIME**: fija la hora con `date -s @<epoch>`.
  - `time_mode=utc`: el valor se interpreta como UTC; mismo instante en todos.
  - `time_mode=local`: el valor es la hora de pared de cada host.

### Políticas NTP

| Política | Comportamiento |
|---|---|
| `keep` | No toca la configuración NTP existente |
| `off` | Desactiva NTP y lo deja desactivado |
| `restore` | Desactiva NTP para fijar la hora y restaura su estado previo (si estaba activo, lo reactiva; si estaba apagado, lo deja apagado) |
| `configure` | Configura servidores NTP en chrony o systemd-timesyncd y activa NTP |

---

## Endpoints

| Método | Ruta | Auth | Descripción |
|---|---|---:|---|
| GET/POST | `/login` | No | Inicio de sesión |
| GET | `/healthz` | No | Salud para Docker/balanceadores |
| POST | `/logout` | Sí | Cierre de sesión |
| GET/POST | `/` | Sí | Panel; POST ejecuta según `mode` |
| POST | `/preview` | Sí | Alias explícito de vista previa |
| POST | `/execute` | Sí | Alias explícito de ejecución |
| GET | `/progress/<task_id>` | Sí | Página de progreso |
| GET | `/progress/<task_id>/events` | Sí | Stream SSE de progreso |
| GET | `/api/task-status/<task_id>` | Sí | Estado JSON de la tarea |
| GET | `/batches/<batch_id>` | Sí | Detalle del lote y hosts |
| GET | `/preview/<batch_id>` | Sí | Redirección al detalle del lote |
| GET/POST | `/passwords` | Sí | Gestión del vault |
| GET | `/history` | Sí | Historial de lotes |

CLI:

```bash
flask --app app init-db
flask --app app init-admin [--username USUARIO]
flask --app app reconcile-batches
```

---

## Modelo de datos

### `execution_batches`

`batch_id`, `created_at`, `finished_at`, `operator`, `action`, `value`,
`target`, `time_mode`, `ntp_policy`, `status`, `total_hosts`,
`success_count`, `failure_count`, `skipped_count`, `error_count`,
`preview_count`.

Estados: `running`, `success`, `partial`, `failure`, `preview`, `error`.

### `execution_hosts`

`batch_id`, `host`, `result`, `error`, `duration_ms`, `before_state`,
`after_state`, `password_hash`, `password_origin`, `created_at`.

Resultados: `success`, `failure`, `skipped`, `error`, `preview`.

### Tablas heredadas

`users`, `passwords`, `executions`, `execution_summaries`. El historial
principal es el lote; `executions` se conserva por compatibilidad.

Esquema actual: `PRAGMA user_version = 5`.

---

## Playbooks y contrato

### `ansible/cambiar_hora.yml`

Variables:

| Variable | Descripción |
|---|---|
| `accion` | `TZ` o `TIME` |
| `valor` | Zona horaria o `AAAA-MM-DDTHH:MM` |
| `modo_hora` | `utc` o `local` |
| `valor_epoch` | Epoch objetivo en modo UTC |
| `ntp_policy` | `keep`, `off`, `restore`, `configure` |
| `ntp_servers` | Lista de servidores NTP |
| `sync_rtc` | Sincronizar RTC con `hwclock --systohc` |
| `force` | Aplicar aunque ya coincida |
| `tolerancia` | Desviación máxima al verificar (s) |

Marcadores publicados:

| Marcador | Contenido |
|---|---|
| `HORA_BEFORE_B64` | Estado inicial (JSON base64) |
| `HORA_STATE_B64` | Estado final (JSON base64) |
| `HORA_CHANGED` | `true`/`false` |
| `HORA_VERIFIED` | `true`/`false` |
| `HORA_ERRORS_B64` | Errores de verificación |
| `HORA_WARNINGS_B64` | Advertencias |
| `HORA_DEVIATION` | Desviación en segundos |

### `ansible/estado_hora.yml`

Solo lectura. Publica `HORA_STATE_B64` con el estado actual.

### `ansible/tasks/leer_estado.yml`

Define `hora_estado` con: `timezone`, `ntp`, `ntp_synchronized`,
`local_rtc`, `utc`, `local`, `epoch`, `rtc`.

---

## Seguridad

- No hay contraseña maestra por defecto: se pide en cada operación.
- `SECRET_KEY` se genera y guarda con permisos `0600` si no se define.
- El depurador Flask está desactivado por defecto.
- Las contraseñas candidatas se cifran con `ansible-vault`; SQLite solo guarda
  hashes cortos para la interfaz.
- Las credenciales de conexión viajan en ficheros temporales `0600` y se borran
  tras cada ejecución.
- CSRF activo por defecto.
- Rate limiting en memoria; con varios workers cada proceso lleva su ventana.
- Los rangos CIDR se validan y acotan antes de enumerarse.
- En producción: HTTPS, `HORA_ADMIN_COOKIE_SECURE=1`, `HORA_ADMIN_HSTS=1`,
  `HORA_ADMIN_TRUST_PROXY=1` solo tras proxy de confianza y usuario de
  servicio sin privilegios.

---

## Limitaciones conocidas

- **Snapshots por proceso**: el SSE y la consola se sirven desde memoria; SQLite
  guarda instantáneas para sobrevivir a reinicios, pero varios workers seguirían
  necesitando un almacén compartido. El worker `gthread` y un `timeout` superior
  al SSE evitan bloqueos.
- **Consola acotada**: se conservan las últimas 300 líneas y se recorta cada
  línea a 500 caracteres. El snapshot persistido puede perder la última fracción
  de segundo antes de un reinicio.
- **Ejecución secuencial**: los hosts se procesan uno a uno; no hay
  paralelismo, cancelación ni reintento selectivo de fallidos. Los intentos
  por host y el tiempo total del lote sí están acotados.
- **Sin RBAC/MFA**: un único rol administrador; la allowlist de red es global.
- **Sin inventario/CSV/grupos**: solo IP/CIDR.
- **Solo Ubuntu/systemd**: no hay soporte para RHEL, Windows o dispositivos de
  red.
- **Sin programación ni notificaciones**.
- **Retención parcial**: las instantáneas de tareas se purgan al arrancar según
  `HORA_ADMIN_JOB_RETENTION_HOURS`; `HORA_ADMIN_HISTORY_DAYS` sigue sin job de
  purga.
- **Sin API/CLI de ejecución**: solo endpoints internos de la interfaz.
- **IPv6**: `ipaddress` lo acepta, pero el flujo con Ansible no está probado.

---

## Tests

```bash
source .venv/bin/activate
python -m pytest -q
```

Módulos:

- `test_app.py`: login, CSRF, cabeceras, validación, historial.
- `test_security.py`: CSRF, rate limiter, CIDR.
- `test_db.py`: migraciones, idempotencia, concurrencia, paginación.
- `test_ansible_runner.py`: dry-run, fichero seguro, ping.
- `test_vault_manager.py`: cifrado, descifrado, borrado.
- `test_services.py`: resolución y servicios.
- `test_p4.py`: lotes, todos los hosts, reintento de contraseñas, preview y
  propagación de opciones de hora/NTP.

---

## Documentación

- [Índice de documentación](docs/README.md)
- [Despliegue](DEPLOYMENT.md)
- [Despliegue con Docker](docs/DOCKER.md)
- [Arquitectura y flujos](docs/ARQUITECTURA.md)
- [Referencia técnica](docs/REFERENCIA.md)
- [Operación y runbook](docs/OPERACION.md)
- [Análisis profundo y recomendaciones](docs/ANALISIS_Y_RECOMENDACIONES.md)
- [Análisis funcional y roadmap](ANALISIS.md)
- [Changelog](CHANGELOG.md)

---

## Roadmap

### P5 — Operación

- Inventarios, CSV, grupos y exclusiones.
- Canario, paralelismo, reintentos y cancelación.
- Programación y ventanas de mantenimiento.
- RBAC, MFA, aprobaciones y auditoría en BD.
- API/CLI, webhooks y exportación.
- Filtros y paginación del historial.
- Health checks, métricas y logs estructurados.

### P6 — Multiplataforma

- RHEL/Rocky, Windows (`w32tm`), dispositivos de red.
- Casos especiales de VMs y contenedores.
