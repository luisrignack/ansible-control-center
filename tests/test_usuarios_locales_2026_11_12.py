"""Gestión de usuarios locales desde /usuarios.

Cubre el alta con contraseña propia, el cambio de contraseña, la eliminación
con transferencia de datos y la separación respecto a las cuentas LDAPS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
from acc import runtime
from models.ops import HostGroup, Schedule
from models.user import AuthEvent, User

PASSWORD = "clave-local-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "usuarios.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
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


def _post(client, url, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload, **kwargs)


def _login(client, username: str, password: str):
    return _post(client, "/login", {"username": username, "password": password})


def _login_admin(client) -> None:
    assert _login(client, "admin", "admin-password-123").status_code == 302


def _logout(client) -> None:
    _post(client, "/logout")


def _create_local(client, username: str, role: str = "operator", password=PASSWORD):
    return _post(
        client,
        "/usuarios/crear",
        {
            "username": username,
            "password": password,
            "password_confirm": password,
            "role": role,
            "active": "1",
        },
        follow_redirects=True,
    )


def test_admin_creates_local_user_and_it_can_log_in(client):
    _login_admin(client)
    response = _create_local(client, "maria.local")
    body = response.get_data(as_text=True)
    assert "maria.local" in body
    assert "creado con rol" in body

    user = User.get_by_username("maria.local", db_path=runtime.get_db_path())
    assert user is not None
    assert user.auth_source == "local"
    assert user.role == "operator"
    assert user.is_active
    assert user.check_password(PASSWORD)

    _logout(client)
    response = _login(client, "maria.local", PASSWORD)
    assert response.status_code == 302
    assert client.get("/inventario").status_code == 200


def test_non_admin_cannot_manage_local_users(client):
    _login_admin(client)
    _create_local(client, "operador.local")
    _logout(client)
    assert _login(client, "operador.local", PASSWORD).status_code == 302

    assert _post(client, "/usuarios/crear", {}).status_code == 403
    assert _post(client, "/usuarios/password", {}).status_code == 403
    assert _post(client, "/usuarios/eliminar", {}).status_code == 403


def test_create_rejects_duplicate_username_including_ldap_shadow(client):
    User.create_or_update(
        "jose.cu",
        "",
        role="viewer",
        auth_source="ldaps",
        db_path=runtime.get_db_path(),
    )
    _login_admin(client)
    response = _create_local(client, "jose.cu")
    body = response.get_data(as_text=True)
    assert "ya existe" in body
    shadow = User.get_by_username("jose.cu", db_path=runtime.get_db_path())
    assert shadow is not None
    assert shadow.auth_source == "ldaps"

    _create_local(client, "ana.local")
    response = _create_local(client, "ana.local")
    assert "ya existe" in response.get_data(as_text=True)


def test_create_rejects_short_or_mismatched_password(client):
    _login_admin(client)
    response = _post(
        client,
        "/usuarios/crear",
        {
            "username": "corta.local",
            "password": "corta",
            "password_confirm": "corta",
            "role": "viewer",
            "active": "1",
        },
        follow_redirects=True,
    )
    assert "al menos" in response.get_data(as_text=True)
    assert User.get_by_username("corta.local", db_path=runtime.get_db_path()) is None

    response = _post(
        client,
        "/usuarios/crear",
        {
            "username": "desajuste.local",
            "password": PASSWORD,
            "password_confirm": PASSWORD + "x",
            "role": "viewer",
            "active": "1",
        },
        follow_redirects=True,
    )
    assert "no coinciden" in response.get_data(as_text=True)


def test_create_rejects_invalid_username(client):
    _login_admin(client)
    response = _post(
        client,
        "/usuarios/crear",
        {
            "username": "nombre con espacios",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "role": "viewer",
            "active": "1",
        },
        follow_redirects=True,
    )
    assert "sin espacios" in response.get_data(as_text=True)
    assert (
        User.get_by_username("nombre con espacios", db_path=runtime.get_db_path())
        is None
    )


def test_admin_changes_local_password_and_clears_lock(client):
    _login_admin(client)
    _create_local(client, "carlos.local")
    user = User.get_by_username("carlos.local", db_path=runtime.get_db_path())
    assert user is not None
    user.register_failure(
        1,
        10,
        db_path=runtime.get_db_path(),
    )
    assert User.get_by_username(
        "carlos.local", db_path=runtime.get_db_path()
    ).is_locked

    new_password = "otra-clave-456"
    response = _post(
        client,
        "/usuarios/password",
        {
            "username": "carlos.local",
            "password": new_password,
            "password_confirm": new_password,
        },
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "carlos.local" in body
    assert "actualizada" in body

    _logout(client)
    assert _login(client, "carlos.local", PASSWORD).status_code == 200
    assert _login(client, "carlos.local", new_password).status_code == 302

    events = AuthEvent.recent(limit=20, db_path=runtime.get_db_path())
    assert any(event["event"] == "password_changed" for event in events)


def test_ldap_password_cannot_be_changed_locally(client):
    User.create_or_update(
        "ldap.user",
        "",
        role="operator",
        auth_source="ldaps",
        db_path=runtime.get_db_path(),
    )
    _login_admin(client)
    response = _post(
        client,
        "/usuarios/password",
        {
            "username": "ldap.user",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
        },
        follow_redirects=True,
    )
    assert "directorio" in response.get_data(as_text=True)
    shadow = User.get_by_username("ldap.user", db_path=runtime.get_db_path())
    assert shadow is not None
    assert shadow.password_hash == ""


def test_delete_local_user_transfers_owned_data_to_admin(client):
    _login_admin(client)
    _create_local(client, "temporal.local")
    user = User.get_by_username("temporal.local", db_path=runtime.get_db_path())
    assert user is not None

    db_path = runtime.get_db_path()
    group = HostGroup.create(
        "Grupo de temporal",
        owner_user_id=user.id,
        db_path=db_path,
    )
    schedule = Schedule.create(
        "Programación de temporal",
        "zona-todas",
        datetime.now(UTC) + timedelta(hours=1),
        {},
        created_by="temporal.local",
        owner_user_id=user.id,
        db_path=db_path,
    )

    response = _post(
        client,
        "/usuarios/eliminar",
        {"username": "temporal.local", "confirm_username": "temporal.local"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "eliminado" in body
    assert "Se transfirieron" in body
    assert User.get_by_username("temporal.local", db_path=db_path) is None

    admin = User.get_by_username("admin", db_path=db_path)
    assert admin is not None
    moved_group = HostGroup.get(group.id, db_path=db_path)
    moved_schedule = Schedule.get(schedule.id, db_path=db_path)
    assert moved_group is not None and moved_group.owner_user_id == admin.id
    assert moved_schedule is not None and moved_schedule.owner_user_id == admin.id


def test_delete_requires_exact_confirmation(client):
    _login_admin(client)
    _create_local(client, "confirmar.local")
    response = _post(
        client,
        "/usuarios/eliminar",
        {"username": "confirmar.local", "confirm_username": "otro"},
        follow_redirects=True,
    )
    assert "escribe su nombre exacto" in response.get_data(as_text=True)
    assert (
        User.get_by_username("confirmar.local", db_path=runtime.get_db_path())
        is not None
    )


def test_admin_cannot_delete_own_account(client):
    _login_admin(client)
    response = _post(
        client,
        "/usuarios/eliminar",
        {"username": "admin", "confirm_username": "admin"},
        follow_redirects=True,
    )
    assert "No puedes eliminar tu propia cuenta" in response.get_data(as_text=True)
    assert User.get_by_username("admin", db_path=runtime.get_db_path()) is not None


def test_last_local_admin_is_protected_even_for_ldap_admin(client):
    """Un admin LDAPS no puede dejar el sistema sin administración local."""
    db_path = runtime.get_db_path()
    ldap_admin = User.create_or_update(
        "ldap.admin",
        "",
        role="admin",
        auth_source="ldaps",
        db_path=db_path,
    )
    # Se inicia sesión como admin local para obtener el identificador de
    # sesión que exige ``session_protection='strong'`` y luego se cambia el
    # usuario a la sombra LDAPS con rol admin.
    _login_admin(client)
    with client.session_transaction() as sess:
        sess["_user_id"] = str(ldap_admin.id)
        sess["_fresh"] = True

    response = _post(
        client,
        "/usuarios/eliminar",
        {"username": "admin", "confirm_username": "admin"},
        follow_redirects=True,
    )
    assert "último administrador local activo" in response.get_data(as_text=True)
    assert User.get_by_username("admin", db_path=db_path) is not None
