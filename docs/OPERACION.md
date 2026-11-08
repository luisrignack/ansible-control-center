# Operación y runbook — Ansible Control Center

Guía práctica para operar, diagnosticar y mantener Ansible Control Center en producción.
Complementa [ARQUITECTURA](ARQUITECTURA.md) y [REFERENCIA](REFERENCIA.md).

> **Antes de operar sobre equipos reales:** confirma que el interruptor
> **Simulación** está desactivado (o `ACC_DRY_RUN=0` si aún no hay valor
> guardado), que el host está en `known_hosts` y que tienes
> autorización explícita. La interfaz muestra un banner de simulación, el lote
> guarda `dry_run` y el historial separa hosts reales de simulados. La vista
> previa ejecuta una inspección real de solo lectura incluso en DRY RUN.

---

## 1. Ciclo de vida de un lote

```
Formulario
   │
   ├── mode=preview ──► lote status=preview
   │                      ├── host result=preview (inspección OK)
   │                      └── host result=error   (sin acceso)
   │
   └── mode=execute ──► lote status=running
                          ├── por host: ping + playbook + verificación
                          ├── host result=success | failure
                          └── finish_batch → success | partial | failure | error
```

### 1.1. Creación

`execute_with_progress` resuelve el objetivo, descifra el vault (o usa las
contraseñas temporales), deduplica candidatas y crea el lote con un `batch_id`
UUID antes de tocar ningún host.

Si falla la resolución, el vault o la lista de contraseñas, **no se crea lote**:
la tarea SSE termina en `error`.

### 1.2. Procesamiento por host

Para cada host, en orden secuencial:

1. Se prueba `ansible -m ping` con las contraseñas candidatas, hasta
   `ACC_MAX_PASSWORD_ATTEMPTS` (3 por defecto).
2. La primera que autentica ejecuta `cambiar_hora.yml` mediante `apply_host`.
3. Si el playbook falla o la verificación no coincide, se prueba la siguiente
   candidata para ese mismo host, sin superar el límite anterior.
4. Se guarda **un único resultado final** por host en `execution_hosts`, con
   duración, estado previo, estado posterior y error.
5. Se conserva además un registro en `executions` (tabla heredada).

Un fallo de host **no detiene** el lote (best-effort). El lote tiene un
presupuesto total de `ACC_BATCH_TIME_BUDGET` segundos (3600 por
defecto); al agotarse, los hosts pendientes se registran como `skipped` y el
lote cierra en vez de quedar abierto durante horas.

### 1.3. Cierre

`finish_batch` recalcula contadores desde `execution_hosts`, fija `finished_at`
y deriva el estado global:

| Estado | Significado |
|---|---|
| `preview` | Lote de vista previa (no se aplicó nada) |
| `success` | Todos los hosts procesados terminaron con éxito |
| `partial` | Hay al menos un éxito y algún fallo/error o estación con atención |
| `failure` | Ningún host tuvo éxito |
| `error` | Error global del lote o todos los hosts con resultado `error` |
| `running` | Lote aún abierto (o huérfano; ver §4) |

---

## 2. Operación normal

### 2.1. Antes de ejecutar

- [ ] Interruptor **Simulación** desactivado si el cambio debe ser real.
- [ ] Hosts accesibles por SSH y presentes en `known_hosts`.
- [ ] Usuario con `sudo`/`become` y contraseña correcta en el vault o `.txt`.
- [ ] Objetivo dentro del rango permitido (`MAX_HOSTS`, `MIN_CIDR_PREFIX`) y,
      si aplica, de `ACC_ALLOWED_NETWORKS`.
- [ ] Acción y valor revisados:
  - `TZ` → zona IANA existente (`America/Caracas`). Antes del cambio, el
    playbook guarda `/etc/timezone` en `/etc/timezone.bak`.
  - `TIME` → `AAAA-MM-DDTHH:MM`; decide `utc` o `local`.
  - `APT` → revisa el umbral de espacio libre y el estado de la estación en
    la vista previa (migración o espacio insuficiente se informan, no se
    ejecutan).
- [ ] Política NTP elegida conscientemente (ver §2.3).
- [ ] Ventana de mantenimiento si el salto de hora puede afectar servicios.
- [ ] Copia reciente de SQLite y del vault.

### 2.2. Vista previa recomendada

Ejecuta primero `mode=preview`. Crea un lote `preview`, inspecciona cada host
con `estado_hora.yml` (solo lectura) y guarda. La inspección se ejecuta de
verdad aunque `DRY_RUN` esté activo: es de solo lectura y así se detectan
hosts caídos o credenciales inválidas:

- estado actual (`before_state`);
- plan calculado (`after_state`): `planned`, `would_change`, acción, valor,
  `time_mode` y `ntp_policy`.

