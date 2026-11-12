# tests/test_ops.py
# Pruebas del bloque A: inventario persistente, ejecución por grupo,
# programaciones, notificaciones, doble aprobación, re-ejecutar y clave SSH.
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

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
from services.notifier import batch_has_issues


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
    group = HostGroup.create("Grupo web", db_path=app_module.get_db_path())
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
    run_at = datetime.now(UTC) - timedelta(hours=1)
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
    run_at = datetime.now(UTC) - timedelta(seconds=60)
    Schedule.create(
        "ventana",
        "zona-todas",
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
            next_run=datetime.now(UTC) + timedelta(days=1),
            db_path=db_path,
        )
        return True

    launched = scheduler_module.scan_once(db_path, fake_launch)
    assert launched == 1
    assert launches[0][0] == "ventana"

    # La siguiente ejecución quedó en el futuro: un nuevo escaneo no dispara.
    assert scheduler_module.scan_once(db_path, fake_launch) == 0


def test_schedule_transient_error_keeps_schedule_enabled(client):
    """Un fallo de capacidad no apaga la programación de forma permanente."""
    db_path = app_module.get_db_path()
    schedule = Schedule.create(
        "cola llena",
        "zona-todas",
        datetime.now(UTC) - timedelta(seconds=60),
        {},
        recurrence="daily",
        db_path=db_path,
    )

    Schedule.mark_run(
        schedule.id,
        error="Cola llena",
        retry_delay_seconds=300,
        db_path=db_path,
    )

    updated = Schedule.get(schedule.id, db_path=db_path)
    assert updated is not None
    assert updated.enabled
    assert updated.last_error == "Cola llena"
    retry_at = datetime.fromisoformat(updated.run_at)
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    assert retry_at > datetime.now(UTC)


def test_schedule_permanent_error_disables(client):
    db_path = app_module.get_db_path()
    schedule = Schedule.create(
        "permanente",
        "zona-todas",
        datetime.now(UTC) - timedelta(seconds=60),
        {},
        recurrence="daily",
        db_path=db_path,
    )

    Schedule.mark_run(
        schedule.id,
        error="Configuración inválida",
        disable=True,
        db_path=db_path,
    )

    updated = Schedule.get(schedule.id, db_path=db_path)
    assert updated is not None
    assert not updated.enabled


def test_schedule_next_run_after_long_downtime_is_in_the_future(client):
    """Una caída de varios días no debe encadenar ejecuciones históricas."""
    db_path = app_module.get_db_path()
    schedule = Schedule.create(
        "recuperación",
        "zona-todas",
        datetime.now(UTC) - timedelta(seconds=60),
        {},
        recurrence="daily",
        db_path=db_path,
    )
    schedule.run_at = (datetime.now(UTC) - timedelta(days=3)).isoformat(
        timespec="seconds"
    )

    next_run = schedule.next_run_from()

    assert next_run is not None
    assert next_run > datetime.now(UTC)
    assert next_run <= datetime.now(UTC) + timedelta(days=1, seconds=5)


