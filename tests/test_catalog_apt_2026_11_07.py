"""Pruebas de la tarjeta de actualización del sistema (apt, 2026-11-07).

La tarjeta se publica en estado ``planned`` hasta que el motor de ejecución
admita la acción ``APT``. Estas pruebas fijan el contrato del catálogo y las
decisiones de seguridad del playbook (apt-get upgrade seguro, no
dist-upgrade, sin preguntas interactivas).
"""

from __future__ import annotations

from pathlib import Path

import yaml

import app as app_module
from models.user import User
from services.task_catalog import TASK_CATALOG, get_task

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "ansible" / "apt_upgrade.yml"


def test_apt_card_is_planned_and_restricted_to_admin():
    task = get_task("apt-upgrade")
    assert task is not None
    assert task["status"] == "planned"
    assert task["platforms"] == ["linux"]
    assert task["actions"] == ["APT"]
    assert task["min_role"] == "admin"
    assert task["playbook"] == "apt_upgrade.yml"


def test_apt_playbook_uses_safe_apt_get_upgrade():
    data = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    tasks = data[0]["tasks"]

    apt_upgrade = next(
        item
        for item in tasks
        if item.get("name") == "Aplicar la actualización segura"
    )
    module = apt_upgrade["ansible.builtin.apt"]
    assert module["upgrade"] == "safe"
    assert module["update_cache"] is False
    assert "force-confold" in module["dpkg_options"]
    assert module["lock_timeout"] == 300
    assert module["autoremove"] is False

    # No se debe usar el comando interactivo ni una actualización destructiva.
    for item in tasks:
        module_data = item.get("ansible.builtin.apt")
        if isinstance(module_data, dict):
            assert module_data.get("upgrade") != "dist-upgrade"
            assert module_data.get("upgrade") != "full-upgrade"
        assert "ansible.builtin.shell" not in item


def test_apt_playbook_sets_noninteractive_environment_and_reboot_marker():
    data = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = data[0]
    assert play["environment"]["DEBIAN_FRONTEND"] == "noninteractive"
    serialized = PLAYBOOK.read_text(encoding="utf-8")
    assert "/var/run/reboot-required" in serialized
    assert "APT_VERIFIED=True" in serialized


def test_apt_card_does_not_change_active_task_selection():
    active = [task for task in TASK_CATALOG if task["status"] == "active"]
    assert [task["id"] for task in active] == ["zona-todas", "tapiz"]


def test_planned_apt_page_has_no_execution_form(tmp_path, monkeypatch):
    db_path = str(tmp_path / "apt.db")
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
                "csrf_token": token,
                "username": "admin",
                "password": "admin-password-123",
            },
        )
        response = client.get("/tarea/apt-upgrade")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "En preparación" in body
    assert "Formulario de" not in body
    assert "apt-get upgrade" in body
