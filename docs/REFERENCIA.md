# Referencia técnica — Ansible Control Center

Referencia de rutas, servicios, modelos, esquema de datos, configuración y
contrato de los playbooks.

---

## 0. Organización del código

`app.py` es una fachada compatible: crea la aplicación con
`acc.bootstrap.create_app()` y reexporta los símbolos históricos que consumen
`gunicorn`, el CLI (`flask --app app …`) y las pruebas. La lógica vive en el
paquete `acc/`:

| Módulo | Responsabilidad |
|---|---|
| `acc/bootstrap.py` | `create_app()`, registro de rutas/hooks, CLI y scheduler |
| `acc/runtime.py` | Rutas de datos, SQLite, `DRY_RUN`, vault y cola de trabajos |
| `acc/task_state.py` | Estado de tareas en memoria/SQLite, SSE e idempotencia |
| `acc/security.py` | Roles, propiedad de filas y límites de peticiones |
| `acc/operations.py` | Análisis y validación de formularios de operación |
| `acc/execution.py` | Lotes de vista previa y aplicación, progreso y cancelación |
| `acc/console.py` | Consola de Ansible saneada, hitos y omisión de hosts |
| `acc/safety.py` | Criticidad y ventana de mantenimiento |
| `acc/approvals.py` | Parámetros estables y payload de aprobaciones |
| `acc/scheduling.py` | Validación y lanzamiento de programaciones |
| `acc/cli.py` | Comandos `init-db`, `init-admin`, `scheduler`… |
| `acc/version.py` | Versión (`VERSION`) y commit de construcción |
| `acc/web/registry.py` | `RouteRegistry`: decoradores y `add_url_rule` sin blueprints |
| `acc/web/*.py` | Rutas por dominio: `auth`, `tasks`, `inventory`, `schedules`, `approvals`, `passwords`, `admin`, `errors`, y `hooks` |

Las rutas conservan exactamente los mismos nombres de *endpoint* que antes del
troceado (los usan las plantillas vía `url_for`). El estado de proceso se
consulta siempre a través de `acc.runtime`, que es el único punto de parcheo
en las pruebas.

### Versiones

La versión vive en `VERSION` (SemVer) y `acc/version.py` la expone como
`__version__`. `create_app()` publica `APP_VERSION` y `APP_BUILD_COMMIT` en la
configuración de Flask, y `/acerca-de` los muestra. Las imágenes Docker usan
la misma cifra: `<versión>`, `<minor>`, `latest` y `sha-<commit>`. El paquete
offline fija `ACC_IMAGE_TAG` a la versión exacta.

---

## 1. Rutas

| Método | Ruta | Auth | Descripción |
|---|---|---:|---|
| GET, POST | `/login` | No | Inicio de sesión; POST valida credenciales |
| POST | `/logout` | Sí | Cierre de sesión |
| GET | `/` | Sí | Panel de configuración |
| POST | `/` | Sí | Ejecuta según `mode` (`preview`/`execute`) |
| POST | `/preview` | Sí | Alias de vista previa |
| POST | `/execute` | Sí | Alias de ejecución |
| GET | `/progress/<task_id>` | Sí (dueño/admin) | Página de progreso |
| GET | `/progress/<task_id>/events` | Sí (dueño/admin) | Stream SSE |
| GET | `/api/task-status/<task_id>` | Sí (dueño/admin) | Estado JSON de la tarea |
| GET | `/configuracion` | Sí | Redirige a la primera pestaña de configuración (Inventario) |
| GET | `/modulos` | Sí | Tareas que el usuario quitó de su panel |
| POST | `/modulos/<task_id>/ocultar` | Sí | Mueve una tarjeta del panel a Módulos |
| POST | `/modulos/<task_id>/mostrar` | Sí | Devuelve una tarjeta al panel |
| POST | `/modulos/restaurar` | Sí | Devuelve todas las tarjetas al panel |
| GET | `/batches/<batch_id>` | Sí | Detalle del lote |
| GET | `/preview/<batch_id>` | Sí | Redirección a `/batches/<batch_id>` |
| GET, POST | `/passwords` | Sí | Vault compartido: lectura para todos; alta/borrado solo `admin` |
| GET, POST | `/inventario` | Sí | Grupos propios y compartidos; edición `admin`/`operator` |
| GET, POST | `/programaciones` | Sí | Programaciones propias y compartidas; alta/edición `admin` |
| GET | `/usuarios` | Admin | Cuentas locales y LDAPS, roles, estado y acceso LDAPS |
| POST | `/usuarios/crear` | Admin | Crea una cuenta local con contraseña propia |
| POST | `/usuarios/password` | Admin | Cambia la contraseña de una cuenta local |
| POST | `/usuarios/eliminar` | Admin | Elimina una cuenta local y transfiere sus datos |
| POST | `/usuarios/rol` | Admin | Asigna el rol de una cuenta |
| POST | `/usuarios/estado` | Admin | Activa o desactiva una cuenta |
| POST | `/usuarios/ldaps` | Admin local | Activa o desactiva el acceso LDAPS |
| GET | `/history` | Sí | Historial de lotes |