La vista previa **nunca** llama a `apply_host`. Si un host falla en la
inspección, queda como `error` y el resto continúa.

### 2.3. Políticas NTP

| Política | Efecto | Cuándo usarla |
|---|---|---|
| `keep` | No toca NTP | Solo `TZ`; con `TIME` NTP puede revertir el cambio |
| `off` | Desactiva NTP y lo deja off | Equipos que no deben sincronizar |
| `restore` | Desactiva, fija hora y restaura el estado NTP previo | Ajuste puntual sin cambiar el estado final de NTP |
| `configure` | Escribe servidores y activa NTP | Estado final deseado con sincronización |

> **Riesgo `TIME` + `keep`:** si NTP sigue activo, puede corregir
> inmediatamente la hora fijada con `date -s`. La verificación puede pasar por
> tolerancia y el reloj cambiar después. Para `TIME`, usa `off`, `restore` o
> `configure`.
>
> **`restore`:** conserva el estado previo de NTP. Si estaba activo, se
> reactiva tras fijar la hora; si estaba desactivado, sigue desactivado.

### 2.4. Criticidad y ventanas de mantenimiento

Cada grupo del inventario tiene una criticidad (`low`, `medium`, `high` o
`critical`) y, opcionalmente, una ventana en **UTC**. Los hosts pueden
sobrescribir la criticidad del grupo; si el campo queda vacío, heredan.

- **Ventana**: días ISO (`1`=lunes … `7`=domingo), hora de inicio y hora de
  fin. Si `inicio > fin` la ventana cruza medianoche (p. ej. `22:00–06:00`).
- **Bloqueo**: solo los hosts `critical` bloquean una ejecución real fuera de
  la ventana. Los hosts `high` generan aviso; `low`/`medium` no bloquean.
- **Sin ventana**: un grupo `critical` sin ventana avisa pero no bloquea, para
  no romper despliegues existentes. Configura la ventana antes de clasificar
  hosts como críticos en producción.
- **Vista previa y DRY RUN**: nunca se bloquean; permiten comprobar el estado
  sin modificar nada.
- **Excepción**: solo un administrador puede autorizarla, con un motivo de al
  menos 10 caracteres. Queda registrada en `/audit` como `safety_override` con
  el usuario, el objetivo y el motivo. El bloqueo se registra como
  `safety_blocked`.
- **Programaciones**: no admiten excepción. Si al vencer caen fuera de la
  ventana, se posponen hasta la próxima apertura conservando la programación
  activa y dejando el motivo en `last_error`.

La política se configura desde `/inventario` (tarjeta de cada grupo y selector
por host). El formulario de ejecución muestra la criticidad y la ventana del
grupo seleccionado antes de enviar.

### 2.5. Durante la ejecución

- La página `/progress/<task_id>` muestra progreso, resumen de hitos y la
  salida de `ansible-playbook` en vivo por SSE.
- La consola permite auto-scroll, limpieza y descarga del log. Los secretos
  conocidos se redactan, los marcadores de estado se compactan y solo se
  conservan las últimas 300 líneas.
- Si el SSE no está disponible, la página reintenta por polling cada 2 s. El
  stream se acota a 90 s y se cierra solo al terminar el lote; si sigue en
  curso, la página continúa por polling.
- Un operador solo puede cancelar las operaciones que lanzó; un administrador
  puede cancelar cualquiera. La cancelación y la denegación quedan auditadas.
- El detalle final está en `/batches/<batch_id>`.
- No cierres el proceso ni reinicies el servicio durante un lote: el estado de
  las tareas vive en memoria y el lote puede quedar `running`.

### 2.6. Después de ejecutar

1. Revisa el estado global y los contadores del lote.
2. Revisa la tabla por host: resultado, duración, error y estados.
3. Si hay `failure`, consulta §4 y §5.
4. Si el cambio era `TIME`, comprueba que el reloj sigue estable (NTP, VM,
   hipervisor).

### 2.7. Tapiz oficial (fondo de escritorio)

La tarjeta **Tapiz oficial** aplica una imagen de fondo en estaciones Linux
con GNOME. Solo se ejecuta manualmente: una programación no puede transportar
la imagen subida.

1. Abre `/tarea/tapiz` y selecciona la imagen (PNG, JPEG, WebP, GIF o BMP;
   máximo `ACC_MAX_UPLOAD_BYTES`, 2 MB por defecto). Al elegirla, la tarjeta
   muestra una **miniatura de vista previa** junto al selector de acción; es
   una lectura local del navegador y la imagen solo se envía al confirmar.
