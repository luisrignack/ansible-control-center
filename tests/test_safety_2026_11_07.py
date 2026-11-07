"""Pruebas de criticidad y ventanas de mantenimiento (S1, 2026-11-07).

Cubren las reglas puras de ``services.safety``, la persistencia en el
inventario, la migración v14 y los dos puntos de bloqueo: ejecución manual y
programaciones (que se posponen en vez de usar una excepción).
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
from models.db import get_connection, init_schema
from models.ops import HostGroup, InventoryHost, Schedule
from models.user import AuthEvent, User
from services.safety import (
    evaluate_safety,
    is_window_open,
    next_window_open,
    normalize_criticality,
    normalize_criticality_override,
    parse_window_days,
    parse_window_time,
    window_error,
)


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "safety.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: path)
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


def _login(client, username="admin", password="admin-password-123"):
    return _post(
        client, "/login", {"username": username, "password": password}
    )


def _active_task():
    for task in app_module.TASK_CATALOG:
        if task.get("status") == "active" and "TZ" in (
            task.get("actions") or []
        ):
            return task
    raise AssertionError("El catálogo no tiene una tarea TZ activa")


def _execution_form(group_id: int, **overrides):
    data = {
        "task": _active_task()["id"],
        "target_mode": "group",
        "target_group": str(group_id),
        "target_ip": "10.0.0.5",
        "admin_user": "root",
        "password_origin": "temporal",
        "action": "TZ",
        "value": "UTC",
        "time_mode": "utc",
        "ntp_policy": "keep",
        "mode": "execute",
        "password_file": (io.BytesIO(b"clave-uno\n"), "passwords.txt"),
    }
    data.update(overrides)
    return data


class _StubQueue:
    def __init__(self) -> None:
        self.calls = []

    def submit(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _critical_group(db_path, *, days="", start="", end="", hosts=("10.0.0.5",)):
    group = HostGroup.create(
        "Crítico",
        os_flavor="linux",
        db_path=db_path,
        criticality="critical",
        window_days=days,
        window_start=start,
        window_end=end,
        window_note="prueba",
    )
    for address in hosts:
        host, error = InventoryHost.add(
            group.id, address, db_path=db_path, criticality="critical"
        )
        assert error is None and host is not None
    return group


def _outside_window_now():
    """Ventana mañana a las 00:00–00:01: nunca abierta hoy."""
    tomorrow = (datetime.now(timezone.utc).isoweekday() % 7) + 1
    return str(tomorrow), "00:00", "00:01"


# ---------------------------------------------------------------------------
# Reglas puras
# ---------------------------------------------------------------------------
def test_normalize_criticality_and_override():
    assert normalize_criticality(" HIGH ") == "high"
    assert normalize_criticality("") == "medium"
    assert normalize_criticality_override("") == ""
    assert normalize_criticality_override("inherit") == ""
    assert normalize_criticality_override("critical") == "critical"
    with pytest.raises(ValueError):
        normalize_criticality("urgente")
    with pytest.raises(ValueError):
        normalize_criticality_override("urgente")


def test_parse_window_days_and_time():
    assert parse_window_days("1, 3;dom") == (1, 3, 7)
    assert parse_window_days("lun,mar") == (1, 2)
    assert parse_window_time("06:05") == "06:05"
    assert parse_window_time("") == ""
    with pytest.raises(ValueError):
        parse_window_days("0")
    with pytest.raises(ValueError):
        parse_window_time("25:00")


def test_window_error_validation():
    assert window_error((), "", "") is None
    assert "al menos un día" in window_error((), "08:00", "09:00")
    assert "inicio y hora de fin" in window_error((1,), "08:00", "")
    assert "iguales" in window_error((1,), "08:00", "08:00")
    assert window_error((1,), "08:00", "09:00") is None


def test_day_window_is_open_and_next_open():
    # Lunes 2026-11-02 10:30 UTC.
    moment = datetime(2026, 11, 2, 10, 30, tzinfo=timezone.utc)
    assert is_window_open((1,), "08:00", "12:00", moment) is True
    assert is_window_open((2,), "08:00", "12:00", moment) is False
    nxt = next_window_open((2,), "08:00", "12:00", moment)
    assert nxt == datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)


def test_night_window_crosses_midnight():
    # Ventana lunes 22:00 → 06:00: abierta el lunes por la noche y el martes
    # de madrugada, cerrada el martes por la noche.
    monday_night = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)
    tuesday_dawn = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
    tuesday_night = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)
    assert is_window_open((1,), "22:00", "06:00", monday_night) is True
    assert is_window_open((1,), "22:00", "06:00", tuesday_dawn) is True
    assert is_window_open((1,), "22:00", "06:00", tuesday_night) is False


def test_evaluate_safety_blocks_critical_outside_window():
    days, start, end = _outside_window_now()
    result = evaluate_safety(
        group_name="Crítico",
        group_criticality="critical",
        hosts=[{"address": "10.0.0.5", "criticality": "critical"}],
        window_days=days,
        window_start=start,
        window_end=end,
    )
    assert result["blocked"] is True
    assert result["level"] == "block"
    assert result["critical_hosts"] == ["10.0.0.5"]
    assert result["next_open"]


def test_evaluate_safety_warns_high_without_blocking():
    days, start, end = _outside_window_now()
    result = evaluate_safety(
        group_name="Alto",
        group_criticality="high",
        hosts=[{"address": "10.0.0.6", "criticality": "high"}],
        window_days=days,
        window_start=start,
        window_end=end,
    )
    assert result["blocked"] is False
    assert result["level"] == "warn"
    assert "criticidad alta" in result["warnings"][0]


def test_evaluate_safety_critical_without_window_only_warns():
    result = evaluate_safety(
        group_name="Crítico",
        group_criticality="critical",
        hosts=[{"address": "10.0.0.5", "criticality": "critical"}],
    )
    assert result["blocked"] is False
    assert result["level"] == "warn"
    assert "sin ventana" in result["warnings"][0]


# ---------------------------------------------------------------------------
# Persistencia y migración
# ---------------------------------------------------------------------------
def test_group_and_host_safety_persist(db_path):
    group = HostGroup.create(
        "Ventana",
        os_flavor="linux",
        db_path=db_path,
        criticality="high",
        window_days="1,2,3",
        window_start="22:00",
        window_end="06:00",
        window_note="nocturna",
    )
    host, error = InventoryHost.add(
        group.id, "10.0.0.7", db_path=db_path, criticality="critical"
    )
    assert error is None and host is not None

    stored = HostGroup.get(group.id, db_path=db_path)
    assert stored.criticality == "high"
    assert stored.window_days == "1,2,3"
    assert stored.window_start == "22:00"
    assert stored.window_label == "Lun, Mar, Mié 22:00–06:00 UTC"

    found, found_group = InventoryHost.find_by_address(
        "10.0.0.7", db_path=db_path
    )
    assert found is not None and found.criticality == "critical"
    assert found_group is not None and found_group.id == group.id

    InventoryHost.set_criticality(host.id, "", db_path=db_path)
    found, _ = InventoryHost.find_by_address("10.0.0.7", db_path=db_path)
    assert found.criticality == ""


def test_group_rejects_incomplete_window(db_path):
    with pytest.raises(ValueError, match="al menos un día"):
        HostGroup.create(
            "Inválido", db_path=db_path, window_start="08:00", window_end="09:00"
        )


def test_migration_v14_adds_safety_columns(db_path):
    init_schema(db_path)
    with get_connection(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(inventory_groups)")
        }
        host_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(inventory_hosts)")
        }
        assert version == 14
        assert {
            "criticality",
            "window_days",
            "window_start",
            "window_end",
            "window_note",
        } <= columns
        assert "criticality" in host_columns
        # Simula una base v13 sin los campos y vuelve a migrar.
        for column in (
            "criticality",
            "window_days",
            "window_start",
            "window_end",
            "window_note",
        ):
            conn.execute(f"ALTER TABLE inventory_groups DROP COLUMN {column}")
        conn.execute("ALTER TABLE inventory_hosts DROP COLUMN criticality")
        conn.execute("PRAGMA user_version = 13")

    init_schema(db_path)
    with get_connection(db_path) as conn:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(inventory_groups)")
        }
        host_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(inventory_hosts)")
        }
        assert version == 14
        assert {
            "criticality",
            "window_days",
            "window_start",
            "window_end",
            "window_note",
        } <= columns
        assert "criticality" in host_columns


# ---------------------------------------------------------------------------
# Gate de ejecución manual
# ---------------------------------------------------------------------------
def test_execute_blocks_critical_outside_window(client, db_path, monkeypatch):
    days, start, end = _outside_window_now()
    group = _critical_group(db_path, days=days, start=start, end=end)
    queue = _StubQueue()
    monkeypatch.setattr(app_module, "get_job_queue", lambda: queue)
    _login(client)

    response = _post(client, "/execute", _execution_form(group.id))

    assert response.status_code == 302
    assert queue.calls == []
    events = [event["event"] for event in AuthEvent.recent(db_path=db_path)]
    assert "safety_blocked" in events


def test_preview_is_allowed_outside_window(client, db_path, monkeypatch):
    days, start, end = _outside_window_now()
    group = _critical_group(db_path, days=days, start=start, end=end)
    queue = _StubQueue()
    monkeypatch.setattr(app_module, "get_job_queue", lambda: queue)
    _login(client)

    response = _post(
        client,
        "/preview",
        _execution_form(group.id, mode="preview"),
    )

    assert response.status_code == 302
    assert len(queue.calls) == 1
    events = [event["event"] for event in AuthEvent.recent(db_path=db_path)]
    assert "safety_blocked" not in events


def test_admin_override_requires_reason_and_is_audited(
    client, db_path, monkeypatch
):
    days, start, end = _outside_window_now()
    group = _critical_group(db_path, days=days, start=start, end=end)
    queue = _StubQueue()
    monkeypatch.setattr(app_module, "get_job_queue", lambda: queue)
    _login(client)

    short = _post(
        client,
        "/execute",
        _execution_form(
            group.id, safety_override="1", safety_reason="corto"
        ),
    )
    assert short.status_code == 302
    assert queue.calls == []

    allowed = _post(
        client,
        "/execute",
        _execution_form(
            group.id,
            safety_override="1",
            safety_reason="Cambio urgente aprobado por el cliente",
        ),
    )
    assert allowed.status_code == 302
    assert len(queue.calls) == 1
    recent = AuthEvent.recent(limit=20, db_path=db_path)
    override = [e for e in recent if e["event"] == "safety_override"]
    assert override and "urgente" in override[0]["detail"]


def test_operator_cannot_override(client, db_path, monkeypatch):
    User.create_or_update(
        "operador",
        "operador-password-123",
        role="operator",
        db_path=db_path,
    )
    days, start, end = _outside_window_now()
    group = _critical_group(db_path, days=days, start=start, end=end)
    queue = _StubQueue()
    monkeypatch.setattr(app_module, "get_job_queue", lambda: queue)
    _login(client, "operador", "operador-password-123")

    response = _post(
        client,
        "/execute",
        _execution_form(
            group.id,
            safety_override="1",
            safety_reason="Intento sin permisos suficientes",
        ),
    )

    assert response.status_code == 302
    assert queue.calls == []
    events = [event["event"] for event in AuthEvent.recent(db_path=db_path)]
    assert "safety_override" not in events


# ---------------------------------------------------------------------------
# Programaciones
# ---------------------------------------------------------------------------
def test_schedule_creation_rejects_outside_window(client, db_path):
    group = _critical_group(db_path)
    # El grupo no tiene ventana configurada: no bloquea, solo avisa. Se le
    # añade una ventana que no cubre mañana para forzar el rechazo.
    today = datetime.now(timezone.utc).isoweekday()
    HostGroup.set_safety(
        group.id,
        "critical",
        window_days=str(today),
        window_start="00:00",
        window_end="00:01",
        db_path=db_path,
    )
    _login(client)
    run_at = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M"
    )

    response = _post(
        client,
        "/programaciones",
        {
            "op": "create",
            "name": "Fuera de ventana",
            "task_id": _active_task()["id"],
            "run_at": run_at,
            "recurrence": "once",
            "target_mode": "group",
            "target_group": str(group.id),
            "admin_user": "root",
            "action": "TZ",
            "value": "UTC",
            "ntp_policy": "keep",
            "master_password": "vault-pass",
        },
    )

    assert response.status_code == 302
    assert Schedule.all(db_path=db_path) == []


def test_launch_scheduled_defers_outside_window(db_path, monkeypatch):
    days, start, end = _outside_window_now()
    group = _critical_group(db_path, days=days, start=start, end=end)
    monkeypatch.setattr(
        app_module.scheduler, "decrypt_secret", lambda value: "vault-pass"
    )
    task = _active_task()
    run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    Schedule.create(
        "Pendiente",
        task["id"],
        run_at,
        {
            "target": f"inventory:{group.id}",
            "target_mode": "group",
            "target_group_id": group.id,
            "admin_user": "root",
            "origin": "stored",
            "action": "TZ",
            "value": "UTC",
            "time_mode": "utc",
            "ntp_policy": "keep",
            "ntp_servers": [],
            "master_password_enc": "x",
        },
        recurrence="daily",
        db_path=db_path,
    )
    schedule = Schedule.all(db_path=db_path)[0]

    launched = app_module.launch_scheduled(
        schedule.to_dict(), schedule.params
    )

    assert launched is False
    stored = Schedule.get(schedule.id, db_path=db_path)
    assert "Pospuesta por ventana" in stored.last_error
    assert datetime.fromisoformat(stored.run_at) > run_at
    assert stored.enabled is True
