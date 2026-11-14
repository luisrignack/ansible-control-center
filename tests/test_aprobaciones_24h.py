# tests/test_aprobaciones_24h.py
# Doble aprobación con ventana de 24 h: caducidad de la solicitud pendiente,
# validez de la aprobación, payload sin secretos y ejecución desde
# "Mis solicitudes" sin volver a rellenar el formulario.
from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
from acc import runtime
from models.db import get_connection, init_schema
from models.ops import ApprovalRequest
from models.user import User

NEW_PASSWORD = "Clave-Nueva-2026"
PARAMS = {
    "target": "10.55.0.9",
    "target_mode": "manual",
    "target_group_id": None,
    "task_id": "contrasena",
    "admin_user": "root",
    "action": "PASSWORD",
    "value": "ladmin",
    "target_user": "ladmin",
}


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_schema(path)
    return path


@pytest.fixture()
def client(db_path, monkeypatch):
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    User.create_or_update(
        "admin2", "admin2-password-123", role="admin", db_path=db_path
    )
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


def _post(client, url, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload, **kwargs)


def _login(client, username="admin", password="admin-password-123"):
    # Cambiar de usuario exige cerrar la sesión anterior; si no, el
    # aprobador seguiría siendo el solicitante.
    with client.session_transaction() as sess:
        logged = bool(sess.get("_user_id"))
    if logged:
        _post(client, "/logout")
    return _post(client, "/login", {"username": username, "password": password})


def _backdate(db_path, request_id, *, requested_at=None, decided_at=None):
    with get_connection(db_path) as conn:
        if requested_at is not None:
            conn.execute(
                "UPDATE approvals SET requested_at = ? WHERE id = ?",
                (requested_at, request_id),
            )
        if decided_at is not None:
            conn.execute(
                "UPDATE approvals SET decided_at = ? WHERE id = ?",
                (decided_at, request_id),
            )


