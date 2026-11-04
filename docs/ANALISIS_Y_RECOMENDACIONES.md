# Análisis profundo y recomendaciones — Ansible Control Center

**Fecha:** 2026-11-03
**Alcance:** revisión de código, seguridad, corrección temporal, datos,
concurrencia, operación, UX, testing, despliegue y documentación.
**Estado analizado:** fases P0–P4.2 implementadas; 88 tests en verde.

> Este documento recoge el análisis por aspecto y las recomendaciones
> priorizadas. No describe únicamente lo implementado: también identifica
> riesgos, deuda técnica y el roadmap propuesto.
>
> **Actualización P4.1/P4.2:** C1, C3, C4, C6, C7, C8 y C9 se resolvieron en
> P4.1; C10 y la vista previa engañosa en DRY RUN, en P4.2. Los textos de la
> sección 3 se conservan como histórico del análisis original; las etiquetas
> **Resuelto** indican qué ya no describe el código actual. C2 sigue mitigado
> con aviso, no bloqueado, y C5 (cola/paralelismo/cancelación) sigue abierto.

---

## 1. Veredicto general

| Área | Nota orientativa | Comentario |
|---|---:|---|
| Seguridad base (CSRF, sesión, cabeceras) | 7/10 | Buen baseline; falta MFA/RBAC y allowlist de red |
| Corrección temporal/NTP | 7/10 | Queda `keep` + TIME (avisado, no bloqueado) |
| Modelo de datos/auditoría | 7/10 | Lotes con `dry_run`; faltan warnings/intentos por host y auditoría independiente |
| Concurrencia/escala | 4/10 | Secuencial, estado en memoria, SSE bloqueante |
| Resiliencia operativa | 6/10 | Reconciliación y presupuesto de tiempo; faltan cancelación y parada ordenada |
| UX | 7/10 | Banner, confirmación e idempotencia; faltan filtros y exportación |
| Testing/CI | 6/10 | 88 tests y CI; faltan lint, tipos y pruebas de playbook reales |
| Documentación | 8/10 | Muy buena; algunas inconsistencias |
| Mantenibilidad | 6/10 | `app.py` monolítico y duplicación de bucles |

**Conclusión:** es una herramienta usable y bastante segura para un operador de
confianza, pero **todavía no es una herramienta de producción para grupos
grandes** sin resolver los puntos críticos de abajo.

---

## 2. Fortalezas que conviene preservar

- Contraseñas de conexión en ficheros temporales `0600`, nunca en `argv`
  (`services/ansible_runner.py`, `services/password_tester.py`).
- Vault real con `ansible-vault`, escritura atómica y sin persistir la
  contraseña maestra.
- CSRF global, rate limiting, CSP con nonce, `no-store`, límites CIDR y
  validación con `ipaddress`.
- Migraciones SQLite no destructivas, WAL y `busy_timeout`.
- Ejecución best-effort real en todos los hosts y reintento de contraseñas.
- Modelo de lote con estado previo/posterior por host.
- Documentación y tests muy por encima de la media para este tipo de
  herramienta.

---

## 3. Hallazgos críticos y estado actual

> Texto original conservado como histórico. Las etiquetas de estado resumen
> el comportamiento actual.

### C1. `DRY_RUN` es invisible y no queda registrado — **Resuelto en P4.1/P4.2**

`ACC_DRY_RUN=1` es el valor por defecto, pero **ninguna plantilla lo
muestra** y `execution_batches` no tiene columna `dry_run`. El playbook
simulado devuelve `success=True`, `verified=True`
(`services/ansible_runner.py:391-408`). Un operador puede ver “éxito” y creer
que cambió la hora.

**Recomendación:** banner permanente “MODO SIMULACIÓN”, columna `dry_run` en
lote/host, badge en historial y detalle, y bloquear la ejecución real si el
operador no confirma que conoce el modo.

### C2. `TIME` con política NTP `keep` puede no mantenerse — **Mitigado, no bloqueado**

El playbook solo desactiva NTP para `off`, `restore` y `configure`
(`ansible/cambiar_hora.yml:105-113`). Con `keep`, NTP sigue activo y puede
corregir inmediatamente la hora fijada con `date -s` (`:139-146`). La
verificación puede pasar por tolerancia y luego el reloj vuelve a cambiar.

**Recomendación:** para `TIME`, exigir `restore`/`off` o desactivar/reactivar
NTP automáticamente; avisar en UI y en la vista previa si NTP está activo.

