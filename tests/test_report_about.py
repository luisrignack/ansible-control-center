# tests/test_report_about.py
# Pruebas del informe PDF de última ejecución y de la página "Acerca de".
#
# El PDF se genera sin dependencias (services/pdf_report.py): las comprobaciones
# validan tanto la estructura del fichero (cabecera, xref) como el texto
# visible (los streams no van comprimidos, así que se puede decodificar con
# cp1252 y buscar las frases).
from __future__ import annotations

import pytest

import app as app_module
from models.batch import ExecutionBatch
from services.pdf_report import classify_failure_reason
from models.user import User
from pathlib import Path


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


def _login(client):
    response = _post(
        client, "/login", {"username": "admin", "password": "admin-password-123"}
    )
    assert response.status_code in (302, 200)
    return response


def _create_batch(task_id: str = "zona-todas", operator: str = "admin", target: str = "10.42.0.10",
                  status: str = "partial", dry_run: bool = False) -> str:
    import uuid
    batch = ExecutionBatch.create_batch(
        batch_id=f"b-{task_id or 'legacy'}-{uuid.uuid4().hex[:8]}",
        operator=operator,
        action="TZ",
        value="America/Caracas",
        target=target,
        total_hosts=3,
        status=status,
        dry_run=dry_run,
        admin_user="admin",
        password_origin="stored",
        task_id=task_id,
        db_path=app_module.get_db_path(),
    )
    ExecutionBatch.finish_batch(batch.batch_id, status=status,
                                db_path=app_module.get_db_path())
    return batch.batch_id


def _add_results(batch_id: str) -> None:
    db_path = app_module.get_db_path()
    for name, result, error in (
        ("10.42.0.10", "failure", "UNREACHABLE! => Failed to connect."),
        ("10.42.0.11", "success", ""),
        ("10.42.0.12", "failure", "Permission denied."),
    ):
        ExecutionBatch.add_host_result(
            batch_id, name, result, error=error, db_path=db_path
        )


# ---------------------------------------------------------------------------
# Clasificación de motivos
# ---------------------------------------------------------------------------
def test_classify_reason_connection_failure():
    assert classify_failure_reason(
        "UNREACHABLE! => Failed to connect to the host via ssh."
    ) == "Estación apagada o sin conexión"


def test_classify_reason_password():
    assert (
        classify_failure_reason("Permission denied (publickey,password).")
        == "Credenciales rechazadas"
    )


def test_classify_reason_unknown_falls_back_to_error_text():
    text = "Error esotérico del módulo de zona horaria"
    assert classify_failure_reason(text) == text


def test_classify_reason_only_warnings_is_unknown():
    assert (
        classify_failure_reason(
            "[WARNING]: Deprecation warnings can be disabled.\n"
            "[DEPRECATION WARNING]: The '-o' argument is deprecated."
        )
        == "Motivo desconocido (detalle técnico en el historial)"
    )


def test_ping_failure_error_prefers_host_line_over_warnings(monkeypatch):
    """El error del ping guarda el fallo del host, no los avisos de Ansible."""
    import subprocess as sp

    import services.password_tester as tester

    def fake_run(cmd, **_kw):
        return sp.CompletedProcess(
            cmd,
            1,
            # stdout: el host prueba una conexión fallida real.
            "10.42.0.9 | UNREACHABLE! => {\"changed\": false}",
            # stderr: solo avisos de configuración.
            "[WARNING]: Deprecation warnings can be disabled.",
        )

    monkeypatch.setattr(tester.subprocess, "run", fake_run)
    monkeypatch.setattr(tester, "DRY_RUN", False)
    result = tester.test_ansible_ping("10.42.0.9", "admin", "clave")
    assert not result["success"]
    assert "UNREACHABLE" in result["error"]
    assert "[WARNING]" not in result["error"]


