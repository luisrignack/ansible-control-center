# tests/test_db.py
# Pruebas de integridad de datos: esquema, migraciones, agregados y concurrencia.
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from models import db as db_module
from models.batch import ExecutionBatch
from models.db import SCHEMA_VERSION, get_connection, init_schema
from models.execution import ExecutionRecord
from models.password import PasswordRecord
from models.user import User


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "acc.db")


def _columns(db_path: str, table: str) -> set[str]:
    with get_connection(db_path) as conn:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_init_schema_sets_version_and_wal(db_path):
    init_schema(db_path)

    with get_connection(db_path) as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert version == SCHEMA_VERSION
    assert journal == "wal"
    assert {"users", "passwords", "executions"} <= tables


def test_init_schema_is_idempotent_and_preserves_data(db_path):
    init_schema(db_path)
    created = PasswordRecord.store_password("secreto", "manual", db_path=db_path)

    # Reinicializar varias veces no debe borrar ni duplicar datos.
    init_schema(db_path)
    ExecutionRecord.init_db(db_path)
    User.init_db(db_path)

    assert PasswordRecord.get_password_count(db_path=db_path) == 1
    records = PasswordRecord.get_all_passwords(db_path=db_path)
    assert [r.id for r in records] == [created.id]


def test_migrates_legacy_v0_database(db_path):
    """Un esquema antiguo (sin versión ni columnas nuevas) se migra sin pérdida."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                value TEXT NOT NULL,
                host TEXT NOT NULL,
                result TEXT NOT NULL
            );
            CREATE TABLE execution_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER NOT NULL,
                host_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO executions (timestamp, action, value, host, result)
            VALUES ('2024-01-01T00:00:00+00:00', 'TZ', 'UTC', '10.0.0.1', 'success');
            INSERT INTO execution_summaries
                (execution_id, host_count, success_count, failure_count)
            VALUES (1, 1, 1, 0), (1, 1, 1, 0);
            """)
        conn.commit()
    finally:
        conn.close()

    init_schema(db_path)

    assert {"password_hash", "password_origin"} <= _columns(db_path, "executions")
    with get_connection(db_path) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        summaries = conn.execute(
            "SELECT COUNT(*) FROM execution_summaries WHERE execution_id = 1"
        ).fetchone()[0]
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(execution_summaries)")
        }
        legacy = conn.execute(
            "SELECT password_hash, password_origin FROM executions WHERE id = 1"
        ).fetchone()

    assert summaries == 1
    assert "idx_execution_summaries_execution_id" in indexes
    assert legacy["password_hash"] == ""
    assert legacy["password_origin"] == "manual"


def test_init_schema_repairs_missing_tables(db_path):
    """Una base parcial (p. ej. truncada) se completa sin perder executions."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                value TEXT NOT NULL,
                host TEXT NOT NULL,
                result TEXT NOT NULL,
                password_hash TEXT NOT NULL DEFAULT '',
                password_origin TEXT NOT NULL DEFAULT 'manual'
            );
            INSERT INTO executions
                (timestamp, action, value, host, result)
            VALUES ('2024-01-01T00:00:00+00:00', 'TZ', 'UTC', '10.0.0.9', 'success');
            PRAGMA user_version = 2;
            """)
        conn.commit()
    finally:
        conn.close()

    init_schema(db_path)

    with get_connection(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        preserved = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]

    assert {"users", "passwords", "execution_summaries"} <= tables
    assert preserved == 1


def test_get_stats_aggregates_real_executions(db_path):
    init_schema(db_path)
    assert ExecutionRecord.get_stats(db_path=db_path) == {
        "total": 0,
        "success": 0,
        "failure": 0,
        "other": 0,
        "success_rate": 0.0,
    }

    for result in ("success", "success", "failure", "timeout"):
        ExecutionRecord.record_execution(
            action="TZ",
            value="UTC",
            host="10.0.0.1",
            result=result,
            password_hash="abcd1234",
            password_origin="manual",
            db_path=db_path,
        )

    stats = ExecutionRecord.get_stats(db_path=db_path)
    assert stats["total"] == 4
    assert stats["success"] == 2
    assert stats["failure"] == 1
    assert stats["other"] == 1
    assert stats["success_rate"] == 0.5