### C3. Arquitectura SSE + Gunicorn sync + estado en memoria — **Resuelto en P4.1**

- `gunicorn.conf.py:12` fija `timeout=120`.
- El SSE espera hasta `max_wait=300` (`app.py:503`).
- Las tareas viven en `_tasks`, un diccionario global en memoria
  (`app.py:105`).
- Con `workers=1` y `worker_class=sync`, una conexión SSE **bloquea todo el
  worker**.

**Riesgo:** el worker puede ser reciclado/matado, se pierde `_tasks`, el
progreso devuelve 404 y el lote queda “running” en BD para siempre.

**Recomendación:** worker `threads`/`gevent`, `timeout` > `max_wait`, estado de
tareas persistido (SQLite/Redis), fallback a polling
(`/api/task-status`) y heartbeat SSE.

### C4. Sin reconciliación de lotes huérfanos ni parada ordenada — **Resuelto en P4.1**

Si el proceso muere a mitad de lote, `finish_batch` no se ejecuta y el lote
queda `running`. No hay barrido al arranque ni manejo de señales.

**Recomendación:** al iniciar, marcar como `error`/`interrupted` los lotes
`running` con `created_at` antiguo; capturar `SIGTERM` para cerrar lotes en
curso; hacer `finish_batch` idempotente.

### C5. Ejecución secuencial sin cola ni cancelación — **Abierto; acotado en P4.2**

`_run_apply_batch` procesa host por host y contraseña por contraseña
(`app.py:904-1044`), con ping + playbook por candidata. Con 256 hosts y 200
contraseñas el peor caso es de horas. No hay cancelación, prioridad ni límite
global de lotes.

**Recomendación:** `ThreadPoolExecutor` con paralelismo configurable
(p. ej. 8–16), cola persistente, botón “Cancelar”, timeout por host y límite de
lotes concurrentes.

### C6. Hashes de contraseñas sin sal en SQLite — **Resuelto en P4.1**

`PasswordRecord.hash_full` usa SHA-256 sin sal (`models/password.py:22-29`). Si
se filtra la BD, las contraseñas candidatas (a menudo de baja entropía) son
atacables offline.

**Recomendación:** HMAC-SHA256 con una clave del servidor (pepper) o Argon2id
con sal por registro. Mantener el hash corto solo como identificador derivado.

### C7. No hay allowlist de redes objetivo — **Resuelto en P4.1**

Cualquier usuario autenticado puede apuntar a cualquier IP/CIDR
(`services/host_resolver.py:150-183`). En una herramienta que prueba
credenciales y cambia la hora, esto es un riesgo de abuso interno.

**Recomendación:** `ACC_ALLOWED_NETWORKS` (lista de CIDR); rechazar
objetivos fuera de ella y registrar el intento.

### C8. Sin confirmación ni protección frente a doble envío — **Resuelto en P4.1**

El formulario ejecuta directamente (`templates/index.html:161-177`). Un doble
clic crea dos lotes y puede lanzar dos cambios simultáneos.

**Recomendación:** modal de confirmación con número de hosts y resumen de
acción; deshabilitar el botón al enviar; token de idempotencia por operación.

### C9. `restore` reactiva NTP aunque estuviera apagado — **Resuelto en P4.1**

En `cambiar_hora.yml:219-226`, con `restore` + `TIME` la condición
`(accion == 'TIME')` fuerza la reactivación, sin mirar `hora_antes.ntp`.
“Restaurar” debería devolver el estado previo, no encender NTP siempre.

**Recomendación:** guardar `hora_antes.ntp` y restaurar exactamente ese valor;
probar el caso “NTP estaba off”.

### C10. La API legacy no verifica el resultado — **Resuelto en P4.2**

`run_ansible_playbook` considera éxito solo con `returncode == 0`
(`services/ansible_runner.py:275-282`), ignorando `HORA_VERIFIED`.
`test_passwords_single` usa esa ruta (`services/password_tester.py:94-100`).
La UI usa `apply_host`, pero cualquier integración futura puede reportar éxito
falso.

**Recomendación:** deprecar la ruta legacy o hacer que delegue en `apply_host`;
añadir test que demuestre que `HORA_VERIFIED=false` no es éxito.

---

## 4. Análisis por aspecto y recomendaciones

### 4.1. Arquitectura y mantenibilidad

- `app.py` tiene 1.634 líneas y mezcla rutas, validación, workers, SSE y CLI.
  **Recomendación:** app factory + blueprints (`auth`, `operations`, `batches`,
  `passwords`) y mover workers a `services/execution_service.py`.
