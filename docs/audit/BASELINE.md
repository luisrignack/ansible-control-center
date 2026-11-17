# Línea base de Ansible Control Center (FASE 0)

Fecha de inspección: 2026-11-16
Repositorio: `ansible-control-center`
Rama: `master`
Commit: `83228cf4aa6fd369f06657ce3abd881d601ef060` (`chore(release): publica la 0.9.9`)
Árbol de trabajo inicial: limpio.
Versión declarada: `VERSION=0.9.9`.
Esquema SQLite inicial: **v20** (`PRAGMA user_version`). Tras las mejoras de la
FASE 3/4 de esta auditoría el esquema pasa a **v21** (ver `DATABASE_HARDENING.md`).

> Esta línea base describe el estado **anterior** a las modificaciones. Los
> cambios introducidos se detallan en `docs/IMPLEMENTATION_REPORT.md`.

## 1. Propósito y alcance

ACC es una aplicación web interna para ejecutar playbooks de Ansible sobre
estaciones Linux (Ubuntu) y Windows (WinRM) desde un catálogo cerrado:
zona horaria, hora exacta/NTP, contraseña local, tapiz de escritorio,
actualización APT y firewall. Incluye inventario por grupos, programaciones,
aprobaciones de tareas destructivas, vault de credenciales, historial por
usuario, informes PDF y auditoría de autenticación.

La inspección cubrió todo el árbol versionado: 111 ficheros Python (57
módulos de aplicación, 46 de pruebas), 17 playbooks/tareas de Ansible,
plantillas Jinja, estáticos, Docker, Nginx, CI y documentación.

## 2. Arquitectura real

### 2.1 Capas

```
Navegador
   │ HTTPS (Nginx, TLS 1.2/1.3)
   ▼
Gunicorn (1 worker gthread × 8 hilos por defecto)
   │
   ├── Flask app factory ............ acc/bootstrap.py
   │     ├── acc/web/*.py ........... rutas por dominio (registry)
   │     ├── acc/security.py ........ RBAC, propiedad, CSRF helpers
   │     ├── acc/execution.py ....... orquestación de lotes
   │     ├── acc/operations.py ...... validación de formularios
   │     ├── acc/scheduling.py ...... programaciones y lanzamiento
   │     ├── acc/task_state.py ...... estado de tareas + SQLite
   │     └── acc/runtime.py ......... estado de proceso, cola, vault
   │
   ├── models/ ...................... SQLite (WAL) sin ORM
   │     db, user, batch, execution, ops, password
   │
   ├── services/ .................... integraciones y utilidades
   │     ansible_runner, host_resolver, host_locks, job_queue,
   │     scheduler, vault_manager, ldap_auth, csrf, rate_limiter,
   │     secure_temp, ssh_host_keys, password_tester, notifier,
   │     pdf_report, task_catalog, wallpaper, metrics, audit_chain
   │
   └── Ansible (subprocess argv, shell=False)
         ansible-playbook -i <ip>, --extra-vars @<tmp 0600>
```

No hay ORM, ni Redis, ni Celery, ni microservicios, ni workers separados por
diseño. El estado de las tareas vive en memoria del proceso y se replica en
SQLite para sobrevivir a reinicios. La cola de trabajos es un `queue.Queue`
acotado servido por hilos daemon.

### 2.2 Punto de entrada y compatibilidad

- `app.py` crea la app con `acc.bootstrap.create_app()` y **reexporta** los
  símbolos históricos (`create_task`, `update_task`, `_get_task`, etc.) para
  que `flask --app app`, `gunicorn app:app` y las pruebas sigan funcionando.
- `acc/web/registry.py` permite declarar rutas con `@app.route` sobre un
  registro y aplicarlas después a la app real conservando el nombre de
  endpoint (`url_for` estable).

### 2.3 Módulos y responsabilidades

