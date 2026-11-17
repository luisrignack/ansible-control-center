# models/db.py
# Acceso SQLite centralizado: conexiones, WAL, busy_timeout y migraciones.
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# Versión de esquema actual. Se guarda en ``PRAGMA user_version``.
SCHEMA_VERSION = 21


def _secure_sqlite_files(path: str) -> None:
    """Restringe a ``0600`` la base y sus ficheros auxiliares WAL/SHM.

    SQLite crea los ficheros con el umask del proceso (habitualmente ``0644``),
    lo que expone el historial y los hashes a otros usuarios del sistema.
    """
    if path == ":memory:" or path.startswith("file:"):
        return
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            os.chmod(candidate, 0o600)
        except OSError:
            # El fichero auxiliar puede no existir todavía; no es un error.
            pass


def connect(db_path: str) -> sqlite3.Connection:
    """Abre una conexión con ajustes seguros para concurrencia."""
    path = str(db_path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    _secure_sqlite_files(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # WAL permite lecturas mientras hay una escritura en curso.
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # ``:memory:`` u otros motores no soportan WAL; no es fatal.
        pass
    _secure_sqlite_files(path)
    return conn


@contextmanager
def get_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """Context manager que hace commit/rollback y cierra siempre."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _default_db_path() -> str:
    """Ruta de SQLite resuelta en tiempo de ejecución (producción o instance/)."""
    from config import resolve_db_path

    return str(resolve_db_path())


def integrity_check(db_path: str, *, quick: bool = False) -> dict:
    """Comprueba la integridad física del fichero SQLite.

    ``quick=True`` ejecuta ``PRAGMA quick_check`` (más rápido y suficiente para
    un chequeo rutinario); por defecto se usa ``integrity_check`` completo.
    Devuelve un diccionario estable para healthchecks y scripts.
    """
    pragma = "PRAGMA quick_check" if quick else "PRAGMA integrity_check"
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(pragma).fetchall()
    except (sqlite3.Error, OSError) as exc:
        return {"ok": False, "details": [str(exc)]}
    details = [str(row[0]) for row in rows]
    return {"ok": details == ["ok"], "details": details}


def backup_database(db_path: str, destination: str | Path) -> Path:
    """Copia consistente de SQLite con la API de backup en línea.

    No se copia el fichero a mano: con WAL activo eso puede producir una copia
    incoherente. ``Connection.backup`` toma una instantánea consistente aunque
    haya escrituras en curso. La copia se escribe en un temporal 0600 y se
    renombra de forma atómica, de modo que un fallo no deja un backup a medias
    con el nombre definitivo.
    """
    source_path = str(db_path)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        source = sqlite3.connect(source_path, timeout=10)
        try:
            dest_conn = sqlite3.connect(str(tmp))
            try:
                source.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            source.close()
        os.chmod(tmp, 0o600)
        check = integrity_check(str(tmp), quick=True)
        if not check["ok"]:
            raise sqlite3.DatabaseError(
                f"La copia de seguridad no pasó quick_check: {check['details']}"
            )
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:  # pragma: no cover - limpieza best-effort
            pass
    return target


def checkpoint_wal(db_path: str) -> dict:
    """Fuerza un checkpoint WAL y devuelve el resultado de ``PRAGMA``.

    Antes de copiar el fichero a mano (por ejemplo, con ``cp``) conviene
    ejecutar esto para volcar el WAL al fichero principal. Aun así se
    recomienda ``backup_database``, que no depende de este paso.
    """
    with get_connection(db_path) as conn:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is None:  # pragma: no cover - SQLite siempre devuelve una fila
        return {"busy": 0, "log": 0, "checkpointed": 0}
    return {
        "busy": int(row[0]),
        "log": int(row[1]),
        "checkpointed": int(row[2]),
    }



def get_setting(
    key: str, default: str | None = None, db_path: str | None = None
) -> str | None:
    """Lee un parámetro persistente de ``app_settings``.

    Devuelve ``default`` si la clave no existe o si la tabla todavía no se ha
    creado (por ejemplo, durante una actualización a medio aplicar).
    """
    try:
        with get_connection(db_path or _default_db_path()) as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
    except sqlite3.OperationalError:
        return default
    if row is None:
        return default
    return row["value"]


def set_setting(key: str, value: str, db_path: str | None = None) -> None:
    """Crea o actualiza un parámetro persistente de ``app_settings``."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with get_connection(db_path or _default_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, str(value), now),
        )


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> None:
    """Añade una columna si no existe (migración no destructiva)."""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS passwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            password_hash_short CHAR(8) NOT NULL,
            password_hash_full TEXT NOT NULL,
            creation_date TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'manual'
        );

        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            value TEXT NOT NULL,
            host TEXT NOT NULL,
            result TEXT NOT NULL,
            password_hash TEXT NOT NULL DEFAULT '',
            password_origin TEXT NOT NULL DEFAULT 'manual'
        );

        -- Tabla heredada: se conserva para compatibilidad, pero el historial
        -- se agrega bajo demanda con ExecutionRecord.get_stats().
        CREATE TABLE IF NOT EXISTS execution_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER NOT NULL UNIQUE,
            host_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (execution_id) REFERENCES executions (id)
        );

        CREATE INDEX IF NOT EXISTS idx_executions_timestamp
            ON executions (timestamp);
        CREATE INDEX IF NOT EXISTS idx_executions_host
            ON executions (host);
        CREATE INDEX IF NOT EXISTS idx_executions_result
            ON executions (result);
        CREATE INDEX IF NOT EXISTS idx_passwords_creation_date
            ON passwords (creation_date);
        """)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    # Bases de datos creadas antes de P1 pueden carecer de estas columnas.
    _ensure_column(
        conn,
        "executions",
        "password_hash",
        "password_hash TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        "executions",
        "password_origin",
        "password_origin TEXT NOT NULL DEFAULT 'manual'",
    )

    # Normaliza resúmenes duplicados y garantiza unicidad por ejecución.
    conn.execute("""
        DELETE FROM execution_summaries
        WHERE id NOT IN (
            SELECT MIN(id) FROM execution_summaries GROUP BY execution_id
        )
        """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_execution_summaries_execution_id "
        "ON execution_summaries (execution_id)"
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Añade el modelo de lotes y resultados por host (P4)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS execution_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            operator TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            value TEXT NOT NULL,
            target TEXT NOT NULL,
            time_mode TEXT NOT NULL DEFAULT 'utc',
            ntp_policy TEXT NOT NULL DEFAULT 'keep',
            status TEXT NOT NULL DEFAULT 'running',
            total_hosts INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            preview_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS execution_hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            host TEXT NOT NULL,
            result TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            before_state TEXT NOT NULL DEFAULT '',
            after_state TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL DEFAULT '',
            password_origin TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (batch_id)
                REFERENCES execution_batches (batch_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_batches_created_at
            ON execution_batches (created_at);
        CREATE INDEX IF NOT EXISTS idx_batches_status
            ON execution_batches (status);
        CREATE INDEX IF NOT EXISTS idx_hosts_batch_id
            ON execution_hosts (batch_id);
        CREATE INDEX IF NOT EXISTS idx_hosts_host
            ON execution_hosts (host);
        CREATE INDEX IF NOT EXISTS idx_hosts_result
            ON execution_hosts (result);
        """)


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """Marca los lotes ejecutados en modo simulación (P4.1)."""
    _ensure_column(
        conn,
        "execution_batches",
        "dry_run",
        "dry_run INTEGER NOT NULL DEFAULT 0",
    )


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """Persiste el estado de las tareas SSE para sobrevivir a reinicios (P4.1)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_state (
            task_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_task_state_updated_at
            ON task_state (updated_at);
        """)


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """Añade roles, bloqueo/auditoría de login y parámetros de prellenado."""
    _ensure_column(conn, "users", "role", "role TEXT NOT NULL DEFAULT 'admin'")
    _ensure_column(
        conn,
        "users",
        "failed_attempts",
        "failed_attempts INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(conn, "users", "locked_until", "locked_until TEXT")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS auth_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            ip TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_auth_events_timestamp
            ON auth_events (timestamp);
        CREATE INDEX IF NOT EXISTS idx_auth_events_username
            ON auth_events (username);
        """)

    for column, ddl in (
        ("admin_user", "admin_user TEXT NOT NULL DEFAULT ''"),
        ("password_origin", "password_origin TEXT NOT NULL DEFAULT ''"),
        ("ntp_servers", "ntp_servers TEXT NOT NULL DEFAULT ''"),
        ("sync_rtc", "sync_rtc INTEGER NOT NULL DEFAULT 0"),
        ("force", "force INTEGER NOT NULL DEFAULT 0"),
        ("local_fold", "local_fold INTEGER"),
        ("time_zone", "time_zone TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(conn, "execution_batches", column, ddl)


def _migrate_v7(conn: sqlite3.Connection) -> None:
    """Parámetros de ejecución persistentes (por ejemplo, el modo simulación)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)