- `_run_preview_batch` y `_run_apply_batch` duplican el bucle host/contraseña.
  **Recomendación:** extraer un iterador/estrategia común y un
  `OperationRequest` (dataclass) en lugar de 14 parámetros.
- Uso de `assert` para control de flujo (`app.py:723`, `models/batch.py:336`,
  `models/user.py:79`). Con `python -O` desaparecen. **Recomendación:**
  excepciones explícitas.
- `db_path: str = None` y `_default_db_path` repetido en cada modelo.
  **Recomendación:** `Optional[str]` y un helper común.
- Mezcla de nombres español/inglés (`accion`, `valor`, `modo_hora` vs
  `time_mode`). **Recomendación:** mantener el contrato del playbook y
  documentar el mapeo, o renombrar todo a inglés.
- Sin `pyproject.toml`, `ruff`, `black`, `mypy` ni pre-commit.
  **Recomendación:** añadirlos y fijar estilo en CI.

### 4.2. Seguridad y autenticación

- **MFA/SSO:** para una herramienta que puede cambiar la hora de servidores,
  añadir TOTP/OIDC es la mejora de mayor impacto.
- **RBAC:** hoy hay un único rol admin. Añadir roles (`viewer`, `operator`,
  `approver`, `admin`) y aprobaciones para lotes grandes.
- **Sesión:** resuelto en P4.1 (`session.permanent`, `REMEMBER_COOKIE_SECURE`
  y duración). Queda usar `_form_bool` para `remember` y valorar
  `session.clear()` antes de `login_user`.
- **Fijación de sesión:** hacer `session.clear()` antes de `login_user`.
- **Bloqueo de cuenta:** el rate limit es solo por IP y en memoria. Añadir
  intentos por usuario, bloqueo temporal y auditoría de logins fallidos.
- **Allowlist de red:** implementada en P4.1; revisar que cubra todos los
  destinos y registrar los rechazos.
- **Host key checking:** está activo por defecto, pero no hay provisión de
  `known_hosts`; el primer acceso falla. Añadir `ssh-keyscan` guiado o un
  `known_hosts` gestionado.
- **Contraseña de sudo distinta de SSH:** hoy se reutiliza la misma
  (`ansible_become_password = ansible_password`). Soportar credenciales
  separadas.
- **Cabeceras:** `no-store` ya está en HTML y en `/api/task-status`. Valorar
  `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy` y
  `X-Permitted-Cross-Domain-Policies`.
- **CSP/CDN:** Bootstrap, iconos y Google Fonts vienen de CDN sin SRI
  (`templates/base.html:9-17`). En una herramienta de infraestructura suele
  haber redes cerradas. **Recomendación:** self-hosting + SRI.
- **Auditoría:** no hay tabla de eventos. Añadir `audit_events` con actor, IP,
  user-agent, batch, acción, resultado y timestamp.

### 4.3. Secretos y vault

- ~~Hashes sin sal (C6).~~ Resuelto con HMAC-SHA256 y pepper; queda valorar
  Argon2id con sal por registro si se quiere resistencia a GPU.
- **Concurrencia del vault:** `encrypt_passwords` y `remove_password` hacen
  read-modify-write sin `flock`; dos operaciones simultáneas pueden perder
  datos. Añadir bloqueo de fichero.
- **Consistencia al borrar:** primero se actualiza el vault y luego SQLite; si
  falla SQLite, quedan desincronizados. Hacer borrado con compensación o
  transacción lógica.
- **Texto plano temporal:** `_write_encrypted` escribe el vault descifrado en
  `TEMP_DIR` antes de cifrar. Es inevitable con la CLI, pero conviene que
  `TEMP_DIR` esté en `tmpfs` (`/run`) y no en disco persistente.
- **Unicidad:** añadir índice único en `passwords.password_hash_full` y en
  `execution_hosts(batch_id, host)`.
- **Rotación:** documentar rotación de la contraseña maestra y del
  `SECRET_KEY`.
- **Backend de secretos:** a mayor escala, valorar HashiCorp Vault/AWS Secrets
  Manager en lugar de un fichero local.

### 4.4. Ansible y playbooks

- **Parseo de marcadores de texto:** funciona, pero es frágil. Valorar
  `ANSIBLE_STDOUT_CALLBACK=json` o `ansible-runner` para resultados
  estructurados.
- **`LC_ALL=C`:** añadir al entorno de Ansible para que `date -d` no dependa
  del locale del host.
