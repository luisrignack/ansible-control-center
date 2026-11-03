# Operación y runbook — Hora Admin

Guía práctica para operar, diagnosticar y mantener Hora Admin en producción.
Complementa [ARQUITECTURA](ARQUITECTURA.md) y [REFERENCIA](REFERENCIA.md).

> **Antes de operar sobre equipos reales:** confirma que
> `HORA_ADMIN_DRY_RUN=0`, que el host está en `known_hosts` y que tienes
> autorización explícita. La interfaz actual **no muestra un banner permanente**
> de modo simulación y el valor `dry_run` **no se guarda** en el lote.

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

1. Se prueba `ansible -m ping` con cada contraseña candidata.
2. La primera que autentica ejecuta `cambiar_hora.yml` mediante `apply_host`.
3. Si el playbook falla o la verificación no coincide, se prueba la siguiente
   candidata para ese mismo host.
4. Se guarda **un único resultado final** por host en `execution_hosts`, con
   duración, estado previo, estado posterior y error.
5. Se conserva además un registro en `executions` (tabla heredada).

Un fallo de host **no detiene** el lote (best-effort).

### 1.3. Cierre

`finish_batch` recalcula contadores desde `execution_hosts`, fija `finished_at`
y deriva el estado global:

| Estado | Significado |
|---|---|
| `preview` | Lote de vista previa (no se aplicó nada) |
| `success` | Todos los hosts procesados terminaron con éxito |
| `partial` | Hay al menos un éxito y algún fallo/error |
| `failure` | Ningún host tuvo éxito |
| `error` | Error global del lote o todos los hosts con resultado `error` |
| `running` | Lote aún abierto (o huérfano; ver §4) |

---

## 2. Operación normal

### 2.1. Antes de ejecutar

- [ ] `HORA_ADMIN_DRY_RUN=0` si el cambio debe ser real.
- [ ] Hosts accesibles por SSH y presentes en `known_hosts`.
- [ ] Usuario con `sudo`/`become` y contraseña correcta en el vault o `.txt`.
- [ ] Objetivo dentro del rango permitido (`MAX_HOSTS`, `MIN_CIDR_PREFIX`).
- [ ] Acción y valor revisados:
  - `TZ` → zona IANA existente (`Europe/Madrid`).
  - `TIME` → `AAAA-MM-DDTHH:MM`; decide `utc` o `local`.
- [ ] Política NTP elegida conscientemente (ver §2.3).
- [ ] Ventana de mantenimiento si el salto de hora puede afectar servicios.
- [ ] Copia reciente de SQLite y del vault.

### 2.2. Vista previa recomendada

Ejecuta primero `mode=preview`. Crea un lote `preview`, inspecciona cada host
con `estado_hora.yml` (solo lectura) y guarda:

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
| `restore` | Desactiva, fija hora y reactiva NTP | Ajuste puntual con NTP activo |
| `configure` | Escribe servidores y activa NTP | Estado final deseado con sincronización |

> **Riesgo `TIME` + `keep`:** si NTP sigue activo, puede corregir
> inmediatamente la hora fijada con `date -s`. La verificación puede pasar por
> tolerancia y el reloj cambiar después. Para `TIME`, usa `off`, `restore` o
> `configure`.
>
> **Riesgo `restore`:** la implementación actual reactiva NTP aunque estuviera
> desactivado antes. No es una restauración exacta del estado previo.

### 2.4. Durante la ejecución

- La página `/progress/<task_id>` muestra progreso, resumen de hitos y la
  salida de `ansible-playbook` en vivo por SSE.
- La consola permite auto-scroll, limpieza y descarga del log. Los secretos
  conocidos se redactan, los marcadores de estado se compactan y solo se
  conservan las últimas 300 líneas.
- Si el SSE no está disponible, la página reintenta por polling cada 2 s.
- El detalle final está en `/batches/<batch_id>`.
- No cierres el proceso ni reinicies el servicio durante un lote: el estado de
  las tareas vive en memoria y el lote puede quedar `running`.

### 2.5. Después de ejecutar

1. Revisa el estado global y los contadores del lote.
2. Revisa la tabla por host: resultado, duración, error y estados.
3. Si hay `failure`, consulta §4 y §5.
4. Si el cambio era `TIME`, comprueba que el reloj sigue estable (NTP, VM,
   hipervisor).

---

## 3. Interpretación de resultados

### 3.1. Resultado por host

| Resultado | Origen | Interpretación |
|---|---|---|
| `success` | `apply_host` | Playbook ejecutado y `HORA_VERIFIED=true` |
| `failure` | `apply_host` | Sin acceso, playbook fallido o verificación incorrecta |
| `error` | Vista previa | No se pudo inspeccionar/autenticar |
| `preview` | Vista previa | Inspección correcta; incluye plan |
| `skipped` | Reservado | No se usa actualmente en el flujo |

