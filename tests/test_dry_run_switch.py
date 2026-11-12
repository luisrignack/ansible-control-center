# tests/test_dry_run_switch.py
# Pruebas del interruptor de simulación (DRY RUN) de la interfaz web.
from __future__ import annotations

import pytest

import app as app_module
from models.db import get_setting
from models.user import User
from services import ansible_runner, password_tester


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    User.create_or_update(
        "operator", "operator-password-123", db_path=db_path, role="operator"
    )
    User.create_or_update(
        "viewer", "viewer-password-123", db_path=db_path, role="viewer"
    )
    app_module.app.config.update(TESTING=True)
    app_module.apply_dry_run(True)
    with app_module.app.test_client() as test_client:
        yield test_client
    app_module.apply_dry_run(True)


def _csrf_token(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _login(client, username: str, password: str):
    token = _csrf_token(client)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )


def _post_mode(client, enabled: bool, token: str | None = None):
    return client.post(
        "/settings/dry-run",
        json={"enabled": enabled},
        headers={
            "X-CSRFToken": _csrf_token(client) if token is None else token,
            "X-Requested-With": "XMLHttpRequest",
        },
    )


def test_admin_can_disable_simulation(client):
    _login(client, "admin", "admin-password-123")

    response = _post_mode(client, False)

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert response.get_json()["dry_run"] is False
    assert app_module.is_dry_run() is False
    assert ansible_runner.DRY_RUN is False
    assert password_tester.DRY_RUN is False


def test_admin_can_enable_simulation_again(client):
    _login(client, "admin", "admin-password-123")
    _post_mode(client, False)

    response = _post_mode(client, True)

    assert response.status_code == 200
    assert response.get_json()["dry_run"] is True
    assert app_module.is_dry_run() is True
    assert ansible_runner.DRY_RUN is True
    assert password_tester.DRY_RUN is True


def test_switch_persists_across_restart(client):
    _login(client, "admin", "admin-password-123")
    _post_mode(client, False)

    # Simula un reinicio: el valor de entorno vuelve a True, pero la lectura
    # de SQLite debe recuperar el último estado elegido en la interfaz.
    app_module.apply_dry_run(True)
    assert app_module.load_dry_run_setting() is False

    app_module.apply_dry_run(app_module.load_dry_run_setting())
    assert app_module.is_dry_run() is False


def test_setting_is_stored_in_database(client):
    _login(client, "admin", "admin-password-123")
    _post_mode(client, False)

    assert get_setting("dry_run", db_path=app_module.get_db_path()) == "0"


def test_switch_is_blocked_while_a_task_is_queued(client):
    """El modo no puede cambiar a mitad de un lote, ni siquiera encolado."""
    _login(client, "admin", "admin-password-123")
    task_id = app_module.create_task(owner="admin")
    try:
        response = _post_mode(client, False)

        assert response.status_code == 409
        assert response.get_json()["success"] is False
        assert app_module.is_dry_run() is True
    finally:
        app_module.remove_task(task_id)


def test_operator_cannot_change_mode(client):
    _login(client, "operator", "operator-password-123")

    response = _post_mode(client, False)

    assert response.status_code == 403
    assert app_module.is_dry_run() is True


def _logout(client):
    token = _csrf_token(client)
    client.post("/logout", data={"csrf_token": token})


def test_csrf_is_required(client):
    _login(client, "admin", "admin-password-123")

    response = client.post(
        "/settings/dry-run",
        json={"enabled": False},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )

    assert response.status_code == 400
    assert app_module.is_dry_run() is True


def test_invalid_value_is_rejected(client):
    _login(client, "admin", "admin-password-123")

    response = client.post(
        "/settings/dry-run",
        json={"enabled": "quizá"},
        headers={
            "X-CSRFToken": _csrf_token(client),
            "X-Requested-With": "XMLHttpRequest",
        },
    )

    assert response.status_code == 400
    assert app_module.is_dry_run() is True


def test_form_submit_without_js_disables_simulation(client):
    """Sin JavaScript, una casilla desmarcada no envía ``enabled``."""
    _login(client, "admin", "admin-password-123")

    response = client.post(
        "/settings/dry-run",
        data={
            "csrf_token": _csrf_token(client),
            "dry_run_form": "1",
            "next": "/",
        },
    )

    assert response.status_code == 302
    assert app_module.is_dry_run() is False


def test_form_submit_without_js_enables_simulation(client):
    _login(client, "admin", "admin-password-123")
    _post_mode(client, False)

    response = client.post(
        "/settings/dry-run",
        data={
            "csrf_token": _csrf_token(client),
            "dry_run_form": "1",
            "enabled": "1",
            "next": "/",
        },
    )

    assert response.status_code == 302
    assert app_module.is_dry_run() is True


def test_form_without_marker_is_rejected(client):
    _login(client, "admin", "admin-password-123")

    response = client.post(
        "/settings/dry-run",
        data={"csrf_token": _csrf_token(client)},
    )

    assert response.status_code == 302
    assert app_module.is_dry_run() is True


def test_switch_visible_only_for_admins(client):
    _login(client, "admin", "admin-password-123")
    admin_page = client.get("/").get_data(as_text=True)
    assert 'id="dryRunToggle"' in admin_page
    assert 'role="switch"' in admin_page

    _logout(client)
    _login(client, "operator", "operator-password-123")
    operator_page = client.get("/").get_data(as_text=True)
    assert 'id="dryRunToggle"' not in operator_page