- **Validación en el playbook:** añadir `assert` de formato para `valor` y para
  `ntp_servers` (defensa en profundidad si se invoca el playbook directamente).
- **Limpieza de configuración NTP:** al pasar de `configure` a `keep`/`off`,
  los ficheros `90-ansible-control-center.sources`/`.conf` permanecen. Eliminarlos o
  documentarlo.
- **Contenedores/VMs:** detectar `systemd-detect-virt --container` y evitar
  `hwclock`; hoy solo se avisa si falla.
- **RTC local vs UTC:** `hwclock --systohc` asume UTC. Si `LocalRTC=yes`, usar
  `--localtime` o documentar la limitación.
- **`changed_when`:** varias tareas marcan `changed` por `rc == 0` aunque no
  cambien nada. Ajustar para que `HORA_CHANGED` sea fiel.
- **IPv6:** `-i f"{host},"` puede no funcionar con IPv6; probar y, si no, usar
  `[addr]` o restringir a IPv4.
- **Salida:** `_playbook_error` devuelve stdout/stderr completos y se guarda en
  BD sin límite. Capar y redactar.
- **`no_log`:** no hay secretos en tareas actuales, pero añadir `no_log: true`
  en cualquier tarea futura que los manipule.
- **Lint:** añadir `ansible-lint` y `yamllint` con configuración.

### 4.5. Corrección temporal y NTP

- **Canonicalización de zonas:** comparar `UTC` y `Etc/UTC` como equivalentes;
  validar que la zona existe con `zoneinfo.available_timezones()` antes de
  enviar.
- **`TIME` + `keep`:** ver C2.
- **`restore`:** ver C9.
- **Verificación NTP:** solo se comprueba el booleano `ntp`. Verificar también
  los servidores configurados y `NTPSynchronized` tras un periodo de gracia.
- **Verificación RTC:** no se verifica. Leer `hwclock --show` después de
  `--systohc` y comparar.
- **Salto máximo:** añadir un guardrail (`ACC_MAX_TIME_JUMP`) que exija
  `force` si el salto supera, por ejemplo, 24 h.
- **DST:** en modo local, documentar el comportamiento en horas
  ambiguas/inexistentes.
- **Precisión:** el formulario solo permite minutos. Valorar segundos para
  operaciones de precisión.
- **Preview:** `_would_change` no compara servidores NTP ni contempla
  `sync_rtc` (`app.py:437-440`). Ampliar el plan.
- **`DEFAULT_NTP_SERVERS`:** si el campo va vacío se usa la lista por defecto
  **sin validar** (`app.py:340-341`). Validar también ese fallback.

### 4.6. Modelo de datos y auditoría

El lote actual es correcto, pero insuficiente para auditoría real. Añadir en
una migración v4:

**En `execution_batches`:**

- `dry_run`, `admin_user`, `password_origin`, `ntp_servers`, `sync_rtc`,
  `force`
- `client_ip`, `request_id`, `app_version`, `playbook_version`
- `request_payload` (JSON completo)
- `error_summary`

**En `execution_hosts`:**

- `changed`, `verified`, `warnings`, `deviation`, `attempts`
- `started_at`, `finished_at`
- `output_excerpt` (truncado y redactado)

**Otras:**

- `UNIQUE(batch_id, host)` para garantizar “un resultado final por host”.
- `audit_events`.
- Backfill de `executions` antiguas a lotes o vista separada.
- Implementar `HISTORY_RETENTION_DAYS`, hoy definido pero **sin purga de
  lotes**. `JOB_RETENTION_HOURS` ya se aplica al arrancar a las instantáneas de
  tareas.
- Las estadísticas ya separan ejecuciones reales de simuladas; falta separar
  previews y añadir filtros por operador/fecha.
- Añadir paginación, filtros (estado, operador, fecha, objetivo) y exportación
  CSV/JSON.

### 4.7. Concurrencia, rendimiento y escala

- Paralelizar hosts (C5) o usar una sola ejecución Ansible con inventario y
  `serial`/`forks` y callback JSON.
- Cola persistente (RQ/Celery/arq) para trabajos largos; el proceso web no
  debería ser el dueño del trabajo.
- Estado de tareas fuera de memoria (C3).
- Añadir `ACC_MAX_PARALLEL_HOSTS` y
  `ACC_MAX_CONCURRENT_BATCHES`.
- Limitar el tamaño de la salida y del log en memoria (`_append_log` ya limita
  a 200 líneas, bien).