2. El servidor valida la **firma real** del archivo (no el nombre ni el
   `Content-Type`), lo guarda en `ACC_DATA_DIR/wallpapers` con permisos
   `0600` y lo borra al terminar el lote. SVG y ejecutables se rechazan.
3. Elige una IP o un grupo del inventario Linux y pulsa **Vista previa** para
   comprobar la URI actual, o **Ejecutar** para aplicar el cambio.
4. El playbook copia la imagen a `/usr/local/share/backgrounds/acc-tapiz.<ext>`,
   escribe `/etc/dconf/db/local.d/00-acc-tapiz`, ejecuta `dconf update` y
   aplica el fondo a las sesiones GNOME activas con `gsettings`.
5. La verificación exige que la imagen exista, que la configuración
   persistente contenga la URI y que no haya fallos en las sesiones activas.

La configuración se publica como **valor predeterminado del sistema**, por lo
que el tapiz se conserva tras reiniciar y se aplica a cualquier usuario que
inicie sesión. El botón **Descargar informe** genera el PDF con estación,
fecha/hora, estado y causa del error. No hay soporte para Windows ni para
escritorios distintos de GNOME; en esos casos la estación se reporta como
fallida con la causa correspondiente.

### 2.8. Actualización segura del sistema (APT)

La tarjeta **Actualización del sistema** (`apt-upgrade`) aplica
`apt-get upgrade` en estaciones Linux con `apt`. Solo la puede lanzar un
administrador y también admite programación reutilizando el formulario
existente.

1. Abre `/tarea/apt-upgrade`, elige la acción **Actualización segura del
   sistema (APT)** y ajusta el **umbral mínimo de espacio libre (GB)**. El
   valor por defecto es `ACC_APT_MIN_FREE_GB` (2 GB); el rango del formulario
   está acotado por `ACC_APT_MIN_FREE_GB_FLOOR`/`_CEILING`.
2. **Vista previa** inspecciona sin modificar: sistema operativo, nombre,
   RAM, CPU, disco, paquetes pendientes, estimación de descarga/instalación y
   si la estación está bloqueada por política o por espacio.
3. **Ejecutar** actualiza el índice y aplica `upgrade: safe` con
   `DEBIAN_FRONTEND=noninteractive`, `force-confdef`/`force-confold` y
   `lock_timeout=300`. No usa `dist-upgrade`/`full-upgrade`, no elimina
   paquetes y no reinicia.
4. Al terminar, la consola avisa si quedan paquetes pendientes (retenidos o
   por fases) y si existe `/var/run/reboot-required`.

**Política de migración.** Las distribuciones `ubuntu:20.04`,
`linuxmint:20`, `linuxlite:5` y `mx:19` (configurables con
`ACC_APT_BLOCKED_OS`) no se actualizan. Tampoco las versiones cuyo codename
sea `focal`. La estación queda en estado **Requiere atención**, motivo
`migration`, y se incluye en el **informe de migración** con su nombre, IP,
SO, RAM, CPU, disco, acción recomendada y referencia de 25 GB de disco de
Ubuntu Server. La migración se planifica fuera de la plataforma: no se
ejecuta `do-release-upgrade`.

**Política de espacio.** El umbral efectivo es
`max(umbral_configurado, estimación_APT + margen)`, con margen
`max(20 %, 256 MB)`; si APT no estima, se exige 1 GB adicional. Se compara el
espacio libre de `/` y de `/var/cache/apt/archives`. Si no alcanza, la
estación queda en **Requiere atención**, motivo `space`, y aparece con IP,
nombre y motivo en el **informe general** del lote.

Los resultados se ven en `/batches/<batch_id>` y `/history`; el botón
**Informe de migración** del detalle (y de la tarjeta) genera el PDF aparte.
Un lote con estaciones correctas y estaciones con atención queda en estado
`partial`.

### 2.9. Contraseña administrativa (Linux)

La tarjeta **Contraseña administrativa** (`contrasena`) cambia la contraseña
de un usuario local en estaciones Linux. Solo la puede lanzar un
administrador y **no se puede programar**: la contraseña no se persiste en
ningún sitio, por lo que no hay forma segura de transportarla a una ejecución
desatendida.

1. Abre `/tarea/contrasena`, elige la acción **Cambiar contraseña de usuario
   (PASSWORD)**.
2. Escribe el **usuario del sistema** al que se cambiará la contraseña. El
   campo propone `ACC_OS_PASSWORD_DEFAULT_USER` (`ladmin` por defecto) y
   admite cualquier usuario local existente con nombre válido.
3. Escribe la **nueva contraseña** y **repítela**. El servidor exige que
   coincidan y que tengan al menos `ACC_OS_PASSWORD_MIN_LENGTH` caracteres
   (8 por defecto). La contraseña no se recorta ni se muestra en errores.
