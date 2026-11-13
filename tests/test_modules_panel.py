# tests/test_modules_panel.py
# Preferencias del panel: ocultar/restaurar tarjetas y pestañas de Configuración.
from __future__ import annotations

import pytest

import app as app_module
from acc import runtime
from models.user import User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "modules.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path, role="admin")
    User.create_or_update(
        "operador", "operador-password-123", db_path=db_path, role="operator"
    )
    User.create_or_update(
        "viewer", "viewer-password-123", db_path=db_path, role="viewer"
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


def _post(client, url):
    return client.post(url, data={"csrf_token": _csrf(client)})


def _hide(client, task_id="kaspersky"):
    return _post(client, f"/modulos/{task_id}/ocultar")


def _show(client, task_id="kaspersky"):
    return _post(client, f"/modulos/{task_id}/mostrar")


HIDE_KASPERSKY = b'action="/modulos/kaspersky/ocultar"'
SHOW_KASPERSKY = b'action="/modulos/kaspersky/mostrar"'


def test_hide_and_restore_moves_card_between_views(client):
    _login(client)
    assert HIDE_KASPERSKY in client.get("/").data

    response = _hide(client)
    assert response.status_code == 302
    panel = client.get("/").data
    assert HIDE_KASPERSKY not in panel
    assert "Módulos".encode() in panel

    parked = client.get("/modulos").data
    assert SHOW_KASPERSKY in parked

    response = _show(client)
    assert response.status_code == 302
    assert HIDE_KASPERSKY in client.get("/").data
    assert SHOW_KASPERSKY not in client.get("/modulos").data


def test_hidden_cards_are_per_user(client):
    _login(client, "operador", "operador-password-123")
    _hide(client, "tapiz")
    assert b'action="/modulos/tapiz/ocultar"' not in client.get("/").data

    _login(client, "viewer", "viewer-password-123")
    assert b'action="/modulos/tapiz/ocultar"' in client.get("/").data
    assert b'action="/modulos/tapiz/mostrar"' not in client.get("/modulos").data


def test_hidden_card_still_opens_by_url(client):
    """Ocultar es cosmético: la tarea sigue accesible y no cambia permisos."""
    _login(client)
    _hide(client, "tapiz")
    response = client.get("/tarea/tapiz")
    assert response.status_code == 200


def test_hide_rejects_unknown_task(client):
    _login(client)
    assert _hide(client, "no-existe").status_code == 404
    assert _show(client, "no-existe").status_code == 404


def test_hide_requires_csrf(client):
    _login(client)
    response = client.post("/modulos/kaspersky/ocultar")
    assert response.status_code == 400


def test_restore_all_returns_every_card(client):
    _login(client)
    _hide(client, "kaspersky")
    _hide(client, "tapiz")
    assert b'action="/modulos/kaspersky/ocultar"' not in client.get("/").data

    response = _post(client, "/modulos/restaurar")
    assert response.status_code == 302
    panel = client.get("/").data
    assert b'action="/modulos/kaspersky/ocultar"' in panel
    assert b'action="/modulos/tapiz/ocultar"' in panel
    assert b"No hay m" in client.get("/modulos").data


def test_configuration_tabs_respect_roles(client):
    _login(client, "operador", "operador-password-123")
    body = client.get("/inventario").data
    assert b"config-tabs" in body
    assert b"/usuarios" not in body
    assert b"/aprobaciones" not in body

    _login(client)
    body = client.get("/inventario").data
    assert b"/usuarios" in body
    assert b"/aprobaciones" in body
    assert b"/modulos" in body


def test_configuration_entry_redirects_to_inventory(client):
    _login(client)
    response = client.get("/configuracion")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/inventario")


def test_feature_flag_restores_previous_ui(client, monkeypatch):
    monkeypatch.setattr(runtime, "UI_CONFIG_PANEL", False)
    _login(client)

    panel = client.get("/").data
    assert b"task-hide" not in panel
    assert client.get("/modulos").status_code == 404
    assert client.get("/configuracion").status_code == 404

    body = client.get("/inventario").data
    assert b"config-tabs" not in body
    # El menú anterior sigue mostrando las categorías sueltas.
    assert b"Credenciales" in body
    assert b"Programaciones" in body
