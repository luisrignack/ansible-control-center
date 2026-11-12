# Ansible Control Center — Informe técnico integral

**Estado de referencia:** 12/11/2026, commit `b77f153`, esquema SQLite **v18**, **429 pruebas recolectadas** (428 correctas, 1 omitida por depender de SSH/Docker).

---

## 1. Qué es el sistema

Ansible Control Center es una **aplicación web monolítica** de operación y automatización para administrar estaciones Linux y Windows mediante Ansible. Permite seleccionar un objetivo, inspeccionarlo, aplicar cambios, verificar el resultado y dejar un historial auditable por lote y por host.

No es un CMDB, ni un orquestador distribuido, ni una plataforma multiusuario/SaaS. Es una consola centralizada para un equipo de operaciones, pensada para redes gestionadas y despliegues pequeños o medianos.

**Casos de uso principales:**

- Cambiar la zona horaria de grupos de estaciones Linux y Windows.
- Fijar hora exacta, configurar NTP y sincronizar el RTC (motor disponible; no expuesto como tarjeta activa).
- Actualizar paquetes de forma segura con APT.
- Rotar la contraseña de un usuario local Linux con doble aprobación.
- Aplicar un tapiz corporativo en estaciones Linux con GNOME.
- Activar, desactivar o permitir una IP/red en el firewall de estaciones Linux.
- Programar ejecuciones y ejecutarlas desde un planificador dedicado.
- Auditar qué se ejecutó, sobre qué host, con qué resultado y quién lo hizo.

---

## 2. Stack tecnológico

### Backend y aplicación

| Componente | Tecnología / versión |
|---|---|
| Lenguaje | Python 3.11+; entorno local validado con 3.14.4; contenedor con 3.12 |
| Framework web | Flask 3.1.3 |
| Autenticación de sesión | Flask-Login 0.6.3 |
| Utilidades web / hash local | Werkzeug 3.1.9 |
| Servidor de producción | Gunicorn 23.0.0, worker `gthread`, 8 hilos por defecto |
| Plantillas | Jinja2, renderizado en servidor |
| Configuración | Variables de entorno y `.env` mediante `python-dotenv` 1.2.4 |
| Automatización | `ansible-core` 2.19.15, invocado como subproceso (`ansible`, `ansible-playbook`, `ansible-vault`) |
| Windows | `pywinrm` 0.5.0, conexión WinRM |
| LDAPS | `ldap3` 2.9.1 |
| Cifrado | `cryptography` 50.0.2 para Fernet; `ansible-vault` para credenciales |
| Base de datos | SQLite mediante la biblioteca estándar; sin ORM |
| Informes PDF | Generador PDF propio, sin `reportlab` ni dependencias externas |
| Correo | `smtplib` y `email` de la biblioteca estándar |

### Frontend

- **Bootstrap 5.3.2** y **Bootstrap Icons 1.11.1**, servidos localmente desde `static/vendor/`.
- CSS propio (`static/css/app.css`, ~1.327 líneas), tema oscuro y diseño responsive.
- JavaScript vanilla, sin React/Vue/Angular ni proceso de build.
- **SSE (Server-Sent Events)** para progreso en vivo.
- Fallback automático a polling cada 2 segundos si el SSE falla.
- Formularios HTML clásicos con CSRF y validación en servidor.

### Infraestructura

- **Docker** con imagen basada en `python:3.12-slim`.
- **Tini** como PID 1.
- Usuario no root `acc` (UID 10001).
- `cap_drop: ALL` y `no-new-privileges`.
- **Nginx 1.27 Alpine** como proxy inverso TLS.
- **Docker Compose** con servicios `app`, `nginx` y perfil opcional `scheduler`.
- Certificado autofirmado generado al primer arranque o certificado real montado en `certs/`.
- HTTP redirigido a HTTPS; TLS 1.2/1.3; HTTP/2; SSE sin buffering.

### Calidad y pruebas

- **pytest 9.0.3** + `pytest-cov`.
- Ruff, Black, mypy, Bandit y `pip-audit`.
- 27 módulos de prueba.
- Cobertura funcional de autenticación, CSRF, SQLite, Ansible, vault, inventario, programaciones, aprobaciones, LDAP, Windows, APT, firewall, tapiz, contraseñas, concurrencia, rendimiento y migraciones.