def test_history_pagination_is_stable(db_path):
    init_schema(db_path)
    for index in range(5):
        ExecutionRecord.record_execution(
            action="TIME",
            value=f"2024-01-0{index + 1}T00:00:00",
            host=f"10.0.0.{index + 1}",
            result="success",
            password_hash="abcd1234",
            password_origin="manual",
            db_path=db_path,
        )

    page_one = ExecutionRecord.get_all_executions(limit=2, offset=0, db_path=db_path)
    page_two = ExecutionRecord.get_all_executions(limit=2, offset=2, db_path=db_path)

    assert len(page_one) == 2
    assert len(page_two) == 2
    ids = [record.id for record in page_one + page_two]
    assert len(set(ids)) == 4


def test_concurrent_writes_do_not_lose_records(db_path):
    init_schema(db_path)
    total = 24

    def write(index: int) -> int:
        return ExecutionRecord.record_execution(
            action="TZ",
            value="UTC",
            host=f"10.1.0.{index}",
            result="success",
            password_hash="abcd1234",
            password_origin="manual",
            db_path=db_path,
        ).id

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(write, range(total)))

    assert len(set(ids)) == total
    assert ExecutionRecord.get_stats(db_path=db_path)["total"] == total
    assert PasswordRecord.get_password_count(db_path=db_path) == 0


def test_all_models_share_the_same_schema(db_path):
    # Inicializar desde cualquier modelo crea el esquema completo.
    User.init_db(db_path)

    with get_connection(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"users", "passwords", "executions"} <= tables
    assert int(db_module.SCHEMA_VERSION) >= 2


def test_stats_separate_real_and_simulated_batches(db_path):
    init_schema(db_path)

    ExecutionBatch.create_batch(
        batch_id="real-1",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.1",
        status="running",
        dry_run=False,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="real-1",
        host="10.0.0.1",
        result="success",
        db_path=db_path,
    )
    ExecutionBatch.finish_batch("real-1", db_path=db_path)

    ExecutionBatch.create_batch(
        batch_id="sim-1",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.2",
        status="running",
        dry_run=True,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="sim-1",
        host="10.0.0.2",
        result="success",
        db_path=db_path,
    )
    ExecutionBatch.finish_batch("sim-1", db_path=db_path)

    stats = ExecutionBatch.get_stats(db_path=db_path)
    assert stats["batches"] == 2
    assert stats["simulated_batches"] == 1
    assert stats["success"] == 1
    assert stats["simulated"] == 1

    # La vista previa es una inspección real, no una ejecución simulada.
    ExecutionBatch.create_batch(
        batch_id="preview-1",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.3",
        status="preview",
        dry_run=False,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="preview-1",
        host="10.0.0.3",
        result="preview",
        db_path=db_path,
    )
    ExecutionBatch.finish_batch("preview-1", db_path=db_path)

    stats = ExecutionBatch.get_stats(db_path=db_path)
    assert stats["preview"] == 1
    assert stats["simulated"] == 1


def test_preview_failures_do_not_contaminate_real_stats(db_path):
    """Los fallos de una inspección no cuentan como errores de ejecución."""
    init_schema(db_path)

    # Un fallo de vista previa (el host no autentica) es un lote preview.
    ExecutionBatch.create_batch(
        batch_id="preview-error",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.9",
        status="preview",
        dry_run=False,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="preview-error",
        host="10.0.0.9",
        result="error",
        error="Sin conexión",
        db_path=db_path,
    )
    ExecutionBatch.finish_batch("preview-error", status="preview", db_path=db_path)

    stats = ExecutionBatch.get_stats(db_path=db_path)
    assert stats["error"] == 0
    assert stats["failure"] == 0
    assert stats["preview"] == 0

    # Un error de una ejecución real sí se conserva.
    ExecutionBatch.create_batch(
        batch_id="real-error",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.10",
        status="running",
        dry_run=False,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="real-error",
        host="10.0.0.10",
        result="error",
        error="Playbook falló",
        db_path=db_path,
    )
    ExecutionBatch.finish_batch("real-error", db_path=db_path)

    stats = ExecutionBatch.get_stats(db_path=db_path)
    assert stats["error"] == 1


def test_migrates_v3_batches_to_dry_run_column(db_path):
    """Los lotes creados antes de P4.1 quedan como ejecuciones reales."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE execution_batches (
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
            INSERT INTO execution_batches
                (batch_id, created_at, action, value, target)
            VALUES ('legacy', '2026-01-01T00:00:00+00:00', 'TZ', 'UTC', '10.0.0.1');
            """)
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    init_schema(db_path)

    assert "dry_run" in _columns(db_path, "execution_batches")
    with get_connection(db_path) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        legacy = conn.execute(
            "SELECT dry_run FROM execution_batches WHERE batch_id = 'legacy'"
        ).fetchone()

    assert legacy["dry_run"] == 0