Errores: `400`, `404`, `429`, `500`. Las peticiones AJAX/JSON reciben JSON en
`400`/`429`; el resto, HTML.

### Formulario de operación

Campos aceptados por `/`, `/preview` y `/execute`:

| Campo | Valores | Obligatorio |
|---|---|---|
| `target_ip` | IP, hostname, CIDR o rango `inicio-fin` | Sí |
| `admin_user` | Usuario SSH | Sí |
| `password_origin` | `stored`, `temporal` | Sí |
| `master_password` | Contraseña maestra | Si `stored` |
| `password_file` | `.txt` | Si `temporal` |
| `action` | `TZ`, `TIME` | Sí |
| `value` | Zona horaria o `AAAA-MM-DDTHH:MM` | Sí |
| `mode` | `preview`, `execute` | No (por defecto `execute`) |
| `time_mode` | `utc`, `local` | Solo `TIME` |
| `ntp_policy` | `keep`, `off`, `restore`, `configure` | No (`keep`) |
| `ntp_servers` | Lista separada por comas/espacios | Si `configure` |
| `sync_rtc` | `1`/`on` | No |
| `force` | `1`/`on` | No |
| `csrf_token` | Token de sesión | Sí |

### Acciones de `/passwords`

| `action` | Campos | Descripción |
|---|---|---|
| `encrypt` | `passwords_textarea`, `master_password` | Cifra y añade al vault |
| `delete` | `password_id`, `master_password` | Elimina del vault y SQLite |
| `load_file` | `password_file`, `master_password` | Importa `.txt` o vault `.yml/.yaml` |

Las tres acciones requieren rol `admin`. Cualquier usuario autenticado puede
consultar el listado y usar el vault al ejecutar tareas.

### Acciones de `/usuarios`

Todas requieren rol `admin`; el interruptor LDAPS exige además una cuenta
local (una cuenta LDAPS no puede cambiar el acceso del que depende).

| Ruta | Campos | Descripción |
|---|---|---|
| `/usuarios/crear` | `username`, `password`, `password_confirm`, `role`, `active` | Alta de cuenta local; el nombre no puede existir ya |
| `/usuarios/password` | `username`, `password`, `password_confirm` | Cambia la contraseña local y limpia el bloqueo |
| `/usuarios/eliminar` | `username`, `confirm_username` | Borra una cuenta local; sus programaciones y grupos pasan al admin actual |
| `/usuarios/rol` | `username`, `role`, `next_username` | Asigna `viewer`, `operator` o `admin` |
| `/usuarios/estado` | `username`, `active` | Activa o desactiva sin borrar datos |
| `/usuarios/ldaps` | — | Conmuta el acceso corporativo |

Las cuentas **LDAPS** no tienen contraseña local: su contraseña se cambia en el
directorio y `/usuarios/password` la rechaza. Tampoco se eliminan desde la
aplicación; se desactivan para bloquear el acceso. El sistema impide eliminar,
desactivar o retirar el rol a la última cuenta de administración **local**
activa, porque el acceso LDAPS depende de que el directorio esté disponible.

---

## 2. CLI

```bash
flask --app app init-db
flask --app app init-admin [--username USUARIO]
flask --app app reconcile-batches
flask --app app scheduler [--once]
```

- `init-db`: crea o migra el esquema SQLite (idempotente).
- `reconcile-batches`: cierra los lotes huérfanos en estado `running` (con
  `stale_minutes` por defecto, solo los que no tienen actividad reciente).
- `init-admin`: crea o actualiza el usuario administrador; pide contraseña y
  exige la longitud mínima configurada.
- `scheduler`: ejecuta el programador en primer plano; `--once` hace una sola
  revisión. Recomendado con `ACC_ROLE=worker` y el servicio web en
  `ACC_ROLE=web`.