---

## 3. Arquitectura

### Vista general

```text
Navegador
   │ HTTPS / SSE
   ▼
Nginx (TLS, HTTP/2, proxy inverso)
   │
   ▼
Gunicorn + Flask (app.py)
   ├── Autenticación, sesión, CSRF, rate limiting
   ├── Rutas HTML y endpoints JSON internos
   ├── Validación de formularios y reglas de seguridad
   ├── Cola de trabajos en hilos (JobQueue)
   │      └── ejecución de lotes
   ├── SQLite (WAL) para estado e historial
   └── Servicios
          ├── ansible_runner: subprocess → ansible-playbook
          ├── vault_manager: ansible-vault
          ├── host_resolver: IP/CIDR/rango/DNS
          ├── scheduler: programaciones y claims
          ├── ldap_auth: LDAPS
          ├── notifier: SMTP
          └── pdf_report: PDF propio
```

### Componentes principales

| Archivo / carpeta | Responsabilidad |
|---|---|
| `app.py` | Rutas, validación, orquestación, SSE, CLI y buena parte del flujo. Es un monolito de ~5.960 líneas y 37 rutas. |
| `config.py` | Configuración central, rutas, validación de seguridad de producción, logging y valores por defecto. |
| `models/db.py` | Conexiones SQLite, WAL, `busy_timeout`, permisos `0600` y migraciones v1–v18. |
| `models/batch.py` | Lotes, resultados por host, contadores, estados, estadísticas y reconciliación. |
| `models/ops.py` | Inventario, programaciones y solicitudes de aprobación. |
| `models/user.py` | Usuarios, roles, bloqueo, eventos de autenticación. |
| `models/password.py` | Hashes HMAC-SHA256 con pepper de contraseñas candidatas. |
| `models/execution.py` | Historial heredado por host. |
| `services/ansible_runner.py` | Inspección y aplicación de playbooks, parseo de marcadores, ejecución segura y cancelación. |
| `services/password_tester.py` | Ping y prueba de credenciales candidatas. |
| `services/vault_manager.py` | Cifrado, descifrado y borrado atómico del vault. |
| `services/host_resolver.py` | Resolución y validación de IP, hostname, CIDR y rangos; allowlist y bloqueo de redes. |
| `services/safety.py` | Criticidad, ventanas de mantenimiento y evaluación de bloqueo. |
| `services/scheduler.py` | Disparo de programaciones, lock de instancia y cifrado Fernet. |
| `services/host_locks.py` | Reserva por host para evitar playbooks simultáneos sobre la misma IP. |
| `services/job_queue.py` | Cola FIFO acotada, cancelación cooperativa y apagado ordenado. |
| `services/metrics.py` | Métricas en memoria: p50, p95, errores y latencia. |
| `services/notifier.py` | Notificaciones SMTP de resultados. |
| `services/pdf_report.py` | Generación de PDF sin dependencias. |
| `services/wallpaper.py` | Validación y almacenamiento privado de imágenes de tapiz. |
| `services/ldap_auth.py` | Búsqueda y autenticación LDAPS. |
| `services/modules_panel.py` | Preferencias cosméticas de tarjetas ocultas por usuario. |
| `ansible/` | Playbooks Linux y Windows, tareas compartidas y scripts auxiliares. |
| `templates/` | 23 plantillas Jinja2. |
| `docker/` | Entrypoint, Nginx, generación TLS y empaquetado portátil. |

### Modelo de concurrencia

- La cola de trabajos tiene **2 workers** por defecto (`ACC_JOB_WORKERS`) y **10 puestos de espera** (`ACC_JOB_QUEUE_SIZE`).
- Cada lote procesa hasta **4 hosts en paralelo** (`ACC_BATCH_HOST_CONCURRENCY`).
- Todos los lotes comparten un cupo global de **8 playbooks simultáneos** (`ACC_GLOBAL_HOST_CONCURRENCY`).
- Las **vistas previas son secuenciales** porque solo leen estado.
- Antes de modificar un host se toma una **reserva en `host_locks`** con TTL de 30 minutos. Si otro lote lo tiene, el host se marca `skipped`.
- La cancelación es **cooperativa**: se atiende entre hosts y dentro de cada playbook; el proceso de Ansible se mata al cancelar.
- Un lote tiene presupuesto de tiempo (`ACC_BATCH_TIME_BUDGET`, 1 hora por defecto). Al agotarse, los hosts pendientes quedan `skipped` y el lote cierra como `incomplete`.
- SQLite usa WAL y `busy_timeout` de 5 segundos para tolerar escrituras concurrentes.
- El planificador usa un lock `flock` y un **claim atómico** por fila (`schedules.claimed_at`) para que dos procesos no lancen la misma programación.

