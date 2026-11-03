# Referencia técnica — Hora Admin

Referencia de rutas, servicios, modelos, esquema de datos, configuración y
contrato de los playbooks.

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
| GET | `/progress/<task_id>` | Sí | Página de progreso |
| GET | `/progress/<task_id>/events` | Sí | Stream SSE |
| GET | `/api/task-status/<task_id>` | Sí | Estado JSON de la tarea |
| GET | `/batches/<batch_id>` | Sí | Detalle del lote |
| GET | `/preview/<batch_id>` | Sí | Redirección a `/batches/<batch_id>` |
| GET, POST | `/passwords` | Sí | Gestión del vault |
| GET | `/history` | Sí | Historial de lotes |

Errores: `400`, `404`, `429`, `500`. Las peticiones AJAX/JSON reciben JSON en
`400`/`429`; el resto, HTML.

### Formulario de operación

Campos aceptados por `/`, `/preview` y `/execute`:

| Campo | Valores | Obligatorio |
|---|---|---|
| `target_ip` | IP, hostname o CIDR | Sí |
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

---

## 2. CLI

```bash
flask --app app init-db
flask --app app init-admin [--username USUARIO]
flask --app app reconcile-batches
```

- `init-db`: crea o migra el esquema SQLite (idempotente).
- `reconcile-batches`: cierra los lotes huérfanos en estado `running`.
- `init-admin`: crea o actualiza el usuario administrador; pide contraseña y
  exige la longitud mínima configurada.

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
| `resolve_target(target, max_hosts, min_prefix)` | IP/hostname/CIDR → lista |
| `validate_ip_format(ip_str)` | Valida IP/CIDR |
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

`SCHEMA_VERSION = 3`.

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

---

## 6. Configuración

La tabla completa está en [README](../README.md#configuración). Resumen de
grupos:

- Rutas: `HORA_ADMIN_SECRETS_DIR`, `HORA_ADMIN_DATA_DIR`, `HORA_ADMIN_LOG_DIR`.
- Sesión: `HORA_ADMIN_SECRET_KEY`, `HORA_ADMIN_SESSION_HOURS`,
  `HORA_ADMIN_REMEMBER_DAYS`, `HORA_ADMIN_COOKIE_SECURE`,
  `HORA_ADMIN_PASSWORD_PEPPER`.
- Ejecución: `HORA_ADMIN_DRY_RUN`, `HORA_ADMIN_MAX_HOSTS`,
  `HORA_ADMIN_MIN_CIDR_PREFIX`, `HORA_ADMIN_ALLOWED_NETWORKS`,
  `HORA_ADMIN_MAX_PASSWORDS`, `HORA_ADMIN_MAX_PASSWORD_ATTEMPTS`,
  `HORA_ADMIN_BATCH_TIME_BUDGET`, `HORA_ADMIN_TIME_TOLERANCE`,
  `HORA_ADMIN_DEFAULT_NTP_SERVERS`.
- Seguridad: `HORA_ADMIN_CSRF_ENABLED`, `HORA_ADMIN_*_RATE_LIMIT`,
  `HORA_ADMIN_TRUST_PROXY`, `HORA_ADMIN_CSP_ENABLED`,
  `HORA_ADMIN_HSTS`, `HORA_ADMIN_SECURITY_HEADERS`.
- Ansible: `HORA_ADMIN_ANSIBLE_BIN`,
  `HORA_ADMIN_ANSIBLE_PLAYBOOK_BIN`, `HORA_ADMIN_ANSIBLE_VAULT_BIN`,
  `HORA_ADMIN_PING_TIMEOUT`, `HORA_ADMIN_PLAYBOOK_TIMEOUT`,
  `HORA_ADMIN_CONNECT_TIMEOUT`.
- Logging: `HORA_ADMIN_LOG_LEVEL`, `HORA_ADMIN_LOG_TO_FILE`,
  `HORA_ADMIN_LOG_MAX_BYTES`, `HORA_ADMIN_LOG_BACKUPS`.

Reservada (definida, sin job de purga de lotes): `HORA_ADMIN_HISTORY_DAYS`.
`HORA_ADMIN_JOB_RETENTION_HOURS` se aplica al arrancar a las instantáneas de
tareas.

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
    "created_at": str,
    # añadidos durante la ejecución:
    "mode", "batch_id", "total_hosts", "operator", "time_mode", "ntp_policy"
}
```

Estados terminales del stream: `completed`, `failed`, `error`, `found`,
`failure`, `timeout`.

`generate_progress_events(task_id)` emite `retry`, `progress`, `completed` y
`timeout`.

---

## 9. Errores y logging

- El logger principal es `hora_admin`; los servicios usan su propio logger.
- No se registran contraseñas ni la contraseña maestra.
- Los errores de Ansible se guardan en `execution_hosts.error`.
- `HORA_ADMIN_LOG_TO_FILE=1` escribe en `LOG_DIR/hora-admin.log` con rotación.
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
