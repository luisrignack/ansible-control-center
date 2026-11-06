# Revisión de código y lógica — 2026-11-05

## Objetivo

Registrar los hallazgos detectados en la revisión del proyecto **Ansible Control
Center / fecha**, con su ubicación, impacto, corrección propuesta y estado de
aplicación. Este documento sirve como lista de comprobación para verificar más
adelante qué recomendaciones se aplicaron.

## Alcance y método

- Revisión **solo lectura** de la versión con último commit
  `f5b261b Corregir hallazgos de seguridad, programaciones y despliegue`.
- Revisión manual de rutas, modelos, servicios, plantillas y despliegue.
- Validación base ejecutando la suite completa con `pytest`.
- No se modificaron archivos durante la fase de análisis.

## Línea base

| Comprobación | Resultado |
|---|---|
| Suite de pruebas inicial | `250 passed, 1 skipped` |
| Advertencia LDAP | `ldap3` usa una API obsoleta de Python |
| Advertencia Flask-Login | `datetime.utcnow()` obsoleto |
| Árbol de trabajo inicial | Limpio |

La suite pasaba, por lo que los hallazgos son casos límite, condiciones de
carrera, problemas de seguridad o incoherencias no cubiertas por las pruebas.

---

## Resumen de prioridad alta

| # | Hallazgo | Estado |
|---|---|---|
| 1 | El programador relanza la misma tarea tras una caída | Aplicado |
| 2 | Condición de carrera al consumir aprobaciones | Aplicado |
| 3 | DRY RUN reporta éxito y credenciales válidas sin contactar al host | Aplicado |
| 4 | Las cuentas desactivadas conservan la sesión activa | Aplicado |
| 5 | XSS en manejadores inline y confirmaciones bloqueadas por CSP | Aplicado |
| 6 | El PDF ignora el orden “fallidos primero” | Aplicado |

---

## Hallazgos de prioridad alta

### 1. El programador puede relanzar la misma tarea varias veces tras una caída

**Ubicación**

- `models/ops.py:534-540` (`Schedule.next_run_from`)
- `services/scheduler.py:104` (`scan_once`)
- `app.py:2214-2225` (cálculo de `next_run` al lanzar)

**Problema**

`next_run_from()` calcula la siguiente ejecución sumando el intervalo a la
fecha programada original, no a la fecha actual:

```python
return (base or _parse_utc(self.run_at)) + timedelta(days=1)
```

Si la aplicación estuvo caída más de un intervalo, la nueva `run_at` sigue en
el pasado. El escaneo ocurre cada 15 s, vuelve a considerarla vencida y lanza
otro lote completo.

**Impacto**

- Cambios de hora duplicados.
- Lotes redundantes y auditoría confusa.
- Consumo innecesario de credenciales y tiempo de ejecución.

**Corrección propuesta**

Calcular desde `max(now, run_at)` o avanzar en bucle hasta quedar en el futuro:

```python
base = max(datetime.now(timezone.utc), _parse_utc(self.run_at))
return base + timedelta(days=1)
```

Además, decidir explícitamente la política de ejecuciones perdidas (“omitir” o
“ejecutar una sola vez”) y, si se prevén varios workers, hacer un *claim*
atómico de la programación antes de lanzarla.

**Verificación**

- Prueba nueva: programación diaria con `run_at` de hace tres días; tras un
  disparo, `next_run` debe quedar en el futuro.
- Prueba existente `test_schedule_scan_once_triggers_and_advances` no detecta el
  fallo porque su `fake_launch` inyecta directamente `next_run=now+1d`.

**Estado:** Aplicado (2026-11-05).

**Implementación:** `Schedule.next_run_from()` usa `max(run_at, now)` antes de
sumar el intervalo. Se añadió `test_schedule_next_run_after_long_downtime_is_in_the_future`.
El *claim* atómico para varios workers queda pendiente y sigue documentado en
el plan de arquitectura.

---

### 2. Condición de carrera en el consumo de aprobaciones

**Ubicación**

- `models/ops.py:690-745` (`ApprovalRequest.consume_if_approved`)
- `models/ops.py:650-687` (`ApprovalRequest.decide`)
- `app.py:2278-2321` (consumo antes de la comprobación de idempotencia)