**Limitación estructural:** el estado de las tareas y del SSE vive en memoria del proceso. Las lecturas refrescan desde `task_state`, pero la ejecución y la cancelación siguen siendo por proceso. Por eso el valor por defecto de Gunicorn es **1 worker**. Para ejecutar con varios workers haría falta una cola compartida tipo Redis/Celery, que el proyecto no usa.

---

## 4. Flujo completo de una operación

1. El usuario inicia sesión y elige una tarjeta del catálogo.
2. Selecciona un objetivo:
   - IP, hostname, CIDR o rango inclusivo `inicio-fin`.
   - Varias entradas separadas por comas.
   - Un grupo del inventario Linux o Windows.
3. Elige el origen de credenciales:
   - Vault cifrado con `ansible-vault`.
   - Archivo `.txt` temporal procesado en memoria.
4. El formulario se valida: tarea activa, rol mínimo, acción permitida, valor, plataforma, NTP, zona horaria, usuario y límites.
5. Se aplica la política de criticidad y ventana de mantenimiento.
6. Se comprueba idempotencia mediante un `operation_id` persistente.
7. Si la tarea es destructiva, se exige una aprobación de otro administrador.
8. El trabajo entra en la cola y se crea un lote con UUID.
9. Por cada host:
   - Se resuelve y fija una IP literal para evitar DNS rebinding.
   - Se prueba `ansible -m ping` con las contraseñas candidatas.
   - La primera credencial que autentica ejecuta el playbook.
   - Si el playbook falla, se prueban las siguientes candidatas hasta el límite.
   - Se guarda un único resultado final por host.
   - Se verifica el estado posterior; sin verificación correcta no hay éxito.
10. Al terminar, `finish_batch` recalcula contadores y deriva el estado global.
11. El navegador recibe el progreso por SSE y muestra la consola de Ansible en vivo.
12. Se guarda el historial, se puede descargar el PDF y, si está configurado, se envía correo.

### Vista previa frente a ejecución

- **Vista previa:** ejecuta playbooks de inspección de solo lectura, incluso si `DRY_RUN` está activo. Nunca llama a la aplicación de cambios. Registra un lote con estado `preview`.
- **Ejecución real:** aplica el playbook, verifica y registra.
- **DRY RUN:** visible y persistente en SQLite. El modo se captura al arrancar el lote, de modo que cambiar el interruptor global no mezcla hosts simulados y reales a mitad de una ejecución.
- En DRY RUN, el resultado se marca como simulado, `changed=false` y `verified=false`; no se presenta como un cambio confirmado.

### Estados

**Lote:** `running`, `success`, `partial`, `failure`, `preview`, `error`, `incomplete`, `cancelled`.

**Host:** `success`, `failure`, `skipped`, `error`, `preview`, `attention`.

`attention` se usa para estaciones APT que no se actualizaron por política: migración requerida o espacio insuficiente.

---

## 5. Modelo de datos

SQLite centralizado, esquema **v18**, migraciones idempotentes y no destructivas. La base y sus archivos WAL/SHM se restringen a `0600`.

### Tablas principales

| Tabla | Uso |
|---|---|
| `users` | Cuentas locales y sombras LDAPS; rol, estado, bloqueo, origen. |
| `auth_events` | Login, fallos, bloqueos, cambios de rol y eventos de seguridad. |
| `passwords` | Hashes cortos/largos de contraseñas candidatas y origen. |
| `executions` | Historial heredado por host. |
| `execution_summaries` | Resumen heredado. |
| `execution_batches` | Cabecera del lote: operador, objetivo, acción, valor, modo, NTP, estado, contadores, `dry_run`, `task_id`, `attention_count`, `min_free_gb`. |
| `execution_hosts` | Resultado por host: estado antes/después, error, duración, hash corto, origen, `position`. |
| `task_state` | Instantánea persistida del progreso, consola y log de tareas. |
| `app_settings` | Preferencias persistentes: DRY RUN, LDAPS, tarjetas ocultas. |
| `inventory_groups` | Grupos Linux/Windows, conexión, clave SSH, criticidad y ventana. |
| `inventory_hosts` | Hosts del grupo, etiqueta y criticidad propia opcional. |
| `schedules` | Programaciones, recurrencia, parámetros cifrados, último resultado y claim. |
| `approvals` | Solicitudes de doble aprobación, payload sin secretos, caducidad y consumo único. |
| `operation_ids` | Idempotencia de envíos entre reinicios y workers. |
| `host_locks` | Reserva temporal por host. |

