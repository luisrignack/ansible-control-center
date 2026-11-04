# Changelog — Ansible Control Center

Historial de cambios por fase. El proyecto no usa versiones semánticas
todavía; cada fase agrupa un conjunto coherente de funcionalidad y pruebas.

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).

## [Informe PDF, "Acerca de" y favicon] — 2026-11-04

### Añadido

- **Informe PDF de la última ejecución**: botón "Descargar informe" en cada
  vista de tarea. La ruta `GET /tarea/<task_id>/informe.pdf` genera un PDF A4
  con el resumen de la última corrida de esa tarea (tarea, acción, objetivo,
  operador, credenciales, tipo de corrida, inicio/fin, resultado global) y la
  tabla de estaciones con estado y **motivo breve** (estación apagada o sin
  conexión, credenciales rechazadas, verificación no confirmada, etc.),
  fallidas primero. Culpa por host a partir del texto técnico del error con
  un clasificador (`services/pdf_report.REASON_PATTERNS`); el error original
  sigue disponible en el historial.
- **Generador PDF sin dependencias**: se construye el PDF a mano con fuentes
  base Helvetica y WinAnsiEncoding (acentos y Ñ) — no se añaden librerías de
  pip, así que la imagen sigue construyéndose **sin acceso a red**. Los
  streams van sin comprimir para inspección y pruebas. Paginación, aprontado
  zebra, pies con el organismo y la fecha de generación (hora del servidor).
- **Vínculo lote↔tarea del catálogo**: columna nueva `task_id` en
  `execution_batches` (migración v9 no destructiva); la ruta del informe
  busca la última corrida de esa tarea y, si no existe ninguna (lotes
  anteriores a la migración), recurre a la última corrida global.
- **Página "Acerca de"** (`GET /acerca-de`): muestrea el texto institucional
  *Grupo de Migración a Código Abierto / DCTI - ETECSA* con la **fecha del
  host** (formateada en español, hora local del servidor). Enlace nuevo en
  la barra lateral (sección "Sistema"), visible para todos los roles.
- **Favicon de Ansible**: `static/img/ansible-{48,96}.png` (iconos de
  Icons8) enlazados en el `<head>` (firefox/chrome, apple-touch-icon).

### Cambiado

- `ExecutionBatch.create_batch()` acepta `task_id`; `handle_execution`,
  `_run_scheduler_tick` y `execute_with_progress` la propagan.

## [Multiplataforma Windows/Linux (bloque B)] — 2026-11-04

### Añadido

- **Tarea `zona-todas` (Zona horaria Linux y Windows):** aplica la zona IANA
  (por defecto `America/Caracas`) a estaciones Linux (`timedatectl`) y
  Windows (`ansible.windows.win_timezone`). Solo la acción TZ: no toca
  NTP, ni la hora exacta, ni w32tm. No destructiva, `min_role` operator.
  El mapa IANA→tzutil vive en el playbook (`America/Caracas` →
  `SA Pacific Standard Time`); zonas sin equivalencia se rechazan con
  mensaje claro en vez de aplicar algo incorrecto.
- **Grupos de inventario Windows:** al crear un grupo se elige la
  plataforma (`Linux (Ubuntu) · SSH` o `Windows · WinRM`); la conexión se
  deriva automáticamente (`ssh`/`winrm`) y no se puede configurar una
  contra la otra. La subida de clave SSH se rechaza para grupos Windows
  (autenticación winrm es usuario/contraseña del grupo) y la ficha del
  grupo lo indica en pantalla con el badge de plataforma.
- **Conexión WinRM en el runner:** las variables de conexión
  (`ansible_connection=winrm`, `ansible_port` de `ACC_WINRM_PORT`
  (5985 por defecto), `ansible_winrm_transport` de `ACC_WINRM_TRANSPORT`
  (`basic`/`ntlm`/`credssp`, `ntlm` por defecto) y
  `ansible_winrm_server_cert_validation=ignore`) van siempre por
  extra-vars en fichero `0600`, nunca por línea de comandos. El ping de
  verificación usa el módulo builtin `raw` (`echo PING_OK`), que funciona
  sobre WinRM con solo pywinrm y sin colecciones adicionales.