**Problema**

`consume_if_approved()` primero lee las solicitudes y después actualiza por
`id`, sin comprobar el estado en el `UPDATE`:

```sql
UPDATE approvals SET status = ?, consumed_at = ? WHERE id = ?
```

Dos ejecuciones concurrentes pueden leer la misma aprobación como `approved` y
ambas consumirla. Se rompe la regla de “un solo uso”.

`decide()` tiene el mismo patrón: lee el estado y luego actualiza sin condición.

**Impacto**

- Dos ejecuciones destructivas pueden ampararse en una sola aprobación.
- Una solicitud puede ser aprobada y rechazada a la vez según el orden de las
  peticiones.

**Corrección propuesta**

Consumo atómico y comprobación de filas afectadas:

```sql
UPDATE approvals
SET status = 'consumed', consumed_at = ?
WHERE id = ? AND status = 'approved' AND consumed_at IS NULL
```

```python
if cursor.rowcount != 1:
    continue
```

Aplicar el mismo patrón a `decide()` con `WHERE id = ? AND status = 'pending'`.

Además:

- Exigir que el aprobador sea distinto del solicitante.
- Reservar el `operation_id` idempotente antes de consumir la aprobación, para
  que un doble clic no gaste una aprobación.
- Consumir la aprobación dentro de la misma transacción que crea el lote, si se
  quiere evitar perderla cuando falla el encolado.

**Estado:** Aplicado (2026-11-05).

**Implementación:** `decide()` y `consume_if_approved()` usan `UPDATE`
condicional con `rowcount`; se exige aprobador distinto del solicitante y del
ejecutor. En `app.py` la comprobación de idempotencia se movió antes del
consumo y se añadió `_release_operation_id()` para liberar la reserva cuando no
se llega a lanzar la tarea. Se actualizó
`test_approval_flow_create_approve_consume`.

**Limitación pendiente:** si la cola se llena después de consumir la aprobación,
la autorización se pierde. La transacción conjunta aprobación + creación del
lote sigue recomendada.

---

### 3. El modo DRY RUN reporta éxito y credenciales válidas aunque no contacte al host

**Ubicación**

- `services/password_tester.py:168-175` (`test_ansible_ping`)
- `services/ansible_runner.py:361-380` (`run_ansible_playbook`)
- `services/ansible_runner.py:620-645` (aplicación P4 simulada)
- `app.py:2861-2925` (marcado de éxito y `used_hash`)

**Problema**

En simulación:

- `test_ansible_ping()` devuelve `success=True` sin ejecutar Ansible.
- La aplicación del cambio devuelve éxito con `dry_run=True`.
- El flujo marca el host como `success` y guarda `used_hash` como si la
  contraseña se hubiera validado.

**Impacto**

- El operador puede creer que todas las credenciales y hosts funcionan.
- La auditoría registra hashes de contraseñas no verificadas.
- El panel muestra un lote exitoso que nunca tocó la red.

**Corrección propuesta**

- No simular el ping en una ejecución DRY RUN: el ping es de solo lectura y
  sirve para validar credenciales y alcanzabilidad.
- Mantener la simulación únicamente en la aplicación del cambio.
- Marcar el resultado como simulación en consola, UI y PDF, y no presentar
  “verificación OK” cuando no se verificó nada.
- Opcionalmente, añadir un estado `simulated` independiente de `success` en el
  modelo de lotes.

**Estado:** Aplicado (2026-11-05).

**Implementación:** `test_ansible_ping()` ejecuta el ping real incluso con
DRY RUN; `apply_host()` devuelve `changed=False`, `verified=False` y
`simulated=True`. La consola muestra “SIMULACIÓN: credencial válida; no se
aplicó ningún cambio” y el PDF indica “Simulación: sin cambios aplicados”.
Se añadió `test_ping_runs_even_in_dry_run`.

**Limitación pendiente:** el resultado del host sigue siendo `success` con el
lote marcado `dry_run`; separar un estado `simulated` propio requeriría una
columna/migración adicional.

---

### 4. Las cuentas desactivadas conservan la sesión activa

**Ubicación**

- `app.py:1347-1352` (`load_user`)
- `app.py:3908` (`User.set_active`)

**Problema**