- Rate limiter compartido (Redis) si se escala a varios workers.
- Índices: `execution_hosts(batch_id, host)`,
  `execution_batches(created_at, status)`, `executions(timestamp)` ya existe.

### 4.8. Resiliencia y operación

- Reconciliación de lotes huérfanos (C4).
- Cancelación y reintento selectivo (“reintentar hosts fallidos”).
- Reintentos con backoff para errores transitorios.
- `/healthz` y `/readyz` (BD, vault, binarios Ansible).
- Métricas Prometheus y logs estructurados con `request_id`.
- Alertas/webhooks al terminar un lote (especialmente si hay fallos).
- Backup automatizado de SQLite (`.backup`) y vault; probar restauración.
- Runbook: qué hacer si un lote falla, cómo revertir hora/zona, cómo
  restaurar.
- **Rollback:** guardar `before_state` ya se hace; ofrecer un botón “revertir al
  estado anterior” por host/lote.

### 4.9. UX/frontend

- Preservar todos los parámetros al volver de un error o al pulsar “Ejecutar
  esta configuración”: hoy se pierden `admin_user`, `password_origin`,
  `ntp_servers`, `sync_rtc` y `force` (`templates/batch.html:170-173`,
  `app.py:664-675`).
- Vista previa más rica: diff antes→después, servidores NTP, RTC, “no
  cambiaría nada”.
- Progreso por host en vivo (tabla), no solo log.
- Historial con filtros, paginación, búsqueda y exportación.
- Accesibilidad: `aria-live` en progreso, foco en errores, estados de carga.
- Sustituir `alert()` por toasts/modales.
- Fallback de SSE a polling si falla la conexión.
- Mostrar advertencias y desviación por host (requiere modelo ampliado).

### 4.10. Testing y CI

Cobertura actual: 88 tests, buena base. El CI ya existe (Python 3.11–3.13,
compilación, YAML y pytest). Faltan:

- Linters y tipos en CI: `ruff`, `black --check`, `mypy`, `ansible-lint`,
  `yamllint`, `pip-audit`, `bandit`, `pytest --cov`.
- Tests de los marcadores del playbook con stdout grabado (la ruta legacy ya
  exige `HORA_VERIFIED`; falta cubrir `HORA_VERIFIED=false`).
- Test de integración con un contenedor Ubuntu y Ansible real.
- Tests de `finish_batch` idempotente, estados límite de `_derive_status`,
  presupuesto de tiempo agotado a mitad de lote y concurrencia de lotes.
- Tests de seguridad: CSRF en cada ruta POST, redacción de secretos en logs,
  ownership de tareas.
- Tests de concurrencia y de lotes grandes.
- `conftest.py` usa `setdefault`; debería **forzar** las rutas temporales para
  que un `.env` del desarrollador no haga que los tests toquen rutas reales.
- `pytest-cov` está en `requirements.txt` pero no instalado en el venv actual;
  CI lo resolverá.
- Limpiar el `mkdtemp` de `conftest.py` al terminar.

### 4.11. Despliegue y configuración

- **Inconsistencia de usuario:** `install.sh` crea directorios y `.env` para el
  usuario que lo ejecuta (`install.sh:76-98`), pero el unit de systemd usa
  `User=ansible-control-center` (`DEPLOYMENT.md:171`). El servicio no podrá leer `.env` ni
  escribir la BD. **Recomendación:** crear el usuario de servicio en
  `install.sh` o documentar el `chown` explícito.
- **Hardening systemd:** añadir `NoNewPrivileges`, `PrivateTmp`,
  `ProtectSystem=strict`, `ProtectHome`, `ReadWritePaths`, `UMask=0077`,
  `TimeoutStopSec`.
- **Fail-closed en producción:** hoy, si no hay permisos, se cae
  silenciosamente a `instance/`. Añadir `ACC_ENV=production` y abortar
  si las rutas de producción no son escribibles.
- **Lockfile:** `requirements.txt` con rangos amplios; usar `pip-tools`/`uv`
  con hashes para reproducibilidad.
- **`install.sh`**: no instala `sshpass` (solo avisa); valorar instalarlo o
  fallar en producción.
- **Healthcheck** y **backup timer** en el despliegue.
- **Logs:** RotatingFileHandler para la app; gunicorn a journald. Añadir
  retención y rotación documentada.

### 4.12. Documentación y gobierno

- `README.md:31` decía “versión actual: 2” cuando el esquema ya es 3.
  Corregido en la documentación actual.
