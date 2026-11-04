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


def _create_batch(task_id: str = "hora", operator: str = "admin", target: str = "10.42.0.10",
                  status: str = "partial", dry_run: bool = False) -> str:
    batch = ExecutionBatch.create_batch(
        batch_id=f"b-{task_id}-1"[:36],
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


# ---------------------------------------------------------------------------
# Ruta del informe
# ---------------------------------------------------------------------------
def test_report_of_last_task_execution(client):
    _login(client)
    batch_id = _create_batch(task_id="hora")
    _add_results(batch_id)
    response = client.get("/tarea/hora/informe.pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    data = response.data
    assert data.startswith(b"%PDF-1.4")
    assert b"%%EOF" in data
    # Texto visible del informe (streams sin comprimir, WinAnsi/cp1252).
    text = data.decode("cp1252", errors="replace")
    assert "Hora y zona horaria" in text
    assert "Estación" in text and "Motivo" in text
    assert "Estación apagada o sin conexión" in text
    assert "Credenciales rechazadas" in text
    assert "PARCIAL" in text


def test_report_header_and_footer_brands(client):
    _login(client)
    batch_id = _create_batch(task_id="hora")
    _add_results(batch_id)
    response = client.get("/tarea/hora/informe.pdf")
    text = response.data.decode("cp1252", errors="replace")
    assert "Grupo de Migración a Código Abierto / DCTI - ETECSA" in text
    assert "Informe generado el" in text
    assert "Página 1 de 1" in text


def test_report_uses_task_batch_not_global(client):
    _login(client)
    hora_id = _create_batch(task_id="hora", target="10.42.0.10")
    ExecutionBatch.add_host_result(
        hora_id, "10.42.0.10", "failure",
        error="UNREACHABLE", db_path=app_module.get_db_path(),
    )
    otra_id = _create_batch(task_id="zona-todas", target="Inventario: Estaciones")
    ExecutionBatch.add_host_result(
        otra_id, "10.42.0.90", "failure",
        error="Permission denied.", db_path=app_module.get_db_path(),
    )
    text = client.get("/tarea/hora/informe.pdf").data.decode("cp1252", errors="replace")
    # El informe de "hora" refleja su propio lote, no el último global.
    assert "10.42.0.90" not in text
    assert "10.42.0.10" in text


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
    response = client.get("/tarea/hora/informe.pdf", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "aún no tiene ejecuciones registradas" in body


def test_report_requires_login(client):
    response = client.get("/tarea/hora/informe.pdf")
    assert response.status_code == 302


# ---------------------------------------------------------------------------
# Botón en la vista de tarea
# ---------------------------------------------------------------------------
def test_task_view_has_download_report_button(client):
    _login(client)
    response = client.get("/tarea/hora")
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
