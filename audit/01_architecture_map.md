# 01 — Mapa de arquitectura

## Visión general (derivada de código y configuración inspeccionados)

```text
Navegador (UI Flask/Jinja2, fetch/EventSource/SSE, formularios)
    |
    | HTTPS (terminación en Nginx) / SSE sin buffering
    v
Nginx (reverse proxy, TLS, headers, /static, proxy a Gunicorn)
    |
    v
Gunicorn (gthread, por defecto 1 worker) + Flask app (app.py)
    |
    +-- Autenticación: Flask-Login, sesiones, login local (Werkzeug), LDAP/LDAPS opcional (ldap3)
    +-- Autorización: roles viewer/operator/admin, decoradores/gates (a confirmar exhaustivamente en app.py)
    +-- CSRF: services/csrf.py (token en sesión, validación en mutaciones)
    +-- Rate limiting: services/rate_limiter.py (memoria en proceso)
    +-- Validación/seguridad de entrada: config/host_resolver/safety/task_catalog/wallpaper
    +-- Estado de tareas/progreso: memoria + persistencia task_state/batches en SQLite, SSE
    +-- JobQueue (services/job_queue.py): cola acotada, workers en proceso
    +-- Scheduler (services/scheduler.py): claim/launch, cifrado Fernet de params sensibles
    +-- Persistencia: SQLite WAL (models/db.py, schema v18)
    +-- Ejecución: services/ansible_runner.py -> ansible-playbook / ansible (subprocess)
    +-- Secretos: services/vault_manager.py (Ansible Vault CLI), extra-vars temporales 0600
    +-- Resolución de destinos: services/host_resolver.py (IP/CIDR/rango/hostname, pinning)
    +-- Host locks: services/host_locks.py (SQLite TTL)
    +-- Notificación: services/notifier.py (SMTP opcional)
    +-- Reporting: services/pdf_report.py, métricas (services/metrics.py)
    +-- Auxiliares: ssh_host_keys, password_tester, wallpaper, time_utils, modules_panel
```

## Componentes y dependencias

| Componente | Responsabilidad principal | Dependencias clave | Estado/superficie crítica |
|---|---|---|---|
| `app.py` | Rutas, parsing/validación de formularios, orquestación de ejecución/aprobaciones/SSE, manejo de sesión/roles, integración de servicios | Flask, modelos, todos los servicios | **Concentración alta (5960 líneas)**; riesgo de acoplamiento y regresiones |
| `models/db.py` | Conexión SQLite, pragmas (WAL, busy_timeout, foreign_keys), migraciones v1–v18, settings | `sqlite3`, filesystem | Consistencia, concurrencia de escritura, evolución de esquema |
| `models/batch.py` | Lotes/resultados por host, estados terminales, reconciliación, retención/estadísticas | `db.py`, JSON | Integridad de historial y contadores |
| `models/ops.py` | Inventario, grupos, hosts, programaciones, aprobaciones, hashes | `db.py` | Doble aprobación, parámetros autorizados, ventanas/criticidad asociadas |
| `models/user.py` | Usuarios, roles, bloqueo, eventos auth, cuentas locales/LDAPS sombra | `db.py`, hashing Werkzeug | Autenticación/autorización y trazabilidad |
| `services/ansible_runner.py` | Construcción/ejecución de comandos Ansible, streaming, parsing marcadores, DRY_RUN, verificación | `subprocess`, `os`, playbooks, temp files | **Superficie de ejecución remota** y manejo de secretos |
| `services/vault_manager.py` | Cifrado/descifrado con `ansible-vault`, escritura segura, bloqueo de fichero | CLI `ansible-vault`, filesystem | **Gestión de secretos en reposo/transición** |
| `services/host_resolver.py` | Validación/pinning de objetivos, allowlist/redes prohibidas, resolución DNS única | `ipaddress`, `socket`, `config` | **Control de destinos** y prevención de SSRF/redes no autorizadas |
| `services/job_queue.py` | Encolado, backpressure, workers, cancelación cooperativa | hilos, memoria proceso | Disponibilidad y límites de ejecución |
| `services/host_locks.py` | Reservas por host con TTL en SQLite | `db.py` | Exclusión lógica entre lotes |
| `services/scheduler.py` | Programaciones, cifrado Fernet, claims atómicos, lock instancia | `db.py`, cryptography, ejecución | Ejecución desatendida y secretos programados |
| `services/ldap_auth.py` | Búsqueda/bind LDAPS, TLS, mapeo/errores | `ldap3`, `ssl`, `config` | Autenticación corporativa y confianza TLS |
| `services/rate_limiter.py` | Limitación por ventana deslizante en memoria | memoria proceso | Protección ante abuso; acotada al proceso |
| `services/csrf.py` | Token de sesión y verificación en peticiones mutables | sesión Flask | Protección CSRF |
| `services/notifier.py` | SMTP opcional con control de cifrado cuando hay credenciales | `smtplib`, `ssl` | Exfiltración/fuga en correo si mal configurado; resiliencia |
| `services/wallpaper.py` | Validación por firma, almacenamiento privado 0600, limpieza | filesystem, `config` | Carga de archivos y path safety |
| `services/ssh_host_keys.py` | `ssh-keyscan`, parseo/merge con lock, verificación de registro | `subprocess`, filesystem | TOFU/host key pinning operativo |