`load_user()` devuelve el usuario por ID sin comprobar `is_active`:

```python
return User.get_by_id(int(user_id), db_path=get_db_path())
```

Al desactivar una cuenta, las sesiones ya abiertas siguen siendo válidas hasta
que el usuario cierre sesión o caduque la cookie.

**Impacto**

- Una cuenta desactivada puede seguir operando.
- La desactivación no funciona como revocación inmediata de acceso.

**Corrección propuesta**

```python
user = User.get_by_id(...)
if user is None or not user.is_active:
    return None
```

Para invalidar también las sesiones al cambiar contraseña o rol, añadir un
`auth_version`/`session_epoch` en `users`, incrementarlo en esas operaciones y
guardarlo en la sesión.

**Estado:** Aplicado (2026-11-05).

**Implementación:** `load_user()` devuelve `None` si la cuenta no existe o está
desactivada. Se añadió `test_deactivated_user_session_is_rejected`.
La invalidación por cambio de contraseña/rol con `auth_version` queda pendiente.

---

### 5. XSS almacenado en `onclick`/`onsubmit` y confirmaciones bloqueadas por CSP

**Ubicación**

- `templates/inventory.html:165`
- `templates/schedules.html:167`
- `app.py:1313-1330` (cabecera CSP)

**Problema**

```html
onclick="return confirm('¿Eliminar el grupo {{ group.name }} y sus hosts?');"
```

Jinja escapa `'` como `&#39;`, pero el navegador decodifica las entidades del
atributo antes de ejecutar JavaScript. Un nombre como
`x');alert(document.cookie);//` se convierte en código ejecutable. Es XSS
almacenado si se desactiva la CSP (`ACC_CSP_ENABLED=0`).

Además, la CSP actual usa `script-src 'self' 'nonce-...'` sin
`'unsafe-inline'`, por lo que los navegadores bloquean los manejadores inline.
Hoy las confirmaciones de borrado no se muestran y los formularios destructivos
se envían directamente.

**Impacto**

- Ejecución de JavaScript almacenado si se desactiva la CSP.
- Pérdida de la confirmación en acciones destructivas con la CSP activa.

**Corrección propuesta**

Eliminar los manejadores inline y usar atributos de datos con un listener
global en el script con nonce de `base.html`:

```html
<button type="submit" data-confirm="¿Eliminar el grupo y sus hosts?">…</button>
```

```js
document.addEventListener('submit', function (event) {
    const message = event.target.dataset.confirm;
    if (message && !window.confirm(message)) {
        event.preventDefault();
    }
});
```

**Estado:** Aplicado (2026-11-05).

**Implementación:** se eliminaron los `onclick`/`onsubmit` de
`inventory.html` y `schedules.html`; la confirmación se hace con
`data-confirm` y un listener delegado en `base.html`. Se añadió
`test_templates_do_not_use_inline_event_handlers` para evitar regresiones.

---

### 6. El PDF ignora el orden “fallidos primero”

**Ubicación**

- `services/pdf_report.py:329-335` (cálculo de `ordered_rows`)
- `services/pdf_report.py:440` (bucle sobre `host_rows`)

**Problema**

Se calcula `ordered_rows`, pero el bucle sigue iterando `host_rows`:

```python
ordered_rows = failed_rows + [...] + ok_rows
...
for index, row in enumerate(host_rows, start=1):
```

El docstring promete “tabla de estaciones (fallidas primero)”, pero el informe
sale en el orden de inserción.

**Impacto**

- Los fallos no aparecen primero, dificultando la lectura del informe.
- El comportamiento no coincide con la documentación.

**Corrección propuesta**

Iterar `ordered_rows` y usar ese mismo tamaño para la paginación.

**Estado:** Aplicado (2026-11-05).

**Implementación:** el bucle del informe usa `ordered_rows`; además, en lotes
DRY RUN la razón de un host correcto es “Simulación: sin cambios aplicados”.

---

## Implementación de prioridad alta (2026-11-05)

