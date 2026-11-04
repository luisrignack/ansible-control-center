# Revisión técnica y propuesta de producto — Ansible Control Center

**Fecha:** 3 de noviembre de 2026
**Alcance:** código, playbooks, datos, seguridad, operación, pruebas, documentación y experiencia de usuario.
**Naturaleza:** auditoría de solo lectura. No se modificó el código de la aplicación.
**Verificación:** `.venv/bin/python -m pytest -q` → **88 pruebas superadas**, 1 aviso de deprecación (`datetime.utcnow()` en Flask-Login).

---

## 1. Veredicto ejecutivo

Ansible Control Center es un proyecto **funcionalmente ambicioso y bien encaminado**: cubre el ciclo completo (autenticación → vault → vista previa → ejecución por lotes → verificación → auditoría) y tiene una base de pruebas real. La arquitectura, sin embargo, está en el punto donde el prototipo deja de escalar: un `app.py` de 2.188 líneas, hilos sin control, estado en memoria, ejecución secuencial y una interfaz que obliga a rellenar un formulario de 10+ campos.

La aplicación **no debería considerarse lista para producción** sin resolver antes cuatro frentes:

1. **Seguridad de destino**: por defecto se puede apuntar a cualquier red (incluido `localhost` y metadatos cloud), y la resolución DNS se valida y se usa en momentos distintos (TOCTOU).
2. **Corrección temporal**: en varias combinaciones de `TIME` + NTP, el objetivo puede revertirse inmediatamente; la tolerancia por defecto de 120 s es demasiado laxa para una herramienta cuyo propósito es fijar la hora.
3. **Concurrencia y ciclo de vida**: un hilo demonio por petición, sin cola, sin cancelación ni límite; el estado vive en memoria y el diseño asume un solo worker.
4. **Cadena de suministro**: el paquete `dist/` incluye una imagen de 177 MB anterior al último commit, y esa imagen entra en el contexto de Docker.

**Nota global: 6,5/10.** Base sólida, riesgo operativo alto. Con las correcciones P0/P1 de este documento puede llegar a 8,5/10 sin reescribir el producto.

### Lo que está bien

- Flujo de confirmación explícito, idempotencia de envío y botón de vista previa.
- Credenciales inyectadas por fichero temporal `0600` + `--extra-vars @`, nunca por línea de comandos.
- CSRF ligado a sesión, cabeceras de seguridad, rate limiting y cookies endurecidas.
- Migraciones versionadas, WAL, `busy_timeout`, resultados por host y estados antes/después.
- Playbook idempotente, `become`, comandos con `argv` (sin shell) y verificación posterior.
- Documentación muy por encima de la media y CI multi-versión de Python.

---

## 2. Tabla de hallazgos priorizada

