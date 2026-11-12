# tests/test_alta_prioridad_2026_11_06.py
# Pruebas de las correcciones de alta prioridad del análisis 2026-11-06.
from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
import config as config_module
import services.scheduler as scheduler_module
from models.batch import ExecutionBatch
from models.db import get_connection
from models.ops import (
    APPROVAL_CONSUMED,
    APPROVAL_RESERVED,
    ApprovalRequest,
    HostGroup,
    InventoryHost,
    Schedule,
)
from models.user import User
from services.job_queue import JobQueueFull


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=path)
    return path


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


# ---------------------------------------------------------------------------
# A4: reserva y liberación de aprobaciones
# ---------------------------------------------------------------------------
def _approval_params(**extra):
    base = {
        "target": "10.44.0.1",
        "target_mode": "manual",
        "target_group_id": None,
        "group_name": "",
        "group_os_flavor": "",
        "group_connection": "",
        "target_hosts": [],
        "task_id": "kaspersky",
        "admin_user": "root",
        "action": "TZ",
        "value": "UTC",
    }
    base.update(extra)
    return base


def _approved_request(db_path):
    req = ApprovalRequest.create(
        "kaspersky", _approval_params(), requested_by="operador", db_path=db_path
    )
    decided, error = ApprovalRequest.decide(
        req.id, approver="aprobador", approve=True, db_path=db_path
    )
    assert error is None and decided is not None
    return req


def _consume(db_path, **params):
    return ApprovalRequest.reserve_if_approved(
        "kaspersky",
        _approval_params(**params),
        executor="ejecutor",
        approves_by_differs=True,
        ttl_minutes=30,
        db_path=db_path,
    )


def test_approval_reservation_can_be_released_and_reused(db_path):
    req = _approved_request(db_path)

    request_id, error = _consume(db_path)
    assert error is None and request_id == req.id
    assert ApprovalRequest.get(req.id, db_path=db_path).status == APPROVAL_RESERVED

    assert ApprovalRequest.release_reserved(req.id, db_path=db_path)
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "approved"

    # Tras liberar, otro envío puede reservarla.
    request_id, error = _consume(db_path)
    assert error is None and request_id == req.id
    assert ApprovalRequest.confirm_reserved(req.id, db_path=db_path)
    assert ApprovalRequest.get(req.id, db_path=db_path).status == APPROVAL_CONSUMED


def test_stale_approval_reservation_returns_to_approved(db_path):
    req = _approved_request(db_path)
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE approvals SET status = ?, consumed_at = ? WHERE id = ?",
            (APPROVAL_RESERVED, old, req.id),
        )

    request_id, error = _consume(db_path)
    assert error is None and request_id == req.id


def test_queue_full_releases_approval(monkeypatch, db_path):
    """Un fallo de capacidad no debe gastar la aprobación de un solo uso."""
    form = {
        "target": "10.44.0.1",
        "target_mode": "manual",
        "target_group_id": None,
        "group_name": "",
        "group_os_flavor": "",
        "group_connection": "",
        "target_hosts": [],
        "task_id": "destructiva-test",
        "task_name": "Destructiva de prueba",
        "admin_user": "root",
        "origin": "stored",
        "master_password": "clave",
        "action": "TZ",
        "value": "UTC",
        "mode": "execute",
        "time_mode": "utc",
        "time_zone": "",
        "time_fold": None,
        "target_epoch": None,
        "ntp_policy": "keep",
        "ntp_servers": [],
        "sync_rtc": False,
        "force": False,
        "temporal_passwords": [],
    }
    monkeypatch.setattr(app_module, "_parse_operation_form", lambda **_kw: (form, []))
    monkeypatch.setattr(
        app_module,
        "get_task",
        lambda _task_id: {
            "id": "destructiva-test",
            "name": "Destructiva de prueba",
            "destructive": True,
            "min_role": "operator",
            "actions": ["TZ"],
            "platforms": ["linux"],
            "status": "active",
        },
    )

    class FullQueue:
        def submit(self, *args, **kwargs):
            raise JobQueueFull("cola llena")

    monkeypatch.setattr(app_module, "get_job_queue", lambda: FullQueue())

    req = ApprovalRequest.create(
        "destructiva-test",
        app_module._approval_params(form),
        requested_by="operador",
        db_path=db_path,
    )
    ApprovalRequest.decide(req.id, approver="aprobador", approve=True, db_path=db_path)

    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        _post(client, "/login", {"username": "admin", "password": "admin-password-123"})
        response = _post(client, "/", {"operation_id": "op-cola-llena"})
        assert response.status_code == 302

    assert ApprovalRequest.get(req.id, db_path=db_path).status == "approved"


