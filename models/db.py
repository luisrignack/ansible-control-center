# models/db.py
# Acceso SQLite centralizado: conexiones, WAL, busy_timeout y migraciones.
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Versión de esquema actual. Se guarda en ``PRAGMA user_version``.
SCHEMA_VERSION = 3


def connect(db_path: str) -> sqlite3.Connection:
    """Abre una conexión con ajustes seguros para concurrencia."""
    path = str(db_path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # WAL permite lecturas mientras hay una escritura en curso.
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # ``:memory:`` u otros motores no soportan WAL; no es fatal.
        pass
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


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> None:
    """Añade una columna si no existe (migración no destructiva)."""
    columns = {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
        """
    )


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
    conn.execute(
        """
        DELETE FROM execution_summaries
        WHERE id NOT IN (
            SELECT MIN(id) FROM execution_summaries GROUP BY execution_id
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_execution_summaries_execution_id "
        "ON execution_summaries (execution_id)"
    )


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """Añade el modelo de lotes y resultados por host (P4)."""
    conn.executescript(
        """
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
        """
    )


def init_schema(db_path: str) -> None:
    """Crea o migra el esquema de forma idempotente y no destructiva.

    ``_migrate_v1`` y ``_migrate_v3`` se ejecutan siempre porque usan
    ``CREATE ... IF NOT EXISTS``: así se reparan bases de datos parciales o
    creadas por versiones antiguas. Las migraciones que transforman datos solo
    se aplican si la versión registrada en ``PRAGMA user_version`` es anterior.
    """
    with get_connection(db_path) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        _migrate_v1(conn)
        if version < 2:
            _migrate_v2(conn)
        _migrate_v3(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