| ID | Severidad | Área | Hallazgo | Evidencia |
|---|---|---|---|---|
| S1 | **P0** | Seguridad | Allowlist de destino vacía por defecto: cualquier IP/hostname, incluido `localhost`, link-local y metadatos cloud | `config.py:270`, `services/host_resolver.py` |
| S2 | **P0** | Seguridad | DNS rebinding TOCTOU: se resuelve al validar y se vuelve a resolver al ejecutar Ansible | `host_resolver.py`, `app.py` |
| T1 | **P0** | Corrección | `TIME` + NTP `keep` puede ser revertido por NTP; solo se emite un aviso | `ansible/cambiar_hora.yml`, `app.py:_ntp_keep_warning` |
| T2 | **P0** | Corrección | `restore`/`configure` reactivan NTP justo después de `date -s`; la verificación es de una sola muestra | playbook |
| A1 | **P0** | Arquitectura | Un hilo demonio por operación, sin cola, cupo, cancelación ni apagado ordenado | `app.py:989-1013` |
| D1 | **P0** | DevOps | `dist/.../ansible-control-center-images.tar` (177 MB) es anterior al HEAD y `COPY . .` lo mete en la imagen | `Dockerfile:35`, `dist/` |
| S3 | **P1** | Seguridad | Sin MFA, RBAC, bloqueo de cuenta ni auditoría de login duradera | `app.py` login |
| S4 | **P1** | Seguridad | Rate limit en memoria por proceso, clave solo IP en login | `services/rate_limiter.py` |
| S5 | **P1** | Seguridad | Vault sin lock: lectura-modificación-escritura concurrente pierde actualizaciones | `services/vault_manager.py` |
| S6 | **P1** | Seguridad | `HOST_KEY_CHECKING=1` con `known_hosts` vacío: primer despliegue falla; no hay UI de huellas | `config.py:226`, `docker/entrypoint.sh` |
| S7 | **P1** | Seguridad | Bootstrap/fuentes por CDN sin SRI; CSP permite el CDN y estilos inline; rompe entornos aislados | `templates/base.html`, `app.py:776-790` |
| S8 | **P1** | Secretos | `ansible_environment()` hereda todo el entorno (incluidos secretos de la app) | `services/ansible_runner.py` |
| T3 | **P1** | Corrección | Tolerancia de verificación por defecto 120 s; insuficiente para “hora exacta” | `config.py:278` |
| T4 | **P1** | Corrección | Modo local no contempla huecos/solapamientos DST; preview y playbook calculan distinto | `app.py`, playbook |
| T5 | **P1** | Corrección | Validación de zona horaria solo por regex; no comprueba que exista en `zoneinfo` | `app.py:245,346` |
| T6 | **P1** | Corrección | `ntp configure` siempre se muestra como cambio porque la inspección no devuelve `ntp_servers` | `ansible/tasks/leer_estado.yml` |
| D2 | **P1** | Datos | Estado de tarea completo persistido hasta 4 veces/s; amplificación de escritura | `app.py:_persist_task` |
| D3 | **P1** | Datos | `_tasks` y `task_state` crecen sin límite; `remove_task` no se usa; no hay purga por retención | `app.py:583,659`, `config.py:241` |
| D4 | **P1** | Datos | `init_schema` fija `user_version=5` aunque la BD sea más nueva | `models/db.py:241` |
| A2 | **P1** | Arquitectura | Estado en memoria + SQLite de respaldo: varios workers rompen SSE e idempotencia | `app.py`, `gunicorn.conf.py` |
| A3 | **P1** | Rendimiento | Ejecución secuencial: ping + playbook + verificación por host y por intento | `app.py:_run_apply_batch` |
| U1 | **P1** | UX | Tablas claras de Bootstrap sobre fondo oscuro: contraste potencialmente ilegible | `templates/history.html`, `batch.html` |
| U2 | **P1** | UX | Formulario único de 10+ campos; sin asistente ni perfiles | `templates/index.html` |
| U3 | **P1** | UX | Estados internos en inglés (`success`, `partial`, `failure`) visibles al usuario | varias plantillas |
| D5 | **P1** | DevOps | Sin lockfile, sin lint/tipos/seguridad, sin build Docker ni ansible-lint en CI | `requirements.txt`, `.github/workflows/ci.yml` |
| D6 | **P1** | DevOps | Contenedor sin hardening: sin `cap_drop`, `no-new-privileges`, rootfs de solo lectura ni límites | `docker-compose.yml` |
| D7 | **P1** | DevOps | `ACC_ADMIN_PASSWORD` por entorno queda visible en `docker inspect` | `docker/entrypoint.sh` |
| U4 | **P1** | UX | Sin inventario/perfiles; el operador reescribe destinos cada vez | `index.html` |
| T7 | **P1** | Corrección | Lote con todos los hosts `skipped` se deriva como `failure` | `models/batch.py:539` |
| X1 | **P2** | UX | Sin tema claro/oscuro/sistema; solo oscuro fijo | `base.html` |
| X2 | **P2** | UX | Sin validación inline, `aria-invalid`, `scope` en tablas ni `aria-label` en iconos | plantillas |
| X3 | **P2** | UX | Fechas sin zona/UTC explícita ni localización | plantillas |
| X4 | **P2** | UX | Página de progreso centrada en consola, sin tabla por host, filtros ni reintento | `progress.html` |
| X5 | **P2** | UX | Modal de confirmación sin confirmación tipada para rangos grandes; dry-run registra `success` por host | `index.html`, `app.py` |
| X6 | **P2** | UX | Login con clase inexistente `text-acento-principal` y errores genéricos | `templates/auth/login.html` |
| X7 | **P2** | UX | Sección de progreso muerta en `index.html` | `index.html:209` |
| D8 | **P2** | Documentación | Documentos duplicados y divergentes; sin runbook de rollback/upgrade/backup | `docs/`, `README.md` |
| D9 | **P2** | Pruebas | 88 tests unitarios/mock; sin E2E con hosts reales ni contenedor; sin umbral de cobertura | `tests/` |
| D10 | **P2** | Operación | Sin unidad systemd; healthcheck solo comprueba la BD | `Dockerfile:46` |
| A4 | **P2** | Código | Duplicación de bucles de credenciales/host entre preview y apply; `datetime.now()` naive mezclado con UTC | `app.py` |
| A5 | **P2** | Datos | Doble historial (`executions` legado + `execution_batches`) sin enlace | `models/execution.py`, `models/batch.py` |

