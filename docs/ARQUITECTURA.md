# Arquitectura y flujos — Ansible Control Center

## 1. Visión general

Ansible Control Center es una aplicación Flask que orquesta Ansible para cambiar la hora,
la zona horaria, NTP y el RTC de equipos Ubuntu. El proceso web atiende la
interfaz, valida formularios y lanza trabajos en hilos; Ansible se ejecuta como
subproceso contra un host cada vez.

```
Navegador
   │  HTTP / SSE
   ▼
Flask (app.py)
   ├── Autenticación (Flask-Login + SQLite)
   ├── CSRF / rate limiting / cabeceras
   ├── Validación de formularios
   ├── Hilos de ejecución (preview / apply)
   │      ├── services/host_resolver.py
   │      ├── services/vault_manager.py
   │      ├── services/password_tester.py
   │      └── services/ansible_runner.py
   │               └── ansible-playbook ──SSH──► Hosts Ubuntu
   └── SQLite (usuarios, vault-hashes, lotes, resultados)
```

## 2. Componentes

| Componente | Responsabilidad |
|---|---|
| `app.py` | Rutas, validación, tareas SSE, workers y CLI |
| `config.py` | Configuración central, rutas *fail-closed* en producción y logging |
| `models/db.py` | Conexiones SQLite, WAL, busy_timeout y migraciones |
| `models/batch.py` | Lotes y resultados por host |
| `models/execution.py` | Historial heredado por host |
| `models/password.py` | Hashes de contraseñas candidatas |
| `models/user.py` | Usuarios administradores |
| `services/ansible_runner.py` | `inspect_host`, `apply_host`, ejecución segura |
| `services/password_tester.py` | Ping y prueba de contraseñas |
| `services/host_resolver.py` | Validación y resolución IP/CIDR |
| `services/vault_manager.py` | Cifrado/descifrado con `ansible-vault` |
| `services/secure_temp.py` | Ficheros temporales `0600` |
| `services/csrf.py` | Token CSRF de sesión |
| `services/rate_limiter.py` | Ventana deslizante en memoria |
| `services/scheduler.py` | Disparo de programaciones con lock de instancia y *claim* atómico |
| `ansible/cambiar_hora.yml` | Cambio + verificación |
| `ansible/estado_hora.yml` | Inspección de solo lectura |
| `ansible/tasks/leer_estado.yml` | Lectura de TZ, NTP, hora y RTC |

## 3. Flujo de vista previa

```
Formulario (mode=preview)
  → validación (_parse_operation_form)
  → resolve_target
  → descifrar vault o contraseñas temporales
  → crear lote (status=preview)
  → por host y contraseña:
        inspect_host()  (estado_hora.yml, solo lectura)
        → guardar execution_hosts (result=preview)
        → calcular would_change
  → finish_batch (status=preview)
  → SSE + /batches/<id>
```

La vista previa **nunca** llama a `apply_host`.

Tanto la vista previa como la ejecución y las programaciones pasan por
`_validate_operation_core()`: tarea activa, acción admitida, valor fijo,
plataforma del grupo, zona/hora, política NTP y usuario. Al disparar una
programación se vuelve a validar contra el grupo actual (defensa en
profundidad).

## 4. Flujo de ejecución

```
Formulario (mode=execute)
  → validación
  → resolve_target
  → descifrar vault o contraseñas temporales
  → crear lote (status=running)
  → por host:
        por cada contraseña candidata:
            test_ansible_ping()
            si autentica:
                apply_host()  (cambiar_hora.yml)
                si success: break
                si falla: probar siguiente contraseña
        guardar execution_hosts (success/failure)
        guardar executions (registro heredado)
  → finish_batch (status derivado)
  → SSE + /batches/<id>
```

Cada llamada a Ansible puede recibir un `line_callback`: la salida estándar y
de error se unifican y se entregan línea a línea al buffer de la consola. El
resumen de hitos se mantiene aparte (`log`) y la consola se acota y redacta
antes de publicarse por SSE.

Un fallo de un host no detiene el lote (best-effort). El estado global se
deriva de los resultados por host:

- `success`: todos los hosts procesados con éxito.
- `partial`: hay éxitos y fallos/errores.
- `failure`: ningún éxito.
- `error`: error global del lote.
- `preview`: lote de vista previa.

## 5. Flujo de contraseñas