| Archivo | Cambio |
|---|---|
| `models/ops.py` | Progresión del programador desde “ahora”; aprobaciones atómicas y validación solicitante/aprobador |
| `app.py` | Idempotencia antes de consumir aprobaciones; liberación de reservas; `load_user` rechaza cuentas inactivas; consola distingue simulación |
| `services/password_tester.py` | El ping se ejecuta realmente en DRY RUN |
| `services/ansible_runner.py` | Resultado simulado sin `changed`/`verified` falsos |
| `services/pdf_report.py` | Orden “fallidos primero” y razón de simulación |
| `templates/base.html` | Listener global de `data-confirm` |
| `templates/inventory.html` | Eliminado `onclick` inline |
| `templates/schedules.html` | Eliminado `onsubmit` inline |
| `tests/test_ops.py` | Pruebas de caída larga, sesión desactivada y aprobaciones |
| `tests/test_ansible_runner.py` | Prueba de ping real en DRY RUN |
| `tests/test_security.py` | Prueba contra handlers inline |
| `tests/test_app.py` | Prueba de historial aislada de la red real |

**Resultado de la suite tras los cambios:** `254 passed, 1 skipped`.

---

## Hallazgos de prioridad media

### 7. Los fallos de vista previa contaminan las estadísticas reales

**Ubicación:** `app.py:2688`, `app.py:3246`, `models/batch.py:380-402`.

Cuando falla una inspección de vista previa se registra `result="error"`. El
lote de vista previa se crea con `dry_run=0`, así que `get_stats()` lo cuenta
como ejecución real. **Corrección:** filtrar por estado/modo del lote o usar un
resultado específico `preview_error`.

**Estado:** Pendiente.

### 8. Una cola llena desactiva permanentemente la programación

**Ubicación:** `app.py:2203-2210`, `models/ops.py:487-532`.

`JobQueueFull` se registra como error y `mark_run()` desactiva la programación
cuando hay error. Un problema transitorio de capacidad la apaga para siempre.
**Corrección:** distinguir errores permanentes de transitorios y reintentar con
backoff.

**Estado:** Pendiente.

### 9. El informe de una tarea puede mostrar el lote de otra

**Ubicación:** `app.py:1684-1689`.

Si no hay lote para la tarea, se usa la última ejecución global y se le pasa el
nombre de la tarea solicitada. **Corrección:** no hacer *fallback* silencioso o
etiquetar el informe como “última corrida general” con el `task_id` real.

**Estado:** Pendiente.

### 10. Doble consulta y condición de carrera al activar/desactivar usuarios

**Ubicación:** `app.py:3884-3888`.

Se llama dos veces a `User.get_by_username()`. Entre ambas el usuario podría
desaparecer y `target` quedaría `None`. **Corrección:** consultar una sola vez y
comprobar `None`.

**Estado:** Pendiente.

### 11. Validación de certificado WinRM desactivada de forma fija

**Ubicación:** `services/password_tester.py:192`,
`services/ansible_runner.py:149`.

Se fuerza `ansible_winrm_server_cert_validation: ignore`. **Corrección:** hacerlo
configurable con valor seguro por defecto y permitir `ignore` solo de forma
explícita.

**Estado:** Pendiente.

### 12. `known_hosts` puede perder claves con lotes concurrentes

**Ubicación:** `services/ssh_host_keys.py:206-260`.

Lectura, modificación y reemplazo atómicos, pero sin bloqueo entre procesos.
**Corrección:** usar `fcntl.flock()` durante todo el ciclo.

**Estado:** Pendiente.

### 13. Salida de Ansible acumulada sin límite en memoria

**Ubicación:** `services/ansible_runner.py:180-250`.

`_run_streaming()` guarda todas las líneas y devuelve `"".join(lines)`.
**Corrección:** conservar una cola acotada o volcar la salida a un archivo
temporal.

**Estado:** Pendiente.

### 14. La reserva de idempotencia es solo en memoria

**Ubicación:** `app.py:1202-1231`.

`_claim_operation_id()` usa un diccionario en memoria. Se pierde al reiniciar y
no es válido con varios workers. **Corrección:** persistir las operaciones en
SQLite o documentar el límite de un solo worker.

**Estado:** Pendiente.

---

## Hallazgos menores y limpieza

- **Anotación incorrecta:** `_try_ldap_login()` está declarada `-> None`
  (`app.py:1428-1430`), pero devuelve la respuesta de `_finish_session_login()`.