def _migrate_v8(conn: sqlite3.Connection) -> None:
    """Inventario de grupos/hosts, programaciones y aprobaciones.

    Todas con ``CREATE TABLE IF NOT EXISTS``: la migración es idempotente y
    nunca transforma datos, así que puede ejecutarse siempre.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS inventory_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            os_flavor TEXT NOT NULL DEFAULT 'linux',
            connection TEXT NOT NULL DEFAULT 'ssh',
            ssh_key_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inventory_hosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL
                REFERENCES inventory_groups(id) ON DELETE CASCADE,
            address TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (group_id, address)
        );

        CREATE INDEX IF NOT EXISTS idx_inventory_hosts_group
            ON inventory_hosts (group_id);

        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL DEFAULT 'hora',
            run_at TEXT NOT NULL,
            recurrence TEXT NOT NULL DEFAULT 'once',
            enabled INTEGER NOT NULL DEFAULT 1,
            params TEXT NOT NULL DEFAULT '{}',
            last_run TEXT,
            last_batch TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_schedules_due
            ON schedules (enabled, run_at);

        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            requested_by TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            approver TEXT,
            decided_at TEXT,
            note TEXT NOT NULL DEFAULT '',
            consumed_at TEXT,
            payload TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_approvals_status
            ON approvals (status, task_id);
        """)


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """Vincula cada lote con la tarea del catálogo que lo lanzó (informes)."""
    _ensure_column(
        conn,
        "execution_batches",
        "task_id",
        "task_id TEXT NOT NULL DEFAULT ''",
    )


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """Marcar el origen de cada cuenta: local o autenticada por LDAPS.

    Las cuentas creadas por el primer login LDAPS no guardan contraseña
    local (``password_hash`` vacío), solo el rol asignado por el admin.
    """
    _ensure_column(
        conn,
        "users",
        "auth_source",
        "auth_source TEXT NOT NULL DEFAULT 'local'",
    )


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """Reservas de idempotencia persistentes entre reinicios.

    Antes vivían solo en memoria: un reinicio permitía reenviar la misma
    operación y varios workers no compartían la reserva.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS operation_ids (
            operation_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_operation_ids_created
            ON operation_ids (created_at);
        """)


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """Reserva atómica de programaciones entre varios procesos.

    Dos workers podían leer la misma fila vencida y lanzar el lote dos veces.
    ``claimed_at``/``claimed_by`` permiten que solo uno gane el *claim*; la
    reserva caduca para que un proceso caído no bloquee la programación.
    """
    _ensure_column(conn, "schedules", "claimed_at", "claimed_at TEXT")
    _ensure_column(conn, "schedules", "claimed_by", "claimed_by TEXT")


