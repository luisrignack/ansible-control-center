# tests/test_windows.py
# Pruebas del bloque B: soporte multiplataforma (Linux + Windows).
#
# Todo lo que toca WinRM/ansible.windows se prueba con mocks (no hay hosts
# Windows reales en la suite): el contrato se valida a nivel de cmd/payload
# y del catálogo/validación de la app.
from __future__ import annotations

import io
import html
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

import app as app_module
from models.ops import (
    OS_CONNECTIONS,
    OS_FLAVORS,
    HostGroup,
    InventoryHost,
)
from models.user import User
from services.task_catalog import get_task


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


def _post(client, url, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload, **kwargs)


def _body_text(response) -> str:
    """HTML del body sin escapar (los flashes usan apóstrofes y acentos)."""
    return html.unescape(response.get_data(as_text=True))


def _login(client):
    return _post(
        client, "/login", {"username": "admin", "password": "admin-password-123"}
    )


def _wait_for_task(client, task_id: str) -> dict:
    status: dict = {}
    for _ in range(200):
        status = client.get(f"/api/task-status/{task_id}").get_json()
        if status.get("completed"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"La tarea no terminó: {status}")


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------
def test_zona_todas_task_is_active_and_multiplatform():
    task = get_task("zona-todas")
    assert task is not None
    assert task["status"] == "active"
    assert set(task["platforms"]) == {"linux", "windows"}
    assert task["actions"] == ["TZ"]
    assert not task["destructive"]


def test_all_tasks_declare_platforms():
    import services.task_catalog as catalog

    for task in catalog.TASK_CATALOG:
        assert task.get("platforms"), task["id"]
        assert task.get("actions"), task["id"]


# ---------------------------------------------------------------------------
# Inventario por plataforma
# ---------------------------------------------------------------------------
def test_windows_group_creation():
    db_path = app_module.get_db_path()
    group = HostGroup.create("Estaciones Win", os_flavor="windows", db_path=db_path)
    assert group.os_flavor == "windows"
    assert group.connection == "winrm"
    assert sorted(OS_FLAVORS) == ["linux", "windows"]
    assert sorted(OS_CONNECTIONS) == ["ssh", "winrm"]


def test_windows_group_rejects_ssh_key(client):
    """Los grupos Windows no admiten la subida de clave SSH."""
    _login(client)
    db_path = app_module.get_db_path()
    group = HostGroup.create("Win", os_flavor="windows", db_path=db_path)
    response = _post(
        client,
        "/inventario",
        data={"action": "upload_key", "group_id": group.id},
        follow_redirects=True,
    )
    assert "no admiten clave SSH" in _body_text(response)