---

## 3. Análisis por área

### 3.1 Corrección temporal (el corazón del producto)

Es el área de mayor riesgo funcional, porque un “éxito” puede no serlo.

**T1 — `TIME` con NTP activo y política `keep`.** El playbook fija la hora con `date -s`, pero systemd-timesyncd/NTP sigue activo y puede corregirla en segundos. Hoy solo se registra un aviso; el lote puede reportar éxito mientras el reloj vuelve a su valor anterior. *Recomendación:* convertir el aviso en bloqueo configurable (`ACC_TIME_REQUIRE_NTP_OFF=1` por defecto) o suspender NTP durante la operación y restaurarlo después, verificando dos veces separadas ≥ 5 s.

**T2 — Carrera `restore`/`configure`.** La secuencia desactiva NTP, fija la hora y reactiva NTP; la reactivación puede adelantar el reloj antes de la comprobación final. Además, la verificación es una única lectura. *Recomendación:* verificar antes de reactivar NTP, luego reactivar, esperar un ciclo de sincronización y volver a verificar. Registrar el resultado de ambas comprobaciones.

**T3 — Tolerancia de 120 s.** Para `TZ` es irrelevante; para `TIME` es enorme. *Recomendación:* tolerancia por modo (`TZ`: N/A; `TIME`: 2 s por defecto, configurable), y mostrar la tolerancia efectiva en la vista previa y en la confirmación.

**T4 — DST en modo local.** Una hora local puede no existir (hueco de primavera) o existir dos veces (solapamiento de otoño). El playbook y la vista previa no tratan ambos casos igual. *Recomendación:* validar con `zoneinfo` + `fold=0/1` en Python, rechazar horas inexistentes y pedir desambiguación explícita en las ambiguas.

**T5 — Zonas horarias inventadas.** `_TIMEZONE_RE` acepta `America/Atlantida`. Ansible fallará tarde y por host. *Recomendación:* `zoneinfo.ZoneInfo(value)` en la validación; catálogo con offset y nombre legible; soportar `UTC` y `Etc/GMT±N` con explicación del signo POSIX.

**T6 — Idempotencia falsa en `ntp configure`.** `leer_estado.yml` no devuelve `ntp_servers`, así que la comparación siempre concluye “cambia”. *Recomendación:* leer `timedatectl show-timesync`/`systemd-analyze` o `/etc/systemd/timesyncd.conf.d/`, y comparar el conjunto normalizado.

**T7 — Estado global engañoso.** `_derive_status` devuelve `failure` si todos los hosts quedaron `skipped` (por presupuesto de tiempo). *Recomendación:* estado `cancelled`/`incomplete` explícito, distinto de fallo técnico.

**Riesgo residual:** el presupuesto de tiempo se comprueba entre hosts; un playbook en curso puede excederlo hasta su timeout. Es aceptable si se documenta, pero la UI debe mostrar “presupuesto agotado” con claridad.

### 3.2 Seguridad

**S1 — Allowlist vacía = sin restricción.** Es el hallazgo más grave. La app prueba credenciales y ejecuta `become: true` contra lo que se le indique. Un usuario autenticado (o una sesión robada) puede apuntar a `127.0.0.1`, `169.254.169.254`, la red de gestión del propio servidor o cualquier host de Internet. *Recomendación:* allowlist obligatoria en producción (fallar al arrancar si está vacía y `ACC_ENV=production`), bloquear por defecto loopback, link-local, multicast y metadatos, y registrar cada destino rechazado.