4. **Vista previa** no pide contraseña: solo comprueba que el usuario exista
   y muestra su estado. Si el usuario no existe, la estación se reporta como
   fallo.
5. **Ejecutar** exige la contraseña. La tarea es destructiva, así que otro
   administrador debe aprobarla antes de que se aplique. Tras la aprobación,
   vuelve a enviar el formulario con el usuario y la contraseña: el secreto no
   forma parte de los parámetros de la aprobación y debe reescribirse.

**Seguridad del secreto.** La contraseña se envía al playbook dentro del
fichero temporal de variables (`0600`) y se entrega a `chpasswd` por `stdin`
con `no_log: true`. No se guarda en SQLite, en el historial, en la consola, en
los marcadores de estado, en los informes PDF ni en el vault. Los parámetros
de aprobación solo contienen el usuario objetivo y los datos de la operación.

**Resultado.** El playbook publica `PASSWORD_BEFORE_B64` y
`PASSWORD_STATE_B64` (usuario, existencia, UID, shell y estado de
`passwd -S`) y verifica que la cuenta quede con contraseña activa (`P`) en
`PASSWORD_VERIFIED`. El valor visible del lote es el nombre del usuario.

---

## 3. Interpretación de resultados

### 3.1. Resultado por host

| Resultado | Origen | Interpretación |
|---|---|---|
| `success` | `apply_host` | Playbook ejecutado y verificado (`HORA_VERIFIED`, `TAPIZ_VERIFIED`, `APT_VERIFIED` o `PASSWORD_VERIFIED`) |
| `failure` | `apply_host` | Sin acceso, playbook fallido o verificación incorrecta |
| `error` | Vista previa | No se pudo inspeccionar/autenticar |
| `preview` | Vista previa | Inspección correcta; incluye plan |
| `attention` | `apply_host` | APT no se ejecutó por política: `migration` (distribución bloqueada) o `space` (espacio insuficiente) |
| `skipped` | Reservado | No se usa actualmente en el flujo |

`error` de un host contiene el último error relevante: autenticación,
playbook o verificación. No contiene contraseñas.

### 3.2. Estados antes/después

`before_state` y `after_state` son JSON. En ejecución contienen el estado leído
por el playbook (`timezone`, `ntp`, `ntp_synchronized`, `local_rtc`, `utc`,
`local`, `epoch`, `rtc`). Para `WALLPAPER` contienen `wallpaper_uri`,
`image`, `applied_users` y `session_failures`. Para `APT` contienen
`os_id`, `os_version`, `os_pretty`, `hostname`, `ram_mb`, `cpu_cores`,
`disk_total_gb`, `disk_free_gb`, `pending`, `blocked`, `space_ok`,
`attention_kind`, `attention_reason` y `reboot_required`. En vista previa,
`after_state` es el plan. Para `PASSWORD` contienen `user`, `exists`, `uid`,
`shell`, `status` y, en ejecución, `changed`, `verified` y
`reboot_required`. El secreto nunca aparece en estos estados.

### 3.3. Dónde mirar

```bash
# Log de la aplicación (si LOG_TO_FILE=1)
sudo tail -f /var/log/ansible-control-center/ansible-control-center.log

# Servicio systemd
sudo journalctl -u ansible-control-center -f

# Últimos lotes
sqlite3 /var/lib/ansible-control-center/acc.db \
  "SELECT batch_id, created_at, status, operator, action, value,
          success_count, failure_count, error_count
     FROM execution_batches ORDER BY created_at DESC LIMIT 10;"

# Detalle de un lote
sqlite3 -header -column /var/lib/ansible-control-center/acc.db \
  "SELECT host, result, duration_ms, substr(error,1,120) AS error
     FROM execution_hosts WHERE batch_id='<BATCH_ID>' ORDER BY id;"
```

---

## 4. Diagnóstico de fallos

### 4.1. El lote se queda en `running`

Causas habituales:

- el proceso murió o fue reiniciado durante el lote;
- el worker de Gunicorn alcanzó su `timeout` (330 s por defecto) mientras el
  lote seguía en curso;
- excepción no capturada antes de `finish_batch` (poco probable: hay
  salvaguarda).

Comprobación:

```bash
sqlite3 /var/lib/ansible-control-center/acc.db \
  "SELECT batch_id, created_at, total_hosts,
          success_count + failure_count + error_count + preview_count AS procesados
     FROM execution_batches
    WHERE status='running' AND finished_at IS NULL
    ORDER BY created_at;"
```

No existe reconciliación automática. Procedimiento manual:

1. Confirma que no hay ningún proceso trabajando en ese lote.
2. Revisa `execution_hosts` para saber qué hosts llegaron a procesarse.
3. Si el lote ya no puede continuar, ciérralo de forma explícita:

```sql
UPDATE execution_batches
   SET status='error', finished_at=datetime('now')
 WHERE batch_id='<BATCH_ID>' AND status='running';
```

4. Relanza solo los hosts pendientes con una nueva operación (no hay reintento
   selectivo integrado).

### 4.2. `failure` por autenticación

Síntomas: error de ping, `Permission denied`, `sshpass` ausente, timeout.

- Verifica `sshpass` en el servidor: `command -v sshpass`.
- Comprueba conectividad y puerto 22 desde el servidor.
- Revisa `known_hosts` si `HOST_KEY_CHECKING=1` (primer acceso falla).
- Confirma que la contraseña está en el vault y que la maestra es correcta.
- Recuerda que el usuario debe poder usar `sudo`; hoy se reutiliza la misma
  contraseña para SSH y `become`.

### 4.3. `failure` por playbook o verificación

- `HORA_VERIFIED=false`: el estado final no coincide con el objetivo.
- Comprueba `before_state`/`after_state` y el campo `error`.
- Causas típicas:
  - NTP activo revirtió la hora (`TIME` + `keep`);
  - VM/hipervisor resincronizó el reloj;
  - `hwclock` no disponible en contenedor;
  - zona horaria inexistente o `tzdata` desactualizado;
  - permisos insuficientes para `timedatectl`/`date`.
- La tolerancia se controla con `ACC_TIME_TOLERANCE` (2 s por defecto).

### 4.4. `partial`

Es el resultado esperado cuando algunos hosts funcionan y otros no. No es un
error del motor: revisa host por host y decide si relanzar el subconjunto.

### 4.5. El progreso SSE no avanza o devuelve 404

- Con `workers=1` y `worker_class=gthread`, el SSE no bloquea otros hilos.
  Como alternativa, consulta `/api/task-status/<task_id>`.
- Si el proceso se reinició, `_tasks` se perdió: el SSE devolverá 404 aunque el
  lote siga en la base de datos.
- Si el lote terminó, abre `/batches/<batch_id>` directamente.

### 4.6. Lote atascado en `running`

Al arrancar, solo se marcan como `error` los lotes `running` **sin actividad
reciente** (ni resultados de host dentro de `ACC_RECOVERY_STALE_MINUTES`, 15
min por defecto); así un worker no cierra un lote que otro está ejecutando.
También puedes forzar la reconciliación manualmente:

```bash
sudo -u ansible-control-center .venv/bin/flask --app app reconcile-batches
```

### 4.7. “Éxito” en modo simulación

Con `ACC_DRY_RUN=1` el resultado del cambio se simula: no se toca el
host. El panel y el detalle muestran el badge `DRY RUN`, el lote guarda
`dry_run` y el historial separa los hosts simulados de los reales. Aun así,
**comprueba el modo antes de dar un cambio por real**: el estado `success` de
un lote simulado no significa que la hora se haya aplicado.

La vista previa es la excepción: su playbook de inspección es de solo lectura y
se ejecuta de verdad incluso en `DRY_RUN`, para detectar hosts caídos o
credenciales inválidas.

### 4.8. `attention` en la tarjeta APT

Una estación pasa a **Requiere atención** cuando la política decide no
actualizarla. No es un fallo del motor ni cuenta como error:

- `migration`: Ubuntu 20.04, Linux Mint 20, Linux Lite 5, MX 19 o codename
  `focal`. Revisa el **informe de migración**; el espacio también aparece en
  el informe general.
- `space`: el espacio libre de `/` o de la caché de APT no alcanza
  `max(umbral, estimación + margen)`. Libera espacio o reduce la caché y
  relanza; el motivo incluye los GB libres, los requeridos y el umbral.

El detalle del lote muestra IP, nombre y motivo, y el historial incluye el
contador de atenciones. Si el lote mezcla éxitos y atenciones, el estado es
`partial`.

---

## 5. Rollback

No hay botón de rollback ni reversión automática. El estado previo sí queda
guardado en `execution_hosts.before_state`.

### 5.1. Revertir zona horaria

1. Lee `before_state.timezone` del host.
2. Lanza una nueva operación `TZ` con ese valor.
3. Usa `ntp_policy=keep` (cambiar `TZ` no requiere tocar NTP).

### 5.2. Revertir hora

1. Lee `before_state.utc` o `before_state.epoch`.
2. Lanza `TIME` en modo `utc` con la hora previa.
3. Elige la política NTP según `before_state.ntp`:
   - si NTP estaba activo: `restore` o `configure`;
   - si NTP estaba desactivado: `off` o `restore` (ambos lo dejan
     desactivado).
4. Si el salto es grande, coordina ventana de mantenimiento.

### 5.3. Revertir un lote completo