# ---------------------------------------------------------------------------
# Tareas retiradas y valor fijo
# ---------------------------------------------------------------------------
def test_retired_task_rejected_with_clear_message(client):
    """La tarjeta antigua 'hora' ya no es operativa desde el catálogo."""
    _login(client)
    response = _post(
        client,
        "/",
        data={
            "task": "hora",
            "target_ip": "10.55.0.1",
            "admin_user": "admin",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
            "mode": "preview",
            "password_file": (io.BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Selecciona una tarea activa del catálogo" in _body_text(response)


def test_zona_task_single_active_and_fixed_value():
    from services.task_catalog import TASK_CATALOG

    active = [t for t in TASK_CATALOG if t["status"] == "active"]
    assert [t["id"] for t in active] == [
        "zona-todas",
        "contrasena",
        "tapiz",
        "apt-upgrade",
    ]
    assert active[0]["fixed_value"] == "America/Caracas"
    assert set(active[0]["platforms"]) == {"linux", "windows"}


def test_zona_task_form_renders_fixed_value(client):
    _login(client)
    body = client.get("/tarea/zona-todas").get_data(as_text=True)
    assert 'name="value" value="America/Caracas"' in body
    assert 'data-fixed="true"' in body
    assert "permanente" in body.lower()


def test_zona_todas_accepts_windows_group(client, monkeypatch):
    """La tarea zona-todas admite el grupo Windows y notifica la conexión."""
    _login(client)
    db_path = app_module.get_db_path()
    group = HostGroup.create("Win", os_flavor="windows", db_path=db_path)
    InventoryHost.add(group.id, "10.55.0.2", db_path=db_path)

    seen = {}

    def fake_ping(**kwargs):
        seen["ping_conn"] = kwargs.get("connection")
        return {"success": True, "error": ""}

    def fake_apply(host, password=None, **kwargs):
        seen["apply_conn"] = kwargs.get("connection")
        return {
            "success": True,
            "host": host,
            "error": "",
            "before_state": {"timezone": "Etc/UTC"},
            "after_state": {"timezone": "America/Caracas"},
            "changed": True,
            "verified": True,
            "warnings": "",
        }

    monkeypatch.setattr(app_module, "test_ansible_ping", fake_ping)
    monkeypatch.setattr(app_module, "apply_host", fake_apply)

    response = _post(
        client,
        "/",
        data={
            "task": "zona-todas",
            "target_mode": "group",
            "target_group": group.id,
            "admin_user": "admin",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "America/Caracas",
            "mode": "execute",
            "password_file": (io.BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.data
    task_id = response.headers["Location"].rstrip("/").split("/")[-1]
    status = _wait_for_task(client, task_id)
    assert status["overall_result"] == "success", status
    assert seen.get("ping_conn") == "winrm"
    assert seen.get("apply_conn") == "winrm"


def test_zona_todas_rejects_action_time(client):
    _login(client)
    response = _post(
        client,
        "/",
        data={
            "task": "zona-todas",
            "target_ip": "10.55.0.3",
            "admin_user": "admin",
            "password_origin": "temporal",
            "action": "TIME",
            "value": "2026-11-04T09:00",
            "time_mode": "utc",
            "mode": "preview",
            "password_file": (io.BytesIO(b"clave\n"), "p.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "no admite la acción TIME" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Runner: winrm en el payload
# ---------------------------------------------------------------------------
def test_winrm_cert_validation_defaults_to_secure():
    from config import WINRM_SERVER_CERT_VALIDATION

    assert WINRM_SERVER_CERT_VALIDATION == "validate"


def test_win_ping_module_and_connection_vars(monkeypatch, tmp_path):
    import services.ansible_runner as runner
    import services.password_tester as tester

    captured = {}

    def fake_run(cmd, **_kw):
        vars_file = cmd[cmd.index("--extra-vars") + 1].lstrip("@")
        captured["cmd"] = cmd
        captured["payload"] = json.loads(open(vars_file, encoding="utf-8").read())
        # El tester exige ver "SUCCESS" en stdout cuando el returncode es 0.
        return subprocess.CompletedProcess(cmd, 0, "10.55.0.2 | SUCCESS >> {}", "")

    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(tester, "DRY_RUN", False)

    result = tester.test_ansible_ping(
        "10.55.0.2", "admin", "clave", connection="winrm"
    )
    assert result["success"]
    # raw + echo: el ping sobre WinRM con solo módulos builtin y pywinrm.
    cmd = captured["cmd"]
    assert "raw" in cmd and "echo PING_OK" in cmd
    assert captured["payload"]["ansible_connection"] == "winrm"
    assert captured["payload"]["ansible_winrm_transport"] == runner.WINRM_TRANSPORT
    assert (
        captured["payload"]["ansible_winrm_server_cert_validation"]
        == runner.WINRM_SERVER_CERT_VALIDATION
    )
    assert int(captured["payload"]["ansible_port"]) == runner.WINRM_PORT


def test_apply_host_windows_playbook_path(monkeypatch, tmp_path):
    import services.ansible_runner as runner

    seen = {}

    def fake_invoke(host, user, password, playbook_path, extra_vars, **kw):
        seen["path"] = playbook_path
        seen["connection"] = kw.get("connection")
        out = SimpleNamespace(
            returncode=0,
            stdout=(
                "HORA_BEFORE_B64=eyJ0aW1lem9uZSI6ICJFVEEvVXRjI30=\n"
                "HORA_CHANGED=true\nHORA_VERIFIED=true\n"
                "HORA_STATE_B64=eyJ0aW1lem9uZSI6ICJBTWVyaWNhL0NhcmFjYXMiXQ==\n"
            ),
            stderr="",
        )
        return out

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    result = runner.apply_host(
        "10.55.0.2", "admin", "TZ", "America/Caracas",
        password="clave", connection="winrm",
    )
    assert result["success"], result
    assert "win" in seen["path"] and seen["path"].endswith(".yml")
    assert seen["connection"] == "winrm"
    assert result["changed"] and result["verified"]


def test_windows_playbooks_exist_with_expected_markers():
    from pathlib import Path

    import config

    apply_pb = Path(config.WINDOWS_APPLY_PLAYBOOK_FILE).read_text(encoding="utf-8")
    # Solo módulos builtin: la imagen se construye sin red/colecciones.
    assert "raw: tzutil" in apply_pb
    assert "win_timezone" not in apply_pb
    assert "HORA_VERIFIED" in apply_pb
    assert "HORA_BEFORE_B64" in apply_pb
    assert "HORA_STATE_B64" in apply_pb
    assert "America/Caracas" in apply_pb
    assert "SA Pacific Standard Time" in apply_pb
    assert "become: false" in apply_pb

    inspect_pb = Path(config.WINDOWS_INSPECT_PLAYBOOK_FILE).read_text(
        encoding="utf-8"
    )
    assert "HORA_STATE_B64" in inspect_pb
    assert "genuine" not in inspect_pb
