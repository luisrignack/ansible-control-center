"""Pruebas de la tarjeta «Firewall» (2026-11-10).

Cubre el catálogo, la validación de la operación (activar, desactivar o
permitir una IP/red), la selección de playbooks, la decodificación de los
marcadores del runner y la presencia de los campos en el formulario.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import yaml

import app as app_module
import services.ansible_runner as runner
from services.task_catalog import TASK_CATALOG, get_task

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "ansible" / "firewall.yml"
INSPECT_PLAYBOOK = ROOT / "ansible" / "firewall_estado.yml"

ACTIVE_STATE = {
    "manager": "ufw",
    "active": True,
    "allowed_sources": ["192.168.1.25"],
}
ENCODED_ACTIVE = base64.b64encode(json.dumps(ACTIVE_STATE).encode()).decode()
ENCODED_INACTIVE = base64.b64encode(
    json.dumps({"manager": "ufw", "active": False, "allowed_sources": []}).encode()
).decode()


def test_firewall_card_is_active_and_admin_only():
    task = get_task("firewall")
    assert task is not None
    assert task["status"] == "active"
    assert task["platforms"] == ["linux"]
    assert task["actions"] == ["FIREWALL"]
    assert task["min_role"] == "admin"
    assert task["playbook"] == "firewall.yml"
    assert task["schedulable"] is False
    assert task["destructive"] is False


def test_firewall_card_is_in_the_catalog():
    assert "firewall" in [task["id"] for task in TASK_CATALOG]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("enable", "enable"),
        ("DISABLE", "disable"),
        ("allow:192.168.1.25", "allow:192.168.1.25"),
        ("allow:10.0.0.0/24", "allow:10.0.0.0/24"),
        ("allow:2001:db8::1", "allow:2001:db8::1"),
        ("allow:2001:db8::/32", "allow:2001:db8::/32"),
    ],
)
def test_parse_firewall_value_accepts_valid_operations(raw, expected):
    value, error = app_module._parse_firewall_value(raw)
    assert error is None
    assert value == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "encender",
        "allow:",
        "allow:no-es-una-ip",
        "allow:0.0.0.0/0",
        "allow:::/0",
        "allow:10.0.0.0/99",
    ],
)
def test_parse_firewall_value_rejects_invalid_operations(raw):
    value, error = app_module._parse_firewall_value(raw)
    assert value == ""
    assert error


def test_validate_core_accepts_enable_and_normalizes_value():
    validated, errors = app_module._validate_operation_core(
        task=get_task("firewall"),
        task_id="firewall",
        admin_user="root",
        action="FIREWALL",
        value="ENABLE",
        ntp_policy="configure",
        ntp_servers_raw="pool.ntp.org",
    )
    assert errors == []
    assert validated["action"] == "FIREWALL"
    assert validated["value"] == "enable"
    # El firewall no toca el reloj: la política NTP se normaliza.
    assert validated["ntp_policy"] == "keep"
    assert validated["ntp_servers"] == []


def test_validate_core_rejects_invalid_firewall_value():
    validated, errors = app_module._validate_operation_core(
        task=get_task("firewall"),
        task_id="firewall",
        admin_user="root",
        action="FIREWALL",
        value="permitir todo",
        ntp_policy="keep",
        ntp_servers_raw="",
    )
    assert validated is None
    assert any("firewall" in error.lower() for error in errors)


def test_validate_core_rejects_firewall_for_non_firewall_task():
    validated, errors = app_module._validate_operation_core(
        task=get_task("apt-upgrade"),
        task_id="apt-upgrade",
        admin_user="root",
        action="FIREWALL",
        value="enable",
        ntp_policy="keep",
        ntp_servers_raw="",
    )
    assert validated is None
    assert errors


def test_would_change_for_firewall():
    assert (
        app_module._would_change(
            ACTIVE_STATE, "FIREWALL", "disable", "utc", "keep", False
        )
        is True
    )
    assert (
        app_module._would_change(
            ACTIVE_STATE, "FIREWALL", "enable", "utc", "keep", False
        )
        is False
    )
    assert (
        app_module._would_change(
            ACTIVE_STATE, "FIREWALL", "allow:10.9.9.9", "utc", "keep", False
        )
        is True
    )
    assert (
        app_module._would_change(
            ACTIVE_STATE,
            "FIREWALL",
            "allow:192.168.1.25",
            "utc",
            "keep",
            False,
        )
        is False
    )


def test_inspect_firewall_decodes_state(monkeypatch):
    class Process:
        returncode = 0
        stdout = f"FIREWALL_STATE_B64={ENCODED_ACTIVE}\n"

    captured = {}

    def fake_invoke(**kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    result = runner.inspect_firewall(host="10.0.0.10", user="root", password="clave")

    assert captured["playbook_path"] == str(runner.FIREWALL_INSPECT_PLAYBOOK_FILE)
    assert result["success"] is True
    assert result["state"]["manager"] == "ufw"
    assert result["state"]["allowed_sources"] == ["192.168.1.25"]


def test_apply_host_selects_firewall_playbook_and_decodes_markers(
    monkeypatch,
):
    class Process:
        returncode = 0
        stdout = (
            f"FIREWALL_BEFORE_B64={ENCODED_INACTIVE}\n"
            f"FIREWALL_STATE_B64={ENCODED_ACTIVE}\n"
            "FIREWALL_CHANGED=True\n"
            "FIREWALL_VERIFIED=True\n"
            "FIREWALL_ERRORS=\n"
        )

    captured = {}

    def fake_invoke(**kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    result = runner.apply_host(
        host="10.0.0.20",
        user="root",
        action="FIREWALL",
        value="allow:192.168.1.25",
        password="clave",
    )

    assert captured["playbook_path"] == str(runner.FIREWALL_APPLY_PLAYBOOK_FILE)
    assert captured["extra_vars"]["accion"] == "FIREWALL"
    assert captured["extra_vars"]["valor"] == "allow:192.168.1.25"
    assert result["success"] is True
    assert result["changed"] is True
    assert result["verified"] is True
    assert result["before_state"]["active"] is False
    assert result["after_state"]["active"] is True


def test_apply_host_firewall_reports_verification_failure(monkeypatch):
    class Process:
        returncode = 0
        stdout = (
            f"FIREWALL_STATE_B64={ENCODED_INACTIVE}\n"
            "FIREWALL_CHANGED=True\n"
            "FIREWALL_VERIFIED=False\n"
            "FIREWALL_ERRORS=El firewall no quedó activo.\n"
        )

    monkeypatch.setattr(runner, "_invoke_playbook", lambda **kwargs: Process())
    monkeypatch.setattr(runner, "DRY_RUN", False)

    result = runner.apply_host(
        host="10.0.0.21",
        user="root",
        action="FIREWALL",
        value="enable",
        password="clave",
    )
    assert result["success"] is False
    assert "no quedó activo" in result["error"]


def test_firewall_playbook_is_safe_and_reloads_rules():
    data = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = data[0]
    assert play["hosts"] == "all"
    serialized = PLAYBOOK.read_text(encoding="utf-8")

    for item in play["tasks"]:
        assert "ansible.builtin.shell" not in item
        command = item.get("ansible.builtin.command")
        if isinstance(command, dict):
            # Todos los comandos usan argv: nada de cadenas interpretadas.
            assert isinstance(command.get("argv"), list)
            assert "&&" not in " ".join(str(part) for part in command["argv"])

    assert "ufw, --force, enable" in serialized
    assert "ufw, disable" in serialized
    assert "ufw, allow, from" in serialized
    assert "ufw, reload" in serialized
    assert "firewall-cmd, --reload" in serialized
    assert "FIREWALL_VERIFIED" in serialized
    assert "FIREWALL_ERRORS" in serialized


def test_firewall_inspect_playbook_is_read_only():
    serialized = INSPECT_PLAYBOOK.read_text(encoding="utf-8")
    assert "FIREWALL_STATE_B64" in serialized
    assert "allow from" in serialized
    assert "ufw, --force, enable" not in serialized
    assert "ufw, disable" not in serialized
    assert "ufw, allow" not in serialized
    assert "ansible.builtin.shell" not in serialized


def test_firewall_form_renders_operation_and_ip_fields(tmp_path, monkeypatch):
    from models.user import User

    db_path = str(tmp_path / "firewall.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    app_module.app.config.update(TESTING=True)

    with app_module.app.test_client() as client:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
        client.post(
            "/login",
            data={
                "username": "admin",
                "password": "admin-password-123",
                "csrf_token": token,
            },
        )
        body = client.get("/tarea/firewall").data.decode()

    assert 'name="firewall_op"' in body
    assert 'name="firewall_ip"' in body
    assert 'value="FIREWALL"' in body
    assert "Activar el firewall" in body
    assert "Desactivar el firewall" in body
    assert "Permitir una IP o red" in body