def _migrate_v13(conn: sqlite3.Connection) -> None:
    """Índice para retención y métricas de duración por host.

    ``execution_hosts`` crece con cada ejecución y se consulta por lote y por
    fecha (retención, p50/p95). El índice por ``created_at`` evita escaneos
    completos cuando la tabla acumula meses de historial.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hosts_created_at "
        "ON execution_hosts (created_at)"
    )


def _migrate_v14(conn: sqlite3.Connection) -> None:
    """Criticidad y ventana de mantenimiento en el inventario.

    ``criticality`` es por grupo; el host puede sobrescribirla con una cadena
    vacía = heredar. La ventana es una sola por grupo, en UTC, con días ISO
    (1=lunes … 7=domingo). Los grupos existentes quedan en ``medium`` sin
    ventana: no cambian el comportamiento hasta que un administrador los
    clasifique.
    """
    _ensure_column(
        conn,
        "inventory_groups",
        "criticality",
        "criticality TEXT NOT NULL DEFAULT 'medium'",
    )
    _ensure_column(
        conn, "inventory_groups", "window_days", "window_days TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        conn,
        "inventory_groups",
        "window_start",
        "window_start TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        "inventory_groups",
        "window_end",
        "window_end TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        "inventory_groups",
        "window_note",
        "window_note TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        "inventory_hosts",
        "criticality",
        "criticality TEXT NOT NULL DEFAULT ''",
    )


def _migrate_v15(conn: sqlite3.Connection) -> None:
    """Resultado ``attention`` y umbral de espacio de la tarjeta APT.

    ``attention_count`` cuenta estaciones que no se actualizaron por una
    decisión de política (distribución bloqueada o espacio insuficiente), sin
    confundirlas con fallos técnicos ni con omisiones por cancelación.
    ``min_free_gb`` conserva el umbral usado en el lote para que el informe
    pueda explicar la decisión.
    """
    _ensure_column(
        conn,
        "execution_batches",
        "attention_count",
        "attention_count INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        "execution_batches",
        "min_free_gb",
        "min_free_gb REAL",
    )


def _migrate_v16(conn: sqlite3.Connection) -> None:
    """Payload de ejecución de las solicitudes de doble aprobación.

    Guarda el formulario original **sin secretos** para que, una vez aprobada
    la solicitud, el solicitante pueda ejecutarla sin volver a rellenarlo.
    """
    _ensure_column(
        conn,
        "approvals",
        "payload",
        "payload TEXT NOT NULL DEFAULT '{}'",
    )


def _migrate_v17(conn: sqlite3.Connection) -> None:
    """Reserva por host para impedir playbooks simultáneos sobre el mismo equipo.

    Dos operadores podían lanzar lotes distintos contra la misma IP: ambos
    autenticaban y aplicaban el cambio, el último ganaba y los dos registraban
    éxito. La reserva se toma antes de tocar cada host, se renueva durante los
    intentos de credencial y caduca sola si el proceso muere, para que un
    despliegue caído no bloquee el host para siempre.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS host_locks (
            host TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '',
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_host_locks_expires_at
            ON host_locks (expires_at);
        """)


def _migrate_v18(conn: sqlite3.Connection) -> None:
    """Posición del host dentro del lote para conservar el orden de entrada.

    Con el procesamiento en paralelo, el orden de inserción depende de qué
    host termina antes. La posición permite que el detalle del lote siga
    mostrando los hosts en el orden en que el operador los envió.
    """
    _ensure_column(
        conn,
        "execution_hosts",
        "position",
        "position INTEGER NOT NULL DEFAULT 0",
    )
    # Los lotes anteriores conservan el orden histórico (id de inserción).
    conn.execute("UPDATE execution_hosts SET position = id WHERE position = 0")


def _migrate_v19(conn: sqlite3.Connection) -> None:
    """Propiedad por usuario en programaciones e inventario.

    Las filas existentes quedan con ``owner_user_id = 0``: se consideran
    **compartidas** y siguen visibles para todos, de modo que la actualización
    no oculta ni inutiliza datos previos. Las nuevas filas guardan el ``id``
    del usuario que las crea. Las contraseñas del vault siguen siendo un
    recurso compartido y no cambian de esquema.
    """
    _ensure_column(
        conn,
        "schedules",
        "owner_user_id",
        "owner_user_id INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        "inventory_groups",
        "owner_user_id",
        "owner_user_id INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_schedules_owner "
        "ON schedules (owner_user_id, run_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inventory_groups_owner "
        "ON inventory_groups (owner_user_id, name)"
    )


def _migrate_v20(conn: sqlite3.Connection, backfill: bool = True) -> None:
    """Propiedad por usuario del historial de ejecuciones.

    Cada lote guarda el ``id`` del usuario que lo lanzó para que el historial
    sea privado. Las filas antiguas se atribuyen por su columna ``operator``
    (el usuario que las lanzó) y, en el caso de las programaciones, por el
    dueño de la programación homónima. Lo que no se pueda atribuir queda en
    ``0``, que solo ven los administradores en la vista global.

    ``backfill`` solo es cierto la primera vez: en arranques posteriores no
    hay filas antiguas que atribuir, así que se evita recorrer la tabla.
    """
    _ensure_column(
        conn,
        "execution_batches",
        "owner_user_id",
        "owner_user_id INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_batches_owner "
        "ON execution_batches (owner_user_id, created_at)"
    )
    if not backfill:
        return
    # Lotes lanzados a mano: el operador es el nombre de usuario.
    conn.execute("""
        UPDATE execution_batches
        SET owner_user_id = (
            SELECT users.id FROM users
            WHERE users.username = execution_batches.operator
        )
        WHERE owner_user_id = 0
          AND EXISTS (
              SELECT 1 FROM users
              WHERE users.username = execution_batches.operator
          )
        """)
    # Lotes desatendidos: ``operator`` es ``agenda:<nombre>``; se hereda el
    # dueño de la programación con ese nombre (la más reciente).
    conn.execute("""
        UPDATE execution_batches
        SET owner_user_id = COALESCE((
            SELECT schedules.owner_user_id FROM schedules
            WHERE 'agenda:' || schedules.name = execution_batches.operator
            ORDER BY schedules.id DESC
            LIMIT 1
        ), 0)
        WHERE owner_user_id = 0
          AND operator LIKE 'agenda:%'
        """)


def _migrate_v21(conn: sqlite3.Connection, backfill: bool = True) -> None:
    """Integridad de auditoría y estado «interrumpido».

    - ``auth_events`` gana ``event_id``, ``prev_hash`` y ``event_hash`` para
      formar una cadena SHA-256 verificable (ver ``services/audit_chain.py``).
    - ``audit_checkpoints`` guarda anclas firmadas con HMAC que permiten
      detectar el borrado de la cola de eventos y exportar el estado a un
      sistema externo.
    - ``execution_batches.interrupted`` distingue un lote que quedó a medias
      por un reinicio (resultado remoto desconocido) de un fallo real.

    La migración es no destructiva: añade columnas y tablas. ``backfill`` solo
    es cierto la primera vez y encadena los eventos históricos para que la
    verificación cubra también la auditoría anterior a la v21.
    """
    for column, ddl in (
        ("event_id", "event_id TEXT"),
        ("prev_hash", "prev_hash TEXT NOT NULL DEFAULT ''"),
        ("event_hash", "event_hash TEXT NOT NULL DEFAULT ''"),
    ):
        _ensure_column(conn, "auth_events", column, ddl)
    _ensure_column(
        conn,
        "execution_batches",
        "interrupted",
        "interrupted INTEGER NOT NULL DEFAULT 0",
    )
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            upto_event_id INTEGER NOT NULL,
            last_hash TEXT NOT NULL,
            signature TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_auth_events_event_id
            ON auth_events (event_id);
        CREATE INDEX IF NOT EXISTS idx_audit_checkpoints_created
            ON audit_checkpoints (created_at);
        """)
    if not backfill:
        return

    from services.audit_chain import canonical_event, compute_hash

    previous = ""
    rows = conn.execute(
        """
        SELECT id, timestamp, username, ip, event, detail, event_id, event_hash
        FROM auth_events
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        event_id = row["event_id"] or f"legacy-{row['id']}"
        canonical = canonical_event(
            event_id=event_id,
            timestamp=row["timestamp"],
            username=row["username"],
            ip=row["ip"],
            event=row["event"],
            detail=row["detail"],
        )
        current = compute_hash(previous, canonical)
        if row["event_hash"] != current or row["event_id"] != event_id:
            conn.execute(
                """
                UPDATE auth_events
                SET event_id = ?, prev_hash = ?, event_hash = ?
                WHERE id = ?
                """,
                (event_id, previous, current, row["id"]),
            )
        previous = current


def init_schema(db_path: str) -> None:
    """Crea o migra el esquema de forma idempotente y no destructiva.

    ``_migrate_v1``, ``_migrate_v3``, ``_migrate_v4`` y ``_migrate_v5`` se
    ejecutan siempre porque usan ``CREATE ... IF NOT EXISTS`` o
    ``_ensure_column``: así se reparan bases de datos parciales o creadas por
    versiones antiguas. ``_migrate_v20`` también repara la columna, pero solo
    atribuye el historial antiguo la primera vez. Las migraciones que
    transforman datos se aplican si la versión registrada en
    ``PRAGMA user_version`` es anterior.
    """
    with get_connection(db_path) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"La base de datos {db_path} usa el esquema v{version}, "
                f"más nuevo que el soportado por esta aplicación "
                f"(v{SCHEMA_VERSION}). Actualiza Ansible Control Center o restaura una "
                "copia de seguridad compatible."
            )
        _migrate_v1(conn)
        if version < 2:
            _migrate_v2(conn)
        _migrate_v3(conn)
        _migrate_v4(conn)
        _migrate_v5(conn)
        _migrate_v6(conn)
        _migrate_v7(conn)
        _migrate_v8(conn)
        _migrate_v9(conn)
        _migrate_v10(conn)
        _migrate_v11(conn)
        _migrate_v12(conn)
        _migrate_v13(conn)
        _migrate_v14(conn)
        _migrate_v15(conn)
        _migrate_v16(conn)
        _migrate_v17(conn)
        _migrate_v18(conn)
        _migrate_v19(conn)
        _migrate_v20(conn, backfill=version < 20)
        _migrate_v21(conn, backfill=version < 21)
        # No se rebaja la versión de una base creada por una versión más nueva.
        if version < SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