1. `stored`: se descifra `passwords_vault.yml` con la contraseña maestra.
2. `temporal`: se lee el `.txt` en memoria y se deduplica.
3. Se prueba cada candidata por host.
4. Si el ping falla, se pasa a la siguiente.
5. Si el ping funciona pero el playbook falla, se prueba la siguiente.
6. Se guarda el hash corto de la última contraseña que autenticó.

La contraseña nunca se pasa por `argv`: se escribe en un fichero temporal
`0600` y se usa `--extra-vars @fichero`.

## 6. Modelo de concurrencia

- Cada ejecución se encola en la cola acotada (`ACC_JOB_WORKERS` hilos
  daemon); la cancelación es cooperativa mediante un evento.
- El estado de progreso vive en `_tasks` (memoria del proceso) y se respalda
  en `task_state` con escrituras agrupadas para consola/log.
- El SSE espera un evento por tarea y envía el estado completo; el stream se
  acota a 90 s.
- La salida de Ansible se entrega a un buffer acotado (`console`, 300 líneas)
  mediante un hilo lector por proceso; el resumen de hitos va en `log`.
- Si el SSE falla de forma repetida o expira, la página cae a polling de
  `/api/task-status/<task_id>`.
- Los hosts se procesan secuencialmente.
- Antes de aplicar un cambio se reserva el host en `host_locks`; si otro lote
  lo tiene, el host queda `skipped`. La reserva se renueva antes de cada
  intento y caduca con `ACC_HOST_LOCK_TTL`.
- SQLite usa WAL y `busy_timeout` para tolerar escrituras concurrentes.
- El scheduler se protege con un lock `flock` y un *claim* atómico por fila
  (`schedules.claimed_at`); `ACC_ROLE=web|worker|cli` permite separarlo del
  servidor web. La recuperación de tareas y lotes solo toca registros
  estancados (`ACC_RECOVERY_STALE_MINUTES`).

Limitaciones: con varios workers el estado de progreso no se comparte; si el
proceso muere, las tareas se pierden y los lotes pueden quedar `running` hasta
la reconciliación de registros estancados.

## 7. Modelo de datos

```
users
  id, username, password_hash, is_active, created_at

passwords
  id, password_hash_short, password_hash_full, creation_date, origin

executions (heredado)
  id, timestamp, action, value, host, result,
  password_hash, password_origin

execution_batches
  id, batch_id, created_at, finished_at, operator, action, value,
  target, time_mode, ntp_policy, status, total_hosts,
  success_count, failure_count, skipped_count, error_count, preview_count

execution_hosts
  id, batch_id, host, result, error, duration_ms,
  before_state, after_state, password_hash, password_origin, created_at
```

`execution_hosts.batch_id` referencia `execution_batches.batch_id` con
`ON DELETE CASCADE`.

## 8. Decisiones de diseño

### UTC vs local

- `utc`: el valor se interpreta como UTC y se convierte a epoch. Todos los
  hosts reciben el mismo instante.
- `local`: el valor se interpreta en la zona IANA seleccionada en `time_zone`.
  La aplicación calcula un único `target_epoch` y lo envía a todos los hosts,
  por lo que todos quedan en el mismo instante. No se usa la zona particular de
  cada host; para lotes heterogéneos hay que ejecutar un lote por zona o usar
  `utc`.

### Política NTP

- `keep`: no tocar.
- `off`: desactivar y dejar desactivado.
- `restore`: desactivar, fijar hora y restaurar el estado NTP previo.
- `configure`: escribir servidores NTP y activar.

### Best-effort

Se prefiere reportar el resultado de todos los hosts antes que abortar el lote
completo. El estado global refleja éxitos, fallos y errores.

### Idempotencia

El playbook compara el estado inicial con el objetivo y solo ejecuta el cambio
si difiere, salvo `force`.

### Verificación

El éxito no depende solo de `returncode == 0`: el playbook publica
`HORA_VERIFIED` y estados antes/después. `apply_host` exige verificación
correcta.

## 9. Puntos de extensión

- Inventarios/CSV/grupos: extender `host_resolver` y el formulario.
- Paralelismo: `ThreadPoolExecutor` en `_run_apply_batch`.
- Cola persistente: sustituir el hilo por RQ/Celery.
- RBAC/MFA: extender `User` y Flask-Login.
- Auditoría: nueva tabla `audit_events`.
- Multiplataforma: playbooks por familia de SO.
- API/CLI: reutilizar `execute_with_progress` y el modelo de lotes.