**S2 — DNS rebinding.** Se valida el hostname resolviéndolo, pero Ansible lo resuelve otra vez al conectar. Un DNS malicioso puede devolver una IP permitida en la validación y otra distinta en la ejecución. *Recomendación:* resolver una vez, fijar la IP en el inventario y usar `ansible_host=<ip>` con `Host`/SNI si hace falta; rechazar si la resolución cambia entre validación y ejecución.

**S3/S4 — Autenticación y abuso.** No hay MFA, roles, bloqueo progresivo ni auditoría de inicios de sesión; el limitador es por proceso y por IP. *Recomendación:* MFA TOTP para cuentas admin, rol `viewer`/`operator`/`admin`, bloqueo tras N fallos por usuario+IP, tabla `auth_events`, y backend compartido (Redis) si se escala.

**S5 — Vault sin lock.** Dos peticiones simultáneas pueden leer el mismo estado y perder una adición. *Recomendación:* `fcntl.flock` sobre un fichero `.lock` del vault, o una única cola de escritura; prueba de concurrencia.

**S6 — Host keys.** El valor seguro por defecto es correcto, pero con `known_hosts` vacío todo falla en el primer uso y no hay forma de aceptar huellas desde la UI. *Recomendación:* pantalla “Confianza SSH” que muestre la huella y permita añadirla explícitamente; nunca desactivar la comprobación de forma global.

**S7 — Dependencias externas.** Bootstrap, iconos y Google Fonts vienen de CDN sin `integrity`, y la CSP los autoriza. En una red de gestión aislada la interfaz se rompe; además se expone la IP del operador a terceros. *Recomendación:* empaquetar Bootstrap/icons/fuentes en `static/`, CSP `default-src 'self'` sin CDN, y eliminar `'unsafe-inline'` migrando estilos a ficheros.

**S8 — Entorno heredado.** `ansible_environment()` copia `os.environ` completo; el proceso hijo de Ansible puede ver `ACC_SECRET_KEY`, pepper, etc. *Recomendación:* construir un entorno mínimo allowlist (`PATH`, `HOME`, `LANG`, `ANSIBLE_*`).

**S9 — Secretos en entorno del contenedor.** `ACC_ADMIN_PASSWORD` es visible en `docker inspect` y en `/proc`. *Recomendación:* fichero de secreto montado o comando interactivo `flask init-admin`; rotar tras el primer arranque.

**S10 — Identificadores cortos.** Los hashes de 8 caracteres usados en UI/auditoría tienen 32 bits: colisiones plausibles. *Recomendación:* mostrar 12–16 caracteres o un ID opaco independiente; conservar el HMAC completo en BD.

**Fortalezas que conviene preservar:** CSRF con caducidad, rotación tras login, cookies `HttpOnly`/`SameSite`, HSTS opcional, `no-store` en HTML, `secure_temp_file`, `argv` sin shell y bloqueo de secretos en la consola.

### 3.3 Arquitectura, concurrencia y rendimiento

**A1 — Hilos sin control.** Cada POST crea un `threading.Thread(daemon=True)`. No hay cupo, cola, prioridad, cancelación ni espera ordenada al apagar. Un operador puede lanzar N lotes de 256 hosts y saturar CPU, disco y red; al reiniciar, los hilos mueren a mitad y los lotes quedan reconciliados como huérfanos.

*Recomendación:* cola persistente (RQ/Celery + Redis, o una tabla `jobs` con workers) con concurrencia configurable, cancelación cooperativa, reintentos y `SIGTERM` ordenado. Si se quiere evitar infraestructura extra: un `ThreadPoolExecutor(max_workers=N)` global + tabla de trabajos y `cancel_event` por lote.

**A2 — Un solo worker de facto.** `_tasks`, `_operation_ids` y el rate limiter viven en memoria. `gunicorn.conf.py` usa `gthread` con un worker, lo que funciona pero limita CPU y crea un punto único de fallo. *Recomendación:* externalizar estado a SQLite/Redis y permitir N workers; o documentar explícitamente “1 worker, no escalar” como decisión de diseño.

**A3 — Secuencialidad.** Para 256 hosts: ping + playbook + verificación, uno tras otro, con hasta 3 intentos de contraseña. Un lote puede durar horas. *Recomendación:* `forks`/`serial` de Ansible o un pool de hilos por host con límite (p. ej. 8–16 concurrentes), respetando el presupuesto global. Medir y mostrar ETA.

