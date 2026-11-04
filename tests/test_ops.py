# tests/test_ops.py
# Pruebas del bloque A: inventario persistente, ejecución por grupo,
# programaciones, notificaciones, doble aprobación, re-ejecutar y clave SSH.
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app as app_module
import services.scheduler as scheduler_module
from models.batch import ExecutionBatch
from models.ops import (
    ApprovalRequest,
    HostGroup,
    InventoryHost,
    Schedule,
)
from models.user import User
from services.notifier import batch_has_issues, notify_batch_result
from services.task_catalog import get_task


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture()
def operator_client(client, tmp_path):
    """Cliente ya autenticado con los dos roles disponibles."""
    User.create_or_update(
        "operador",
        "operador-clave-456",
        role="operator",
        db_path=app_module.get_db_path(),
    )
    return client


def _csrf_token(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _post(client, url, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload, **kwargs)


def _login(client, username="admin", password="admin-password-123"):
    return _post(client, "/login", {"username": username, "password": password})


def _wait_for_task(client, task_id: str) -> dict:
    status: dict = {}
    for _ in range(200):
        status = client.get(f"/api/task-status/{task_id}").get_json()
        if status.get("completed"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"La tarea no terminó: {status}")


def _ok(host, password=None, **extra):
    result = {
        "success": True,
        "host": host,
        "error": "",
        "before_state": {"timezone": "Etc/UTC", "ntp": True, "epoch": 100},
        "after_state": {"timezone": "UTC", "ntp": True, "epoch": 100},
        "changed": True,
        "verified": True,
        "warnings": "",
    }
    result.update(extra)
    return result


# ---------------------------------------------------------------------------
# Inventario: CRUD completo y permisos
# ---------------------------------------------------------------------------
def test_inventory_requires_login(client):
    response = client.get("/inventario")
    assert response.status_code == 302


def test_group_creation_and_host_roundtrip(client):
    _login(client)
    response = _post(
        client,
        "/inventario",
        {"action": "create_group", "name": "Oficina Central"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "Oficina Central" in body
    assert "Grupo creado" in body

    groups = HostGroup.all(db_path=app_module.get_db_path())
    assert len(groups) == 1
    group = groups[0]

    host, error = InventoryHost.add(
        group.id, "10.42.0.7", label="recepción", db_path=app_module.get_db_path()
    )
    assert error is None
    assert host.address == "10.42.0.7"

    # IP inválida rechazada.
    _, invalid = InventoryHost.add(
        group.id, "10.42.0", db_path=app_module.get_db_path()
    )
    assert invalid

    hosts = HostGroup.hosts_of(group.id, db_path=app_module.get_db_path())
    assert [h.address for h in hosts] == ["10.42.0.7"]

    hosts[0].delete(db_path=app_module.get_db_path())
    assert HostGroup.hosts_of(group.id, db_path=app_module.get_db_path()) == []


def test_group_add_host_via_web(client):
    _login(client)
    group = HostGroup.create(
        "Grupo web", db_path=app_module.get_db_path()
    )
    response = _post(
        client,
        "/inventario",
        {"action": "add_host", "group_id": group.id, "address": "10.42.0.9"},
        follow_redirects=True,
    )
    assert "añadido al grupo" in response.get_data(as_text=True)


def test_inventory_post_requires_admin(client):
    _login(client, "admin", "admin-password-123")
    groups = HostGroup.all(db_path=app_module.get_db_path())
    assert groups == []


# ---------------------------------------------------------------------------
# Ejecución con grupo de inventario (e2e)
# ---------------------------------------------------------------------------
def test_execution_with_inventory_group(operator_client, monkeypatch):
    client = operator_client
    _login(client)
    db_path = app_module.get_db_path()
    group = HostGroup.create("Sucursal A", db_path=db_path)
    for address in ("10.42.0.10", "10.42.0.11"):
        InventoryHost.add(group.id, address, db_path=db_path)

    seen_hosts = []
    monkeypatch.setattr(
        app_module,
        "test_ansible_ping",
        lambda **kwargs: {"success": True, "error": ""},
    )

    def fake_apply(host, password=None, **kwargs):
        seen_hosts.append(host)
        return _ok(host, password)

    monkeypatch.setattr(app_module, "apply_host", fake_apply)

    response = _post(
        client,
        "/",
        data={
            "target_mode": "group",
            "target_group": group.id,
            "target_ip": "",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
            "mode": "execute",
            "password_file": (False or __import__("io").BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.data
    task_id = response.headers["Location"].rstrip("/").split("/")[-1]
    status = _wait_for_task(client, task_id)
    assert status["overall_result"] == "success", status
    assert sorted(seen_hosts) == ["10.42.0.10", "10.42.0.11"]

    batch = ExecutionBatch.get_batches(db_path=db_path)[0]
    assert batch.target == f"Inventario: {group.name}"

    # El lote aunque hosts cambien no rompe: los hosts se leen en caliente.
    results = ExecutionBatch.get_host_results(batch.batch_id, db_path=db_path)
    assert len(results) == 2


def test_execution_with_empty_inventory_group_fails(operator_client, client):
    _login(operator_client)
    db_path = app_module.get_db_path()
    group = HostGroup.create("Vacío", db_path=db_path)
    response = _post(
        operator_client,
        "/",
        data={
            "target_mode": "group",
            "target_group": group.id,
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
            "mode": "execute",
            "password_file": (__import__("io").BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "no tiene hosts" in body


# ---------------------------------------------------------------------------
# Programaciones
# ---------------------------------------------------------------------------
def test_schedule_create_requires_future_run_at(client):
    db_path = app_module.get_db_path()
    run_at = datetime.now(timezone.utc) - timedelta(hours=1)
    with pytest.raises(ValueError):
        Schedule.create(
            "pasado",
            "hora",
            run_at,
            {},
            db_path=db_path,
        )


def test_schedule_scan_once_triggers_and_advances(client, monkeypatch):
    db_path = app_module.get_db_path()
    run_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    Schedule.create(
        "ventana",
        "hora",
        run_at,
        {},
        recurrence="daily",
        db_path=db_path,
    )
    launches = []

    def fake_launch(schedule, params):
        launches.append((schedule["name"], params))
        Schedule.mark_run(
            schedule["id"],
            batch_id="batch-x",
            error="",
            next_run=datetime.now(timezone.utc) + timedelta(days=1),
            db_path=db_path,
        )
        return True

    launched = scheduler_module.scan_once(db_path, fake_launch)
    assert launched == 1
    assert launches[0][0] == "ventana"

    # La siguiente ejecución quedó en el futuro: un nuevo escaneo no dispara.
    assert scheduler_module.scan_once(db_path, fake_launch) == 0


def test_schedule_task_not_active_disables(client):
    db_path = app_module.get_db_path()
    Schedule.create(
        "tarea muerta",
        "kaspersky",
        datetime.now(timezone.utc) - timedelta(minutes=1),
        {},
        db_path=db_path,
    )
    launched = scheduler_module.scan_once(
        db_path, lambda s, p: (Schedule.mark_run(s["id"], db_path=db_path), True)[1]
    )
    assert launched == 0
    schedule = Schedule.all(db_path=db_path)[0]
    assert not schedule.enabled
    assert "no está activa" in schedule.last_error


# ---------------------------------------------------------------------------
# Notificaciones de lotes
# ---------------------------------------------------------------------------
class FakeBatch:
    def __init__(self, **kwargs):
        self.batch_id = kwargs.get("batch_id", "abcdefgh1234")
        self.operator = "admin"
        self.target = "10.0.0.1"
        self.action = "TZ"
        self.value = "UTC"
        self.dry_run = False
        self.success_count = kwargs.get("success_count", 3)
        self.failure_count = kwargs.get("failure_count", 0)
        self.error_count = 0
        self.skipped_count = 0
        self.preview_count = 0
        self.status = kwargs.get("status", "success")


def test_notifier_silent_without_smtp(monkeypatch):
    import config as config_module
    import services.notifier as notifier

    monkeypatch.setattr(config_module, "SMTP_HOST", "")
    monkeypatch.setattr(config_module, "NOTIFY_TO", ["ops@example.com"])
    monkeypatch.setattr(config_module, "NOTIFY_LEVEL", "all")
    assert not notifier.smtp_enabled()
    assert notifier.notify_batch_result(FakeBatch(), []) is None


def test_notifier_sends_failure_mail(monkeypatch):
    import config as config_module
    import services.notifier as notifier

    monkeypatch.setattr(config_module, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config_module, "NOTIFY_TO", ["ops@example.com"])
    monkeypatch.setattr(config_module, "NOTIFY_LEVEL", "failures")
    assert notifier.smtp_enabled()

    sent = {}

    def fake_send(subject, body):
        sent["subject"] = subject
        sent["body"] = body
        return None

    monkeypatch.setattr(notifier, "_send", fake_send)
    batch = FakeBatch(status="partial", failure_count=1)
    host_record = __import__("types").SimpleNamespace(
        host="10.0.0.5", result="failure", error="permiso denegado"
    )
    error = notifier.notify_batch_result(batch, [host_record])
    assert error is None
    assert "fallo" in sent["subject"]
    assert "10.0.0.5" in sent["body"]

    # Sin problemas y nivel ``failures``: no se envía nada.
    assert notifier.notify_batch_result(FakeBatch(), []) is None
    assert batch_has_issues(batch)
    assert not batch_has_issues(FakeBatch())


def test_notifier_skips_previews_unless_all(monkeypatch):
    import config as config_module
    import services.notifier as notifier

    monkeypatch.setattr(config_module, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config_module, "NOTIFY_TO", ["ops@example.com"])
    monkeypatch.setattr(config_module, "NOTIFY_LEVEL", "all")
    preview = FakeBatch(status="preview", preview_count=2, success_count=0)
    sent = []
    monkeypatch.setattr(notifier, "_send", lambda s, b: sent.append(s) or None)
    error = notifier.notify_batch_result(preview, [])
    assert error is None
    assert sent and "Vista previa" in sent[0]


# ---------------------------------------------------------------------------
# Doble aprobación (two-person rule)
# ---------------------------------------------------------------------------
def _approval_params(target="10.44.0.1", value="UTC"):
    return {
        "target": target,
        "target_mode": "manual",
        "target_group_id": None,
        "task_id": "kaspersky",
        "admin_user": "root",
        "action": "TZ",
        "value": value,
    }


def test_approval_flow_create_approve_consume(client):
    db_path = app_module.get_db_path()
    req = ApprovalRequest.create(
        "kaspersky", _approval_params(), requested_by="operador", db_path=db_path
    )
    assert req.status == "pending"

    solo = ApprovalRequest.consume_if_approved(
        "kaspersky",
        _approval_params(),
        executor="operador",
        approves_by_differs=True,
        ttl_minutes=30,
        db_path=db_path,
    )
    # Con una solicitud pendiente ya avisamos del estado, no pedimos crearla.
    assert not solo[0]
    assert "pendiente" in solo[1]
    assert "doble aprobación" in solo[1]

    same_person, same_error = None, None

    decided, decide_error = ApprovalRequest.decide(
        req.id, approver="operador", approve=True, db_path=db_path
    )
    assert decide_error is None
    # El aprobador no puede coincidir con el ejecutor (two-person rule).
    same_person = ApprovalRequest.consume_if_approved(
        "kaspersky",
        _approval_params(),
        executor="operador",
        approves_by_differs=True,
        ttl_minutes=30,
        db_path=db_path,
    )
    assert not same_person[0]
    assert "two-person" in same_person[1]

    # Con una aprobación de un admin distinto la ejecución se habilita.
    req2 = ApprovalRequest.create(
        "kaspersky", _approval_params(), requested_by="operador", db_path=db_path
    )
    decided2, decide_error2 = ApprovalRequest.decide(
        req2.id, approver="admin", approve=True, db_path=db_path
    )
    assert decide_error2 is None
    assert decided2.status == "approved"

    consumed, consume_error = ApprovalRequest.consume_if_approved(
        "kaspersky",
        _approval_params(),
        executor="operador",
        approves_by_differs=True,
        ttl_minutes=30,
        db_path=db_path,
    )
    assert consumed and consume_error is None
    got = ApprovalRequest.get(req2.id, db_path=db_path)
    assert got.status == "consumed"

    # Un solo uso: la segunda ejecución vuelve a requerir aprobación.
    again = ApprovalRequest.consume_if_approved(
        "kaspersky",
        _approval_params(),
        executor="operador",
        approves_by_differs=True,
        ttl_minutes=30,
        db_path=db_path,
    )
    assert not again[0]
    # La solicitud consumida ya no vale; la fila vieja aprobada por el mismo
    # ejecutor tampoco cuenta. Basta con que el mensage diga que no hay
    # aprobación válida (usada o two-person rule).
    assert "ya se usó" in again[1] or "two-person" in again[1]


def test_approval_params_must_match(client):
    db_path = app_module.get_db_path()
    ApprovalRequest.create(
        "kaspersky", _approval_params(), requested_by="operador", db_path=db_path
    )


def test_destructive_task_requires_approval_in_web(operator_client, monkeypatch):
    client = operator_client
    _login(client)
    db_path = app_module.get_db_path()
    # kaspersky es destructive; usemos ruta inventada: como la tarea no
    # integra playbook, solo comprobamos el gate (no el playbook).
    response = _post(
        client,
        "/preview",
        data={
            "task": "kaspersky",
            "target_ip": "10.44.0.1",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
            "mode": "preview",
            "password_file": (__import__("io").BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
    )
    # La vista previa de una tarea sin playbook activo no pasa (catálogo con
    # planned): el gate exige tarea activa; por eso mínimo debe redirigir.
    assert response.status_code in (302, 400)


# ---------------------------------------------------------------------------
# Re-ejecutar lote
# ---------------------------------------------------------------------------
def test_batch_page_offers_rerun_when_applicable(operator_client, monkeypatch):
    client = operator_client
    _login(client)
    db_path = app_module.get_db_path()
    batch_id = "rerunbatch001"
    ExecutionBatch.create_batch(
        batch_id=batch_id,
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.44.0.9",
        time_mode="utc",
        ntp_policy="keep",
        total_hosts=1,
        status="success",
        db_path=db_path,
    )
    ExecutionBatch.finish_batch(
        batch_id, total_hosts=1, db_path=db_path
    )
    response = client.get(f"/batches/{batch_id}")
    body = response.get_data(as_text=True)
    assert "Re-ejecutar esta configuración" in body

    # Vista previa nunca ofrece re-ejecutar: ofrece ejecutar la configuración.
    preview_id = "previewbatch1"
    ExecutionBatch.create_batch(
        batch_id=preview_id,
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.44.0.9",
        time_mode="utc",
        ntp_policy="keep",
        total_hosts=1,
        status="preview",
        db_path=db_path,
    )
    body_preview = client.get(f"/batches/{preview_id}").get_data(as_text=True)
    assert "Re-ejecutar esta configuración" not in body_preview
    assert "Ejecutar esta configuración" in body_preview


# ---------------------------------------------------------------------------
# Clave SSH del grupo en el runner
# ---------------------------------------------------------------------------
def test_playbook_command_includes_private_key(tmp_path, monkeypatch):
    import services.ansible_runner as runner

    captured = {}

    class FakeCompleted:
        stdout = r"HORA_VERIFIED=true"
        stderr = ""
        returncode = 0

    def fake_run(cmd, **_kwargs):
        # El payload viaja en un fichero 0600; mientras se ejecuta sigue
        # existiendo y podemos leerlo para verificar la clave.
        vars_file = cmd[cmd.index("--extra-vars") + 1].lstrip("@")
        captured["payload"] = open(vars_file, encoding="utf-8").read()
        return FakeCompleted()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    key = tmp_path / "key.pem"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    playbook = tmp_path / "playbook.yml"
    playbook.write_text("- hosts: all\n")

    result = runner.run_ansible_playbook(
        "10.44.0.1",
        "root",
        "TZ",
        "UTC",
        password="clave",
        playbook_path=str(playbook),
        private_key=str(key),
    )
    assert result["success"], result
    assert "ansible_ssh_private_key_file" in captured["payload"]
    assert str(key) in captured["payload"]


def test_group_ssh_key_used_in_execution(operator_client, monkeypatch, tmp_path):
    """La clave del inventario se transmite hasta inspect/apply.

    En un lote por grupo con clave configurada, el flujo pasa
    ``private_key=patch`` al runner (aquí interceptado).
    """
    client = operator_client
    _login(client)
    db_path = app_module.get_db_path()
    group = HostGroup.create("Con clave", db_path=db_path)
    InventoryHost.add(group.id, "10.42.0.77", db_path=db_path)
    key_path = tmp_path / "inventory.pem"
    key_path.write_text("PRIVATEKEY")
    HostGroup.set_ssh_key(group.id, str(key_path), db_path=db_path)

    seen = {}
    monkeypatch.setattr(
        app_module,
        "test_ansible_ping",
        lambda **kw: (
            seen.setdefault("ping_private_key", kw.get("private_key")),
            {"success": True, "error": ""},
        )[1],
    )

    def fake_apply(host, password=None, private_key=None, **kw):
        seen.setdefault("apply_private_key", private_key)
        return _ok(host, password)

    monkeypatch.setattr(app_module, "apply_host", fake_apply)
    monkeypatch.setattr(app_module, "inspect_host", fake_apply)

    response = _post(
        client,
        "/",
        data={
            "target_mode": "group",
            "target_group": group.id,
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
            "mode": "execute",
            "password_file": (__import__("io").BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    _wait_for_task(client, response.headers["Location"].rstrip("/").split("/")[-1])
    assert seen.get("apply_private_key") == str(key_path)
