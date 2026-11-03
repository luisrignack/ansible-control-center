# Changelog — Hora Admin

Historial de cambios por fase. El proyecto no usa versiones semánticas
todavía; cada fase agrupa un conjunto coherente de funcionalidad y pruebas.

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).

---

## [Docker] — 2026-11-03

Empaquetado del proyecto para distribuirlo como imagen y desplegarlo con
Docker Compose y HTTPS.

### Añadido

- **Imagen de la aplicación** (`Dockerfile`): `python:3.12-slim`, usuario sin
  privilegios `hora` (UID/GID 10001), `sshpass`/`openssh-client`, Tini como
  PID 1, Gunicorn `gthread` en el puerto interno 8000 y `HEALTHCHECK` contra
  `/healthz`.
- **Entrypoint** (`docker/entrypoint.sh`): prepara directorios, aplica
  migraciones, crea el administrador inicial si se definió
  `HORA_ADMIN_ADMIN_PASSWORD` y arranca Gunicorn.
- **Nginx con TLS** (`docker/nginx/`): `nginx:1.27-alpine`, generación de
  certificado autofirmado en el primer arranque, redirección HTTP→HTTPS,
  proxy compatible con SSE (`proxy_buffering off`, timeouts de 360 s) y
  `HEALTHCHECK`.
- **`docker-compose.yml`**: app y Nginx, red interna `backend`, volúmenes
  `hora_data`, `hora_secrets`, `hora_logs` y `hora_ssh`; solo Nginx publica
  puertos.
- **Configuración de ejemplo** (`.env.docker.example`) y `.dockerignore`.
- **`GET /healthz`**: comprobación sin autenticación que verifica el proceso y
  el acceso a SQLite, con `Cache-Control: no-store`.
- **Documentación** (`docs/DOCKER.md`): puesta en marcha, certificados,
  primer administrador, `known_hosts`, copias de seguridad, actualización,
  diagnóstico y notas de seguridad.

### Cambiado

- `README.md` incluye arranque rápido con Docker, endpoint `/healthz` y enlace
  a la guía de despliegue.
- `.gitignore` ignora `.env.docker` y `certs/*.pem`.

---

## [P4.1] — 2026-11-03

Primera tanda de hardening del roadmap: visibilidad de simulación, semántica
fiel de `restore` y avisos de riesgo.

### Añadido

- **DRY RUN visible y persistido:** banner en el panel, badge en progreso,
  detalle del lote e historial; campo `dry_run` en `execution_batches`
  (esquema v4) y en el estado de la tarea.
- **Aviso `TIME` + `keep`:** si NTP está activo, el playbook añade una
  advertencia de que el reloj puede volver a sincronizarse.
- **Cabecera `Cache-Control: no-store`** en `/api/task-status/<task_id>`.
- **Sesiones acotadas:** `session.permanent` en el login, cookie de sesión de
  8 h por defecto, cookie “recordarme” limitada a 7 días y configuración
  `REMEMBER_COOKIE_SECURE`/`DURATION`.
- **Pepper de hashes:** los hashes de contraseñas candidatas pasan a
  HMAC-SHA256 con pepper persistido en `DATA_DIR` (`HORA_ADMIN_PASSWORD_PEPPER`
  opcional). Se mantiene compatibilidad de lectura y borrado con hashes
  SHA-256 heredados.
- **Reconciliación de lotes huérfanos:** los lotes `running` se marcan como
  `error` al arrancar y con el comando `reconcile-batches`.
- **Allowlist de redes:** `HORA_ADMIN_ALLOWED_NETWORKS` restringe IPs y CIDR
  antes de enumerar o conectar; una entrada inválida impide arrancar.
- **Gunicorn `gthread`:** 4 hilos por worker y `timeout` de 330 s para que el
  SSE no bloquee el resto de peticiones ni muera a los 300 s.
- **Confirmación e idempotencia:** modal de confirmación con objetivo, acción,
  valor y política NTP; UUID oculto por formulario y memoria acotada para que
  un doble envío redirija al progreso existente en vez de crear otro lote.
- **Estado de tareas persistido:** esquema v5 con `task_state`; cada tarea
  guarda una instantánea JSON en SQLite y se recupera si la memoria se pierde.
  Al arrancar se purgan las antiguas (`HORA_ADMIN_JOB_RETENTION_HOURS`) y se
  cierran como error las que quedaron incompletas.
- **CI:** workflow de GitHub Actions con Python 3.11–3.13, compilación,
  validación YAML de Ansible y `pytest`.
- **Tests nuevos:** migración v3→v4, visibilidad/persistencia de `dry_run`,
  ejecución real sin marca, `no-store`, aviso `TIME`+`keep` en vista previa y
  semántica de `restore`/`off`/`configure` en la vista previa, cookies de
  sesión, pepper, allowlist y reconciliación de huérfanos.

### Cambiado

- **`restore` restaura el estado NTP previo:** solo reactiva NTP si estaba
  activo antes; si estaba desactivado, lo deja desactivado. La verificación
  posterior comprueba ambos casos.