- `ANALISIS.md` estaba sin trackear; se incorpora al repositorio.
- Añadir `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md` y `SECURITY.md`.
- Añadir modelo de amenazas y ADRs para: UTC/local, política NTP, semántica de
  grupo, retención.
- Runbook operativo y procedimiento de rollback.
- Documentar troubleshooting: `known_hosts`, `sshpass`, sudo sin TTY, chrony
  vs timesyncd, contenedores sin `hwclock`.
- Documentar el contrato de marcadores `HORA_*` y las variables del playbook.

---

## 5. Roadmap recomendado

### P4.1/P4.2 — Hardening (completado)

1. ✅ Banner + columna `dry_run` + métricas separadas.
2. ✅ `restore` fiel y aviso de `TIME` + `keep` (bloqueo pendiente).
3. ✅ Worker `gthread`, timeout > SSE, estado de tareas persistido.
4. ✅ Reconciliación de lotes y tareas huérfanas al arrancar.
5. ✅ Confirmación + idempotencia de envío.
6. ✅ Allowlist de redes.
7. ✅ Hashes con pepper.
8. ✅ Cookies y `session.permanent`.
9. ✅ Cap/redacción de salida, `no-store` y vista previa real en simulación.
10. ✅ Tests de regresión; quedan linters/tipos y pruebas de integración real.

Pendiente inmediato: bloquear `TIME` + `keep`, tope de intentos ya aplicado y
presupuesto de tiempo por lote ya aplicado.

### P5 — Operación (3–5 semanas)

- Paralelismo/cola, cancelación, reintento de fallidos.
- RBAC, MFA/SSO, aprobaciones y auditoría en BD.
- Historial con filtros, paginación y exportación.
- Vista previa enriquecida y rollback.
- `/healthz`, métricas, alertas y logs estructurados.
- API/CLI e idempotency keys.
- Inventarios guardados, CSV, grupos y exclusiones.
- Canario, ventanas de mantenimiento y programación.

### P6 — Multiplataforma

- RHEL/Rocky (`timedatectl`), Windows (`w32tm`), dispositivos de red.
- Casos especiales de VMs/contenedores.

---

## 6. Quick wins

Completados en P4.1/P4.2: banner y `dry_run`, cookies, `restore`, reconciliación
al arrancar, `no-store`, confirmación/idempotencia, allowlist, pepper y vista
previa real en simulación.

Pendientes rápidos:

1. Validar `admin_user`, que la zona horaria exista y los servidores NTP.
2. Índices únicos `(batch_id, host)` y `password_hash_full`.
3. Bloquear `TIME` + `keep` si NTP está activo.
4. Self-hosting con SRI de Bootstrap/iconos/fuentes.
5. Exportación CSV/JSON del historial.

---

## 7. Decisiones pendientes de confirmar

Para priorizar bien, hace falta decidir:

1. **Alcance de plataformas:** ¿solo Ubuntu o también RHEL/Windows?
2. **Escala real:** ¿cuántos hosts por lote y cuántos operadores simultáneos?
3. **Gobierno:** ¿hacen falta MFA, roles y aprobaciones de dos personas?
4. **Red:** ¿hay que restringir objetivos a una allowlist corporativa?
5. **Ejecución real:** ¿se va a poner `DRY_RUN=0` pronto o sigue en simulación?
6. **Retención:** ¿cuánto tiempo hay que conservar lotes y auditoría?
7. **Integración:** ¿inventario Ansible/NetBox/CSV, API/CLI, notificaciones?

---

## 8. Referencias de código

| Elemento | Ruta |
|---|---|
| Rutas, validación, workers y SSE | `app.py` |
| Configuración central | `config.py` |
| Lotes y resultados por host | `models/batch.py` |
| Esquema SQLite y migraciones | `models/db.py` |
| Ejecución segura de Ansible | `services/ansible_runner.py` |
| Prueba de contraseñas | `services/password_tester.py` |
| Resolución de IP/CIDR | `services/host_resolver.py` |
| Vault con `ansible-vault` | `services/vault_manager.py` |
| Ficheros temporales `0600` | `services/secure_temp.py` |
| CSRF | `services/csrf.py` |
| Rate limiting | `services/rate_limiter.py` |
| Playbook de cambio | `ansible/cambiar_hora.yml` |
| Playbook de inspección | `ansible/estado_hora.yml` |
| Lectura de estado | `ansible/tasks/leer_estado.yml` |
| Formulario principal | `templates/index.html` |
| Detalle de lote | `templates/batch.html` |
| Historial | `templates/history.html` |
| Progreso SSE | `templates/progress.html` |