---

## 3. Servicios

### `services/ansible_runner.py`

| Función | Descripción |
|---|---|
| `ansible_environment()` | Entorno de Ansible sin secretos |
| `run_ansible_playbook(host, user, action, value, password, playbook_path, timeout, line_callback)` | Ruta legacy; ejecuta el playbook de cambio |
| `inspect_host(host, user, password, timeout, line_callback)` | Ejecuta `estado_hora.yml`; devuelve `state` |
| `apply_host(host, user, action, value, password, time_mode, ntp_policy, ntp_servers, sync_rtc, force, tolerance, timeout, line_callback)` | Aplica y verifica; devuelve `before_state`, `after_state`, `changed`, `verified`, `warnings`, `deviation` |
| `run_playbook_simple(...)` | Compatibilidad |

`line_callback` es opcional. Si se pasa, `_invoke_playbook` usa
`subprocess.Popen` y entrega la salida de Ansible línea a línea mediante un
hilo lector; sin callback se mantiene `subprocess.run` con captura completa.
El timeout se aplica sobre el proceso y, al expirar, se mata y se propaga
`TimeoutExpired`.

Resultado habitual:

```python
{
    "success": bool,
    "host": str,
    "output": str,
    "error": str,
    "ok_count": int,
    "changed_count": int,
    "failed_count": int,
    "unreachable_count": int,
    # apply_host / inspect_host:
    "before_state": dict | None,
    "after_state": dict | None,
    "state": dict | None,
    "changed": bool,
    "verified": bool | None,
    "warnings": str,
    "deviation": int | None,
    "dry_run": bool,
}
```

### `services/password_tester.py`

| Función | Descripción |
|---|---|
| `test_passwords_single(target_ip, admin_user, password_origin, master_password, action, value, temporal_passwords)` | Prueba contraseñas contra todos los hosts, con tope por host (API de servicio) |
| `test_ansible_ping(host, user, password, timeout)` | Ejecuta `ansible -m ping` |
| `run_ansible_playbook(host, user, password, action, value)` | Wrapper legacy |
| `resolve_hosts(target_ip)` | Wrapper de `resolve_target` |

### `services/host_resolver.py`

| Función | Descripción |
|---|---|
| `resolve_single_ip(ip_str)` | IP o resolución DNS |
| `resolve_cidr_range(cidr_str, max_hosts, min_prefix)` | Enumera un CIDR acotado |
| `resolve_ipv4_range(range_str, max_hosts, min_prefix)` | Enumera un rango inclusivo `inicio-fin` |
| `resolve_target(target, max_hosts, min_prefix)` | IP/hostname/CIDR/rango → lista |
| `validate_ip_format(ip_str)` | Valida IP/CIDR/rango |
| `validate_target(target, max_hosts, min_prefix)` | Devuelve mensaje de error o `None` |

### `services/vault_manager.py`

| Método | Descripción |
|---|---|
| `encrypt_passwords(passwords, master_password, output_file, origin, append, db_path)` | Cifra y actualiza el vault de forma atómica |
| `remove_password(password_hash_full, master_password, vault_file)` | Elimina una entrada |
| `decrypt_vault_file(vault_file, master_password)` | Devuelve `(ok, mensaje, entradas)` |
| `validate_master_password(vault_file, test_password)` | Comprueba la contraseña maestra |
| `vault_path()` | Ruta efectiva del vault |

Cada entrada descifrada contiene: `index`, `password`, `original`,
`hash_short`, `hash_full`.

### `services/secure_temp.py`

| Función | Descripción |
|---|---|
| `secure_temp_file(content, prefix, suffix, directory)` | Fichero temporal `0600`, borrado al salir |
| `temporary_directory(prefix, directory)` | Directorio temporal `0700`, borrado al salir |

### `services/csrf.py`

| Función | Descripción |
|---|---|
| `generate_csrf_token()` | Token de sesión (crea o renueva) |
| `rotate_csrf_token()` | Token nuevo (login) |
| `validate_csrf_token(token)` | Comparación en tiempo constante |
| `csrf_protect()` | Hook `before_request` |

### `services/rate_limiter.py`

`RateLimiter.check(key, limit, window) -> (permitido, retry_after)` y
`RateLimiter.reset(key=None)`. Instancia global: `LIMITER`.

---

## 4. Modelos

### `models/db.py`

