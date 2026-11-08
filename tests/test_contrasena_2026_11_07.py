# tests/test_contrasena_2026_11_07.py
# Pruebas de la tarjeta "Contraseña administrativa": catálogo, validación del
# usuario/contraseña, playbooks, no persistencia del secreto y formulario.
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

import app as app_module
import services.ansible_runner as runner
from models.user import User
from services.task_catalog import get_task

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "ansible" / "contrasena.yml"
INSPECT_PLAYBOOK = ROOT / "ansible" / "contrasena_estado.yml"

NEW_PASSWORD = "Clave-Segura-2026"
ENCODED_STATE = base64.b64encode(
    json.dumps(
        {
            "user": "ladmin",
            "exists": True,
            "uid": "1000",
            "shell": "/bin/bash",
            "status": "P",
        }
    ).encode()
).decode()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
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


def _login(client):
    payload = {
        "csrf_token": _csrf_token(client),
        "username": "admin",
        "password": "admin-password-123",
    }
    return client.post("/login", data=payload)


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------
def test_password_card_is_active_admin_only_and_not_schedulable():
    task = get_task("contrasena")
    assert task is not None
    assert task["status"] == "active"
    assert task["actions"] == ["PASSWORD"]
    assert task["platforms"] == ["linux"]
    assert task["min_role"] == "admin"
    assert task["destructive"] is True
    assert task["schedulable"] is False
    assert task["playbook"] == "contrasena.yml"


def test_password_card_is_not_offered_for_scheduling():
    validated, errors = app_module._validate_scheduled_params(
        get_task("contrasena"),
        admin_user="root",
        action="PASSWORD",
        value="ladmin",
        ntp_policy="keep",
        ntp_servers_raw="",
    )
    assert validated is None
    assert any("programar" in error.lower() for error in errors)


# ---------------------------------------------------------------------------
# Playbooks
# ---------------------------------------------------------------------------
def test_apply_playbook_never_echoes_the_secret_and_uses_chpasswd():
    text = PLAYBOOK.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    tasks = data[0]["tasks"]

    assert data[0]["become"] is True
    change = next(
        item for item in tasks if item.get("name") == "Cambiar la contraseña del usuario"
    )
    command = change["ansible.builtin.command"]
    assert command["argv"] == ["chpasswd"]
    assert command["stdin"] == "{{ target_user }}:{{ new_password }}"
    assert change["no_log"] is True
    # El secreto no debe aparecer en un `debug` ni en el nombre de la tarea.
    for item in tasks:
        debug = item.get("ansible.builtin.debug")
        if debug:
            assert "new_password" not in str(debug.get("msg", ""))
    assert "PASSWORD_STATE_B64" in text
    assert "PASSWORD_VERIFIED" in text


def test_inspect_playbook_is_read_only():
    text = INSPECT_PLAYBOOK.read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    assert data[0]["become"] is True
    assert "PASSWORD_STATE_B64" in text
    assert "chpasswd" not in text
    assert "new_password" not in text
    assert "ansible.builtin.user" not in text


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------
def test_parse_target_user_defaults_to_ladmin():
    user, error = app_module._parse_target_user("")
    assert error is None
    assert user == app_module.OS_PASSWORD_DEFAULT_USER


def test_parse_target_user_accepts_valid_and_rejects_invalid():
    user, error = app_module._parse_target_user("operador.local")
    assert error is None
    assert user == "operador.local"

    _, error = app_module._parse_target_user("Root")
    assert error
    _, error = app_module._parse_target_user("usuario con espacios")
    assert error


def test_validate_new_password_requires_match_and_minimum_length():
    assert (
        app_module._validate_new_password("", "", required=False) is None
    )
    assert (
        app_module._validate_new_password("", "", required=True)
        == "La nueva contraseña es obligatoria."
    )
    assert "no coinciden" in app_module._validate_new_password(
        NEW_PASSWORD, NEW_PASSWORD + "x", required=True
    )
    assert "al menos" in app_module._validate_new_password(
        "abc", "abc", required=True
    )
    assert (
        app_module._validate_new_password(
            NEW_PASSWORD, NEW_PASSWORD, required=True
        )
        is None
    )


def test_preview_does_not_require_password_but_execute_does():
    preview, errors = app_module._validate_operation_core(
        task=get_task("contrasena"),
        task_id="contrasena",
        admin_user="root",
        action="PASSWORD",
        value="",
        ntp_policy="keep",
        ntp_servers_raw="",
        target_user_raw="",
        require_password=False,
    )
    assert errors == []
    assert preview["value"] == app_module.OS_PASSWORD_DEFAULT_USER
    assert "new_password" not in preview

    _, errors = app_module._validate_operation_core(
        task=get_task("contrasena"),
        task_id="contrasena",
        admin_user="root",
        action="PASSWORD",
        value="",
        ntp_policy="keep",
        ntp_servers_raw="",
        target_user_raw="ladmin",
        new_password=NEW_PASSWORD,
        new_password_confirm="otra-cosa",
        require_password=True,
    )
    assert any("no coinciden" in error for error in errors)


