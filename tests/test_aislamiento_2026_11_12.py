"""Aislamiento de configuraciones por usuario.

Cubre la propiedad de programaciones e inventario, la vista global de
administración y la política del vault compartido: todos lo usan, solo un
administrador lo modifica.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
from acc import runtime
from models.ops import HostGroup, Schedule
from models.user import User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "aislamiento.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    for username, role in (
        ("admin-a", "admin"),
        ("admin-b", "admin"),
        ("operador", "operator"),
        ("viewer", "viewer"),
    ):
        User.create_or_update(
            username,
            f"{username}-password-123",
            db_path=db_path,
            role=role,
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


def _login(client, username: str) -> None:
    with client.session_transaction() as sess:
        sess.clear()
    response = _post(
        client,
        "/login",
        {"username": username, "password": f"{username}-password-123"},
    )
    assert response.status_code == 302


def _user_id(username: str) -> int:
    user = User.get_by_username(username, db_path=runtime.get_db_path())
    assert user is not None
    return int(user.id)


def test_inventory_is_private_and_admin_can_open_global_view(client):
    _login(client, "admin-a")
    response = _post(
        client,
        "/inventario",
        {"action": "create_group", "name": "Grupo privado A"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert "Grupo privado A" in body

    group = HostGroup.all(db_path=runtime.get_db_path())[0]
    assert group.owner_user_id == _user_id("admin-a")
    assert group.owner_name == "admin-a"
    assert group.is_shared is False

    _login(client, "admin-b")
    body = client.get("/inventario").get_data(as_text=True)
    assert "Grupo privado A" not in body
    body = client.get("/inventario?all=1").get_data(as_text=True)
    assert "Grupo privado A" in body
    assert "Vista global de administración" in body


def test_operator_manages_own_groups_and_cannot_touch_foreign_groups(client):
    _login(client, "admin-a")
    _post(
        client,
        "/inventario",
        {"action": "create_group", "name": "Grupo de A"},
    )
    foreign = HostGroup.all(db_path=runtime.get_db_path())[0]

    _login(client, "operador")
    response = _post(
        client,
        "/inventario",
        {"action": "create_group", "name": "Grupo de operador"},
        follow_redirects=True,
    )
    assert "Grupo de operador" in response.get_data(as_text=True)
    groups = HostGroup.all(db_path=runtime.get_db_path())
    own = next(g for g in groups if g.name == "Grupo de operador")
    assert own.owner_user_id == _user_id("operador")

    response = _post(
        client,
        "/inventario",
        {"action": "add_host", "group_id": foreign.id, "address": "10.0.0.9"},
    )
    assert response.status_code == 403

    response = _post(
        client,
        "/inventario",
        {"action": "add_host", "group_id": own.id, "address": "10.0.0.9"},
        follow_redirects=True,
    )
    assert "Host añadido al grupo" in response.get_data(as_text=True)

    body = client.get("/inventario").get_data(as_text=True)
    assert "Grupo de operador" in body
    assert "Grupo de A" not in body


def test_viewer_is_read_only_for_inventory(client):
    _login(client, "viewer")
    response = _post(
        client,
        "/inventario",
        {"action": "create_group", "name": "No permitido"},
    )
    assert response.status_code == 403


def test_legacy_rows_remain_shared_for_everyone(client):
    shared = HostGroup.create("Grupo heredado", db_path=runtime.get_db_path())
    assert shared.owner_user_id == 0
    assert shared.is_shared is True

    for username in ("admin-b", "operador", "viewer"):
        _login(client, username)
        body = client.get("/inventario").get_data(as_text=True)
        assert "Grupo heredado" in body


def test_private_inventory_group_is_not_offered_in_task_form(client):
    _login(client, "admin-a")
    group = HostGroup.create(
        "Solo A", owner_user_id=_user_id("admin-a"), db_path=runtime.get_db_path()
    )
    body = client.get("/tarea/zona-todas").get_data(as_text=True)
    assert group.name in body

    _login(client, "operador")
    body = client.get("/tarea/zona-todas").get_data(as_text=True)
    assert group.name not in body


def test_schedules_are_filtered_by_owner_with_admin_global_view(client):
    db_path = runtime.get_db_path()
    run_at = datetime.now(UTC) + timedelta(hours=1)
    Schedule.create(
        "Programación de A",
        "hora",
        run_at,
        {},
        created_by="admin-a",
        owner_user_id=_user_id("admin-a"),
        db_path=db_path,
    )
    Schedule.create(
        "Programación de B",
        "hora",
        run_at,
        {},
        created_by="admin-b",
        owner_user_id=_user_id("admin-b"),
        db_path=db_path,
    )

    _login(client, "admin-a")
    body = client.get("/programaciones").get_data(as_text=True)
    assert "Programación de A" in body
    assert "Programación de B" not in body

    body = client.get("/programaciones?all=1").get_data(as_text=True)
    assert "Programación de A" in body
    assert "Programación de B" in body

    _login(client, "operador")
    body = client.get("/programaciones").get_data(as_text=True)
    assert "Programación de A" not in body
    assert "Programación de B" not in body


def test_legacy_schedule_is_shared(client):
    db_path = runtime.get_db_path()
    Schedule.create(
        "Programación heredada",
        "hora",
        datetime.now(UTC) + timedelta(hours=1),
        {},
        db_path=db_path,
    )
    _login(client, "operador")
    body = client.get("/programaciones").get_data(as_text=True)
    assert "Programación heredada" in body
    assert "Compartida" in body


def test_only_admin_can_modify_shared_vault(client):
    _login(client, "operador")
    assert client.get("/credenciales").status_code == 200
    response = _post(
        client,
        "/passwords",
        {
            "action": "encrypt",
            "passwords_textarea": "clave-operador",
            "master_password": "master-test",
        },
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 403
