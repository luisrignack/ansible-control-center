"""Pruebas de instrumentación y lecturas compartidas de tareas (2026-11-07).

Cubren el primer paso de escalabilidad (SSE/estado desde SQLite), las métricas
de latencia por ruta y las estadísticas p50/p95 por lote.
"""

from __future__ import annotations

import json

import pytest

import app as app_module
from acc import runtime
from models.batch import ExecutionBatch
from models.db import get_connection, init_schema
from models.user import User
from services.metrics import RequestMetrics, percentile, summarize


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "rendimiento.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: path)
    app_module.init_db()
    return path


@pytest.fixture()
def client(db_path):
    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def _csrf_token(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _post(client, url, data=None):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload)


def _login(client, password="admin-password-123"):
    return _post(client, "/login", {"username": "admin", "password": password})


# ---------------------------------------------------------------------------
# Métricas puras
# ---------------------------------------------------------------------------
def test_percentile_nearest_rank():
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 50) == 30
    assert percentile(values, 95) == 50
    assert percentile([], 95) == 0
    assert percentile([7], 0) == 7


def test_request_metrics_snapshot_and_reset():
    metrics = RequestMetrics(window=10)
    for value in (100, 200, 300, 400):
        metrics.record("index", value)
    metrics.record("execute", 900, status=500)

    snapshot = metrics.snapshot()

    assert snapshot["index"]["count"] == 4
    assert snapshot["index"]["p50_ms"] == 200
    assert snapshot["index"]["p95_ms"] == 400
    assert snapshot["execute"]["errors"] == 1

    summary = summarize(snapshot)
    assert summary["endpoints"] == 2
    assert summary["requests"] == 5
    assert summary["errors"] == 1

    metrics.reset()
    assert metrics.snapshot() == {}


def test_request_metrics_window_is_bounded():
    metrics = RequestMetrics(window=10)
    for value in range(100):
        metrics.record("index", value)

    snapshot = metrics.snapshot()["index"]
    assert snapshot["count"] == 100
    assert snapshot["samples"] == 10
    assert snapshot["max_ms"] == 99


# ---------------------------------------------------------------------------
# Estado de tareas compartido entre procesos
# ---------------------------------------------------------------------------
def test_get_task_refreshes_newer_snapshot_from_sqlite(db_path):
    task_id = app_module.create_task(owner="alice")
    try:
        stale = app_module._get_task(task_id)
        assert stale is not None
        assert stale["progress"] == 0

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            payload = json.loads(row["payload"])
            payload.update(
                {
                    "status": "running",
                    "progress": 42,
                    "message": "otro worker avanzó",
                    # Marca claramente posterior a la copia en memoria.
                    "updated_at": "2099-01-01T00:00:00+00:00",
                }
            )
            conn.execute(
                "UPDATE task_state SET payload = ?, updated_at = ? "
                "WHERE task_id = ?",
                (
                    json.dumps(payload),
                    "2099-01-01T00:00:00+00:00",
                    task_id,
                ),
            )

        # Sin refresh se mantiene la copia local (más rápida).
        assert app_module._get_task(task_id)["progress"] == 0
        # Con refresh se adopta la instantánea más nueva de SQLite.
        refreshed = app_module._get_task(task_id, refresh=True)
        assert refreshed["progress"] == 42
        assert refreshed["status"] == "running"
    finally:
        app_module.remove_task(task_id)


def test_get_task_does_not_downgrade_with_older_snapshot(db_path):
    task_id = app_module.create_task(owner="alice")
    try:
        app_module.update_task(
            task_id,
            {
                "progress": 77,
                "updated_at": "2099-01-02T00:00:00+00:00",
            },
        )

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            payload = json.loads(row["payload"])
            payload.update(
                {
                    "progress": 1,
                    "updated_at": "2000-01-01T00:00:00+00:00",
                }
            )
            conn.execute(
                "UPDATE task_state SET payload = ? WHERE task_id = ?",
                (json.dumps(payload), task_id),
            )

        assert app_module._get_task(task_id, refresh=True)["progress"] == 77
    finally:
        app_module.remove_task(task_id)


def test_task_version_changes_with_progress():
    base = {"updated_at": "2026-01-01T00:00:00+00:00", "console": []}
    assert app_module._task_version(base) != app_module._task_version(
        {**base, "progress": 10}
    )
    assert app_module._task_version(base) != app_module._task_version(
        {**base, "console": ["línea"]}
    )


# ---------------------------------------------------------------------------
# Endpoints de métricas
# ---------------------------------------------------------------------------
def test_metrics_endpoints_require_admin(client):
    response = client.get("/api/metricas")
    assert response.status_code == 302

    assert _login(client).status_code == 302
    client.get("/")
    response = client.get("/api/metricas")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["window"] >= 50
    assert "index" in payload["endpoints"]
    assert payload["endpoints"]["index"]["count"] >= 1
    assert payload["queue"]["workers"] >= 1
    assert "depth" in payload["queue"]
    assert "wait" in payload["queue"]

    page = client.get("/metricas")
    assert page.status_code == 200
    assert b"Latencia por ruta" in page.data
    assert b"Trabajos en cola" in page.data


def test_metrics_endpoint_forbidden_for_viewer(client, db_path):
    User.create_or_update(
        "viewer", "viewer-password-123", db_path=db_path, role="viewer"
    )
    # La sesión de admin del fixture no se usa: login explícito como viewer.
    _post(
        client,
        "/login",
        {"username": "viewer", "password": "viewer-password-123"},
    )
    assert client.get("/api/metricas").status_code == 403


# ---------------------------------------------------------------------------
# Estadísticas de latencia por lote
# ---------------------------------------------------------------------------
def test_batch_duration_stats(db_path):
    batch_id = "lote-rendimiento"
    ExecutionBatch.create_batch(
        batch_id=batch_id,
        operator="admin",
        action="TZ",
        value="America/Caracas",
        target="grupo",
        total_hosts=4,
        db_path=db_path,
    )
    for host, duration in (
        ("h1", 100),
        ("h2", 200),
        ("h3", 300),
        ("h4", 0),
    ):
        ExecutionBatch.add_host_result(
            batch_id=batch_id,
            host=host,
            result="success",
            duration_ms=duration,
            db_path=db_path,
        )

    stats = ExecutionBatch.get_duration_stats(batch_id, db_path=db_path)

    assert stats["count"] == 3
    assert stats["avg_ms"] == 200
    assert stats["p50_ms"] == 200
    assert stats["p95_ms"] == 300
    assert stats["max_ms"] == 300


def test_batch_duration_stats_without_measurements(db_path):
    batch_id = "lote-sin-duraciones"
    ExecutionBatch.create_batch(
        batch_id=batch_id,
        operator="admin",
        action="TZ",
        value="America/Caracas",
        target="grupo",
        db_path=db_path,
    )
    stats = ExecutionBatch.get_duration_stats(batch_id, db_path=db_path)
    assert stats == {
        "count": 0,
        "avg_ms": 0,
        "p50_ms": 0,
        "p95_ms": 0,
        "max_ms": 0,
    }


# ---------------------------------------------------------------------------
# Migración
# ---------------------------------------------------------------------------
def test_schema_v13_adds_hosts_created_at_index(db_path):
    init_schema(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_hosts_created_at'"
        ).fetchone()
    assert row is not None