| Componente | Ficheros | Responsabilidad |
|---|---|---|
| Arranque/config | `acc/bootstrap.py`, `config.py`, `gunicorn.conf.py` | Factory, validación de producción, arranque del scheduler, recuperación de estado, hooks de apagado |
| Autenticación | `acc/web/auth.py`, `models/user.py`, `services/ldap_auth.py` | Login local (scrypt vía Werkzeug), LDAPS, bloqueo, rate limit, sesión, logout, healthz |
| Autorización | `acc/security.py`, decoradores `roles_required` | Roles `viewer`/`operator`/`admin`, propiedad por usuario, scoping de historial |
| Web | `acc/web/{tasks,inventory,schedules,passwords,admin,approvals,system,errors,hooks}.py` | Rutas, plantillas, cabeceras, CSRF, métricas |
| Catálogo | `services/task_catalog.py`, `ansible/*.yml` | Definición declarativa de tareas y playbooks |
| Validación | `acc/operations.py`, `acc/safety.py`, `services/safety.py` | Allowlists de entrada, criticidad, ventanas de mantenimiento |
| Ejecución | `acc/execution.py`, `services/ansible_runner.py` | Lotes por host, locks, aprobaciones, streaming, timeouts |
| Red | `services/host_resolver.py`, `services/ssh_host_keys.py` | Validación de destinos, allowlist, pin a IP, known_hosts |
| Persistencia | `models/db.py`, `models/*.py` | SQLite WAL, 21 migraciones, repositorios |
| Cola/scheduler | `services/job_queue.py`, `services/scheduler.py` | Concurrencia acotada, cancelación, ejecuciones desatendidas |
| Secretos | `services/vault_manager.py`, `services/secure_temp.py`, `models/password.py` | Fernet, Ansible Vault, temporales 0600, hashes HMAC-SHA256 |
| Observabilidad | `services/metrics.py`, `acc/web/hooks.py`, `acc/web/admin.py` | Ventana de latencias por endpoint, `/healthz`, `/api/metricas`, `/metrics` |
| Informes | `services/pdf_report.py` | PDF mínimo sin dependencias externas |
| Auditoría | `models/user.py::AuthEvent`, `services/audit_chain.py` | Eventos de autenticación/administración y cadena SHA-256 |

## 3. Flujos críticos

### 3.1 Login local / LDAPS

1. Rate limit por IP (`LOGIN_RATE_LIMIT`/ventana) y bloqueo por cuenta tras
   `LOGIN_LOCKOUT_THRESHOLD` fallos durante `LOGIN_LOCKOUT_MINUTES`.
2. Local: `check_password_hash` (scrypt de Werkzeug).
   LDAPS: bind de servicio → búsqueda del uid → rebind con la contraseña del
   usuario; `CERT_REQUIRED` por defecto; no se guarda la contraseña.
3. `login_user`, regeneración de sesión y rotación de token CSRF.
4. `AuthEvent.record(...)` deja constancia del intento.

### 3.2 Ejecución manual

1. `POST /execute` (rol `operator`) → `handle_execution`.
2. Validación de tarea, acción, valor, NTP, grupo, criticidad y ventana en
   `acc/operations.py`; allowlist de red con `pin_target`/`resolve_target`.
3. Las tareas destructivas crean una `ApprovalRequest` (un admin distinto
   debe aprobarla) o consumen una aprobación vigente con el mismo hash de
   parámetros.
4. Se crea un `batch_id` UUID, se toma un lock de host en SQLite por IP y se
   encola el trabajo en la cola acotada.
5. `ansible-playbook` se invoca con lista de argumentos, sin `shell=True`.
   Contraseñas y claves van en un JSON temporal 0600 pasado con
   `--extra-vars @fichero`; el fichero se borra siempre.
6. Cada host se procesa en paralelo acotado (`BATCH_HOST_CONCURRENCY`), con
   timeout y cancelación cooperativa. Los resultados se guardan en
   `execution_hosts` y los contadores del lote en `execution_batches`.
7. El progreso se publica por SSE (`/progress/<task_id>/events`) y se replica
   en `task_state` para sobrevivir a reinicios.

### 3.3 Vista previa

Mismo flujo con `default_mode="preview"`; los cambios no se aplican y el lote
queda en estado `preview`. No requiere aprobación destructiva.

### 3.4 Programaciones

`services/scheduler.py` revisa `schedules` con un lock de instancia. Las
contraseñas maestras/nuevas se guardan cifradas con Fernet y solo se descifran
al disparar. Las programaciones heredan el dueño del lote para el historial.

### 3.5 Aprobaciones

`approvals` guarda `params_hash` (canónico) y `payload` sin secretos. La
aprobación caduca (`APPROVAL_TTL_MINUTES`) y solo se consume una vez
(`consumed_at`). El ejecutor reenvía el payload aprobado para que un cambio de
formulario no amplíe el alcance.

### 3.6 Inventario

`HostGroup`/`InventoryHost` con propietario (`owner_user_id=0` = compartido),
criticidad, ventana de mantenimiento y clave SSH opcional. Los hosts se
validan como IPv4 literal y se pinean a IP en el momento de ejecutar.

## 4. Esquema SQLite (v20 inicial)

Tablas: `users`, `passwords`, `executions`, `execution_summaries`,
`execution_batches`, `execution_hosts`, `task_state`, `auth_events`,
`app_settings`, `inventory_groups`, `inventory_hosts`, `schedules`,
`approvals`, `host_locks` y tablas auxiliares de la demo.
Migraciones `_migrate_v1` … `_migrate_v20`; `init_schema` es idempotente y no
rebaja `user_version`. La v20 atribuye el historial antiguo por `operator` y
por dueño de la programación.

Configuración de conexión: `busy_timeout=5000`, `foreign_keys=ON`,
`journal_mode=WAL`, ficheros `0600`.

## 5. Despliegue e infraestructura