- **Playbooks Windows dedicados** (`ansible/win_cambiar_zona.yml`,
  `ansible/win_estado_zona.yml`): mismo contrato de marcadores `HORA_*`
  (HORA_BEFORE_B64/HORA_STATE_B64/HORA_CHANGED/HORA_VERIFIED/HORA_ERRORS)
  que el de Linux; `become: false`, sin NTP, solo módulos builtin. La
  inspección Windows publica solo la zona horaria (los demás relojes
  requieren WMI; fuera de fase).
- **Direccionado de playbook por conexión:** con `connection=winrm` el
  runner usa los playbooks Windows automáticamente, tanto en la vista
  previa como en la aplicación.

### Cambiado

- El catálogo declara `platforms` y `actions` por tarea; la validación del
  formulario bloquea la acción no admitida ("La tarea 'X' no admite la
  acción TIME.") y las combinaciones tarea↔plataforma ("La tarea 'Hora y
  zona horaria' no está disponible para el grupo 'Win' (Windows)").
- `templates/index.html` adapta el formulario a tareas de solo TZ: la
  política/servidores NTP, la semántica de hora local y las opciones
  avanzadas (RTC/forzar) solo se muestran en tareas con acción TIME; el
  resumen y los modales defienden elementos ausentes.

### Infraestructura

- `requirements.txt`: `pywinrm>=0.4.3`.
- Los playbooks Windows usan solo módulos builtin (`ansible.builtin.raw` con
  `tzutil`), así que **no** se necesita la colección `ansible.windows` y la
  imagen sigue construyéndose sin acceso a red (la instalación de la colección
  vía `ansible-galaxy` fallaba en redes aisladas; `raw` no exige Python en el
  destino y funciona sobre WinRM). La idempotencia la aporta el playbook:
  leer → comparar → `tzutil /s` solo si difiere → verificar con `tzutil /g`.
- Pruebas: 10 nuevas (`tests/test_windows.py`), total 174. No hay hosts
  Windows reales disponibles: la cobertura winrm es a nivel cmd/payload,
  catálogo y validación de app; la prueba contra una estación real queda
  pendiente de usuario con red.

## [Operación del bloque A] — 2026-11-04

### Añadido

- **Inventario persistente de grupos y hosts** (`GET/POST /inventario`, solo
  lectura para operadores, escritura para admins): grupos con descripción,
  hosts validados como IPv4 al añadirlos y clave SSH opcional por grupo. La
  clave privada se guarda en `SECRETS_DIR/inventory_keys/` con `0600` y nunca
  sale de ahí.
- **Objetivo por grupo en la vista de tarea:** el formulario permite elegir
  entre IP manual (como siempre) o un grupo del inventario. Los hosts se
  leen del inventario **en el momento de ejecutar**, así que el grupo es
  siempre el estado actual; el lote queda registrado con el nombre del grupo
  como objetivo.
- **Programaciones** (`GET/POST /programaciones`): una, diaria o semanal; el
  horario se interpreta en UTC. Un hilo de fondo (`services/scheduler.py`)
  revisa cada 15 s la tabla `schedules` y lanza los lotes por la misma cola
  que la operación manual, con el operador registrado como
  `agenda:<nombre>`. La contraseña maestra del vault se guarda **cifrada con
  Fernet**, con la clave en `SECRETS_DIR/scheduler_password.key` (0600); sin
  ella el disparo falla de forma segura. Un lote no lanza la tarea si su
  tarea dejó de estar activa: la programación se desactiva con error
  registrado.
- **Notificación de lotes por correo (SMTP opcional):** `ACC_SMTP_HOST`,
  `ACC_SMTP_PORT`, `ACC_SMTP_USER`, `ACC_SMTP_PASSWORD`, `ACC_SMTP_FROM`,
  `ACC_SMTP_STARTTLS`, `ACC_NOTIFY_TO` y `ACC_NOTIFY_LEVEL`
  (`off`/`failures`/`all`). Al cerrar cada lote se envía un resumen (con la
  lista de hosts con problema) y un fallo de correo no afecta el resultado
  persistido.
- **Doble aprobación (two-person rule) para tareas destructivas:** tabla
  `approvals` y página `GET/POST /aprobaciones` (solo admins). Una tarea
  marcada como destructiva en el catálogo exige una solicitud aprobada por
  un admin **distinto del ejecutor**; la aprobación caduca al cabo de
  `ACC_APPROVAL_TTL_MINUTES` (30 por defecto) y es de un solo uso. Los
  parámetros de la solicitud se guardan **sin secretos** (hash de
  parámetros) y la ejecución solo procede si el hash coincide.
- **Resultados por host con rol mínimo por tarea:** el catálogo incorpora
  `min_role` y `destructive`; “Hora” queda `operator` no destructiva y las
  tres tareas en preparación quedan `admin`/destructivas. Un `viewer`
  (solo lectura) sigue bloqueado para lanzar Ansible.
- **Re-ejecutar lote:** en la lista de lotes y en la tarjeta de un lote
  terminado (no de vista previa) hay un botón **“Re-ejecutar esta
  configuración”** que reabre el formulario de la tarea con los mismos
  parámetros prellenados.
- **Autenticación SSH por clave:** los playbooks y el ping Ansible aceptan
  `private_key`; la clave viaja por `--extra-vars @fichero 0600` (nunca en la
  línea de comandos) y con clave la contraseña queda solo para sudo.

### Esquema

- Migración v8 (`SCHEMA_VERSION = 8`): tablas `inventory_groups`,
  `inventory_hosts`, `schedules` y `approvals`, todas idempotentes.

### Interfaz

- Navegación nueva: Inventario y Programaciones (visibles a operadores) y
  Aprobaciones (solo admins).
- Plantillas nuevas `inventory.html`, `schedules.html`, `approvals.html` con
  el estilo del resto del sistema y tokens CSRF en todos los formularios.

### Pruebas

- 18 tests nuevos en `tests/test_ops.py`: CRUD de inventario y permisos,
  ejecución e2e por grupo (con hosts leídos del inventario y error de grupo
  vacío), disparo y recurrencia de programaciones, desactivación por tarea
  fuera del catálogo, notificador (silencioso sin SMTP, envío de fallos y
  nivel `all` para vistas previas), ciclo completo de doble aprobación
  (pendiente → decidida → consumida → un solo uso),
  re-ejecutar en lote terminado y ausencia en vista previa, y transmisión de
  la clave SSH a `ansible-playbook` y a los hooks de lote.

## [Catálogo de tareas en tarjetas] — 2026-11-04

### Añadido

- **Portada como catálogo de tareas** (inspirada en la propuesta 11 de
  `docs/propuestas-ui/11-task-hub.html`): la página “Tareas” muestra una
  tarjeta por tarea de Ansible con nombre, icono, chips de estado, playbook y
  acciones. Ninguna tarea se configura en la portada.
- **Vista de detalle por tarea `GET /tarea/<task_id>`:** cada tarjeta conduce
  a su propia página, con enlace **“Volver al catálogo”** en la cabecera y en
  el panel de tareas pendientes. La tarea “Hora y zona horaria” conserva ahí
  todo el flujo existente de TZ/TIME, vista previa, verificación e
  idempotencia; el formulario sigue enviando por `POST /` sin cambios.
- **Registro de tareas `services/task_catalog.py`:** cada tarea es una entrada
  con `id`, `name`, `icon`, `tone`, `summary`, `playbook`, `status`
  (`active`/`planned`), `chips` y `steps`. Añadir una tarea nueva = añadir una
  entrada y su playbook.
- **Cuatro tarjetas en el catálogo:**
  - *Hora y zona horaria* (activa).
  - *Kaspersky Antivirus*, *Contraseña administrativa* y *Tapiz oficial*
    (en preparación): su vista de detalle muestra el plan de lotes previsto y
    un aviso de playbook pendiente; no exponen formulario ni ejecución hasta
    integrar su playbook.
- **Nuevos tests:** catálogo sin formulario, detalle de tarea activa y
  esperada, fallback de id desconocido y acceso sin sesión.

### Añadido (extra catálogo)

- **Botón “Solicitar nueva tarea”** en las acciones de la portada: abre el
  cliente de correo del sistema (p. ej. Thunderbird) vía `mailto:` a
  `luis.rignack@etecsa.cu` con asunto `solicitud de nueva tarea Ansible`.
  Configurable con `ACC_TASK_REQUEST_EMAIL` y `ACC_TASK_REQUEST_SUBJECT`.
- **Hover con color por tarjeta:** al pasar el puntero (o con foco de
  teclado) la tarjeta se tiñe con un degradado de su tono, coloreando
  nombre, icono y chips, con un anillo sutil del mismo tono.

### Cambiado

- La navegación lateral renombra “Panel” a “Tareas” (activa en la portada y
  en la vista de detalle).
- Un id de tarea desconocido redirige al catálogo con un aviso.
- Los scripts de la página de detalle solo se cargan cuando la tarea está
  activa.

---

## [Copia de seguridad de /etc/timezone] — 2026-11-04

### Añadido

- **Backup antes de cambiar la zona horaria:** el playbook copia
  `/etc/timezone` a `/etc/timezone.bak` antes de ejecutar
  `timedatectl set-timezone`. Si el backup ya existe se conserva, para no
  perder la versión original.

---

## [Origen manual automático] — 2026-11-04

### Cambiado

- **Sin selector de origen en el modal:** al cifrar desde “Cifrar y guardar
  contraseñas” el origen se asigna automáticamente como `Manual`. La
  importación de archivos sigue registrándose como `Archivo`.
- El servidor mantiene la validación de orígenes para llamadas directas a la
  API y conserva las etiquetas legibles en la tabla.

---

## [Orígenes de credenciales y visibilidad de contraseñas] — 2026-11-04

### Añadido

- **Origen configurable:** el modal “Cifrar y guardar contraseñas” ofrece
  `Manual`, `Archivo`, `Generada`, `Compartida` y `Otra`. El servidor valida el
  valor contra `PASSWORD_ORIGINS` y lo persiste en SQLite; la tabla muestra la
  etiqueta legible.
- **Botón de ojo:** todos los campos de contraseña (login, contraseña maestra
  del panel, modal de credenciales e importación) permiten mostrar u ocultar el
  texto sin scripts inline y respetando la CSP con nonce.

### Cambiado

- La tabla de credenciales traduce el código de origen a una etiqueta legible.

---

## [Endurecimiento y correcciones] — 2026-11-04

### Seguridad

- **IP del cliente:** `_client_ip()` usa `request.remote_addr` (ya reescrito por
  `ProxyFix`) en lugar de leer el primer valor de `X-Forwarded-For`. Evita
  suplantar la IP y eludir los límites de login, ejecución y vault.
- **Rate limiter acotado:** purga por antigüedad y límite de claves
  (`MAX_KEYS`) para impedir crecimiento de memoria con claves cambiantes.
- **Permisos SQLite:** la base y sus ficheros `-wal`/`-shm` se fuerzan a `0600`.
- **Producción:** se rechaza el arranque si `ACC_COOKIE_SECURE=0`.

### Corregido

- **Modal “Cifrar y guardar contraseñas”:** se mueve al `<body>` para sacarlo
  del stacking context de `.app-shell`; el backdrop de Bootstrap ya no tapa el
  modal ni deja la página aparentemente congelada.
- **Feedback del vault:** el guardado usa un aviso inline en vez de `alert()`
  bloqueante, y muestra los errores del servidor.
- **Vista previa por lotes:** el enlace de repetición usa `batch.time_zone` en
  lugar de la marca de tiempo.
- **NTP idempotente:** la vista previa considera correcta la configuración si
  están todos los servidores deseados (aunque existan otros del sistema) y el
  playbook no marca `ntp_on` como cambio si NTP ya estaba activo.
- **Usuario por defecto:** el formulario usa `ACC_USERNAME` en vez de `ldamin`.

### Cambiado

- **Zonas horarias sugeridas:** solo se listan zonas de América; se mantiene
  `America/Caracas`.
- **Semántica `local` documentada:** se interpreta en la zona seleccionada y se
  aplica el mismo instante a todos los hosts.
- **DRY RUN:** el banner aclara que la simulación no valida conectividad ni
  credenciales y remite a la vista previa real.
- **CI:** `pytest` con cobertura (`pytest-cov`) y umbral mínimo.

### Documentación

- `README`, `docs/ARQUITECTURA.md` y `.env.example` alineados con el
  comportamiento real (hora local y allowlist en producción).

---

## [Rango IPv4 y máscara de objetivo] — 2026-11-04

### Añadido

- **Rango inclusivo `inicio-fin`:** el campo de objetivo acepta, además de IP,
  hostname y CIDR, un rango como `172.17.128.118-172.17.128.235`.
- **Máscara de entrada:** el formulario limita los octetos a 0–255, impide
  caracteres no válidos y valida en vivo el CIDR, el orden del rango y el
  máximo de hosts.
- **Validación de servidor:** `pin_target` y `resolve_target` normalizan y
  expanden el rango, aplicando allowlist, rangos prohibidos y `MAX_HOSTS`.

### Corregido

- El resumen y el modal de confirmación distinguen CIDR y rango inclusivo.

---

## [Interruptor de simulación] — 2026-11-04

### Añadido

- **Interruptor DRY RUN en la interfaz:** un switch en la barra superior
  permite a los administradores activar o desactivar la simulación sin tocar
  variables de entorno ni reiniciar.
- **Persistencia en SQLite:** el estado se guarda en la nueva tabla
  `app_settings` (esquema v7) y se aplica al arrancar; `ACC_DRY_RUN` queda como
  valor inicial si todavía no hay ninguno guardado.
- **Confirmación explícita** al pasar a ejecución real y aviso si hay
  operaciones en curso.
- **Auditoría:** cada cambio se registra como evento `dry_run_on` /
  `dry_run_off`.
- **Permisos:** solo `admin` puede cambiar el modo; el resto de roles ve un
  indicador de solo lectura.

### Corregido

- **Fallback sin JavaScript:** el formulario del interruptor envía también el
  estado desmarcado, de modo que desactivar la simulación funciona aunque el
  navegador no ejecute el script de la interfaz.

---

## [Sprint 0/1] — 2026-11-03

Aplicación de las recomendaciones de la revisión técnica del 3 de noviembre:
contención de red, ciclo de vida de trabajos, corrección DST/NTP, RBAC y
endurecimiento del empaquetado.

### Seguridad de red

- **Allowlist obligatoria en producción:** `ACC_ALLOWED_NETWORKS` vacía
  aborta el arranque con `ACC_ENV=production`.
- **Bloqueo por defecto** de loopback, link-local (incluidos metadatos cloud
  `169.254.169.254`), multicast, reservadas y no especificadas, aunque la
  allowlist las incluya.
- **Fijación de IP:** los hostnames se resuelven una sola vez, se comprueban
  **todas** sus direcciones y se guarda una IP literal para que Ansible no
  vuelva a consultar DNS (anti-rebinding).
- **`TIME` + NTP `keep`:** se rechaza en ejecución con
  `ACC_TIME_REQUIRE_NTP_OFF=1`; la vista previa solo avisa.
- **Tolerancia de hora:** valor por defecto reducido de 120 s a **2 s**.
- **Lock del vault:** escritura serializada con `flock` entre procesos.
- **Entorno mínimo de Ansible:** solo se heredan variables necesarias
  (`PATH`, `HOME`, `LANG`, `TMPDIR`, `ANSIBLE_*`, etc.); no se filtran secretos
  de la aplicación.

### Ciclo de vida y datos

- **Cola acotada de trabajos:** concurrencia y cola máximas, rechazo con error
  claro al llenarse, cancelación cooperativa y apagado ordenado con gracia.
- **Nuevos estados de lote:** `incomplete` (presupuesto agotado) y `cancelled`,
  distintos de `failure`.
- **Retención y límites:** purga de lotes, instantáneas y eventos de acceso;
  recorte de `task_state` y de tareas en memoria.
- **Esquema v6:** roles, bloqueo de cuenta, `auth_events` y columnas de
  prefill del lote (`admin_user`, `password_origin`, `ntp_servers`,
  `sync_rtc`, `force`, `local_fold`, `time_zone`). Una base con versión futura
  aborta el arranque.
- **RBAC y auditoría:** roles `viewer`, `operator` y `admin`; bloqueo
  progresivo tras fallos de login; registro de accesos correctos, fallidos y
  bloqueados en `/audit`. `init-admin` acepta `--role`.

### Hora, DST y NTP

- **Validación real de zona horaria** con `zoneinfo` (rechaza zonas inventadas).
- **Huecos y solapamientos DST:** se rechazan horas inexistentes y horas
  ambiguas sin `fold` explícito; `local_fold=0/1` selecciona la ocurrencia.
- **Instante único:** la app calcula `target_epoch` y lo comparte con el
  playbook, de modo que vista previa y ejecución coinciden.
- **Verificación en dos fases** antes y después de reactivar NTP, con espera
  de asentamiento (`ACC_NTP_SETTLE_SECONDS`).
- **Inspección de servidores NTP** en la configuración estándar de chrony y
  systemd-timesyncd; corregida la ruta de chrony a
  `/etc/chrony/conf.d/90-ansible-control-center.conf`.

### Interfaz, assets y empaquetado

- **Assets locales:** Bootstrap 5.3.2 e iconos servidos desde
  `static/vendor/`; sin CDN ni Google Fonts.
- **CSP estricta:** `default-src 'self'`, nonce para scripts, sin
  `unsafe-inline`; eliminados los `style=` inline y traducidos los estados.
- **Accesibilidad y UX:** `<progress>` nativo, botón de cancelación, enlace de
  prefill del lote ampliado y vistas `403.html` y `audit.html`.
- **Rediseño visual:** sistema de diseño inspirado en `docs/prototipo-ui.html`
  con shell de barra lateral, cabecera, tarjetas elevadas, tablas, formularios,
  banners y páginas de error modernizadas; tema claro/oscuro persistente y
  resumen dinámico de la operación. Todo el estilo vive en `static/css/app.css`
  (sin `style=` inline) y se mantienen las restricciones de CSP.
- **Docker:** `.dockerignore` excluye `dist/`, `*.tar`, `*.tgz` y `*.tar.gz`;
  `docker/export-images.sh` reconstruye desde HEAD por defecto y registra
  commit y estado del árbol en `IMAGENES_INFO.txt`.

### Pruebas

- Nueva cobertura en `tests/test_sprint01.py`: allowlist/loopback/metadatos,
  DNS pinning, DST, formulario, NTP, cola/cancelación, roles/bloqueo/auditoría,
  CSP/assets, lock del vault, entorno de Ansible, estados de lote y esquema
  futuro.
- `tests/test_ssh_container.py`: integración SSH opcional con Docker
  (`ACC_RUN_SSH_TESTS=1`), omitida por defecto.

---


Correcciones de coherencia detectadas en la revisión del proyecto: simulación
honesta, ejecución acotada y contrato playbook→app más estricto.

### Añadido

- **Vista previa real en DRY RUN:** `inspect_host` ejecuta el playbook de solo
  lectura aunque la simulación esté activa (`force_real=True` desde la vista
  previa). Así se detectan hosts caídos y credenciales inválidas en lugar de
  marcar todos como inspeccionados.
- **Presupuesto de tiempo por lote:** `ACC_BATCH_TIME_BUDGET` (3600 s
  por defecto). Al agotarse, los hosts pendientes se registran como `skipped`
  y el lote cierra.
- **Tope de intentos por host:** `ACC_MAX_PASSWORD_ATTEMPTS` (3 por
  defecto), aplicado también a la vista previa.
- **Hostnames en el formulario:** `validate_target` acepta un hostname
  resoluble y comprueba la allowlist contra la IP resuelta.
- **Métricas separadas:** `ExecutionBatch.get_stats` excluye los lotes
  simulados de los contadores reales y los agrega como `simulated`.
- **Tests:** vista previa real en DRY RUN, tope de intentos, presupuesto de
  tiempo, hostname resoluble, verificación obligatoria en la ruta legacy y
  separación de estadísticas simuladas.

### Cambiado

- **Ruta legacy `run_ansible_playbook`:** ya no considera éxito un
  `returncode == 0` sin `HORA_VERIFIED`; alinea su criterio con `apply_host`.
- **`would_change` para NTP `configure`:** conservador si el estado no incluye
  la lista de servidores; no promete “sin cambios” cuando el playbook reescribe
  la configuración.
- **Normalización de booleanos:** `_state_bool` interpreta `"True"`/`"False"`
  serializados por versiones de Ansible con Jinja no nativa.
- **Historial:** los éxitos reales y los hosts simulados se muestran por
  separado; el resumen de un lote simulado empieza por “SIMULACIÓN”.

### Documentación

- `README`, `DEPLOYMENT`, `docs/OPERACION`, `ANALISIS` y
  `docs/ANALISIS_Y_RECOMENDACIONES` actualizados al estado real tras P4.1/P4.2.

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
  `ACC_ADMIN_PASSWORD` y arranca Gunicorn.
- **Nginx con TLS** (`docker/nginx/`): `nginx:1.27-alpine`, generación de
  certificado autofirmado en el primer arranque, redirección HTTP→HTTPS,
  proxy compatible con SSE (`proxy_buffering off`, timeouts de 360 s) y
  `HEALTHCHECK`.
- **`docker-compose.yml`**: app y Nginx, red interna `backend`, volúmenes
  `acc_data`, `acc_secrets`, `acc_logs` y `acc_ssh`; solo Nginx publica
  puertos.
- **Configuración de ejemplo** (`.env.docker.example`) y `.dockerignore`.
- **Empaquetado portable** (`docker/export-images.sh`): construye o reutiliza
  las imágenes y genera la carpeta `dist/ansible-control-center-despliegue/` con las dos
  imágenes en `imagenes/ansible-control-center-images.tar`, su ficha de arquitectura y
  digests, `docker-compose.yml`, `Dockerfile`, `docker/`,
  `.env.docker.example`, un `certs/` vacío, `LEEME.txt`, la guía
  autocontenida `DESPLIEGUE.md` y `sha256sums.txt`. En la PC de destino basta
  `docker load` + `up --no-build`: no hace falta el código fuente ni acceso a
  Docker Hub.
- **Guía portable** (`docker/despliegue-portable.md`): requisitos, instalación
  en 5 pasos, tabla de configuración, certificados, SSH, copias de seguridad,
  actualización, diagnóstico y notas de seguridad, sin depender del repositorio.
- **`GET /healthz`**: comprobación sin autenticación que verifica el proceso y
  el acceso a SQLite, con `Cache-Control: no-store`.
- **Documentación** (`docs/DOCKER.md`): puesta en marcha, certificados,
  primer administrador, `known_hosts`, copias de seguridad, actualización,
  traslado a otra PC sin registro, diagnóstico y notas de seguridad.

### Cambiado

- `README.md` incluye arranque rápido con Docker, endpoint `/healthz` y enlace
  a la guía de despliegue.
- `.gitignore` ignora `.env.docker`, `certs/*.pem`, `dist/` y `*.tar`.

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
  HMAC-SHA256 con pepper persistido en `DATA_DIR` (`ACC_PASSWORD_PEPPER`
  opcional). Se mantiene compatibilidad de lectura y borrado con hashes
  SHA-256 heredados.
- **Reconciliación de lotes huérfanos:** los lotes `running` se marcan como
  `error` al arrancar y con el comando `reconcile-batches`.
- **Allowlist de redes:** `ACC_ALLOWED_NETWORKS` restringe IPs y CIDR
  antes de enumerar o conectar; una entrada inválida impide arrancar.
- **Gunicorn `gthread`:** 4 hilos por worker y `timeout` de 330 s para que el
  SSE no bloquee el resto de peticiones ni muera a los 300 s.
- **Confirmación e idempotencia:** modal de confirmación con objetivo, acción,
  valor y política NTP; UUID oculto por formulario y memoria acotada para que
  un doble envío redirija al progreso existente en vez de crear otro lote.
- **Estado de tareas persistido:** esquema v5 con `task_state`; cada tarea
  guarda una instantánea JSON en SQLite y se recupera si la memoria se pierde.
  Al arrancar se purgan las antiguas (`ACC_JOB_RETENTION_HOURS`) y se
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
- **Configuración P4:** `ACC_TIME_TOLERANCE`,
  `ACC_DEFAULT_NTP_SERVERS` y `ACC_MAX_NTP_SERVERS`.
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
- **Configuración:** `ACC_CSRF_*`, `ACC_*_RATE_*`,
  `ACC_MIN_CIDR_PREFIX`, `ACC_MAX_HOSTS`,
  `ACC_TRUST_PROXY`, `ACC_CSP_ENABLED`,
  `ACC_SECURITY_HEADERS`, `ACC_HSTS*`.
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

- MFA, aprobaciones y rollback asistido.
- Exportación de historial e inventarios/CSV/grupos/exclusiones.
- API/CLI de ejecución, webhooks y exportación.
- Multiplataforma (RHEL/Rocky, Windows, red).
- Refactor de `app.py` en blueprints y secretos Docker montados como archivo
  (`*_FILE`) en lugar de variables de entorno.
- Linters (`ruff`, `mypy`, `bandit`, `ansible-lint`, `shellcheck`) y pruebas de
  integración SSH/Docker en CI.

Consulta el detalle y la priorización en
[docs/ANALISIS_Y_RECOMENDACIONES.md](docs/ANALISIS_Y_RECOMENDACIONES.md).
