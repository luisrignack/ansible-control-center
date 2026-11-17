# tests/test_login_cuenta_desactivada_2026_11_16.py
# Una cuenta desactivada no es una credencial incorrecta.
#
# Antes, cada intento de acceso con una cuenta desactivada sumaba un fallo y
# podía llegar a bloquearla, ensuciando la auditoría con ``account_locked``.
# Ahora el intento se registra como ``login_disabled`` y no altera el contador.
from __future__ import annotations

import pytest

import app as app_module
from acc import runtime
from models.db import init_schema
from models.user import AuthEvent, User
from services.rate_limiter import LIMITER

PASSWORD = "clave-de-prueba-123"


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    db_path = str(tmp_path / "login.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    init_schema(db_path)
    User.create_or_update("admin", PASSWORD, db_path=db_path, role="admin")
    User.create_or_update("alice", PASSWORD, db_path=db_path, role="operator")
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        yield {"db_path": db_path, "client": client}


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token or ""


def test_cuenta_desactivada_no_acumula_intentos_fallidos(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    User.create_or_update(
        "inactiva", PASSWORD, db_path=db_path, role="operator", is_active=False
    )
    LIMITER.reset("login:127.0.0.1")

    for _ in range(2):
        token = _csrf(client)
        response = client.post(
            "/login",
            data={
                "username": "inactiva",
                "password": "clave-equivocada",
                "csrf_token": token,
            },
        )
        assert response.status_code in (200, 302)

    cuenta = User.get_by_username("inactiva", db_path=db_path)
    assert cuenta is not None
    assert cuenta.failed_attempts == 0
    assert cuenta.locked_until is None

    eventos = [
        event["event"]
        for event in AuthEvent.recent(limit=20, db_path=db_path)
        if event["username"] == "inactiva"
    ]
    assert "login_disabled" in eventos
    assert "login_failure" not in eventos


def test_cuenta_activa_sigue_contando_intentos(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    LIMITER.reset("login:127.0.0.1")

    token = _csrf(client)
    client.post(
        "/login",
        data={"username": "alice", "password": "otra", "csrf_token": token},
    )

    cuenta = User.get_by_username("alice", db_path=db_path)
    assert cuenta is not None
    assert cuenta.failed_attempts == 1