**D2 — Amplificación de escritura.** `update_task` serializa la tarea completa (incluida consola de hasta 300 líneas) y la escribe en SQLite en cada actualización, hasta 4 veces por segundo. *Recomendación:* persistir solo campos de estado (no la consola), con *debounce* de 1–2 s, y guardar la consola en un fichero append-only o en una tabla por líneas.

**D3 — Crecimiento sin límite.** `remove_task` existe pero no se llama; `JOB_RETENTION_HOURS` y `HISTORY_RETENTION_DAYS` no tienen job de purga. *Recomendación:* tarea periódica (cron o hilo ligero al arrancar) que borre tareas y lotes antiguos, con métricas de tamaño.

**D4 — Versión de esquema.** `init_schema` escribe siempre `user_version = 5`; una BD futura (v6) se degradaría silenciosamente. *Recomendación:* comparar y abortar si `version > SCHEMA_VERSION`.

**A4 — Duplicación y naive datetime.** `_run_preview_batch` y `_run_apply_batch` repiten el bucle de credenciales/host; `create_task` mezcla `datetime.now()` con UTC. *Recomendación:* extraer un `HostProcessor` común y usar `datetime.now(timezone.utc)` en todo el backend.

**A5 — Doble historial.** `executions` (legado) y `execution_batches`/`execution_hosts` conviven sin `batch_id`. *Recomendación:* migración que una ambos o marque el legado como solo lectura y lo excluya de la UI.

### 3.4 Datos y auditoría

- Se guardan estados antes/después, error y duración por host: buena base forense.
- **Falta** persistir `warnings` por host y los intentos de contraseña (hoy solo en logs), lo que impide explicar por qué un host quedó en cierto estado.
- La vista previa guarda el plan en `after_state`, mezclado con el “después” real. *Recomendación:* campo `planned_state` separado y etiquetado en la UI.
- No hay firma/inmutabilidad del historial; un admin con acceso a SQLite puede alterarlo. Para auditoría fuerte: tabla append-only con hash encadenado, o export firmado.
- Sin retención efectiva ni backup/restore documentado. *Recomendación:* `sqlite3 .backup` programado, verificación de restauración y retención configurable.

### 3.5 Interfaz y accesibilidad

Problemas concretos observados:

- **Contraste:** las tablas usan `.table` de Bootstrap (texto oscuro por defecto) sobre el fondo oscuro personalizado, sin `data-bs-theme="dark"`. History/batch/passwords son candidatas claras a texto ilegible.
- **Jerga interna:** `success`, `partial`, `failure`, `preview`, `error` se muestran tal cual; deberían ser “Correcto”, “Parcial”, “Fallido”, “Vista previa”, “Error”.
- **Carga cognitiva:** un único formulario con destino, usuario, origen de contraseña, acción, valor, modo de hora, política NTP, servidores, RTC, forzar y confirmación. Sin valores por defecto guiados ni perfiles.
- **Progreso:** consola primero, sin tabla por host, filtros, reintento de fallidos ni cancelación. El operador no puede responder “¿qué hosts faltan?” de un vistazo.
- **Accesibilidad:** faltan `aria-invalid`/mensajes asociados, `scope="col"`, `aria-label` en botones de icono, `aria-live` fiable en progreso y foco visible; el modal de confirmación no atrapa el foco de forma explícita.
- **Fechas:** sin etiqueta UTC/zona ni formato local; en una herramienta de hora esto es especialmente confuso.
- **Login:** clase `text-acento-principal` inexistente y mensaje de error genérico.
- **Restos:** sección de progreso oculta en `index.html`, enlaces que pierden parámetros (`batch.html:183` no reenvía `ntp_servers`, `sync_rtc`, `force`, `admin_user`, `password_origin`).

La propuesta completa de interfaz está en la sección 5 y en el prototipo `docs/prototipo-ui.html`.

### 3.6 Pruebas y calidad