## Flujos principales

### 1. Autenticación
- `GET/POST /login` (`app.py`): rate limit por IP, bloqueo por cuenta, intento local con `User.check_password`, si falla y LDAP habilitado intenta `_try_ldap_login`; registro de eventos (`AuthEvent`) y sesión.
- Logout `POST /logout` con `@login_required`.
- **Límites de confianza:** credenciales usuario → Flask; opcionalmente Flask → directorio LDAPS; estado de sesión/cookies hacia navegador.

### 2. Autorización y navegación
- Decoradores `@login_required`, `@roles_required(...)` y comprobaciones explícitas (`has_role`, propiedad de tarea, `UI_CONFIG_PANEL`).
- Catálogo/tareas condicionado por rol y estado (`task_catalog`, `modules_panel`).

### 3. Ejecución manual (preview/apply)
- `index` POST / rutas de tarea / ejecución de aprobación → `handle_execution` → `_parse_operation_form` (validación común, `pin_target`, políticas, tapiz, firewall, contraseña, etc.) → gates de rol/destructiva/seguridad/idempotencia/aprobación.
- `create_task` + `JobQueue.submit(...)` → `execute_with_progress`/workers (detalles en `app.py`, no totalmente leídos aquí) → `ansible_runner` → parseo de marcadores/estados → `models/batch.py` + `task_state`/SSE.
- **Cancelación:** `POST /tasks/<task_id>/cancel` con control dueño/admin (`app.py:5046+`), delega en `get_job_queue().cancel`.

### 4. Progreso y SSE
- `GET /progress/<task_id>` y `GET /progress/<task_id>/events` (`progress_sse`) requieren login y `_can_view_task`; respuesta `text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- Estado consultable por `/api/task-status/<task_id>` (404 semántico/no confirma dueño según comentario de código).

### 5. Doble aprobación y operaciones destructivas
- Tareas destructivas activas: `contrasena` (admin). Gate en `handle_execution`: reserva/consumo de `ApprovalRequest` (`reserve_if_approved`, `confirm_reserved`, `release_reserved`), creación de solicitudes pendientes, hash de parámetros y payload reconstruible (`_approval_params/_approval_payload`).

### 6. Programación (scheduler)
- `services/scheduler.py`: cifrado de secretos con Fernet (clave gestionada en directorio de secretos), lock de instancia, scan/claim atómicos y lanzamiento periódico (integración con app/cola a confirmar en `app.py`).

### 7. Resolución de destinos
- `pin_target`/`validate_target`/`resolve_target` (`services/host_resolver.py`): normaliza y fija IP literal, valida CIDR/rango/hostname, aplica allowlist y rangos prohibidos, deduplica listas, limita tamaño.
- Hostnames: resuelve y exige que **todas** las direcciones sean permitidas; devuelve primera IP (pinning anti-TOCTOU DNS rebinding).

### 8. Gestión de secretos y credenciales
- Vault: `services/vault_manager.py` + CLI `ansible-vault`.
- Contraseñas de ejecución/prueba: ficheros extra-vars temporales 0600 y no log en playbooks (`ansible/contrasena.yml` usa `no_log`, stdin a `chpasswd`).
- Programaciones: cifrado de parámetros sensibles (Fernet, scheduler).

## Puntos críticos y observaciones tempranas (a profundizar en fases)

- **`app.py` monolítico:** alto riesgo de lógica duplicada, controles inconsistentes y dificultad de verificación exhaustiva.
- **Estado en proceso:** rate limiting, métricas, partes de ejecución/SSE/cola dependen del proceso; Gunicorn por defecto 1 worker (`gunicorn.conf.py`) pero configurable (`ACC_WORKERS`), con advertencia explícita sobre cancelación/estado.
- **LDAP TLS configurable:** `LDAP_TLS_VERIFY=False` conduce a `CERT_NONE` (`services/ldap_auth.py:145`).
- **Subprocesos externos:** Ansible/vault/ssh-keyscan; la seguridad depende de argumentos, entorno, ficheros temporales y parseo de salida (no se observó `shell=True` en código propio).
- **Validación de destinos:** diseño defensivo (pinning, allowlist, todas las IPs de hostname); requiere auditoría de casos límite IPv6/mapeadas/DNS y coherencia en rutas programadas/aprobadas.
- **Playbooks:** uso de `become`, comandos y marcadores en stdout; verificación debe basarse en marcadores/estado real y no solo en `rc`.

## Limitaciones del mapa

- No se reconstruyó un grafo de llamadas completo ni se trazaron todos los endpoints de `app.py` por falta de lectura íntegra.
- Integraciones reales en despliegue (Nginx externo, rutas de volúmenes, certificados efectivos, variables de entorno productivas) no verificadas dinámicamente.