# ---------------------------------------------------------------------------
# Ruta del informe
# ---------------------------------------------------------------------------
def test_report_of_last_task_execution(client):
    _login(client)
    batch_id = _create_batch(task_id="zona-todas")
    _add_results(batch_id)
    response = client.get("/tarea/zona-todas/informe.pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    data = response.data
    assert data.startswith(b"%PDF-1.4")
    assert b"%%EOF" in data
    # Texto visible del informe (streams sin comprimir, WinAnsi/cp1252).
    text = data.decode("cp1252", errors="replace")
    assert "Zona horaria" in text
    assert "Estación" in text and "Motivo" in text
    assert "Estación apagada o sin conexión" in text
    assert "Credenciales rechazadas" in text
    assert "PARCIAL" in text


def test_report_header_and_footer_brands(client):
    _login(client)
    batch_id = _create_batch(task_id="zona-todas")
    _add_results(batch_id)
    response = client.get("/tarea/zona-todas/informe.pdf")
    text = response.data.decode("cp1252", errors="replace")
    assert "Grupo de Migración a Código Abierto / DCTI - ETECSA" in text
    assert "Informe generado el" in text
    assert "Página 1 de 1" in text


def test_report_uses_task_batch_not_global(client):
    _login(client)
    viejo_id = _create_batch(task_id="zona-todas", target="10.42.0.10")
    ExecutionBatch.add_host_result(
        viejo_id, "10.42.0.10", "failure",
        error="UNREACHABLE", db_path=app_module.get_db_path(),
    )
    nuevo_id = _create_batch(task_id="zona-todas", target="Inventario: Estaciones")
    ExecutionBatch.add_host_result(
        nuevo_id, "10.42.0.90", "failure",
        error="Permission denied.", db_path=app_module.get_db_path(),
    )
    text = client.get("/tarea/zona-todas/informe.pdf").data.decode("cp1252", errors="replace")
    # El informe de la tarea refleja su ÚLTIMA corrida (la nueva), no la previa.
    assert "10.42.0.90" in text
    assert "10.42.0.10" not in text


def test_report_falls_back_to_last_batch_without_task_id(client):
    _login(client)
    legacy_id = _create_batch(task_id="", target="10.99.0.5", status="success")
    ExecutionBatch.add_host_result(
        legacy_id, "10.99.0.5", "success", db_path=app_module.get_db_path()
    )
    response = client.get("/tarea/zona-todas/informe.pdf")
    assert response.status_code == 200
    text = response.data.decode("cp1252", errors="replace")
    assert "10.99.0.5" in text


def test_report_without_batches_redirects_with_message(client):
    _login(client)
    response = client.get("/tarea/zona-todas/informe.pdf", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "aún no tiene ejecuciones registradas" in body


def test_report_requires_login(client):
    response = client.get("/tarea/zona-todas/informe.pdf")
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# Botón en la vista de tarea
# ---------------------------------------------------------------------------
def test_task_view_has_download_report_button(client):
    _login(client)
    response = client.get("/tarea/zona-todas")
    assert response.status_code == 200
    assert "Descargar informe" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Acerca de
# ---------------------------------------------------------------------------
def test_about_page_content(client):
    _login(client)
    response = client.get("/acerca-de")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Grupo de Migración a Código Abierto / DCTI - ETECSA" in body
    assert "Acerca de" in body
    # "Fecha del host" con formato español.
    assert "Fecha del host" in body
    assert " de noviembre de " in body or " de 2026 " in body


def test_about_link_in_sidebar(client):
    _login(client)
    response = client.get("/")
    assert "acerca-de" in response.get_data(as_text=True)


def test_about_requires_login(client):
    response = client.get("/acerca-de")
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# Favicon
# ---------------------------------------------------------------------------
def test_favicon_static_served(client):
    for path in ("img/ansible-48.png", "img/ansible-96.png"):
        response = client.get(f"/static/{path}")
        assert response.status_code == 200
        assert response.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_favicon_linked_in_base_template(client):
    _login(client)
    response = client.get("/acerca-de")
    body = response.get_data(as_text=True)
    assert "img/ansible-48.png" in body
    assert 'rel="icon"' in body


# ---------------------------------------------------------------------------
# Backup de /etc/timezone en el servidor de operación
# ---------------------------------------------------------------------------
def test_apply_host_linux_sends_backup_dir(tmp_path, monkeypatch):
    """Linux: acc_backup_dir va en el payload y el directorio se crea."""
    import types

    import services.ansible_runner as runner

    seen = {}

    def fake_invoke(host, user, password, playbook_path, extra_vars, **kw):
        seen["extra_vars"] = dict(extra_vars)
        seen["connection"] = kw.get("connection")
        fake = types.SimpleNamespace(
            returncode=0,
            stdout=(
                "HORA_BEFORE_B64=e30=\nHORA_CHANGED=false\nHORA_VERIFIED=true\n"
                "HORA_STATE_B64=e30=\n"
            ),
            stderr="",
        )
        return fake

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    backup_dir = tmp_path / "timezone_backups" / "batch-1"
    result = runner.apply_host(
        "10.42.0.7", "admin", "TZ", "America/Caracas",
        password="clave", connection="ssh",
        backup_dir=str(backup_dir),
    )
    assert result["success"], result
    assert seen["extra_vars"]["acc_backup_dir"] == str(backup_dir)
    assert backup_dir.is_dir()


def test_apply_host_windows_has_no_backup_dir(tmp_path, monkeypatch):
    """Windows: no existe /etc/timezone, no se envía acc_backup_dir."""
    import types

    import services.ansible_runner as runner

    seen = {}

    def fake_invoke(host, user, password, playbook_path, extra_vars, **kw):
        seen["extra_vars"] = dict(extra_vars)
        return types.SimpleNamespace(
            returncode=0,
            stdout=(
                "HORA_BEFORE_B64=e30=\nHORA_CHANGED=false\nHORA_VERIFIED=true\n"
                "HORA_STATE_B64=e30=\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    result = runner.apply_host(
        "10.42.0.7", "admin", "TZ", "America/Caracas",
        password="clave", connection="winrm",
        backup_dir=str(tmp_path / "timezone_backups"),
    )
    assert result["success"], result
    assert "acc_backup_dir" not in seen["extra_vars"]
    assert not (tmp_path / "timezone_backups").exists()


def test_linux_playbook_fetches_backup_and_checks_persistence():
    import yaml

    playbook = yaml.safe_load(
        Path("ansible/cambiar_hora.yml").read_text(encoding="utf-8")
    )
    tasks = playbook[0]["tasks"]
    names = [t["name"] for t in tasks]

    assert any("Enviar la copia de /etc/timezone al servidor" in n for n in names)
    assert any("/etc/timezone persiste" in n for n in names)

    fetch_task = next(
        t for t in tasks if "Enviar la copia de /etc/timezone" in t["name"]
    )
    assert "acc_backup_dir" in (fetch_task["when"][0] + fetch_task["when"][1])
    assert fetch_task["ansible.builtin.fetch"]["src"] == "/etc/timezone.bak"

    persist_task = next(
        t for t in tasks if "/etc/timezone persiste" in t["name"]
    )
    assert "failed_when" in persist_task
    assert persist_task["when"][-1] == "_hora_tz_file.stat.exists"