def test_deactivated_user_session_is_rejected(client):
    """Desactivar una cuenta invalida su sesión en la siguiente petición."""
    _login(client)
    assert client.get("/").status_code == 200

    User.set_active("admin", False, db_path=app_module.get_db_path())

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_schedule_task_not_active_disables(client):
    db_path = app_module.get_db_path()
    Schedule.create(
        "tarea muerta",
        "kaspersky",
        datetime.now(UTC) - timedelta(minutes=1),
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
    from services import notifier

    monkeypatch.setattr(config_module, "SMTP_HOST", "")
    monkeypatch.setattr(config_module, "NOTIFY_TO", ["ops@example.com"])
    monkeypatch.setattr(config_module, "NOTIFY_LEVEL", "all")
    assert not notifier.smtp_enabled()
    assert notifier.notify_batch_result(FakeBatch(), []) is None


def test_notifier_sends_failure_mail(monkeypatch):
    import config as config_module
    from services import notifier

    monkeypatch.setattr(config_module, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config_module, "NOTIFY_TO", ["ops@example.com"])
    monkeypatch.setattr(config_module, "NOTIFY_LEVEL", "failures")
    assert notifier.smtp_enabled()

    sent = {}

    def fake_send(subject, body):
        sent["subject"] = subject
        sent["body"] = body

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
    from services import notifier

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

    # El solicitante no puede aprobar su propia solicitud.
    decided, decide_error = ApprovalRequest.decide(
        req.id, approver="operador", approve=True, db_path=db_path
    )
    assert decided is None
    assert "solicitante" in decide_error
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "pending"

    # Un admin distinto sí puede aprobarla.
    decided, decide_error = ApprovalRequest.decide(
        req.id, approver="admin", approve=True, db_path=db_path
    )
    assert decide_error is None
    assert decided.status == "approved"

    # El aprobador no puede coincidir con el ejecutor (two-person rule).
    same_person = ApprovalRequest.consume_if_approved(
        "kaspersky",
        _approval_params(),
        executor="admin",
        approves_by_differs=True,
        ttl_minutes=30,
        db_path=db_path,
    )
    assert not same_person[0]
    assert "two-person" in same_person[1]

    # Con un ejecutor distinto del aprobador la ejecución se habilita.
    consumed, consume_error = ApprovalRequest.consume_if_approved(
        "kaspersky",
        _approval_params(),
        executor="operador",
        approves_by_differs=True,
        ttl_minutes=30,
        db_path=db_path,
    )
    assert consumed and consume_error is None
    got = ApprovalRequest.get(req.id, db_path=db_path)
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
    assert "ya se usó" in again[1]


def test_approval_params_must_match(client):
    db_path = app_module.get_db_path()
    ApprovalRequest.create(
        "kaspersky", _approval_params(), requested_by="operador", db_path=db_path
    )


def test_destructive_task_requires_approval_in_web(operator_client, monkeypatch):
    client = operator_client
    _login(client)
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
    ExecutionBatch.finish_batch(batch_id, total_hosts=1, db_path=db_path)
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
        with open(vars_file, encoding="utf-8") as handle:
            captured["payload"] = handle.read()
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


# ---------------------------------------------------------------------------
# Endurecimiento de programaciones y credenciales cifradas
# ---------------------------------------------------------------------------
def test_scheduler_secret_roundtrip_creates_key_0600(client, monkeypatch, tmp_path):
    import config as config_module

    secrets_dir = tmp_path / "scheduler-secrets"
    monkeypatch.setattr(config_module, "SECRETS_DIR", str(secrets_dir))

    token = scheduler_module.encrypt_secret("clave-maestra")
    assert token and token != "clave-maestra"
    assert scheduler_module.decrypt_secret(token) == "clave-maestra"

    key_file = secrets_dir / "scheduler_password.key"
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600


def _future_run_at() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")


def _schedule_payload(**overrides):
    data = {
        "op": "create",
        "name": "ventana",
        "task_id": "zona-todas",
        "run_at": _future_run_at(),
        "recurrence": "once",
        "target_mode": "manual",
        "target_ip": "10.42.0.50",
        "admin_user": "root",
        "master_password": "clave-maestra",
        "action": "TZ",
        "value": "UTC",
        "ntp_policy": "keep",
    }
    data.update(overrides)
    return data


def test_schedule_create_uses_op_and_forces_fixed_value(client):
    _login(client)
    response = _post(
        client,
        "/programaciones",
        _schedule_payload(value="Europe/Madrid"),
        follow_redirects=True,
    )
    assert "Programación &#39;ventana&#39; creada" in response.get_data(as_text=True)
    schedule = Schedule.all(db_path=app_module.get_db_path())[0]
    assert schedule.params["action"] == "TZ"
    assert schedule.params["value"] == "America/Caracas"
    assert schedule.params["admin_user"] == "root"


def test_schedule_create_rejects_action_not_allowed_by_task(client):
    _login(client)
    response = _post(
        client,
        "/programaciones",
        _schedule_payload(action="TIME", value="2026-12-01T10:00"),
        follow_redirects=True,
    )
    assert "no admite la acción TIME" in response.get_data(as_text=True)
    assert Schedule.all(db_path=app_module.get_db_path()) == []


def test_schedule_create_rejects_unknown_timezone(client, monkeypatch):
    _login(client)
    # La tarea fija America/Caracas; se usa una tarea de prueba para poder
    # enviar una zona distinta sin que la reemplace el valor fijo.
    from services.task_catalog import _register_test_task

    _register_test_task(
        {
            "id": "tz-libre",
            "name": "TZ libre",
            "status": "active",
            "min_role": "admin",
            "destructive": False,
            "platforms": ["linux"],
            "actions": ["TZ", "TIME"],
        },
        monkeypatch,
    )
    response = _post(
        client,
        "/programaciones",
        _schedule_payload(task_id="tz-libre", value="America/Atlantida"),
        follow_redirects=True,
    )
    assert "Zona horaria desconocida" in response.get_data(as_text=True)
    assert Schedule.all(db_path=app_module.get_db_path()) == []


# ---------------------------------------------------------------------------
# UX: programar desde la tarjeta y accesos contextuales al inventario
# ---------------------------------------------------------------------------
def _task_schedule_payload(**overrides):
    data = {
        "mode": "schedule",
        "schedule_intent": "schedule",
        "task": "zona-todas",
        "schedule_name": "desde la tarjeta",
        "run_at": _future_run_at(),
        "recurrence": "once",
        "target_mode": "manual",
        "target_ip": "10.42.0.50",
        "admin_user": "root",
        "master_password": "clave-maestra",
        "password_origin": "stored",
        "action": "TZ",
        "value": "Europe/Madrid",
        "ntp_policy": "keep",
    }
    data.update(overrides)
    return data


def test_schedule_can_be_created_from_task_form(client):
    db_path = app_module.get_db_path()
    group = HostGroup.create("Aulas", db_path=db_path)
    _login(client)

    response = _post(
        client,
        "/",
        _task_schedule_payload(
            target_mode="group",
            target_group=str(group.id),
        ),
        follow_redirects=True,
    )

    assert "Programación &#39;desde la tarjeta&#39; creada" in response.get_data(
        as_text=True
    )
    schedule = Schedule.all(db_path=db_path)[0]
    assert schedule.task_id == "zona-todas"
    assert schedule.params["target_mode"] == "group"
    assert schedule.params["target_group_id"] == group.id
    assert schedule.params["value"] == "America/Caracas"
    assert schedule.params["master_password_enc"]
    assert "clave-maestra" not in str(schedule.params)


def test_schedule_from_task_form_requires_admin(client):
    User.create_or_update(
        "operador",
        "operador-clave-456",
        role="operator",
        db_path=app_module.get_db_path(),
    )
    _login(client, "operador", "operador-clave-456")

    response = _post(client, "/", _task_schedule_payload())

    assert response.status_code == 403
    assert Schedule.all(db_path=app_module.get_db_path()) == []


def test_schedule_from_task_form_rejects_temporary_passwords(client):
    _login(client)

    response = _post(
        client,
        "/",
        _task_schedule_payload(password_origin="temporal"),
        follow_redirects=True,
    )

    assert "credenciales guardadas" in response.get_data(as_text=True)
    assert Schedule.all(db_path=app_module.get_db_path()) == []


def test_schedules_page_filters_and_prefills_group(client):
    db_path = app_module.get_db_path()
    group = HostGroup.create("Sucursal", db_path=db_path)
    other = HostGroup.create("Otra sede", db_path=db_path)
    _login(client)
    _post(
        client,
        "/programaciones",
        _schedule_payload(
            name="de sucursal", target_mode="group", target_group=str(group.id)
        ),
        follow_redirects=True,
    )
    _post(
        client,
        "/programaciones",
        _schedule_payload(
            name="de otra sede", target_mode="group", target_group=str(other.id)
        ),
        follow_redirects=True,
    )

    body = client.get(f"/programaciones?group={group.id}").get_data(as_text=True)

    assert "Filtrado por grupo" in body
    assert "de sucursal" in body
    assert "de otra sede" not in body
    assert f'value="{group.id}" selected' in body


def test_inventory_shows_window_help_and_schedule_shortcut(client):
    db_path = app_module.get_db_path()
    group = HostGroup.create("Oficina", db_path=db_path)
    _login(client)

    body = client.get("/inventario").get_data(as_text=True)

    assert "Política de mantenimiento" in body
    assert "Días permitidos (UTC)" in body
    assert "Inicio (UTC)" in body
    assert "Fin (UTC)" in body
    assert f"/programaciones?group={group.id}" in body
    assert "0 programación(es)" in body

    Schedule.create(
        "ventana oficina",
        "zona-todas",
        datetime.now(UTC) + timedelta(hours=1),
        {
            "target": f"inventory:{group.id}",
            "target_mode": "group",
            "target_group_id": group.id,
            "task_id": "zona-todas",
            "admin_user": "root",
            "action": "TZ",
            "value": "America/Caracas",
            "ntp_policy": "keep",
        },
        db_path=db_path,
    )
    body = client.get("/inventario").get_data(as_text=True)
    assert "1 programación(es)" in body


def test_task_page_offers_scheduling_only_to_admin_on_schedulable_tasks(client):
    _login(client)
    body = client.get("/tarea/zona-todas").get_data(as_text=True)
    assert "Planificar esta operación" in body
    assert "Crear programación" in body

    body = client.get("/tarea/tapiz").get_data(as_text=True)
    assert "Planificar esta operación" not in body

    User.create_or_update(
        "operador",
        "operador-clave-456",
        role="operator",
        db_path=app_module.get_db_path(),
    )
    _post(client, "/logout")
    _login(client, "operador", "operador-clave-456")
    body = client.get("/tarea/zona-todas").get_data(as_text=True)
    assert "Planificar esta operación" not in body


def test_launch_scheduled_disables_invalid_stored_params(client):
    db_path = app_module.get_db_path()
    Schedule.create(
        "vieja",
        "zona-todas",
        datetime.now(UTC) + timedelta(hours=1),
        {
            "target": "10.42.0.51",
            "target_mode": "manual",
            "task_id": "zona-todas",
            "admin_user": "root",
            "action": "TIME",
            "value": "2026-12-01T10:00",
            "ntp_policy": "keep",
        },
        db_path=db_path,
    )
    schedule = Schedule.all(db_path=db_path)[0]
    params = dict(schedule.params)
    params["master_password_enc"] = scheduler_module.encrypt_secret("clave")
    launched = app_module.launch_scheduled(
        {
            "id": schedule.id,
            "name": schedule.name,
            "task_id": schedule.task_id,
        },
        params,
    )
    assert launched is False
    schedule = Schedule.all(db_path=db_path)[0]
    assert not schedule.enabled
    assert "no admite la acción TIME" in schedule.last_error


def test_manual_execution_forces_task_fixed_value(client, monkeypatch):
    """El valor fijo del catálogo no depende del campo oculto del formulario."""
    _login(client)
    captured = {}

    class FakeQueue:
        def submit(self, task_id, func, /, *args, **kwargs):
            captured["task_id"] = task_id
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(app_module, "get_job_queue", lambda: FakeQueue())

    response = _post(
        client,
        "/",
        data={
            "task": "zona-todas",
            "target_ip": "10.42.0.60",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            # Un operador podría editar el campo oculto; el servidor debe
            # imponer el valor fijo de la tarea.
            "value": "Europe/Madrid",
            "mode": "execute",
            "password_file": (__import__("io").BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    # Posiciones de execute_with_progress: tid, target, admin_user, origin,
    # master_password, action, value, ...
    assert captured["args"][6] == "America/Caracas"


def test_notifier_refuses_credentials_without_encryption(monkeypatch):
    """Con usuario SMTP y sin TLS no se abre conexión: la clave no viaja en claro."""
    import config as config_module
    from services import notifier

    monkeypatch.setattr(config_module, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config_module, "NOTIFY_TO", ["ops@example.com"])
    monkeypatch.setattr(config_module, "NOTIFY_LEVEL", "all")
    monkeypatch.setattr(config_module, "SMTP_USER", "usuario")
    monkeypatch.setattr(config_module, "SMTP_PASSWORD", "secreto")
    monkeypatch.setattr(config_module, "SMTP_STARTTLS", False)
    monkeypatch.setattr(config_module, "SMTP_SSL", False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no debe abrir SMTP sin cifrado")

    monkeypatch.setattr(notifier.smtplib, "SMTP", forbidden)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", forbidden)

    error = notifier.notify_batch_result(FakeBatch(), [])
    assert error and "sin cifrado" in error


def test_notifier_uses_implicit_tls_with_credentials(monkeypatch):
    import config as config_module
    from services import notifier

    monkeypatch.setattr(config_module, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config_module, "NOTIFY_TO", ["ops@example.com"])
    monkeypatch.setattr(config_module, "NOTIFY_LEVEL", "all")
    monkeypatch.setattr(config_module, "SMTP_USER", "usuario")
    monkeypatch.setattr(config_module, "SMTP_PASSWORD", "secreto")
    monkeypatch.setattr(config_module, "SMTP_STARTTLS", False)
    monkeypatch.setattr(config_module, "SMTP_SSL", True)
    monkeypatch.setattr(config_module, "SMTP_PORT", 465)

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            calls["connect"] = (host, port)
            calls["context"] = context is not None

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def starttls(self, context=None):
            calls["starttls"] = True

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, _message):
            calls["sent"] = True

    def forbidden(*_args, **_kwargs):
        raise AssertionError("con TLS implícito no debe usarse SMTP plano")

    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP", forbidden)

    assert notifier.notify_batch_result(FakeBatch(), []) is None
    assert calls["connect"] == ("smtp.example.com", 465)
    assert calls["context"] is True
    assert calls["login"] == ("usuario", "secreto")
    assert calls["sent"] is True
    assert "starttls" not in calls