| Función | Descripción |
|---|---|
| `connect(db_path)` | Conexión con `WAL`, `busy_timeout`, `foreign_keys` |
| `get_connection(db_path)` | Context manager con commit/rollback |
| `init_schema(db_path)` | Migraciones idempotentes; fija `user_version` |

`SCHEMA_VERSION = 19`. La v19 añade `owner_user_id` a `schedules` e
`inventory_groups`; las filas existentes quedan con `0` (compartidas).

### `models/user.py`

`User`: `get_by_username`, `get_by_id`, `create_or_update`, `count`,
`check_password`, `is_active`.

### `models/password.py`

`PasswordRecord`: `hash_full`, `hash_short`, `store_password`,
`get_all_passwords`, `get_hash_full`, `exists_by_hash_full`,
`delete_password`, `get_password_count`.

### `models/execution.py`

`ExecutionRecord`: `record_execution`, `get_all_executions`,
`get_recent_executions`, `get_stats`.
`ExecutionSummary`: compatibilidad.

### `models/batch.py`

`ExecutionBatch`: `create_batch`, `add_host_result`, `finish_batch`,
`get_batch`, `get_batches`, `get_host_results`, `get_stats`.
`HostResult`: resultado individual.

Estados de lote: `running`, `success`, `partial`, `failure`, `preview`,
`error`.
Resultados de host: `success`, `failure`, `skipped`, `error`, `preview`.

---

## 5. Esquema SQLite

### `users`

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
username TEXT NOT NULL UNIQUE,
password_hash TEXT NOT NULL,
is_active INTEGER NOT NULL DEFAULT 1,
created_at TEXT NOT NULL
```

### `passwords`

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
password_hash_short CHAR(8) NOT NULL,
password_hash_full TEXT NOT NULL,
creation_date TEXT NOT NULL,
origin TEXT NOT NULL DEFAULT 'manual'
```

### `executions` (heredado)

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
timestamp TEXT NOT NULL,
action TEXT NOT NULL,
value TEXT NOT NULL,
host TEXT NOT NULL,
result TEXT NOT NULL,
password_hash TEXT NOT NULL DEFAULT '',
password_origin TEXT NOT NULL DEFAULT 'manual'
```

### `execution_summaries` (heredado)

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
execution_id INTEGER NOT NULL UNIQUE,
host_count INTEGER NOT NULL DEFAULT 0,
success_count INTEGER NOT NULL DEFAULT 0,
failure_count INTEGER NOT NULL DEFAULT 0
```

### `execution_batches`

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
batch_id TEXT NOT NULL UNIQUE,
created_at TEXT NOT NULL,
finished_at TEXT,
operator TEXT NOT NULL DEFAULT '',
action TEXT NOT NULL,
value TEXT NOT NULL,
target TEXT NOT NULL,
time_mode TEXT NOT NULL DEFAULT 'utc',
ntp_policy TEXT NOT NULL DEFAULT 'keep',
status TEXT NOT NULL DEFAULT 'running',
total_hosts INTEGER NOT NULL DEFAULT 0,
success_count INTEGER NOT NULL DEFAULT 0,
failure_count INTEGER NOT NULL DEFAULT 0,
skipped_count INTEGER NOT NULL DEFAULT 0,
error_count INTEGER NOT NULL DEFAULT 0,
preview_count INTEGER NOT NULL DEFAULT 0
```

### `execution_hosts`

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
batch_id TEXT NOT NULL,
host TEXT NOT NULL,
result TEXT NOT NULL,
error TEXT NOT NULL DEFAULT '',
duration_ms INTEGER NOT NULL DEFAULT 0,
before_state TEXT NOT NULL DEFAULT '',
after_state TEXT NOT NULL DEFAULT '',
password_hash TEXT NOT NULL DEFAULT '',
password_origin TEXT NOT NULL DEFAULT '',
created_at TEXT NOT NULL,
FOREIGN KEY (batch_id)
    REFERENCES execution_batches (batch_id) ON DELETE CASCADE
```

Índices: `idx_batches_created_at`, `idx_batches_status`,
`idx_hosts_batch_id`, `idx_hosts_host`, `idx_hosts_result`,
`idx_executions_timestamp`, `idx_executions_host`, `idx_executions_result`,
`idx_passwords_creation_date`.