- **Código muerto / API heredada:** `test_passwords_single()`
  (`services/password_tester.py:30`) no se usa y no respeta
  `MAX_PASSWORD_ATTEMPTS`; `run_playbook_simple()`
  (`services/ansible_runner.py:750`), `ExecutionSummary`
  (`models/execution.py:222`), `active_tasks()`/`get_task_or_default()`
  (`services/task_catalog.py:155-161`) y `resolve_hosts()`
  (`services/password_tester.py:284`) tampoco tienen uso claro.
- **`Schedule.create`:** `row = conn_row = None` en `models/ops.py:461` es código
  muerto.
- **`InventoryHost.add`:** importa `ipaddress` dos veces (`_ip` y `_ipaddr`,
  `models/ops.py:274-286`) y devuelve `pinned` sin normalizar.
- **Metadatos del catálogo:** varias tareas `planned` declaran
  `"actions": ["TZ", "TIME"]` (`services/task_catalog.py:69,91,113`), aunque el
  motor actual solo expone `TZ` en la tarea activa.
- **Docker:** `docker-compose.yml:27` define `ACC_ADMIN_USERNAME`, pero
  `config.py:318` lee `ACC_USERNAME`. El entrypoint contempla ambos para la
  creación inicial, pero `flask init-admin` usa el segundo.
- **Cobertura de pruebas:** faltan pruebas para la progresión de
  `next_run_from()` con caídas largas, consumo concurrente de aprobaciones,
  revocación de sesión al desactivar usuarios y CSP con formularios de borrado.

---

## Plan de mejora recomendado

### Inmediato (corrección funcional y de seguridad)

1. Corregir la progresión del programador.
2. Hacer atómico el consumo de aprobaciones y revisar la regla
   solicitante/aprobador/ejecutor.
3. Invalidar sesiones de usuarios desactivados.
4. Separar claramente “simulado” de “éxito real” en DRY RUN.
5. Sustituir los `onclick`/`onsubmit` inline por `data-confirm`.
6. Corregir el orden de filas del PDF.

### Siguiente iteración

7. Separar errores de vista previa de las estadísticas reales.
8. No desactivar programaciones por errores transitorios de cola.
9. Eliminar el *fallback* engañoso del informe por tarea.
10. Hacer configurable la validación TLS de WinRM.
11. Bloquear `known_hosts` durante la actualización.
12. Acotar la salida de Ansible y persistir la idempotencia si se prevé escalar.

### Arquitectura

La configuración actual usa **un solo worker** de Gunicorn porque el estado de
las tareas, la cola, el rate limiter, las operaciones idempotentes y el
programador viven en memoria. Es una decisión coherente para el tamaño actual,
pero conviene documentarla como límite: para alta disponibilidad o varios
workers habrá que mover ese estado a SQLite/Redis y elegir un líder para el
programador.

---

## Cómo verificar este documento

1. Ejecutar `.venv/bin/python -m pytest -q` y comprobar que no hay regresiones.
   Resultado esperado en esta revisión: `254 passed, 1 skipped`.
2. Revisar la tabla de estado al inicio y los campos **Estado** de cada
   hallazgo.
3. Para los hallazgos marcados como aplicados, revisar el código en las rutas
   indicadas y las pruebas añadidas.
4. Ejecutar las pruebas específicas:
   - `pytest -q tests/test_ops.py`
   - `pytest -q tests/test_p4.py`
   - `pytest -q tests/test_security.py`
   - `pytest -q tests/test_ansible_runner.py`
5. Comprobar manualmente:
   - Desactivar un usuario con sesión abierta y verificar que la siguiente
     petición lo expulsa.
   - En modo DRY RUN, usar una contraseña incorrecta y verificar que el host
     falla en lugar de aparecer como éxito.
   - Borrar un grupo/programación y comprobar que aparece la confirmación.
   - Generar un PDF con hosts fallidos y exitosos y verificar el orden.

---

## Registro de cambios de esta revisión

| Fecha | Cambio | Autor |
|---|---|---|
| 2026-11-05 | Documento creado con los hallazgos de la revisión | Revisión automatizada |
| 2026-11-05 | Aplicados los seis hallazgos de prioridad alta y añadidas pruebas de regresión | Revisión automatizada |
