# tests/test_mejoras_2026_11_06_parte2.py
# Pruebas del plan de mejora posterior al análisis 2026-11-06:
# validador único, ownership de cancelación, retención periódica y
# persistencia agrupada.
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
import config as config_module
import services.scheduler as scheduler_module
from models.ops import HostGroup, InventoryHost, Schedule
from models.user import User


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=path)
    User.create_or_update(
        "alice", "alice-password-123", db_path=path, role="operator"
    )
    User.create_or_update(
        "bob", "bob-password-123", db_path=path, role="operator"
    )
    return path


@pytest.fixture()
def client(db_path, monkeypatch):
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


def _login(client, username: str, password: str):
    return _post(client, "/login", {"username": username, "password": password})


def _logout(client):
    return _post(client, "/logout")


def _fake_task(platforms, *, task_id="tarea-x", destructive=False):
    return {
        "id": task_id,
        "name": "Tarea de prueba",
        "status": "active",
        "actions": ["TZ"],
        "platforms": platforms,
        "fixed_value": None,
        "destructive": destructive,
    }


# ---------------------------------------------------------------------------
# Validador único: plataforma en manual y en programaciones
# ---------------------------------------------------------------------------
def test_validator_core_rechaza_plataforma_incorrecta():
    validated, errors = app_module._validate_operation_core(
        task=_fake_task(["windows"]),
        task_id="tarea-x",
        admin_user="root",
        action="TZ",
        value="UTC",
        ntp_policy="keep",
        ntp_servers_raw="",
        platform="linux",
        group_name="aulas",
    )

    assert validated is None
    assert any("aulas" in error and "Linux" in error for error in errors)


def test_validator_core_acepta_plataforma_correcta():
    validated, errors = app_module._validate_operation_core(
        task=_fake_task(["linux", "windows"]),
        task_id="tarea-x",
        admin_user="root",
        action="TZ",
        value="UTC",
        ntp_policy="keep",
        ntp_servers_raw="",
        platform="windows",
        group_name="laboratorio",
    )

    assert errors == []
    assert validated is not None


def test_formulario_manual_rechaza_plataforma_del_grupo(
    client, db_path, monkeypatch
):
    group = HostGroup.create(
        "aulas-linux", os_flavor="linux", db_path=db_path
    )
    host, error = InventoryHost.add(
        group.id, "10.30.0.10", db_path=db_path
    )
    assert host is not None and error is None
    monkeypatch.setattr(
        app_module, "get_task", lambda _task_id: _fake_task(["windows"])
    )

    assert _login(client, "admin", "admin-password-123").status_code == 302
    response = _post(
        client,
        "/",
        {
            "task": "tarea-x",
            "target_mode": "group",
            "target_group": str(group.id),
            "admin_user": "root",
            "action": "TZ",
            "value": "UTC",
            "time_mode": "utc",
            "ntp_policy": "keep",
            "mode": "preview",
        },
    )

    assert response.status_code == 302
    with client.session_transaction() as sess:
        flashes = sess.get("_flashes") or []
    assert any("aulas-linux" in message for _category, message in flashes)


def test_launch_scheduled_rechaza_grupo_de_otra_plataforma(
    db_path, monkeypatch
):
    group = HostGroup.create(
        "aulas-linux", os_flavor="linux", db_path=db_path
    )
    schedule = Schedule.create(
        "programada",
        "tarea-x",
        datetime.now(timezone.utc) + timedelta(hours=1),
        {"target_mode": "manual", "target": "10.0.0.1"},
        db_path=db_path,
    )
    monkeypatch.setattr(
        app_module.scheduler, "decrypt_secret", lambda _token: "maestra"
    )
    monkeypatch.setattr(
        app_module, "get_task", lambda _task_id: _fake_task(["windows"])
    )
    monkeypatch.setattr(
        app_module, "get_job_queue", lambda: pytest.fail("no debe encolarse")
    )

    launched = app_module.launch_scheduled(
        {"id": schedule.id, "task_id": schedule.task_id},
        {
            "target_mode": "group",
            "target_group_id": group.id,
            "admin_user": "root",
            "action": "TZ",
            "value": "UTC",
            "ntp_policy": "keep",
            "ntp_servers": [],
            "master_password_enc": "cifrada",
        },
    )

    assert launched is False
    stored = Schedule.get(schedule.id, db_path=db_path)
    assert stored.enabled is False
    assert "no está disponible" in (stored.last_error or "")


# ---------------------------------------------------------------------------
# Ownership de cancelación
# ---------------------------------------------------------------------------
class _QueueStub:
    def cancel(self, _task_id):
        return True