`error` de un host contiene el último error relevante: autenticación,
playbook o verificación. No contiene contraseñas.

### 3.2. Estados antes/después

`before_state` y `after_state` son JSON. En ejecución contienen el estado leído
por el playbook (`timezone`, `ntp`, `ntp_synchronized`, `local_rtc`, `utc`,
`local`, `epoch`, `rtc`). En vista previa, `after_state` es el plan.

### 3.3. Dónde mirar

```bash
# Log de la aplicación (si LOG_TO_FILE=1)
sudo tail -f /var/log/hora-admin/hora-admin.log

# Servicio systemd
sudo journalctl -u hora-admin -f

# Últimos lotes
sqlite3 /var/lib/hora-admin/hora_admin.db \
  "SELECT batch_id, created_at, status, operator, action, value,
          success_count, failure_count, error_count
     FROM execution_batches ORDER BY created_at DESC LIMIT 10;"

# Detalle de un lote
sqlite3 -header -column /var/lib/hora-admin/hora_admin.db \
  "SELECT host, result, duration_ms, substr(error,1,120) AS error
     FROM execution_hosts WHERE batch_id='<BATCH_ID>' ORDER BY id;"
```

---

## 4. Diagnóstico de fallos

### 4.1. El lote se queda en `running`

Causas habituales:

- el proceso murió o fue reiniciado durante el lote;
- el worker de Gunicorn alcanzó su `timeout` (120 s por defecto) mientras el
  SSE esperaba hasta 300 s;
- excepción no capturada antes de `finish_batch` (poco probable: hay
  salvaguarda).

Comprobación:

```bash
sqlite3 /var/lib/hora-admin/hora_admin.db \
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
- La tolerancia se controla con `HORA_ADMIN_TIME_TOLERANCE` (120 s por defecto).

### 4.4. `partial`

Es el resultado esperado cuando algunos hosts funcionan y otros no. No es un
error del motor: revisa host por host y decide si relanzar el subconjunto.

### 4.5. El progreso SSE no avanza o devuelve 404

- Con `workers=1` y `worker_class=sync`, una conexión SSE puede bloquear el
  worker. Usa `threads`/`gevent` o consulta `/api/task-status/<task_id>`.
- Si el proceso se reinició, `_tasks` se perdió: el SSE devolverá 404 aunque el
  lote siga en la base de datos.
- Si el lote terminó, abre `/batches/<batch_id>` directamente.

### 4.6. “Éxito” en modo simulación

Con `HORA_ADMIN_DRY_RUN=1` el resultado se simula: `success=True` y
`verified=True` sin tocar el host. La interfaz no lo distingue visualmente.
**Comprueba siempre el valor de la variable de entorno antes de dar un cambio
por real.** El lote tampoco guarda `dry_run`.

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
   - si NTP estaba desactivado: `off` (no uses `restore`, porque lo activaría).
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
| SQLite | `/var/lib/hora-admin/hora_admin.db` | Usuarios, hashes, lotes, resultados |
| WAL/SHM | `hora_admin.db-wal`, `hora_admin.db-shm` | Cambios aún no consolidados |
| Vault | `/etc/hora-admin/secrets/passwords_vault.yml` | Contraseñas cifradas |
| `.env` | `/opt/hora-admin/.env` | `SECRET_KEY` y configuración |
| Logs | `/var/log/hora-admin/` | Diagnóstico |

### 6.2. Copia consistente

Con el servicio parado:

```bash
sudo systemctl stop hora-admin
sudo install -d -m 0700 /var/backups/hora-admin
sudo cp -a /var/lib/hora-admin/hora_admin.db* /var/backups/hora-admin/
sudo cp -a /etc/hora-admin/secrets/passwords_vault.yml /var/backups/hora-admin/
sudo cp -a /opt/hora-admin/.env /var/backups/hora-admin/
sudo systemctl start hora-admin
```

En caliente, usa la API de backup de SQLite (incluye WAL):

```bash
sudo sqlite3 /var/lib/hora-admin/hora_admin.db \
  ".backup '/var/backups/hora-admin/hora_admin-$(date +%F).db'"
```

### 6.3. Restauración

```bash
sudo systemctl stop hora-admin
sudo cp /var/backups/hora-admin/hora_admin-AAAA-MM-DD.db \
      /var/lib/hora-admin/hora_admin.db
sudo rm -f /var/lib/hora-admin/hora_admin.db-wal \
           /var/lib/hora-admin/hora_admin.db-shm
sudo cp /var/backups/hora-admin/passwords_vault.yml \
      /etc/hora-admin/secrets/passwords_vault.yml
