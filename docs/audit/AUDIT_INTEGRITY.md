# Integridad de la auditoría (FASE 3)

Fecha: 2026-11-17
Estado: **COMPLETADA** (con limitaciones de modelo de amenazas documentadas)

## 1. Situación inicial

`auth_events` guardaba `timestamp, username, ip, event, detail` sin ninguna
propiedad de integridad:

- Cualquier `UPDATE`/`DELETE` era indetectable.
- `AuthEvent.record` ignoraba `sqlite3.Error` (`except: pass`), de modo que un
  fallo de auditoría no dejaba rastro ni alerta.
- No había verificación, exportación ni anclaje externo.

## 2. Diseño implementado

### 2.1 Esquema (migración v21)

```sql
ALTER TABLE auth_events ADD COLUMN event_id  TEXT;
ALTER TABLE auth_events ADD COLUMN prev_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE auth_events ADD COLUMN event_hash TEXT NOT NULL DEFAULT '';

CREATE TABLE audit_checkpoints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    upto_event_id  INTEGER NOT NULL,
    last_hash      TEXT NOT NULL,
    signature      TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_auth_events_event_id ON auth_events(event_id);
CREATE INDEX idx_audit_checkpoints_created ON audit_checkpoints(created_at);
```

La migración encadena los eventos históricos una sola vez
(`version < 21`), de modo que la verificación cubre también la auditoría
anterior. Es no destructiva e idempotente.

### 2.2 Canonicalización determinista

`services/audit_chain.py::canonical_event` serializa:

```json
{"detail":"...","event":"login_ok","event_id":"<uuid4>","ip":"...","timestamp":"...","username":"..."}
```

con `sort_keys=True`, separadores `(",", ":")`, `ensure_ascii=False` y
codificación UTF-8 antes de hashear. Así el mismo evento produce siempre el
mismo hash, independientemente del orden de claves o de los acentos.

### 2.3 Cadena

```
event_hash = SHA256(prev_hash + canonical_event)
```

- `prev_hash` del primer evento encadenado es la cadena vacía.
- La escritura se hace en `BEGIN IMMEDIATE`: dos procesos no pueden calcular el
  mismo `prev_hash` y bifurcar la cadena.
- `event_id` es un UUID4 hexadecimal; se rechazan duplicados en la
  verificación.
- Orden canónico: `id` autoincremental de SQLite.

### 2.4 Verificación

`AuthEvent.verify_chain(db_path, secret)` recalcula cada fila y devuelve:

```json
{
  "ok": true,
  "events": 42,
  "hashed": 42,
  "legacy": 0,
  "head": "<sha256>",
  "broken_at": null,
  "reason": "",
  "checkpoint": {"upto_event_id": 40, "verified_signature": true}
}
```

Detecta:

| Manipulación | Detección |
|---|---|
| Editar `detail`, `event`, `username`, `ip`, `timestamp` | El hash recalculado no coincide |
| Borrar un evento intermedio | El `prev_hash` del siguiente no coincide |
| Reordenar eventos | Enlace/hash inconsistente o `event_id` duplicado |
| Insertar un evento sin hash después de la cadena | «evento sin hash después de eventos encadenados» |
| Borrar la cola después de un checkpoint | El checkpoint apunta a un `id` inexistente |
| Cambiar la cabeza sin cambiar el checkpoint | La cabeza no coincide con `last_hash` |
| Falsificar un checkpoint | Firma HMAC inválida (`verified_signature=false`) |

### 2.5 Checkpoints firmados

`AuthEvent.create_checkpoint(secret, note)` firma con HMAC-SHA256:

```
acc-audit-v1|<upto_event_id>|<last_hash>|<created_at>
```

usando la clave de la instancia (`current_app.secret_key`, que proviene de
`ACC_SECRET_KEY` o del fichero 0600). Se guarda en `audit_checkpoints`.

**Operación recomendada:** crear un checkpoint antes de cada purga de
retención y exportarlo fuera del servidor. Si se rota `ACC_SECRET_KEY`, las
firmas antiguas dejan de verificar (igual que las sesiones); hay que conservar
la clave anterior mientras se custodia el histórico o volver a firmar un
checkpoint nuevo aceptando la nueva base de confianza.

## 3. CLI

```bash
flask --app app audit-verify       # exit != 0 si la cadena está rota
flask --app app audit-checkpoint --note "cierre de mes"
flask --app app audit-export --output auditoria.json [--since-id N]
```

`audit-export` incluye `generated_at`, el resultado de la verificación, el
último checkpoint y los eventos (`id, timestamp, username, ip, event, detail,
event_id, prev_hash, event_hash`). El JSON es apto para custodia WORM o para
enviarlo a un SIEM. El fichero se escribe con permisos `0600` porque contiene
usuarios e IPs, y conviene copiarlo fuera del host con la misma restricción.

## 4. Modelo de amenazas y límites

### Protege frente a

- Edición o borrado accidental de filas.
- Manipulación de la base por un usuario con acceso SQL pero sin la clave.
- Borrado de la cola posterior a un checkpoint exportado.
- Silenciar fallos de escritura: ahora se registra un `WARNING` con el evento.

### No protege frente a

- **Administrador con control total** del servidor, la clave y el código: puede
  reescribir la cadena entera y volver a firmar. La inmutabilidad absoluta
  requiere un destino externo de solo escritura (WORM/SIEM) o firma con clave
  fuera del host.
- **Borrado del prefijo por retención** sin checkpoint previo: es
  indistinguible de una manipulación. Mitigación operativa: checkpoint antes de
  purgar y exportación periódica.
- **Denegación de servicio**: un atacante con escritura puede borrar la tabla
  entera; la verificación lo detectará como cadena vacía, no como manipulación
  (no hay eventos que verificar). El checkpoint externo es la referencia.

### Recomendaciones

1. Programar `audit-checkpoint` y `audit-export` (cron/systemd timer) con copia
   fuera del host.
2. Enviar los eventos a un SIEM si existe; el JSON ya tiene un formato estable.
3. Definir retención legal y documentar la relación entre purga y checkpoints.
4. No reutilizar la clave de sesión para nada más si se separa la custodia
   criptográfica; una variable `ACC_AUDIT_KEY` dedicada sería el siguiente paso.

## 5. Pruebas

`tests/test_hardening_2026_11_16.py`:

- `test_audit_chain_valida_y_detecta_manipulacion`
- `test_audit_chain_detecta_borrado_intermedio`
- `test_audit_checkpoint_firmado_y_truncamiento`
- `test_audit_export_incluye_eventos_y_cadena`

Además, la suite existente de autenticación sigue pasando: los eventos se
siguen leyendo con `AuthEvent.recent()` (columnas extra ignoradas por las
plantillas) y el `INSERT` conserva las columnas históricas.

## 6. Compatibilidad

- Bases v20 se migran a v21 al arrancar; no se pierde ningún evento.
- Consultas externas que hagan `SELECT *` recibirán tres columnas más.
- `AuthEvent.record` mantiene firma y semántica best-effort (no lanza), pero
  ahora deja rastro del fallo en el log.
- No se añadió ninguna dependencia: solo `hashlib`, `hmac`, `json` y `uuid`.