# ---------------------------------------------------------------------------
# A3: la aprobación de grupo fija la instantánea de hosts
# ---------------------------------------------------------------------------
def test_group_approval_snapshot_changes_with_membership(db_path):
    group = HostGroup.create("Sucursal", os_flavor="linux", db_path=db_path)
    InventoryHost.add(group.id, "10.42.0.10", db_path=db_path)
    InventoryHost.add(group.id, "10.42.0.11", db_path=db_path)

    data = {
        "task": "zona-todas",
        "target_mode": "group",
        "target_group": str(group.id),
        "admin_user": "root",
        "password_origin": "temporal",
        "action": "TZ",
        "value": "America/Caracas",
        "mode": "execute",
    }

    def _parse():
        payload = dict(data)
        payload["password_file"] = (io.BytesIO(b"clave\n"), "p.txt")
        with app_module.app.test_request_context(
            "/", method="POST", data=payload, content_type="multipart/form-data"
        ):
            return app_module._parse_operation_form()

    form, errors = _parse()
    assert errors == []
    assert form is not None
    assert form["target_hosts"] == ["10.42.0.10", "10.42.0.11"]
    assert form["group_os_flavor"] == "linux"
    first_hash = app_module.params_hash(app_module._approval_params(form))

    InventoryHost.add(group.id, "10.42.0.12", db_path=db_path)
    form2, errors2 = _parse()
    assert errors2 == []
    assert form2 is not None
    assert form2["target_hosts"] == [
        "10.42.0.10",
        "10.42.0.11",
        "10.42.0.12",
    ]
    second_hash = app_module.params_hash(app_module._approval_params(form2))
    assert first_hash != second_hash


# ---------------------------------------------------------------------------
# A1: claim atómico de programaciones y recuperación solo de lo estancado
# ---------------------------------------------------------------------------
def test_schedule_claim_is_atomic(db_path):
    schedule = Schedule.create(
        "ventana",
        "zona-todas",
        datetime.now(UTC) - timedelta(seconds=60),
        {},
        recurrence="daily",
        db_path=db_path,
    )
    assert Schedule.claim(schedule.id, owner="worker-a", db_path=db_path)
    assert not Schedule.claim(schedule.id, owner="worker-b", db_path=db_path)

    Schedule.mark_run(
        schedule.id,
        next_run=datetime.now(UTC) + timedelta(days=1),
        db_path=db_path,
    )
    # mark_run limpia la reserva; la fila ya no está vencida.
    assert not Schedule.claim(schedule.id, owner="worker-b", db_path=db_path)


def test_scheduler_scan_skips_a_claimed_schedule(db_path):
    schedule = Schedule.create(
        "reclamada",
        "zona-todas",
        datetime.now(UTC) - timedelta(seconds=60),
        {},
        recurrence="daily",
        db_path=db_path,
    )
    assert Schedule.claim(schedule.id, owner="otro-worker", db_path=db_path)
    launched = []
    assert (
        scheduler_module.scan_once(
            db_path, lambda row, params: launched.append(row["id"]) or True
        )
        == 0
    )
    assert launched == []

    # Al caducar/limpiar la reserva, el siguiente escaneo sí la lanza.
    Schedule.mark_run(schedule.id, db_path=db_path)

    def fake_launch(row, params):
        launched.append(row["id"])
        Schedule.mark_run(
            row["id"],
            next_run=datetime.now(UTC) + timedelta(days=1),
            db_path=db_path,
        )
        return True

    assert scheduler_module.scan_once(db_path, fake_launch) == 1
    assert launched == [schedule.id]