sudo chown hora-admin:hora-admin \
      /var/lib/hora-admin/hora_admin.db \
      /etc/hora-admin/secrets/passwords_vault.yml
sudo chmod 600 /etc/hora-admin/secrets/passwords_vault.yml
sudo systemctl start hora-admin
```

Después, verifica login, `/history` y que el vault se descifra con la
contraseña maestra esperada.

---

## 7. Retención y limpieza

`HORA_ADMIN_HISTORY_DAYS` (180) y `HORA_ADMIN_JOB_RETENTION_HOURS` (24) están
definidas en `config.py` pero **no hay job de purga**. La retención es manual.

### 7.1. Borrar lotes antiguos

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
sudo sqlite3 /var/lib/hora-admin/hora_admin.db "PRAGMA wal_checkpoint(TRUNCATE);"
sudo sqlite3 /var/lib/hora-admin/hora_admin.db "VACUUM;"
```

Ejecuta `VACUUM` con el servicio parado o en ventana de baja actividad.

---

## 8. Mantenimiento

### 8.1. Actualizar la aplicación

```bash
# 1. Copia de seguridad (§6)
sudo systemctl stop hora-admin
cd /opt/hora-admin
sudo -u hora-admin git pull            # o copia el nuevo código
sudo -u hora-admin .venv/bin/pip install -r requirements.txt
sudo -u hora-admin .venv/bin/flask --app app init-db
sudo systemctl start hora-admin
```

`init-db` es idempotente y aplica migraciones no destructivas. El esquema
actual es `PRAGMA user_version = 3`.

### 8.2. Rotar la contraseña del administrador

```bash
sudo -u hora-admin .venv/bin/flask --app app init-admin
```

Vuelve a pedir la contraseña y actualiza el hash Werkzeug.

### 8.3. Rotar `SECRET_KEY`

1. Genera una nueva: `openssl rand -hex 32`.
2. Actualiza `HORA_ADMIN_SECRET_KEY` en `.env`.
3. Reinicia el servicio. **Todas las sesiones activas se invalidan.**

### 8.4. Rotar la contraseña maestra del vault

`ansible-vault rekey` no está integrado en la interfaz. Procedimiento manual:

```bash
sudo -u hora-admin ansible-vault rekey \
  /etc/hora-admin/secrets/passwords_vault.yml
```

Guarda la nueva contraseña en el gestor de secretos correspondiente. No se
persiste en la aplicación.

### 8.5. Logs

- `HORA_ADMIN_LOG_TO_FILE=1` escribe en `LOG_DIR/hora-admin.log` con rotación
  (`LOG_MAX_BYTES`, `LOG_BACKUPS`).
- Gunicorn escribe a stdout/stderr; en systemd va a journald.
- Revisa crecimiento y errores periódicamente.

### 8.6. `known_hosts`

Con `HOST_KEY_CHECKING=1`, provisiona las claves antes del primer uso:

```bash
ssh-keyscan -H 192.0.2.10 | sudo -u hora-admin tee -a ~/.ssh/known_hosts
```

Verifica las huellas por un canal de confianza antes de aceptarlas. No
desactives la verificación salvo decisión consciente y documentada.

---

## 9. Seguridad operativa

- No ejecutes la app como `root`; usa un usuario de servicio sin privilegios.
- `install.sh` crea directorios para el usuario que lo invoca, pero el unit de
  systemd documentado usa `User=hora-admin`. Alinea ambos (ver
  [DEPLOYMENT](../DEPLOYMENT.md#servicio-systemd)).
- Mantén `.env` en `0600` y el vault en `0600`.
- No compartas la contraseña maestra por chat/correo; usa un gestor de secretos.
- Revisa `/history` tras cada operación sensible.
- El rate limiting es en memoria y por proceso; con varios workers no es
  global.
- No hay RBAC, MFA, allowlist de redes ni auditoría de eventos independiente.
  Un usuario autenticado puede apuntar a cualquier IP/CIDR permitido por los
  límites globales.

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

- Sin paralelismo, cancelación ni reintento selectivo.
- Sin reconciliación automática de lotes huérfanos.
- Sin rollback automático.
- Sin retención automática.
- Estado de tareas en memoria; SSE depende del proceso.
- `DRY_RUN` invisible en la interfaz y ausente del modelo de datos.
- `TIME` + `keep` puede ser revertido por NTP.
- `restore` activa NTP aunque estuviera desactivado.
- Sin RBAC, MFA, allowlist de red ni auditoría independiente.
- Solo Ubuntu/systemd; IPv6 no probado en el flujo de Ansible.

El análisis detallado y el roadmap están en
[ANALISIS_Y_RECOMENDACIONES](ANALISIS_Y_RECOMENDACIONES.md).