### `schedules`

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
name TEXT NOT NULL UNIQUE,
task_id TEXT NOT NULL DEFAULT 'hora',
run_at TEXT NOT NULL,
recurrence TEXT NOT NULL DEFAULT 'once',
enabled INTEGER NOT NULL DEFAULT 1,
params TEXT NOT NULL DEFAULT '{}',
last_run TEXT,
last_batch TEXT,
last_error TEXT NOT NULL DEFAULT '',
created_by TEXT NOT NULL DEFAULT '',
created_at TEXT NOT NULL,
claimed_at TEXT,
claimed_by TEXT,
owner_user_id INTEGER NOT NULL DEFAULT 0
```

`claimed_at`/`claimed_by` (esquema v12) reservan la fila de forma atómica
entre procesos; `Schedule.mark_run` limpia la reserva. `owner_user_id`
(esquema v19) identifica al creador; `0` significa fila heredada compartida.
`Schedule.all(owner_user_id=...)` incluye las compartidas salvo que se pida
lo contrario.

`inventory_groups` incorpora la misma columna `owner_user_id` (v19), con el
mismo significado. Los operadores pueden crear y editar sus propios grupos;
los grupos compartidos solo los modifica un administrador.

### `approvals`

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
task_id TEXT NOT NULL,
params_hash TEXT NOT NULL,
params TEXT NOT NULL DEFAULT '{}',
requested_by TEXT NOT NULL,
requested_at TEXT NOT NULL,
status TEXT NOT NULL DEFAULT 'pending',
approver TEXT,
decided_at TEXT,
note TEXT NOT NULL DEFAULT '',
consumed_at TEXT
```

Estados: `pending`, `approved`, `reserved`, `rejected`, `consumed`. La reserva
se confirma solo después de encolar el lote; si el encolado falla se libera.

---

## 6. Configuración