No existe operación “revertir lote”. Repite el procedimiento por host o prepara
un objetivo agregado con los valores previos. Documenta el nuevo `batch_id`
como operación de reversión.

---

## 6. Copias de seguridad y restauración

### 6.1. Qué guardar

| Elemento | Ruta habitual | Contenido |
|---|---|---|
| SQLite | `/var/lib/ansible-control-center/acc.db` | Usuarios, hashes, lotes, resultados |
| WAL/SHM | `acc.db-wal`, `acc.db-shm` | Cambios aún no consolidados |
| Vault | `/etc/ansible-control-center/secrets/passwords_vault.yml` | Contraseñas cifradas |
| `.env` | `/opt/ansible-control-center/.env` | `SECRET_KEY` y configuración |
| Logs | `/var/log/ansible-control-center/` | Diagnóstico |

### 6.2. Copia consistente

Con el servicio parado:

```bash
sudo systemctl stop ansible-control-center
sudo install -d -m 0700 /var/backups/ansible-control-center
sudo cp -a /var/lib/ansible-control-center/acc.db* /var/backups/ansible-control-center/
sudo cp -a /etc/ansible-control-center/secrets/passwords_vault.yml /var/backups/ansible-control-center/
sudo cp -a /opt/ansible-control-center/.env /var/backups/ansible-control-center/
sudo systemctl start ansible-control-center
```

En caliente, usa la API de backup de SQLite (incluye WAL):

```bash
sudo sqlite3 /var/lib/ansible-control-center/acc.db \
  ".backup '/var/backups/ansible-control-center/acc-$(date +%F).db'"
```

### 6.3. Restauración

```bash
sudo systemctl stop ansible-control-center
sudo cp /var/backups/ansible-control-center/acc-AAAA-MM-DD.db \
      /var/lib/ansible-control-center/acc.db
sudo rm -f /var/lib/ansible-control-center/acc.db-wal \
           /var/lib/ansible-control-center/acc.db-shm
sudo cp /var/backups/ansible-control-center/passwords_vault.yml \
      /etc/ansible-control-center/secrets/passwords_vault.yml
sudo chown ansible-control-center:ansible-control-center \
      /var/lib/ansible-control-center/acc.db \
      /etc/ansible-control-center/secrets/passwords_vault.yml
sudo chmod 600 /etc/ansible-control-center/secrets/passwords_vault.yml
sudo systemctl start ansible-control-center
```

Después, verifica login, `/history` y que el vault se descifra con la
contraseña maestra esperada.

---

## 7. Retención y limpieza

`ACC_JOB_RETENTION_HOURS` (24), `ACC_HISTORY_DAYS` (180) y
`ACC_AUTH_EVENT_DAYS` (365) se aplican al arrancar (solo en procesos con rol
`all`/`worker`, no en `web`/`cli`): se borran las instantáneas de tareas, los
lotes y los eventos de acceso más antiguos. `task_state` también se recorta a
`ACC_MAX_TASK_STATE_ROWS`. El scheduler reaplica esta limpieza cada
`ACC_RETENTION_INTERVAL_HOURS` (24 h por defecto), así que no hace falta
reiniciar. Para forzar la limpieza de inmediato, usa el procedimiento manual
de §7.1.

### 7.1. Borrar lotes antiguos manualmente

La purga automática ya se ejecuta al arrancar; este procedimiento sirve para
forzarla sin reiniciar el servicio.

`execution_hosts` tiene `ON DELETE CASCADE`, así que basta borrar el lote:

```sql
-- Revisa primero qué se va a borrar
SELECT batch_id, created_at, status FROM execution_batches
 WHERE created_at < datetime('now', '-180 days');

-- Borrado (haz copia antes)
DELETE FROM execution_batches
 WHERE created_at < datetime('now', '-180 days');
```

La tabla heredada `executions` no se borra en cascada. Si también quieres
limitarla:

```sql
DELETE FROM executions WHERE timestamp < datetime('now', '-180 days');
```

### 7.2. Compactar

```bash
sudo sqlite3 /var/lib/ansible-control-center/acc.db "PRAGMA wal_checkpoint(TRUNCATE);"
sudo sqlite3 /var/lib/ansible-control-center/acc.db "VACUUM;"
```

Ejecuta `VACUUM` con el servicio parado o en ventana de baja actividad.

---

## 8. Mantenimiento

### 8.1. Actualizar la aplicación

```bash
# 1. Copia de seguridad (§6)
sudo systemctl stop ansible-control-center
cd /opt/ansible-control-center
sudo -u ansible-control-center git pull            # o copia el nuevo código
sudo -u ansible-control-center .venv/bin/pip install -r requirements.txt
sudo -u ansible-control-center .venv/bin/flask --app app init-db
sudo systemctl start ansible-control-center
```