### Estados de aprobación

`pending`, `approved`, `reserved`, `rejected`, `consumed`, `expired`.

La reserva `reserved` dura 10 minutos; si el encolado falla, la aprobación vuelve a `approved` y no se pierde.

### Migraciones destacadas

- v3: lotes y resultados por host.
- v5: `task_state`.
- v6: roles, bloqueo y auditoría.
- v8: inventario, programaciones y aprobaciones.
- v11: `operation_ids`.
- v12: claims de programaciones.
- v14: criticidad y ventanas.
- v15: `attention` y umbral APT.
- v16: payload de aprobaciones.
- v17: `host_locks`.
- v18: `position` para conservar el orden de entrada de hosts.

La aplicación **rechaza arrancar** si la base tiene una versión superior a la soportada.

---

## 6. Funcionalidades

### 6.1 Autenticación y roles

- Login local con Flask-Login y hash Werkzeug.
- **LDAPS opcional**:
  - Bind de solo lectura con un usuario de servicio.
  - Búsqueda del `uid` en el directorio.
  - Rebind simple con el DN del usuario para validar la contraseña.
  - La contraseña corporativa no se guarda.
  - En el primer login se crea una cuenta sombra con rol `viewer`.
  - El administrador local promueve el rol en `/usuarios`.
  - Una cuenta local siempre tiene prioridad sobre un uid del directorio.
- Interruptor LDAPS en `/usuarios`, solo para el admin local; persiste en SQLite y no requiere reiniciar.
- Roles:
  - `viewer`: solo lectura.
  - `operator`: vista previa, ejecución, vault y sus propias solicitudes.
  - `admin`: todo, incluida auditoría, usuarios, métricas y aprobaciones.
- Bloqueo por intentos fallidos: 5 fallos, 15 minutos por defecto.
- Sesión permanente de 8 horas; “recordarme” de 7 días; cookies `HttpOnly`, `SameSite=Lax` y `Secure` en producción.
- Auditoría de accesos y cambios de rol en `auth_events`.

### 6.2 Catálogo de tareas

| Tarjeta | Estado | Rol mínimo | Plataforma | Programable | Destructiva | Comportamiento |
|---|---|---|---:|---|---:|---:|---|
| **Zona horaria** | Activa | operator | Linux + Windows | Sí | No | Aplica `America/Caracas` (UTC-4). Linux con `timedatectl`; Windows con `tzutil` vía WinRM. |
| **Kaspersky Antivirus** | En preparación | admin | Linux | — | Sí | Anunciada; no tiene playbook integrado todavía. |
| **Contraseña administrativa** | Activa | admin | Linux | No | Sí | Cambia la contraseña de un usuario local existente con `chpasswd`. |
| **Tapiz oficial** | Activa | operator | Linux/GNOME | No | No | Sube una imagen y la fija como fondo predeterminado del sistema. |
| **Actualización del sistema** | Activa | admin | Linux/apt | Sí | No | `apt-get upgrade` seguro, con control de espacio y política de migración. |
| **Firewall** | Activa | admin | Linux | No | No | Activa/desactiva ufw o firewalld y permite una IP/red. |

La tarjeta **Zona horaria** tiene el valor fijo `America/Caracas`; el usuario no puede escribir otra zona. El motor interno sigue soportando `TIME`, NTP y RTC, pero **no hay una tarjeta activa que exponga la hora exacta** en el catálogo visible. Esas piezas se usan en pruebas y en el flujo heredado.

### 6.3 Objetivos e inventario

- Objetivo manual:
  - IP individual.
  - Hostname resuelto una sola vez y fijado a una IP literal.
  - CIDR con prefijo mínimo `/22` por defecto.
  - Rango inclusivo `172.17.128.118-172.17.128.235`.
  - Lista separada por comas.