def _old(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Caducidad
# ---------------------------------------------------------------------------
def test_solicitud_pendiente_caduca_a_las_24_horas(db_path):
    req = ApprovalRequest.create(
        "contrasena", PARAMS, requested_by="admin", db_path=db_path
    )
    assert req.status == "pending"
    assert req.requested_deadline(24).endswith("+00:00")

    _backdate(db_path, req.id, requested_at=_old(25))
    assert ApprovalRequest.expire_stale(24, None, db_path=db_path) == 1

    expired = ApprovalRequest.get(req.id, db_path=db_path)
    assert expired.status == "expired"
    assert "Caducada" in expired.note

    # Ya no se puede aprobar ni ejecutar una solicitud caducada.
    decided, error = ApprovalRequest.decide(
        req.id, approver="admin2", approve=True, db_path=db_path
    )
    assert decided is None and error
    reserved, error = ApprovalRequest.reserve_if_approved(
        "contrasena",
        PARAMS,
        executor="admin",
        approves_by_differs=True,
        ttl_minutes=1440,
        db_path=db_path,
    )
    assert reserved is None and "caduc" in error


def test_solicitud_dentro_del_plazo_sigue_pendiente(db_path):
    req = ApprovalRequest.create(
        "contrasena", PARAMS, requested_by="admin", db_path=db_path
    )
    _backdate(db_path, req.id, requested_at=_old(23))
    assert ApprovalRequest.expire_stale(24, None, db_path=db_path) == 0
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "pending"


def test_aprobacion_vale_24_horas_y_luego_caduca(db_path):
    req = ApprovalRequest.create(
        "contrasena", PARAMS, requested_by="admin", db_path=db_path
    )
    decided, error = ApprovalRequest.decide(
        req.id, approver="admin2", approve=True, db_path=db_path
    )
    assert error is None and decided.status == "approved"

    # Recién aprobada: se puede reservar y ejecutar.
    reserved, error = ApprovalRequest.reserve_if_approved(
        "contrasena",
        PARAMS,
        executor="admin",
        approves_by_differs=True,
        ttl_minutes=1440,
        db_path=db_path,
    )
    assert reserved == req.id and error is None
    ApprovalRequest.release_reserved(req.id, db_path=db_path)

    # Pasadas 24 h deja de ser válida y la lista la marca como caducada.
    _backdate(db_path, req.id, decided_at=_old(25))
    reserved, error = ApprovalRequest.reserve_if_approved(
        "contrasena",
        PARAMS,
        executor="admin",
        approves_by_differs=True,
        ttl_minutes=1440,
        db_path=db_path,
    )
    assert reserved is None and "expirado" in error.lower()
    assert ApprovalRequest.expire_stale(24, 1440, db_path=db_path) == 1
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "expired"


# ---------------------------------------------------------------------------
# Payload de ejecución
# ---------------------------------------------------------------------------
def test_payload_guarda_el_formulario_sin_secretos():
    payload = app_module._approval_payload(
        {
            "task": "contrasena",
            "target_ip": "10.0.0.5",
            "admin_user": "root",
            "action": "PASSWORD",
            "value": "ladmin",
            "mode": "execute",
            "master_password": "maestra",
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
            "csrf_token": "token",
            "operation_id": "abc123",
        }
    )
    assert payload["target_ip"] == "10.0.0.5"
    assert payload["value"] == "ladmin"
    for secret in (
        "master_password",
        "new_password",
        "new_password_confirm",
        "csrf_token",
        "operation_id",
    ):
        assert secret not in payload


# ---------------------------------------------------------------------------
# Flujo HTTP completo: solicitud → aprobación → ejecución desde la web
# ---------------------------------------------------------------------------
def _request_password_change(client):
    return _post(
        client,
        "/tareas",
        data={
            "task": "contrasena",
            "target_ip": "10.55.0.9",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "PASSWORD",
            "value": "ladmin",
            "target_user": "ladmin",
            "mode": "execute",
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
            "password_file": (io.BytesIO(b"clave-temporal\n"), "p.txt"),
        },
        content_type="multipart/form-data",
    )


def test_flujo_completo_aprobacion_y_ejecucion(client, db_path, monkeypatch):
    _login(client)
    response = _request_password_change(client)
    assert response.status_code == 302

    pending = ApprovalRequest.list_status("pending", db_path=db_path)
    assert len(pending) == 1
    req = pending[0]
    assert req.requested_by == "admin"
    assert req.payload["target_ip"] == "10.55.0.9"
    assert "new_password" not in req.payload

    # El solicitante ve su solicitud pendiente y no puede ejecutarla aún.
    page = client.get("/solicitudes", query_string={"tab": "mias"})
    assert page.status_code == 200
    assert "pendiente de aprobación" in page.get_data(as_text=True)
    blocked = client.get(f"/mis-solicitudes/{req.id}/ejecutar")
    assert blocked.status_code == 302

    # Otro admin la aprueba desde la bandeja.
    _login(client, "admin2", "admin2-password-123")
    inbox = client.get("/solicitudes", query_string={"tab": "aprobaciones"})
    assert inbox.status_code == 200
    body = inbox.get_data(as_text=True)
    assert "Caduca" in body
    assert f"<code>{req.id}</code>" in body
    approve = _post(
        client,
        "/aprobaciones",
        {"action": "approve", "request_id": str(req.id)},
    )
    assert approve.status_code == 302
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "approved"

    # El aprobador no puede ejecutarla: no es su solicitud.
    assert client.get(f"/mis-solicitudes/{req.id}/ejecutar").status_code == 302

    # El solicitante la ejecuta desde Mis solicitudes sin rellenar nada más.
    captured = {}

    class FakeQueue:
        def submit(self, task_id, func, /, *args, **kwargs):
            captured["task_id"] = task_id
            captured["args"] = args
            captured["kwargs"] = kwargs
            # Sin cola real la tarea quedaría "en curso" para el resto de la
            # suite y bloquearía, por ejemplo, el cambio de modo simulación.
            app_module.update_task(task_id, {"status": "success", "completed": True})

    monkeypatch.setattr(runtime, "get_job_queue", lambda: FakeQueue())
    _login(client)
    confirm = client.get(f"/mis-solicitudes/{req.id}/ejecutar")
    assert confirm.status_code == 200
    assert "aprobada" in confirm.get_data(as_text=True)

    run = _post(
        client,
        f"/mis-solicitudes/{req.id}/ejecutar",
        {
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
            "password_file": (io.BytesIO(b"clave-temporal\n"), "p.txt"),
        },
        content_type="multipart/form-data",
    )
    assert run.status_code == 302
    assert captured["task_id"]
    assert captured["kwargs"]["new_password"] == NEW_PASSWORD
    # Posiciones de execute_with_progress: tid, target, admin_user, origin,
    # master_password, action, value.
    assert captured["args"][5] == "PASSWORD"
    assert captured["args"][6] == "ladmin"
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "consumed"

    # Un solo uso: la solicitud ya no se puede volver a ejecutar.
    assert client.get(f"/mis-solicitudes/{req.id}/ejecutar").status_code == 302


def test_solicitud_antigua_sin_payload_no_se_ejecuta_directo(client, db_path):
    """Las aprobaciones creadas antes de guardar payload exigen reenviar el
    formulario, pero siguen siendo válidas y no se consumen."""
    req = ApprovalRequest.create(
        "contrasena", PARAMS, requested_by="admin", db_path=db_path
    )
    ApprovalRequest.decide(req.id, approver="admin2", approve=True, db_path=db_path)
    _login(client)
    assert client.get(f"/mis-solicitudes/{req.id}/ejecutar").status_code == 302
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "approved"


def test_ejecucion_requiere_aprobacion_ajena(client, db_path):
    _login(client)
    _request_password_change(client)
    req = ApprovalRequest.list_status("pending", db_path=db_path)[0]
    # Sin aprobación no hay ejecución posible.
    response = _post(
        client,
        f"/mis-solicitudes/{req.id}/ejecutar",
        {"new_password": NEW_PASSWORD},
    )
    assert response.status_code == 302
    assert ApprovalRequest.get(req.id, db_path=db_path).status == "pending"
