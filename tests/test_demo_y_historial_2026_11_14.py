"""Borrado del historial y datos ficticios de demostración (0.9.6)."""

from __future__ import annotations

import pytest

import app as app_module
from acc import runtime
from models.batch import ExecutionBatch
from models.db import get_connection, init_schema
from models.ops import HostGroup
from models.user import AuthEvent, User
from services.demo_data import demo_data_present, purge_demo_data, seed_demo_data


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "demo.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path, role="admin")
    User.create_or_update(
        "operador", "operador-password-123", db_path=db_path, role="operator"
    )
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _login(client, username="admin", password="admin-password-123"):
    with client.session_transaction() as sess:
        sess.clear()
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": _csrf(client),
        },
    )


def _post(client, url, data):
    return client.post(url, data={**data, "csrf_token": _csrf(client)})


def _make_batch(db_path: str, batch_id: str, status: str) -> None:
    ExecutionBatch.create_batch(
        batch_id,
        "admin",
        "TZ",
        "America/Caracas",
        "10.0.0.0/24",
        total_hosts=2,
        status="running",
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(batch_id, "10.0.0.10", "success", db_path=db_path)
    if status != "running":
        ExecutionBatch.finish_batch(batch_id, status=status, db_path=db_path)


# ---------------------------------------------------------------------------
# Borrado del historial
# ---------------------------------------------------------------------------
def test_borrar_historial_solo_admin(client):
    _login(client, "operador", "operador-password-123")
    response = _post(client, "/ejecuciones/borrar", {"confirmar": "BORRAR"})
    assert response.status_code == 403


def test_borrar_historial_exige_confirmacion_y_conserva_en_curso(client):
    db_path = runtime.get_db_path()
    _make_batch(db_path, "terminado-1", "success")
    _make_batch(db_path, "en-curso-1", "running")

    _login(client)
    response = _post(client, "/ejecuciones/borrar", {"confirmar": "eliminar"})
    assert response.status_code == 302
    assert ExecutionBatch.get_batch("terminado-1", db_path=db_path) is not None
    assert ExecutionBatch.get_batch("en-curso-1", db_path=db_path) is not None

    response = _post(client, "/ejecuciones/borrar", {"confirmar": "BORRAR"})
    assert response.status_code == 302
    assert ExecutionBatch.get_batch("terminado-1", db_path=db_path) is None
    assert ExecutionBatch.get_batch("en-curso-1", db_path=db_path) is not None
    # No queda ningún resultado huérfano del lote borrado.
    with get_connection(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM execution_hosts WHERE batch_id = 'terminado-1'"
            ).fetchone()[0]
            == 0
        )
    events = AuthEvent.recent(limit=20, db_path=db_path)
    assert any(event["event"] == "history_cleared" for event in events)


def test_boton_borrar_historial_visible_solo_para_admin(client):
    _login(client)
    body = client.get("/ejecuciones?tab=historial").get_data(as_text=True)
    assert "Borrar historial" in body
    assert "clearHistoryModal" in body

    _login(client, "operador", "operador-password-123")
    body = client.get("/ejecuciones?tab=historial").get_data(as_text=True)
    assert "clearHistoryModal" not in body


# ---------------------------------------------------------------------------
# Datos de demostración
# ---------------------------------------------------------------------------
def test_seed_demo_crea_un_conjunto_completo(tmp_path):
    db_path = str(tmp_path / "seed.db")
    init_schema(db_path)

    summary = seed_demo_data(db_path)

    assert summary["groups"] == 4
    assert summary["hosts"] == 20
    assert summary["batches"] == 14
    assert summary["schedules"] == 4
    assert summary["approvals"] == 4
    assert summary["events"] == 8

    groups = HostGroup.all(db_path=db_path)
    assert len(groups) == 4
    assert all(group.name.startswith("Demo · ") for group in groups)

    with get_connection(db_path) as conn:
        host_count = conn.execute("SELECT COUNT(*) FROM inventory_hosts").fetchone()[0]
        running = conn.execute(
            "SELECT COUNT(*) FROM execution_batches WHERE status = 'running'"
        ).fetchone()[0]
        enabled_schedules = conn.execute(
            "SELECT COUNT(*) FROM schedules WHERE enabled = 1"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status = 'pending'"
        ).fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM auth_events").fetchone()[0]
    assert host_count == 20
    assert running == 1
    assert enabled_schedules == 0
    assert pending == 2
    assert events == 8
    assert demo_data_present(db_path)


def test_seed_demo_sin_lote_en_curso(tmp_path):
    db_path = str(tmp_path / "seed.db")
    from models.db import init_schema

    init_schema(db_path)
    summary = seed_demo_data(db_path, with_running=False)
    with get_connection(db_path) as conn:
        running = conn.execute(
            "SELECT COUNT(*) FROM execution_batches WHERE status = 'running'"
        ).fetchone()[0]
    assert running == 0
    assert summary["batches"] > 0


def test_seed_demo_es_idempotente_con_reset_y_purga(tmp_path):
    db_path = str(tmp_path / "seed.db")
    from models.db import init_schema

    init_schema(db_path)
    seed_demo_data(db_path, with_running=False)

    with pytest.raises(ValueError):
        seed_demo_data(db_path, with_running=False)

    seed_demo_data(db_path, with_running=False, reset=True)
    with get_connection(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM inventory_groups").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 4

    removed = purge_demo_data(db_path)
    assert removed["groups"] == 4
    assert removed["schedules"] == 4
    assert removed["approvals"] == 4
    assert removed["events"] == 8
    assert not demo_data_present(db_path)


def test_purga_no_toca_datos_reales(tmp_path):
    db_path = str(tmp_path / "seed.db")
    from models.db import init_schema

    init_schema(db_path)
    real = HostGroup.create("Sucursal real", db_path=db_path)
    _make_batch(db_path, "lote-real", "success")
    seed_demo_data(db_path, with_running=False)

    purge_demo_data(db_path)

    assert HostGroup.get(real.id, db_path=db_path) is not None
    assert ExecutionBatch.get_batch("lote-real", db_path=db_path) is not None


def test_cli_seed_demo_exige_confirmacion(tmp_path, monkeypatch):
    db_path = str(tmp_path / "seed.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    from models.db import init_schema

    init_schema(db_path)

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["seed-demo"])
    assert result.exit_code != 0
    assert "--yes" in result.output

    result = runner.invoke(args=["seed-demo", "--yes", "--no-running"])
    assert result.exit_code == 0, result.output
    assert "Datos de demostración insertados" in result.output

    result = runner.invoke(args=["seed-demo", "--yes", "--purge"])
    assert result.exit_code == 0, result.output
    assert "eliminados" in result.output