`init-db` es idempotente y aplica migraciones no destructivas. El esquema
actual es `PRAGMA user_version = 14`.

### 8.2. Rotar la contraseña del administrador

```bash
sudo -u ansible-control-center .venv/bin/flask --app app init-admin
```

Vuelve a pedir la contraseña y actualiza el hash Werkzeug.

### 8.3. Rotar `SECRET_KEY`

1. Genera una nueva: `openssl rand -hex 32`.
2. Actualiza `ACC_SECRET_KEY` en `.env`.
3. Reinicia el servicio. **Todas las sesiones activas se invalidan.**

### 8.4. Rotar la contraseña maestra del vault

`ansible-vault rekey` no está integrado en la interfaz. Procedimiento manual:

```bash
sudo -u ansible-control-center ansible-vault rekey \
  /etc/ansible-control-center/secrets/passwords_vault.yml
```

Guarda la nueva contraseña en el gestor de secretos correspondiente. No se
persiste en la aplicación.

### 8.5. Actualización de paquetes del sistema (tarjeta APT)

La tarjeta **Actualización del sistema** (`apt-upgrade`) está operativa. El
flujo completo está en §2.8. Recomendación para automatización: usar
**`apt-get`**, no `apt`. `apt` está pensado para uso interactivo y advierte
de que su interfaz CLI no es estable para scripts. En Ansible se usa el
módulo `ansible.builtin.apt`, que invoca `apt-get` internamente.

Equivalente manual de la política del playbook:

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y \
  -o Dpkg::Options::=--force-confdef \
  -o Dpkg::Options::=--force-confold \
  -o DPkg::Lock::Timeout=300 \
  upgrade
```

Decisiones:

- `upgrade` (seguro) no elimina paquetes ni instala dependencias nuevas;
  `full-upgrade`/`dist-upgrade` puede desinstalar paquetes y no se usa.
- `force-confdef`/`force-confold` conservan la configuración local y evitan
  preguntas interactivas.
- Las distribuciones de `ACC_APT_BLOCKED_OS` (Ubuntu 20.04 y derivados basados
  en focal) quedan como `attention`/`migration`; la app no ejecuta
  `do-release-upgrade`.
- El espacio se valida antes de ejecutar con el umbral configurado o la
  estimación de APT, lo que sea mayor. Una estación sin espacio queda como
  `attention`/`space`.
- No se reinicia automáticamente: se comprueba `/var/run/reboot-required` y
  se avisa para programar el reinicio en ventana.
- Debe ejecutarse dentro de la ventana de mantenimiento del grupo; los hosts
  críticos fuera de ventana quedan bloqueados por S1.

### 8.6. Cambio de contraseña de usuario (tarjeta Contraseña administrativa)

La tarjeta **Contraseña administrativa** (`contrasena`) está operativa. El
flujo completo está en §2.9. Equivalente manual del playbook:

```bash
# El nombre de usuario lo elige el operador; ladmin es el valor por defecto.
printf '%s:%s\n' "$USER" "$NEW_PASSWORD" | sudo chpasswd
sudo passwd -S "$USER"   # debe mostrar estado P
```

Decisiones:

- La contraseña se pasa por `stdin`, nunca como argumento de `chpasswd` ni en
  la línea de comandos del playbook, para que no aparezca en `ps`.
- Las tareas de Ansible que manipulan el secreto usan `no_log: true`; la app
  además redacta la contraseña de la consola en vivo.
- El playbook solo admite un usuario existente y un nombre válido; no crea
  usuarios.
- La verificación exige que `passwd -S` devuelva `P` (contraseña activa). Si
  no se puede verificar, el host se reporta como fallo.
- La contraseña no se guarda: no hay historial de contraseñas ni
  reprogramación segura. Para rotaciones periódicas, usa el gestor de secretos
  corporativo y ejecuta la tarjeta manualmente.
- Al ser destructiva, requiere la aprobación de un administrador distinto del
  ejecutor. El aprobador autoriza el usuario objetivo, no el valor del
  secreto; el ejecutor debe reescribir la contraseña después de la aprobación.

### 8.7. Logs

- `ACC_LOG_TO_FILE=1` escribe en `LOG_DIR/ansible-control-center.log` con rotación
  (`LOG_MAX_BYTES`, `LOG_BACKUPS`).
- Gunicorn escribe a stdout/stderr; en systemd va a journald.
- Revisa crecimiento y errores periódicamente.

### 8.7. `known_hosts`

Con `HOST_KEY_CHECKING=1`, provisiona las claves antes del primer uso:

```bash
ssh-keyscan -H 192.0.2.10 | sudo -u ansible-control-center tee -a ~/.ssh/known_hosts
```

Verifica las huellas por un canal de confianza antes de aceptarlas. No
desactives la verificación salvo decisión consciente y documentada.

### 8.8. Programador de tareas

- `ACC_ROLE=all` (por defecto) ejecuta web, scheduler y mantenimiento en el
  mismo proceso. `ACC_ROLE=web` deja el scheduler fuera de Gunicorn y
  `ACC_ROLE=worker` lo ejecuta en un proceso dedicado.
- Con `ACC_SCHEDULER_ENABLED=0` no se programa nada en ese proceso.
- El lock `flock` (`scheduler.lock` junto a la base de datos) garantiza un solo
  programador activo. Si otro proceso tiene el lock, el comando
  `flask --app app scheduler` no arranca y lo indica.
- Cada fila de `schedules` se reclama con un `UPDATE` atómico
  (`claimed_at`/`claimed_by`, esquema v12). La reserva caduca a los 5 minutos,
  así que un proceso caído no bloquea la programación para siempre.
- Para diagnosticar sin esperar al intervalo de 15 s:

```bash
# Una sola revisión, sin dejar el proceso en primer plano.
sudo -u ansible-control-center env ACC_ROLE=worker \
  .venv/bin/flask --app app scheduler --once