def test_valid_execute_returns_only_the_target_user():
    validated, errors = app_module._validate_operation_core(
        task=get_task("contrasena"),
        task_id="contrasena",
        admin_user="root",
        action="PASSWORD",
        value="",
        ntp_policy="keep",
        ntp_servers_raw="",
        target_user_raw="ladmin",
        new_password=NEW_PASSWORD,
        new_password_confirm=NEW_PASSWORD,
        require_password=True,
    )
    assert errors == []
    assert validated["value"] == "ladmin"
    assert NEW_PASSWORD not in json.dumps(validated)


def test_approval_params_exclude_the_secret():
    params = app_module._approval_params(
        {
            "task_id": "contrasena",
            "value": "ladmin",
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
            "master_password": "vault-secret",
            "temporal_passwords": ["otra"],
        }
    )
    assert params == {"task_id": "contrasena", "value": "ladmin"}
    assert NEW_PASSWORD not in json.dumps(params)


# ---------------------------------------------------------------------------
# Inspección y ejecución
# ---------------------------------------------------------------------------
def test_inspect_password_decodes_state_without_secret(monkeypatch):
    class Process:
        returncode = 0
        stdout = f"PASSWORD_STATE_B64={ENCODED_STATE}\n"

    captured = {}
    monkeypatch.setattr(
        runner,
        "_invoke_playbook",
        lambda **kwargs: captured.update(kwargs) or Process(),
    )

    result = runner.inspect_password(
        host="10.0.0.10",
        user="root",
        password="clave-admin",
        target_user="ladmin",
    )

    assert captured["playbook_path"] == str(
        runner.PASSWORD_INSPECT_PLAYBOOK_FILE
    )
    assert captured["extra_vars"] == {"target_user": "ladmin"}
    assert result["success"] is True
    assert result["state"]["user"] == "ladmin"
    assert result["state"]["exists"] is True


def test_apply_host_password_selects_playbook_and_keeps_secret_hidden(
    monkeypatch,
):
    class Process:
        returncode = 0
        stdout = (
            f"PASSWORD_BEFORE_B64={ENCODED_STATE}\n"
            f"PASSWORD_STATE_B64={ENCODED_STATE}\n"
            "PASSWORD_CHANGED=True\n"
            "PASSWORD_VERIFIED=True\n"
        )

    captured = {}
    monkeypatch.setattr(
        runner,
        "_invoke_playbook",
        lambda **kwargs: captured.update(kwargs) or Process(),
    )
    monkeypatch.setattr(runner, "DRY_RUN", False)

    result = runner.apply_host(
        host="10.0.0.20",
        user="root",
        action="PASSWORD",
        value="ladmin",
        password="clave-admin",
        new_password=NEW_PASSWORD,
    )

    assert captured["playbook_path"] == str(runner.PASSWORD_APPLY_PLAYBOOK_FILE)
    assert captured["extra_vars"] == {
        "target_user": "ladmin",
        "new_password": NEW_PASSWORD,
    }
    assert result["success"] is True
    assert result["changed"] is True
    assert result["verified"] is True
    assert NEW_PASSWORD not in json.dumps(result)


def test_apply_host_password_rejects_missing_secret(monkeypatch):
    monkeypatch.setattr(runner, "DRY_RUN", False)
    result = runner.apply_host(
        host="10.0.0.21",
        user="root",
        action="PASSWORD",
        value="ladmin",
        password="clave-admin",
        new_password="",
    )
    assert result["success"] is False
    assert "nueva contraseña" in result["error"].lower()


# ---------------------------------------------------------------------------
# Formulario
# ---------------------------------------------------------------------------
def test_password_form_renders_user_and_confirmation_fields(client):
    _login(client)
    response = client.get("/tarea/contrasena")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Cambiar contraseña de usuario (PASSWORD)" in body
    assert 'id="targetUser"' in body
    assert 'name="target_user"' in body
    assert 'name="new_password"' in body
    assert 'name="new_password_confirm"' in body
    assert 'value="ladmin"' in body
    assert "Repetir contraseña" in body
    assert "no se guarda" in body


def test_password_card_is_shown_in_catalog(client):
    _login(client)
    body = client.get("/").get_data(as_text=True)
    assert "Contraseña administrativa" in body
    assert "Operativa" in body or "Abrir tarea" in body