- `Dockerfile` multi-stage ligero (`python:3.12-slim`), usuario `acc` no root,
  `tini` como PID 1, `sshpass`/`openssh-client` para Ansible.
- `docker-compose.yml`: `cap_drop: ALL`, `no-new-privileges`, volúmenes
  separados para datos, secretos y logs, `ACC_ENV=production` por defecto.
- `docker/nginx/default.conf`: redirección 80→443, TLS 1.2/1.3, HSTS,
  `server_tokens off`, límite de cuerpo 3 MB, SSE sin buffering.
- `gunicorn.conf.py`: 1 worker `gthread`, 8 hilos, timeout 330 s, sin acceso
  directo desde fuera del contenedor.
- CI (`.github/workflows/ci.yml`): comprobación SemVer/tag, Ruff, Mypy,
  Bandit, Pytest con cobertura mínima 78 %, `pip-audit` sobre el lock,
  construcción de las dos imágenes Docker.

## 6. Dependencias de ejecución

Flask, Flask-Login, Werkzeug, python-dotenv, gunicorn, ansible-core, pywinrm y
`cryptography` (declarada; también transitiva de ansible-core). Herramientas de
pruebas/calidad separadas en `requirements-test.txt` y `requirements-dev.txt`.
No hay dependencias de frontend por CDN: Bootstrap y Bootstrap Icons están
vendorizados.

## 7. Estado de pruebas y análisis (línea base)

| Comprobación | Resultado inicial |
|---|---|
| `pytest` completo | **592 passed, 1 skipped** (~65 s) |
| `ruff check .` | limpio |
| `mypy` | limpio (57 módulos) |
| `bandit -ll` | sin hallazgos medios/altos |
| Cobertura CI | umbral 78 % |

Con las mejoras de esta auditoría la suite pasa a **609 passed, 1 skipped**
(17 pruebas nuevas).

## 8. Limitaciones y problemas conocidos (antes de la auditoría)

1. **Historial privado vs. vista global**: el listado global de administración
   enlazaba a `/batches/<id>` sin conservar `all=1`; el detalle respondía 404
   para lotes de otros usuarios aunque el admin ya los veía en el listado.
2. **Auditoría sin integridad**: `AuthEvent` no tenía hash ni cadena; un
   `sqlite3.Error` se ignoraba en silencio. No había detección de borrado ni
   exportación/anclaje.
3. **Interrupción ≠ fallo**: tareas y lotes huérfanos se marcaban `error`
   genérico sin indicar que el resultado remoto era desconocido.
4. **Cola en memoria**: un reinicio pierde los trabajos encolados. Es una
   limitación de diseño aceptada y documentada; no se implementó una cola
   durable para no introducir Redis/Celery.
5. **Rate limit por proceso**: con varios workers cada uno mantiene su ventana.
   El despliegue por defecto usa un solo worker.
6. **Migraciones con `executescript`**: SQLite hace commit implícito entre
   sentencias; una migración interrumpida puede dejar esquema parcial. Todas
   son idempotentes y `user_version` solo se actualiza al final.
7. **Sin MFA ni recuperación de contraseña self-service**: solo un admin puede
   restablecer una cuenta local.
8. **Inventario sin importación/exportación CSV** ni datos de última ejecución
   visibles; sin etiquetas más allá de `label` y criticidad.
9. **Sin endpoint Prometheus**: solo `/api/metricas` en JSON para admins.
10. **Catálogo no expuesto como API declarativa**: la información existe en
    `services/task_catalog.py`, pero no hay contrato JSON ni metadatos de
    verificación/reversibilidad.
11. **Sin ejecución canary por etapas**: el lote procesa todos los hosts con
    concurrencia acotada, sin fases porcentuales ni parada automática por
    umbral de errores.

## 9. Riesgos iniciales priorizados

| Riesgo | Severidad | Estado |
|---|---|---|
| Enlaces rotos del historial global (denegación de acceso autorizado) | MEDIUM | Corregido |
| Auditoría manipulable/sin evidencia de borrado | MEDIUM | Mitigado con cadena + checkpoints |
| Reintento manual de operación interrumpida no idempotente | MEDIUM | Mitigado con estado `interrupted` |
| Fuerza bruta distribuida entre procesos | LOW | Documentado |
| Migración interrumpida | LOW | Documentado, idempotencia verificada |
| Falta de MFA para administradores | MEDIUM (potencial) | Documentado, no implementado |

## 10. Conclusión de la fase

La arquitectura está bien delimitada y ya incorpora controles sólidos
(argumentos estructurados, pin de IP, allowlist, CSRF, cabeceras, cookies
seguras, aprobaciones, locks por host, temporales 0600). No se requiere
reescribir ni sustituir Flask/SQLite. Las brechas relevantes son de
**integridad de auditoría**, **representación de interrupciones**, **acceso
autorizado a informes** y **funcionalidad de inventario/observabilidad**.