- **`_would_change`:** `restore` ya no se considera un cambio neto por sí
  mismo, porque devuelve NTP a su estado inicial.
- El texto de la política NTP y el runbook de reversión reflejan la nueva
  semántica.

### Limitaciones restantes de P4.1

- El SSE y la consola se sirven desde memoria (con snapshots SQLite); varios
  workers seguirían necesitando un almacén compartido.
- La idempotencia de envío es por proceso; al escalar a varios workers hará
  falta un almacén compartido.
- Confirmación de envío e idempotencia de formularios.
- CI con linters, `mypy`, `ansible-lint` y cobertura.

---

## [P4] — 2026-11-03

### Añadido

- **Ejecución en grupo real:** el lote recorre todos los hosts resueltos, no
  solo el primero.
- **Best-effort:** un host inaccesible o un playbook fallido no detiene el
  resto; el estado global agrega éxitos, fallos y errores.
- **Reintento de contraseñas:** si una candidata autentica pero el playbook
  falla o la verificación no coincide, se prueban las siguientes para ese host.
- **Modelo de lote** (`models/batch.py`):
  - `execution_batches`: `batch_id`, operador, objetivo, acción, valor,
    `time_mode`, `ntp_policy`, estado, contadores y fechas.
  - `execution_hosts`: resultado, error, duración, estado previo/posterior,
    hash corto y origen de la contraseña.
- **Vista previa** (`mode=preview`): inspección de solo lectura con
  `inspect_host`, plan `would_change` y lote en estado `preview`. Nunca llama a
  `apply_host`.
- **Verificación posterior:** `timedatectl show`, `date` y `hwclock`; el host
  solo cuenta como éxito si `HORA_VERIFIED` es correcto.
- **Idempotencia:** no se cambia zona horaria ni hora si ya coinciden, salvo
  `force`.
- **Semántica explícita UTC/local** (`time_mode`) para la hora exacta.
- **Política NTP explícita:** `keep`, `off`, `restore` y `configure`, con
  validación de servidores solo para `configure`.
- **Sincronización RTC opcional** (`sync_rtc`).
- **Playbook de inspección** `ansible/estado_hora.yml` y tarea compartida
  `ansible/tasks/leer_estado.yml`.
- **Marcadores de verificación:** `HORA_BEFORE_B64`, `HORA_STATE_B64`,
  `HORA_CHANGED`, `HORA_VERIFIED`, `HORA_ERRORS_B64`, `HORA_WARNINGS_B64` y
  `HORA_DEVIATION`.
- **Rutas nuevas:** `POST /preview`, `POST /execute`, `GET /batches/<batch_id>`
  y `GET /preview/<batch_id>`.
- **Historial por lotes** con contadores, operador, duración y detalle por host.
- **Esquema SQLite v3** con migración no destructiva, índices de lotes y
  `ON DELETE CASCADE` de hosts a lotes.
- **Configuración P4:** `HORA_ADMIN_TIME_TOLERANCE`,
  `HORA_ADMIN_DEFAULT_NTP_SERVERS` y `HORA_ADMIN_MAX_NTP_SERVERS`.
- **Consola de Ansible en vivo:** `run_ansible_playbook`, `inspect_host` y
  `apply_host` aceptan un `line_callback` opcional; `_invoke_playbook` usa
  `Popen` con un hilo lector y entrega la salida línea a línea. Sin callback se
  conserva la ruta `subprocess.run`.
- **Redacción y acotado de la consola:** se ocultan las contraseñas candidatas,
  se compactan los marcadores `HORA_*_B64` y se conservan las últimas 300
  líneas de hasta 500 caracteres.
- **Página de progreso renovada:** consola en vivo, resumen de hitos, metadatos
  del lote, estado global, auto-scroll, limpieza y descarga del log.
- **SSE con fallback a polling:** si `EventSource` falla tres veces, la página
  consulta `/api/task-status/<task_id>` cada 2 s.
- **Tests P4** (`tests/test_p4.py`, `tests/test_ansible_runner.py`): ejecución
  en todos los hosts, best-effort, reintento de contraseñas, vista previa,
  propagación de `time_mode`, política NTP, `sync_rtc`, `force`, streaming de
  salida, timeout del proceso y redacción de la consola.
- **Documentación:** README reescrito, suite `docs/` (índice, arquitectura,
  referencia, operación), análisis profundo y este changelog.

### Cambiado

- El resultado de un host ya no se considera éxito solo por
  `returncode == 0`; `apply_host` exige verificación.
- El historial principal pasa a ser el lote; `executions` se conserva como
  registro heredado por compatibilidad.
- `time_mode` se normaliza a `utc` para la acción `TZ`.
- Los servidores NTP solo se validan cuando la política es `configure`.
- La política NTP por defecto sigue siendo `keep`.

### Corregido

- El lote ya no termina tras el primer host correcto.
- El lote ya no se detiene ante el primer fallo.
- Ya no se pierden credenciales válidas cuando el primer intento de playbook
  falla.