def test_operador_solo_cancela_sus_tareas(client, monkeypatch):
    task_id = app_module.create_task(owner="alice")
    app_module.update_task(task_id, {"status": "running"})
    monkeypatch.setattr(app_module, "get_job_queue", lambda: _QueueStub())

    assert _login(client, "bob", "bob-password-123").status_code == 302
    response = _post(client, f"/tasks/{task_id}/cancel")

    assert response.status_code == 302
    task = app_module._get_task(task_id)
    assert not task.get("cancellation_requested")
    with app_module.get_connection(app_module.get_db_path()) as conn:
        event = conn.execute(
            "SELECT event FROM auth_events WHERE event = ? "
            "ORDER BY id DESC LIMIT 1",
            ("task_cancel_denied",),
        ).fetchone()
    assert event is not None


def test_operador_cancela_su_tarea_y_se_audita(client, monkeypatch):
    task_id = app_module.create_task(owner="alice")
    app_module.update_task(task_id, {"status": "running"})
    monkeypatch.setattr(app_module, "get_job_queue", lambda: _QueueStub())

    assert _login(client, "alice", "alice-password-123").status_code == 302
    response = _post(client, f"/tasks/{task_id}/cancel")

    assert response.status_code == 302
    task = app_module._get_task(task_id)
    assert task["cancellation_requested"] is True
    with app_module.get_connection(app_module.get_db_path()) as conn:
        event = conn.execute(
            "SELECT username FROM auth_events WHERE event = ? "
            "ORDER BY id DESC LIMIT 1",
            ("task_cancel",),
        ).fetchone()
    assert event is not None and event["username"] == "alice"


def test_admin_cancela_tarea_ajena(client, monkeypatch):
    task_id = app_module.create_task(owner="alice")
    app_module.update_task(task_id, {"status": "running"})
    monkeypatch.setattr(app_module, "get_job_queue", lambda: _QueueStub())

    assert _login(client, "admin", "admin-password-123").status_code == 302
    response = _post(client, f"/tasks/{task_id}/cancel")

    assert response.status_code == 302
    assert app_module._get_task(task_id)["cancellation_requested"] is True


def test_progress_oculta_cancelar_a_quien_no_es_dueno(client):
    task_id = app_module.create_task(owner="alice")
    app_module.update_task(task_id, {"status": "running"})

    assert _login(client, "bob", "bob-password-123").status_code == 302
    page = client.get(f"/progress/{task_id}")
    assert page.status_code == 200
    assert b"const canCancel = false;" in page.data

    _logout(client)
    assert _login(client, "alice", "alice-password-123").status_code == 302
    page = client.get(f"/progress/{task_id}")
    assert b"const canCancel = true;" in page.data


# ---------------------------------------------------------------------------
# Persistencia agrupada
# ---------------------------------------------------------------------------
def test_update_task_agrupa_escrituras_y_fuerza_cambios_de_estado(
    db_path, monkeypatch
):
    monkeypatch.setattr(app_module, "TASK_PERSIST_INTERVAL_SECONDS", 3600)
    writes = []
    monkeypatch.setattr(
        app_module, "_persist_task", lambda tid, task: writes.append(dict(task))
    )

    task_id = app_module.create_task(owner="alice")
    writes.clear()

    app_module.update_task(task_id, {"console": ["línea 1"]})
    assert len(writes) == 1

    app_module.update_task(task_id, {"console": ["línea 1", "línea 2"]})
    assert len(writes) == 1  # dentro del intervalo: no se escribe

    app_module.update_task(task_id, {"status": "running"})
    assert len(writes) == 2  # cambio de estado: siempre se persiste

    app_module.update_task(task_id, {"completed": True, "console": ["fin"]})
    assert len(writes) == 3
    assert writes[-1]["completed"] is True


# ---------------------------------------------------------------------------
# Retención periódica dentro del bucle del scheduler
# ---------------------------------------------------------------------------
def test_scheduler_ejecuta_mantenimiento_periodico(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(config_module, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(config_module, "PROCESS_ROLE", "all")
    monkeypatch.setattr(scheduler_module, "_acquire_instance_lock", lambda: True)
    monkeypatch.setattr(scheduler_module, "release_instance_lock", lambda: None)
    monkeypatch.setattr(
        scheduler_module, "scan_once", lambda *_a, **_kw: 0
    )

    def maintenance():
        ran.set()

    thread = scheduler_module.start(
        lambda *_a, **_kw: True,
        lambda: ":memory:",
        interval=0.01,
        maintenance=maintenance,
        maintenance_interval=0,
    )
    try:
        assert thread is not None
        assert ran.wait(2), "el mantenimiento periódico no se ejecutó"
    finally:
        scheduler_module.stop(timeout=2)
