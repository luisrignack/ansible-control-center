# Hardening de SQLite (FASE 4)

Fecha: 2026-11-17
Esquema: **v21** (v20 en la línea base)
Decisión: **se conserva SQLite**. No hay evidencia de que no cubra la carga
actual (un worker, escrituras cortas, lecturas concurrentes en WAL).

## 1. Configuración efectiva

`models/db.py::connect` aplica en cada conexión:

| Ajuste | Valor | Motivo |
|---|---|---|
| `journal_mode` | `WAL` | Lecturas concurrentes durante una escritura |
| `busy_timeout` | 5000 ms | Evita `SQLITE_BUSY` inmediato bajo contención |
| `foreign_keys` | `ON` | Integridad referencial (SQLite la desactiva por defecto) |
| `timeout` (sqlite3) | 10 s | Espera de bloqueo a nivel de driver |
| Permisos | `0600` | Base, `-wal` y `-shm` privados |

Las conexiones se abren por operación con `get_connection()` y se cierran
siempre (`commit`/`rollback` + `close`), no se mantienen conexiones largas.

## 2. Transacciones y escrituras

- Cada operación usa una transacción corta; no hay transacciones que abarquen
  la ejecución de Ansible.
- Los contadores del lote usan `UPDATE ... SET x = x + 1` (atómico).
- La cadena de auditoría usa `BEGIN IMMEDIATE` para serializar el cálculo del
  hash; es la única escritura que necesita exclusividad explícita.
- Los locks de host usan `INSERT OR IGNORE` + `UPDATE` condicionado por
  expiración.
- `PRAGMA foreign_keys=ON` no sustituye a las comprobaciones de negocio
  (propiedad, rol), que se hacen en la capa de aplicación.

### Índices relevantes

`execution_batches(created_at)`, `(status)`, `(owner_user_id, created_at)`,
`execution_hosts(batch_id)`, `(host)`, `(result)`,
`auth_events(timestamp)`, `(username)`, `(event_id)`,
`task_state(updated_at)`, `host_locks(expires_at)`, `schedules(run_at)`,
`approvals(status)`, inventario por grupo. El historial por usuario usa el
índice compuesto de propiedad.

## 3. Migraciones

- `init_schema` lee `PRAGMA user_version`, rechaza bases más nuevas que la app
  y aplica las migraciones en orden.
- Son **idempotentes y no destructivas**: `CREATE ... IF NOT EXISTS`,
  `_ensure_column`, `UPDATE` de atribución solo la primera vez.
- `user_version` se actualiza **al final**; si una migración falla, la versión
  no avanza y el siguiente arranque reintenta.
- La v21 añade la cadena de auditoría, `audit_checkpoints` e
  `execution_batches.interrupted`, con backfill de la cadena histórica.

### Riesgo residual: `executescript` y commit implícito

`sqlite3.Connection.executescript` hace commit de la transacción pendiente
antes de ejecutar el script. Una caída entre sentencias puede dejar un esquema
parcial. Mitigaciones:

1. Todas las sentencias son idempotentes; el reintento completa el esquema.
2. `user_version` solo avanza al final.
3. Antes de actualizar en producción: backup consistente y prueba en copia.

Una migración totalmente transaccional exigiría ejecutar sentencia a sentencia
en lugar de `executescript`; no se reescribe el runner en esta fase porque el
riesgo es bajo y el cambio afecta a 21 migraciones.

## 4. Copias de seguridad

### 4.1 API de backup (nuevo)

`models/db.py::backup_database(source, destination)`:

- Usa `sqlite3.Connection.backup` (instantánea consistente con WAL activo).
- Escribe en un temporal, aplica `0600`, ejecuta `quick_check` y renombra de
  forma atómica.
- Un fallo no deja un fichero definitivo a medias.

CLI:

```bash
flask --app app backup-db --output /backups/acc-$(date +%F).db
```

### 4.2 Verificación

`integrity_check(db_path, quick=True|False)` devuelve
`{"ok": bool, "details": [...]}` y se usa en el backup y en pruebas.

### 4.3 Restauración

1. Detener el servicio (o el contenedor).
2. Conservar la base actual y sus `-wal`/`-shm` (no mezclar ficheros de
   distintas copias).
3. Copiar el backup al path de datos (`ACC_DATA_DIR/acc.db`).
4. `chmod 600`, arrancar y comprobar `flask --app app audit-verify` y
   `PRAGMA quick_check`.
5. Verificar login y que el historial visible es el esperado.

No se debe restaurar solo el `.db` dejando un `-wal` antiguo: SQLite intentaría
aplicarlo. El backup por API ya contiene el estado consolidado.

## 5. Concurrencia

- WAL + `busy_timeout` evita la mayoría de `database is locked` en el
  despliegue de un worker con hilos.
- Las pruebas de `tests/test_paralelismo_2026_11_12.py` cubren escrituras
  concurrentes, locks por host y contadores.
- La nueva cadena de auditoría se prueba con escrituras secuenciales y
  transacción inmediata; no se observaron bloqueos en la suite completa.

## 6. Corrupción controlada y recuperación

`test_integrity_check_detecta_base_corrupta` escribe bytes no SQLite y verifica
que `integrity_check` devuelve `ok=False` con detalle. El healthcheck (`/healthz`)
solo comprueba conectividad (`SELECT 1`), no integridad completa, para no
penalizar cada sondeo; la comprobación profunda es manual o vía CLI/backup.

## 7. Pruebas añadidas

| Prueba | Cobertura |
|---|---|
| `test_backup_db_es_consistente_y_privado` | Backup, permisos 0600, `quick_check`, datos presentes |
| `test_integrity_check_detecta_base_corrupta` | Detección de corrupción |
| `test_lote_huerfano_se_marca_interrumpido` | Nueva columna y reconciliación |
| Suite `test_db.py` / `test_paralelismo_2026_11_12.py` | Migraciones, versiones, concurrencia |

## 8. Riesgos residuales

| Riesgo | Nivel | Mitigación |
|---|---|---|
| Migración interrumpida | LOW | Idempotencia, backup previo, `user_version` al final |
| Crecimiento del WAL | LOW | Checkpoints automáticos de SQLite; `checkpoint_wal()` disponible |
| Backup en caliente con `cp` | LOW | Documentado: usar `backup-db`, no `cp` |
| Corrupción física del disco | MEDIUM | Backups externos periódicos + `quick_check` |
| Bloqueo por transacción larga | LOW | Ninguna transacción abarca Ansible; auditoría es INSERT corto |

## 9. Conclusión

SQLite sigue siendo adecuado para el tamaño y la concurrencia actuales. El
hardening añade una copia consistente verificable, una comprobación de
integridad reutilizable y documenta con precisión los límites de las
migraciones. No se justifica el coste de migrar a PostgreSQL ni de introducir
un servidor de base de datos externo.