- `TIME` deja de depender implícitamente de la zona local del controlador:
  `utc` fija el mismo instante y `local` es una elección explícita.
- Se registra el estado previo y posterior por host para auditoría y reversión
  manual.

### Limitaciones conocidas de P4

- `DRY_RUN` no se muestra en la interfaz ni se guarda en el lote.
- `TIME` + `keep` puede ser revertido por NTP.
- `restore` reactiva NTP aunque estuviera desactivado.
- Ejecución secuencial, sin cancelación ni reintento selectivo.
- Estado SSE en memoria; sin reconciliación automática de lotes huérfanos.
- Sin retención automática, RBAC, MFA ni allowlist de redes.

---

## [P3] — 2026-11-02

### Añadido

- **CSRF** global para `POST/PUT/PATCH/DELETE`, token de sesión con caducidad y
  comparación en tiempo constante.
- **Rate limiting** en memoria para login, ejecuciones y operaciones de vault.
- **Límites de red:** validación con `ipaddress`, prefijo CIDR mínimo y máximo
  de hosts antes de enumerar.
- **Cabeceras de seguridad:** CSP con nonce, `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy`,
  `Permissions-Policy`, HSTS opcional y `Cache-Control: no-store`.
- **Soporte de proxy inverso** con `ProxyFix` opcional.
- **Configuración:** `HORA_ADMIN_CSRF_*`, `HORA_ADMIN_*_RATE_*`,
  `HORA_ADMIN_MIN_CIDR_PREFIX`, `HORA_ADMIN_MAX_HOSTS`,
  `HORA_ADMIN_TRUST_PROXY`, `HORA_ADMIN_CSP_ENABLED`,
  `HORA_ADMIN_SECURITY_HEADERS`, `HORA_ADMIN_HSTS*`.
- Tests de seguridad (`tests/test_security.py`) y de cabeceras.

### Cambiado

- Los errores `400`/`429` devuelven JSON a peticiones AJAX y HTML al resto.
- El login rota el token CSRF.

---

## [P2] — 2026-11-01

### Añadido

- **Vault real** con `ansible-vault`: cifrado, descifrado, importación y
  borrado; escritura atómica y sin persistir la contraseña maestra.
- **Ejecución Ansible real** con `ansible -m ping` y `ansible-playbook`.
- **Inyección segura de credenciales:** ficheros temporales `0600` y
  `--extra-vars @fichero`; nunca en `argv`.
- **Modo temporal `.txt`** procesado en memoria.
- **Historial por host** en SQLite (`executions`) con acción, valor, host,
  resultado y hash corto.
- **Gestión de contraseñas candidatas** con hashes corto y completo.
- **Vista de contraseñas** con alta manual, importación `.txt`/`.yml` y
  borrado.
- **Logging** configurable con rotación.
- Tests de vault, runner de Ansible y servicios.

### Cambiado

- La contraseña de conexión deja de viajar en la línea de comandos.
- El historial muestra el hash corto, no la contraseña.

---

## [P1] — 2026-10-31

### Añadido

- **SQLite centralizado** con conexiones por petición.
- **Migraciones versionadas** mediante `PRAGMA user_version`, no destructivas.
- **WAL** y `busy_timeout` para tolerar concurrencia.
- **Esquema inicial:** `users`, `passwords`, `executions`,
  `execution_summaries`.
- **Índices** de historial y contraseñas.
- Tests de migración, idempotencia y concurrencia.

### Cambiado

- El historial y los usuarios dejan de depender de almacenamiento ad hoc.

---

## [P0] — 2026-10-30

### Añadido

- **Estructura unificada** de la aplicación Flask.
- **Login** con Flask-Login, hash Werkzeug y sesión.
- **Comandos CLI** `init-db` e `init-admin`.
- **Panel de configuración** con objetivo, credenciales, acción y valor.
- **Páginas de error** `400`, `404`, `429` y `500`.
- **Progreso SSE** en `/progress/<task_id>` y estado JSON.
- **Arranque en desarrollo** con `python app.py`.
- Configuración central con rutas de producción y fallback a `instance/`.
- Tests iniciales de login, rutas y cabeceras.

---

## Pendiente (no implementado)

- Banner y persistencia de `DRY_RUN`.
- Corrección de `TIME` + `keep` y de la semántica exacta de `restore`.
- Paralelismo, cola persistente, cancelación y reintento de fallidos.
- Reconciliación de lotes huérfanos y parada ordenada.
- Retención automática (`HORA_ADMIN_HISTORY_DAYS`,
  `HORA_ADMIN_JOB_RETENTION_HOURS`).
- RBAC, MFA, aprobaciones y allowlist de redes.
- Auditoría de eventos independiente.
- Inventarios, CSV, grupos y exclusiones.
- API/CLI de ejecución, webhooks y exportación.
- Multiplataforma (RHEL/Rocky, Windows, red).

Consulta el detalle y la priorización en
[docs/ANALISIS_Y_RECOMENDACIONES.md](docs/ANALISIS_Y_RECOMENDACIONES.md).