- 88 pruebas, todas verdes, buena cobertura de validación, CSRF, vault, lotes y playbooks (mockeados).
- **No hay** pruebas de extremo a extremo contra un host/contenedor real, ni de concurrencia (dos lotes, dos escrituras de vault), ni de DST, ni de DNS rebinding, ni de recuperación tras reinicio a mitad de lote.
- Sin `ruff`/`mypy`/`bandit`/`pip-audit`, sin `ansible-lint`, sin `shellcheck`, sin umbral de cobertura.
- `pytest-cov` figura en `requirements.txt` pero no está instalado en `.venv`; `pytest --cov` falla.
- Python local 3.14 frente a matriz CI 3.11–3.13: conviene añadir 3.14 o fijar versión.

*Recomendación:* pirámide de pruebas con unitarias (ya existen), integración con SQLite temporal y un contenedor SSH efímero, y un “smoke test” de Docker Compose en CI. Añadir `ruff`, `mypy` gradual, `bandit`, `pip-audit`, `ansible-lint`, `shellcheck` y cobertura mínima 70 %.

### 3.7 DevOps, despliegue y cadena de suministro

- **D1 (crítico):** `.dockerignore` no excluye `dist/` ni `*.tar`. El `COPY . .` mete un tar de **177 MB** en la imagen. Además, el tar del paquete tiene mtime 15:26 mientras que `config.py` es de 19:57 y el último commit de 18:59: **la imagen empaquetada es anterior al código actual**. Los `sha256sums.txt` verifican coherencia interna, no frescura. *Recomendación:* excluir `dist/`, `*.tar`, `.venv`, `tests`, `docs`; generar el paquete en CI desde el commit etiquetado; incluir el SHA del commit en `IMAGENES_INFO.txt` y comprobarlo en el despliegue.
- **D6:** Compose sin `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `read_only: true` + `tmpfs`, `pids_limit`, `mem_limit`, `cpu` y logging con rotación.
- **D7:** contraseña inicial por entorno visible; usar secreto montado y rotación.
- **D10:** healthcheck solo consulta la BD; añadir comprobación de `/login` y del binario de Ansible. No hay unidad systemd para instalación nativa; `install.sh` no la crea.
- **CI:** no construye la imagen, no ejecuta Compose, no audita dependencias, no valida el playbook con `ansible-lint`, no comprueba enlaces de documentación.
- **Dependencias:** rangos sin lockfile; reproducibilidad baja. Generar `requirements.lock` (pip-tools/uv) y separar `requirements-dev.txt`.
- **Certificados:** TLS autofirmado correcto para laboratorio, pero el script no advierte que no sirve para producción; documentar sustitución por CA interna.

### 3.8 Documentación y producto

- Hay **cuatro** documentos grandes que se solapan (`README.md`, `ANALISIS.md`, `DEPLOYMENT.md`, `docs/ANALISIS_Y_RECOMENDACIONES.md`) y ya divergen: uno afirma que falta la allowlist cuando ya existe.
- Falta runbook de: rollback de hora/zona, restauración de BD, rotación de secretos, actualización de versión, respuesta a incidentes y recuperación de un lote interrumpido.
- Falta guía de “primeros 10 minutos” para un operador nuevo y una matriz RACI de quién puede cambiar qué.
- **Legal/operativo:** la app prueba credenciales y cambia relojes; conviene registro de autorización por host, banner de uso responsable ya presente, y retención/anonimización de IPs y hashes según política interna.

---

## 4. Hoja de ruta recomendada

### Sprint 0 — Contención (1–2 días)

1. Allowlist obligatoria en producción + bloqueo de loopback/link-local/metadatos (S1).
2. Fijar IP resuelta y verificar que no cambia (S2).
3. Excluir `dist/` y `*.tar` de Docker; reconstruir imagen desde HEAD (D1).
4. Bajar tolerancia de `TIME` a 2 s y bloquear `TIME`+NTP `keep` por defecto (T1, T3).
5. Purga de tareas/historial y límite de `task_state` (D3).
6. Corregir enlace de `batch.html` y estado `skipped` (T7, X5).

### Sprint 1 — Robustez (1–2 semanas)

7. Cola de trabajos con concurrencia limitada, cancelación y apagado ordenado (A1).
8. Verificación temporal en dos fases y DST con `zoneinfo` (T2, T4, T5, T6).
9. Lock de vault + entorno mínimo para Ansible (S5, S8).
10. Auditoría de login, bloqueo y roles básicos (S3, S4).
11. Empaquetar assets y CSP estricta (S7).
12. Pruebas de concurrencia, DST, reinicio y contenedor SSH (D9).

### Sprint 2 — Producto (2–3 semanas)

13. Nueva UI: asistente, panel, progreso por host, historial filtrable, credenciales.
14. Inventario/perfiles de equipos y reutilización de operaciones.
15. Runbooks, unificación documental y `systemd`/healthchecks (D8, D10).
16. Lockfile, linters, auditoría y cobertura en CI (D5).
17. Endurecimiento de Compose y gestión de secretos (D6, D7).

### Sprint 3 — Escala (opcional)

18. Multi-worker con estado compartido, ETA y ejecución paralela por host.
19. Auditoría inmutable/firmada y exportación.
20. Métricas Prometheus + trazas y panel de salud real.

---

## 5. Propuesta de interfaz moderna e intuitiva

### 5.1 Principios

1. **Seguridad visible, no molesta.** El modo simulación, la política NTP y la tolerancia se muestran siempre en contexto; las acciones destructivas piden confirmación proporcionada al riesgo.
2. **Tarea primero, consola después.** El operador ve estado por host y resultado; la consola es un panel secundario para depurar.
3. **Divulgación progresiva.** Un asistente de cuatro pasos sustituye el formulario-monolito; los campos avanzados quedan plegados.
4. **Lenguaje humano en español.** Nada de `partial` o `skipped`; sí “Parcial” y “Omitido”, con explicación en tooltip.
5. **Accesible por defecto.** AA, foco visible, navegación por teclado, `aria-live`, movimiento reducido y contraste verificado.
6. **Sin dependencias externas.** Assets propios: funciona en redes aisladas y elimina el riesgo de CDN.

### 5.2 Arquitectura de información

```
Panel
├── KPIs y salud del sistema
├── Actividad reciente
└── Acciones rápidas
Nueva operación (asistente)
├── 1. Objetivo        → destinos, perfil, usuario, recuento
├── 2. Cambio          → TZ/TIME, NTP, RTC, tolerancia
├── 3. Credenciales    → vault / temporal / manual
└── 4. Revisar         → resumen, riesgos, simular/ejecutar
Progreso / detalle de lote
├── Cabecera de estado, progreso, ETA, cancelar
├── Tabla por host con filtros y reintento
└── Consola plegable
Historial
├── Búsqueda, filtros por estado/fecha/tipo
├── Exportar CSV/JSON
└── Detalle con diff antes/después
Credenciales
├── Salud del vault
├── Probar contraseña maestra
├── Candidatas (hash corto, origen, fecha)
└── Importar (dropzone)
```

Navegación lateral persistente con iconos y etiquetas, colapsable en móvil. Barra superior con contexto de entorno (Producción/Laboratorio), indicador de simulación y conmutador de tema claro/oscuro/sistema.

### 5.3 Pantallas clave

**Panel.** Banner de simulación si `DRY_RUN`; cuatro KPIs (equipos, operaciones hoy, éxito, incidencias); actividad reciente con estado legible y hora local + UTC; tarjeta de salud (vault, Ansible, `known_hosts`, BD, último lote); acciones rápidas.

**Asistente.** Stepper superior; panel lateral “Resumen” fijo que se actualiza en vivo (destinos, operación, NTP, credenciales, riesgos). En el paso 1, pegar rangos y ver el recuento antes de continuar. En el 2, tarjetas de política NTP con explicación del efecto real. En el 4, avisos destacados y confirmación tipada (`ESCRIBE EL NÚMERO DE HOSTS`) cuando el lote supera un umbral.

**Progreso.** Cabecera con estado, barra, transcurrido/ETA, botones “Cancelar” y “Reintentar fallidos”. Tabla por host: host, resultado, duración, hora final, mensaje; filtros “Todos / Correctos / Fallidos / Omitidos”; fila expandible con antes/después. Consola en un `details` o panel inferior con búsqueda y descarga.

**Historial.** Filtros por texto, estado, tipo y rango de fechas; paginación; exportación; estado en español; enlace al detalle. El detalle muestra resumen, timeline y tabla de hosts con diff.

**Credenciales.** Estado del vault (fichero, cifrado, nº de candidatas), prueba de contraseña maestra con feedback inmediato, lista de hashes cortos con origen y fecha, importación por arrastre con validación de extensión y tamaño, y explicación de que nunca se guarda la contraseña en claro.

### 5.4 Sistema de diseño

- **Tipografía:** pila del sistema (`ui-sans-serif`, `Inter`, `Segoe UI`…) para cero dependencias; escala 12/13/14/16/20/24/32; números tabulares en tablas.
- **Color:** acento índigo-teal, neutros fríos; verde/ámbar/rojo semánticos con variantes para ambos temas. Tokens CSS (`--bg`, `--surface`, `--text`, `--muted`, `--border`, `--accent`, `--ok`, `--warn`, `--danger`).
- **Espaciado:** base 4 px; radios 8/12/16; sombras suaves solo en elevación real.
- **Componentes:** botones (primario, secundario, fantasma, peligro), campos con ayuda y error, segmented control, tarjetas seleccionables, badges, tablas, stepper, banner, toast, dropzone, `details` para consola, modal accesible.
- **Temas:** claro/oscuro/sistema con `prefers-color-scheme` y persistencia en `localStorage`; `prefers-reduced-motion` respetado.
- **Estados:** vacío, carga (skeleton), error y éxito; nunca una tabla en blanco sin explicación.

### 5.5 Implementación recomendada

- Mantener Flask + Jinja, sustituir Bootstrap por CSS propio (o Bootstrap empaquetado y `data-bs-theme`), y añadir **HTMX** o `fetch` ligero para el asistente y los filtros, evitando una SPA.
- Extraer `static/css/app.css`, `static/js/app.js` e iconos SVG inline; eliminar CDNs y `unsafe-inline`.
- Reutilizar los endpoints actuales (`/preview`, `/execute`, `/api/task-status`, SSE) y añadir solo los necesarios para perfiles y reintento.
- Medir con Lighthouse yaxe; objetivo: accesibilidad ≥ 95, sin errores de contraste, navegación completa por teclado.

### 5.6 Prototipo

El prototipo navegable está en **`docs/prototipo-ui.html`**. Es un único fichero autocontenido (sin CDN) con:

- Panel, asistente de operación, progreso, historial y credenciales.
- Tema claro/oscuro/sistema, navegación lateral responsive y foco visible.
- Interacciones reales: cambio de vista, pasos del asistente, selección de política NTP, filtros, consola plegable y confirmación tipada.
- Datos de ejemplo realistas para evaluar densidad, jerarquía y microcopy.

Para revisarlo, ábrelo directamente en el navegador. También admite enlaces profundos y tema forzado para revisiones:

```
docs/prototipo-ui.html#panel
docs/prototipo-ui.html#new
docs/prototipo-ui.html#progress
docs/prototipo-ui.html#history
docs/prototipo-ui.html#credentials
docs/prototipo-ui.html?theme=light#panel
```

> El prototipo es una propuesta visual e interactiva, no código listo para producción: usa datos estáticos y no llama a los endpoints reales. La implementación recomendada reutiliza las rutas actuales (`/preview`, `/execute`, `/api/task-status` y SSE) y sustituye Bootstrap por los estilos propios del prototipo.


---

## 6. Quick wins (menos de una hora cada uno)

1. `data-bs-theme="dark"` en `<html>` y revisar contraste de tablas.
2. Traducir estados en plantillas con un diccionario Jinja.
3. `autocomplete`, `aria-describedby`, `aria-invalid` y `scope="col"`.
4. Añadir `dist/` y `*.tar` a `.dockerignore` y reconstruir.
5. Cambiar tolerancia por defecto a 2 s.
6. Corregir el enlace de “Ejecutar esta configuración”.
7. Eliminar la sección de progreso muerta de `index.html`.
8. Añadir `integrity` a los CDN como paso intermedio (mejor aún: empaquetarlos).
9. Instalar `pytest-cov` o retirarlo de `requirements.txt`.
10. Documentar en el README que el despliegue actual asume un solo worker.

---

## 7. Conclusión

La base técnica es buena y el equipo ya demostró disciplina de fases y pruebas. El salto a producción no requiere reescribir: requiere **cerrar los riesgos de seguridad de destino, hacer determinista la semántica temporal, sacar la ejecución de los hilos demonio y modernizar la interfaz**. Los tres primeros son condiciones; la cuarta es lo que convertirá una herramienta potente en una herramienta que un operador puede usar con confianza bajo presión.