La tabla completa está en [README](../README.md#configuración). Resumen de
grupos:

- Rutas: `ACC_SECRETS_DIR`, `ACC_DATA_DIR`, `ACC_LOG_DIR`.
- Entorno y proceso: `ACC_ENV`, `ACC_ROLE`, `ACC_SCHEDULER_ENABLED`,
  `ACC_RECOVERY_STALE_MINUTES`, `ACC_ALLOW_DEV_FALLBACK`.
- Sesión: `ACC_SECRET_KEY`, `ACC_SESSION_HOURS`,
  `ACC_REMEMBER_DAYS`, `ACC_COOKIE_SECURE`,
  `ACC_PASSWORD_PEPPER`.
- Ejecución: `ACC_DRY_RUN`, `ACC_MAX_HOSTS`,
  `ACC_MIN_CIDR_PREFIX`, `ACC_ALLOWED_NETWORKS`,
  `ACC_MAX_PASSWORDS`, `ACC_MAX_PASSWORD_ATTEMPTS`,
  `ACC_BATCH_TIME_BUDGET`, `ACC_TIME_TOLERANCE`,
  `ACC_DEFAULT_NTP_SERVERS`.
- Seguridad: `ACC_CSRF_ENABLED`, `ACC_*_RATE_LIMIT`,
  `ACC_TRUST_PROXY`, `ACC_CSP_ENABLED`,
  `ACC_HSTS`, `ACC_SECURITY_HEADERS`, `ACC_LDAP_ALLOW_INSECURE`.
- Ansible: `ACC_ANSIBLE_BIN`,
  `ACC_ANSIBLE_PLAYBOOK_BIN`, `ACC_ANSIBLE_VAULT_BIN`,
  `ACC_PING_TIMEOUT`, `ACC_PLAYBOOK_TIMEOUT`,
  `ACC_CONNECT_TIMEOUT`.
- Logging: `ACC_LOG_LEVEL`, `ACC_LOG_TO_FILE`,
  `ACC_LOG_MAX_BYTES`, `ACC_LOG_BACKUPS`.

`ACC_HISTORY_DAYS` purga el historial de lotes;
`ACC_AUTH_EVENT_DAYS` purga la auditoría de acceso;
`ACC_JOB_RETENTION_HOURS` purga instantáneas de tareas y
`ACC_MAX_TASK_STATE_ROWS` recorta `task_state`. El scheduler reaplica la
retención cada `ACC_RETENTION_INTERVAL_HOURS`; `ACC_TASK_PERSIST_INTERVAL`
agrupa las escrituras frecuentes de consola/log (los cambios de estado se
guardan siempre).

---

## 7. Playbooks

### `ansible/cambiar_hora.yml`

Variables:

| Variable | Tipo | Descripción |
|---|---|---|
| `accion` | str | `TZ` o `TIME` |
| `valor` | str | Zona horaria o `AAAA-MM-DDTHH:MM` |
| `modo_hora` | str | `utc` o `local` |
| `valor_epoch` | int | Epoch objetivo (modo UTC) |
| `ntp_policy` | str | `keep`, `off`, `restore`, `configure` |
| `ntp_servers` | list | Servidores NTP |
| `sync_rtc` | bool | Sincronizar RTC |
| `force` | bool | Forzar cambio |
| `tolerancia` | int | Tolerancia de verificación (s) |

Tareas principales:

1. Validar acción, modo, política y servidores.
2. Leer estado inicial (`tasks/leer_estado.yml`).
3. Publicar `HORA_BEFORE_B64`.
4. Cambiar zona horaria si procede.
5. Gestionar NTP según política.
6. Calcular epoch objetivo y fijar hora si procede.
7. Sincronizar RTC si `sync_rtc`.
8. Configurar servidores NTP si `configure`.
9. Leer estado final y verificar.
10. Publicar marcadores `HORA_*`.

### `ansible/estado_hora.yml`

Solo lectura; importa `tasks/leer_estado.yml` y publica `HORA_STATE_B64`.

### `ansible/tasks/leer_estado.yml`

Define `hora_estado`:

```yaml
timezone: str
ntp: bool
ntp_synchronized: bool
local_rtc: bool
utc: str
local: str
epoch: int
rtc: str
```

### Marcadores

| Marcador | Formato |
|---|---|
| `HORA_BEFORE_B64` | JSON base64 del estado inicial |
| `HORA_STATE_B64` | JSON base64 del estado final |
| `HORA_CHANGED` | `true`/`false` |
| `HORA_VERIFIED` | `true`/`false` |
| `HORA_ERRORS_B64` | Texto base64 |
| `HORA_WARNINGS_B64` | Texto base64 |
| `HORA_DEVIATION` | Entero (segundos) |

> Las operaciones sobre equipos de red (`network_cli`, `netconf`, `httpapi`) no
> están integradas todavía. Su referencia técnica, catálogo de operaciones y
> diseño propuesto están en [ANSIBLE_REDES](ANSIBLE_REDES.md).

---

## 8. Tareas SSE

`create_task()` crea una entrada en `_tasks` con:

```python
{
    "status": "pending",
    "progress": 0,
    "message": str,
    "log": list[str],
    "host": str,
    "password_hash": str,
    "overall_result": "unknown",
    "completed": bool,
    "owner": str,  # usuario que la lanzó; "" en programaciones
    "created_at": str,
    # añadidos durante la ejecución:
    "mode", "batch_id", "total_hosts", "operator", "time_mode", "ntp_policy"
}
```

Estados terminales del stream: `completed`, `failed`, `error`, `found`,
`failure`, `timeout`.

`generate_progress_events(task_id)` emite `retry`, `progress`, `completed` y
`timeout`. Espera cambios con un evento por tarea (hasta 1 s) en lugar de
dormir, y cierra el stream a los 90 s; el cliente sigue por polling si el
lote continúa. Un operador solo puede cancelar tareas con `owner` igual a su
usuario; un administrador puede cancelar cualquiera.

---

## 9. Errores y logging

- El logger principal es `acc`; los servicios usan su propio logger.
- No se registran contraseñas ni la contraseña maestra.
- Los errores de Ansible se guardan en `execution_hosts.error`.
- `ACC_LOG_TO_FILE=1` escribe en `LOG_DIR/ansible-control-center.log` con rotación.
- Los errores internos devuelven `500.html`; los AJAX reciben JSON en
  `400`/`429`.

---

## 10. Tests

```bash
python -m pytest -q
```

| Módulo | Cobertura |
|---|---|
| `test_app.py` | Login, CSRF, cabeceras, validación, historial |
| `test_security.py` | CSRF, rate limiter, CIDR |
| `test_db.py` | Migraciones, idempotencia, concurrencia, paginación |
| `test_ansible_runner.py` | Dry-run, fichero seguro, ping |
| `test_vault_manager.py` | Cifrado, descifrado, borrado |
| `test_services.py` | Resolución y servicios |
| `test_p4.py` | Lotes, todos los hosts, reintentos, preview, opciones P4 |
| `test_estructura_app.py` | Contrato del troceado: mapa de URLs/endpoints, CLI, hooks, plantillas y fachada de `app.py` |