- Límite de 256 hosts por ejecución.
- Validación contra `ACC_ALLOWED_NETWORKS`, obligatoria en producción.
- Bloqueo permanente de loopback, link-local (incluido `169.254.169.254`), multicast, reservadas y no especificadas.
- Inventario persistente:
  - Grupos Linux o Windows.
  - Conexión SSH o WinRM.
  - Clave SSH privada opcional por grupo, guardada en `DATA_DIR` con directorio `0700` y archivo `0600`.
  - Hosts con etiqueta y criticidad propia opcional.
  - Criticidad del grupo: `low`, `medium`, `high`, `critical`.
  - Ventana de mantenimiento en UTC, días ISO 1–7, con soporte de ventana nocturna.
- El grupo se lee **en caliente** al ejecutar; si cambia su composición, se procesa la composición actual.
- En una ejecución derivada de una aprobación se usa la instantánea de hosts autorizada, no la composición actual del grupo, para no ampliar el alcance aprobado.

### 6.4 Ejecución, credenciales y verificación

- Origen de credenciales:
  - `stored`: vault cifrado con `ansible-vault`.
  - `temporal`: archivo `.txt` procesado en memoria y no persistido.
- Máximo 200 contraseñas y 512 caracteres por contraseña.
- Hasta 3 candidatas por host (`ACC_MAX_PASSWORD_ATTEMPTS`).
- Si una contraseña autentica pero el playbook falla, se prueban las siguientes.
- Las credenciales se pasan a Ansible en un archivo temporal `0600` mediante `--extra-vars @fichero`; **nunca** en `argv`.
- El entorno de Ansible se filtra: no hereda claves de sesión, contraseñas maestras ni credenciales cloud.
- La misma contraseña candidata se usa para SSH y para `sudo`/`become`; si son distintas, el host falla.
- Verificación posterior obligatoria:
  - `TIME`: diferencia máxima de 2 segundos.
  - `TZ`: la zona final debe coincidir.
  - APT, contraseña, firewall y tapiz publican sus propios marcadores de verificación.
- Si el playbook termina con código 0 pero no publica la verificación correcta, se considera fallo.
- Consola en vivo:
  - Últimas 300 líneas.
  - Cada línea recortada a 500 caracteres.
  - Redacción de contraseñas conocidas.
  - Salida completa acotada a 2.000 líneas.
  - Persistencia agrupada cada 2 segundos.
- Estado global:
  - `success`: todos correctos.
  - `partial`: hay éxitos y fallos/errores/atenciones.
  - `failure`: ningún éxito.
  - `error`: error global.
  - `incomplete`: presupuesto agotado.
  - `cancelled`: cancelación solicitada.

### 6.5 Zona horaria, hora, NTP y RTC

**Zona horaria:**

- Linux: `timedatectl set-timezone`.
- Windows: `tzutil /s` con el mapa IANA → Windows; actualmente solo `America/Caracas` → `SA Pacific Standard Time`.
- Antes de cambiar Linux, guarda `/etc/timezone` en `/etc/timezone.bak` sin sobrescribir un backup existente.
- También copia el backup al controlador en `DATA_DIR/timezone_backups/<lote>/<host>/etc/timezone.bak`.
- En Windows no existe `/etc/timezone`; el estado previo queda en `before_state`.
- El cambio es permanente tras reiniciar.

**Hora exacta (motor interno):**

- `utc`: el valor se interpreta como UTC y se aplica el mismo instante a todos.
- `local`: el valor se interpreta en una zona IANA y se calcula un único epoch para todos.
- Soporte de ambigüedad DST: automático, primera o segunda ocurrencia.
- `sync_rtc`: `hwclock --systohc`.
- `force`: aplica aunque ya coincida, rompiendo idempotencia.

**Políticas NTP:**

| Política | Efecto |
|---|---|
| `keep` | No toca NTP. |
| `off` | Desactiva NTP y lo deja desactivado. |
| `restore` | Desactiva, fija la hora y restaura el estado previo de NTP. |
| `configure` | Configura servidores en chrony o systemd-timesyncd y activa NTP. |

`TIME` + `keep` se rechaza en ejecución porque NTP puede revertir el cambio; la vista previa solo avisa.