def test_recover_task_state_only_closes_stale_tasks(db_path):
    app_module._db_path_cache = db_path
    task_id = app_module.create_task()
    app_module.update_task(task_id, {"status": "running", "message": "En curso"})

    # Recién actualizada: no debe cerrarse.
    app_module._recover_task_state(stale_minutes=15)
    assert app_module._get_task(task_id)["completed"] is False

    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE task_state SET updated_at = ? WHERE task_id = ?",
            (old, task_id),
        )
    # La instantánea solo vive en SQLite; se descarta la copia en memoria para
    # comprobar lo que vería otro proceso tras un reinicio.
    with app_module._tasks_lock:
        app_module._tasks.pop(task_id, None)
    app_module._recover_task_state(stale_minutes=15)
    task = app_module._get_task(task_id)
    assert task["completed"] is True
    assert task["status"] == "error"


def test_reconcile_orphans_ignores_recent_batches(db_path):
    ExecutionBatch.create_batch(
        batch_id="reciente",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.1",
        status="running",
        db_path=db_path,
    )
    assert ExecutionBatch.reconcile_orphans(db_path=db_path, stale_minutes=15) == 0

    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE execution_batches SET created_at = ? WHERE batch_id = ?",
            (old, "reciente"),
        )
    assert ExecutionBatch.reconcile_orphans(db_path=db_path, stale_minutes=15) == 1


# ---------------------------------------------------------------------------
# A2: rutas de producción fail-closed
# ---------------------------------------------------------------------------
def test_production_db_path_fails_closed(tmp_path, monkeypatch):
    blocked = tmp_path / "no-es-directorio"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config_module, "IS_PRODUCTION", True)
    monkeypatch.setattr(config_module, "ALLOW_DEV_FALLBACK", False)
    monkeypatch.setattr(config_module, "DATA_DIR", blocked)
    monkeypatch.setattr(config_module, "INSTANCE_DIR", tmp_path / "instance")

    with pytest.raises(RuntimeError, match="no es escribible"):
        config_module.resolve_db_path()


def test_development_fallback_still_allowed(tmp_path, monkeypatch):
    blocked = tmp_path / "no-es-directorio"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config_module, "IS_PRODUCTION", False)
    monkeypatch.setattr(config_module, "ALLOW_DEV_FALLBACK", True)
    monkeypatch.setattr(config_module, "DATA_DIR", blocked)
    monkeypatch.setattr(config_module, "INSTANCE_DIR", tmp_path / "instance")

    assert config_module.resolve_db_path() == tmp_path / "instance" / "acc.db"


# ---------------------------------------------------------------------------
# A5/A6: LDAP sin texto claro y backup de timezone actualizado
# ---------------------------------------------------------------------------
def test_production_rejects_insecure_ldap(monkeypatch):
    import ipaddress

    monkeypatch.setattr(config_module, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/8"),),
    )
    monkeypatch.setattr(config_module, "SECRET_KEY", "clave-explicita")
    monkeypatch.setattr(config_module, "HOST_KEY_CHECKING", True)
    monkeypatch.setattr(config_module, "SESSION_COOKIE_SECURE", True)
    monkeypatch.setattr(config_module, "LDAP_ENABLED", True)
    monkeypatch.setattr(config_module, "LDAP_URL", "ldap://directorio:389")
    monkeypatch.setattr(config_module, "LDAP_ALLOW_INSECURE", False)

    with pytest.raises(RuntimeError, match="ldaps"):
        config_module.validate_security_config()

    monkeypatch.setattr(config_module, "LDAP_ALLOW_INSECURE", True)
    config_module.validate_security_config()


def test_timezone_backup_is_always_refreshed():
    import yaml

    with open("ansible/cambiar_hora.yml", encoding="utf-8") as handle:
        playbook = yaml.safe_load(handle)
    tasks = playbook[0]["tasks"]
    backup = next(
        task
        for task in tasks
        if task["name"] == "Guardar copia de seguridad de /etc/timezone"
    )
    assert backup["ansible.builtin.copy"]["force"] is True