# Ver reservas y próximas ejecuciones.
sqlite3 /var/lib/ansible-control-center/acc.db \
  "SELECT id, name, run_at, enabled, claimed_at, claimed_by FROM schedules;"
```

---

## 9. Seguridad operativa

- No ejecutes la app como `root`; usa un usuario de servicio sin privilegios.
- `install.sh` crea directorios para el usuario que lo invoca, pero el unit de
  systemd documentado usa `User=ansible-control-center`. Alinea ambos (ver
  [DEPLOYMENT](../DEPLOYMENT.md#servicio-systemd)).
- Mantén `.env` en `0600` y el vault en `0600`.
- No compartas la contraseña maestra por chat/correo; usa un gestor de secretos.
- Revisa `/history` tras cada operación sensible.
- El rate limiting es en memoria y por proceso; con varios workers no es
  global.
- Hay roles `viewer`/`operator`/`admin` y auditoría de acceso, pero no MFA ni
  un sistema RBAC granular: cualquier usuario autenticado puede apuntar a una
  IP/CIDR permitida por `ACC_ALLOWED_NETWORKS` dentro de los límites globales.

---

## 10. Checklist de incidente

1. **Identifica el lote** en `/history` o por `batch_id`.
2. **Comprueba el estado global** y los contadores.
3. **Abre el detalle** y localiza hosts `failure`/`error`.
4. **Lee `error`, `before_state` y `after_state`.**
5. **Decide** entre reintentar, revertir o escalar.
6. **Documenta** la acción correctiva y el nuevo `batch_id`.
7. **Verifica** el estado final con la vista previa o manualmente en el host.
8. Si el lote quedó `running`, ciérralo manualmente (§4.1).
9. Si hubo exposición de credenciales, rota la contraseña maestra y las
   candidatas afectadas.
10. Si el incidente afectó a la hora de servicios críticos, revisa sus logs y
    dependencias (TLS, Kerberos, bases de datos, colas).

---

## 11. Limitaciones operativas conocidas

- Concurrencia acotada por cola y cancelación cooperativa; sin paralelismo
  dentro del lote ni reintento selectivo. Los intentos por host y el
  presupuesto de tiempo del lote están acotados.
- Sin rollback automático.
- Retención automática al arrancar de lotes, instantáneas y eventos de acceso;
  `task_state` se recorta por número de filas.
- Estado de tareas en memoria; SSE depende del proceso. `ACC_ROLE` y el lock
  del scheduler evitan duplicar programaciones, pero no comparten el progreso
  SSE entre workers.
- `TIME` + `keep` se rechaza en ejecución (la vista previa avisa) porque NTP
  puede revertir el cambio.
- Roles `viewer`/`operator`/`admin`, bloqueo de cuenta y auditoría de acceso;
  sin MFA.
- El tapiz solo aplica a Linux con GNOME (`dconf`/`gsettings`). Windows y
  otros escritorios se reportan como fallo con su causa; no hay rollback
  automático del fondo anterior.
- La tarjeta APT actualiza paquetes, no la distribución. Ubuntu 20.04 y sus
  derivados quedan como “Requieren atención” para migración manual; tampoco
  hay reinicio automático ni rollback de paquetes.
- Solo Ubuntu/systemd; IPv6 no probado en el flujo de Ansible.

El análisis detallado y el roadmap están en
[ANALISIS_Y_RECOMENDACIONES](ANALISIS_Y_RECOMENDACIONES.md).
