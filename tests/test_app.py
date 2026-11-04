"""Pruebas de humo de la aplicación web (fases P0-P3)."""
import io
import shutil
import time

import pytest

import app as app_module
from config import BIN_ANSIBLE_VAULT
from models.password import PasswordRecord
from models.user import User
from services.vault_manager import VaultManager


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update(
        "admin",
        "admin-password-123",
        db_path=db_path,
    )
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def _csrf_token(client) -> str:
    """Devuelve el token CSRF de la sesión, creando la página si hace falta."""
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


def _login(client, password="admin-password-123"):
    return _post(
        client, "/login", {"username": "admin", "password": password}
    )


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert b"Acceso Administrativo" in response.data
    assert b'name="csrf_token"' in response.data
    assert b'nonce="' in response.data


def test_protected_route_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_and_dashboard(client):
    response = _login(client)
    assert response.status_code == 302

    catalog = client.get("/")
    assert catalog.status_code == 200
    assert "Catálogo de tareas".encode() in catalog.data
    assert b"Formulario de" not in catalog.data
    assert 'href="mailto:luis.rignack@etecsa.cu?subject=solicitud%20de%20nueva%20tarea%20Ansible"' in catalog.get_data(as_text=True)


def test_task_detail_routes(client):
    _login(client)

    activa = client.get("/tarea/zona-todas")
    assert activa.status_code == 200
    assert b"Formulario de" in activa.data
    assert b'name="operation_id"' in activa.data
    assert "Volver al catálogo".encode() in activa.data

    # La tarea 'hora' se retiró del catálogo: la URL antigua deja claro que
    # la tarea no existe y pide una del catálogo.
    retirada = client.get("/tarea/hora", follow_redirects=True)
    assert retirada.status_code == 200
    assert "no existe en el catálogo".encode() in retirada.data

    planned = client.get("/tarea/kaspersky")
    assert planned.status_code == 200
    assert "En preparación".encode() in planned.data
    assert b"Formulario de" not in planned.data
    assert "Volver al catálogo".encode() in planned.data

    unknown = client.get("/tarea/loquesea")
    assert unknown.status_code == 302
    assert unknown.headers["Location"].endswith("/")


def test_task_detail_requires_login(client):
    assert client.get("/tarea/hora").status_code == 302


def test_invalid_credentials(client):
    response = _post(
        client,
        "/login",
        {"username": "admin", "password": "incorrecta"},
        follow_redirects=True,
    )
    assert b"Usuario o contrase" in response.data


def test_post_without_csrf_token_is_rejected(client):
    _login(client)
    response = client.post(
        "/",
        data={
            "target_ip": "127.0.0.1",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
        },
    )
    assert response.status_code == 400
    assert b"CSRF" in response.data


def test_security_headers_are_present(client):
    response = client.get("/login")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    csp = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "nonce-" in csp
    nonce = csp.split("'nonce-", 1)[1].split("'", 1)[0]
    assert f'nonce="{nonce}"' in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_login_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "LOGIN_RATE_LIMIT", 2)
    monkeypatch.setattr(app_module, "LOGIN_RATE_WINDOW", 60)

    for _ in range(2):
        response = _post(
            client, "/login", {"username": "admin", "password": "incorrecta"}
        )
        assert response.status_code == 200

    blocked = _post(
        client, "/login", {"username": "admin", "password": "incorrecta"}
    )
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_404_page(client):
    _login(client)
    response = client.get("/ruta-que-no-existe")
    assert response.status_code == 404
    assert b"404" in response.data


def test_index_validation(client):
    _login(client)
    response = _post(
        client,
        "/",
        {
            "target_ip": "no-es-una-ip",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
        },
        follow_redirects=True,
    )
    assert b"IP o rango CIDR" in response.data


def test_index_rejects_invalid_octets(client):
    _login(client)
    response = _post(
        client,
        "/",
        {
            "target_ip": "999.1.1.1",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
        },
        follow_redirects=True,
    )
    assert b"IP o rango CIDR" in response.data


def test_index_rejects_huge_cidr(client):
    _login(client)
    response = _post(
        client,
        "/",
        {
            "target_ip": "10.0.0.0/8",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
        },
        follow_redirects=True,
    )
    assert b"Rango demasiado grande" in response.data


def test_temporal_txt_requires_txt_file(client):
    _login(client)
    response = _post(
        client,
        "/",
        {
            "target_ip": "127.0.0.1",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
            "password_file": (io.BytesIO(b"clave\n"), "passwords.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b".txt" in response.data


def test_temporal_txt_execution_records_history(client):
    _login(client)
    response = _post(
        client,
        "/",
        {
            "target_ip": "127.0.0.1",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
            "password_file": (
                io.BytesIO(b"# comentario\nclave-uno\nclave-dos\n"),
                "passwords.txt",
            ),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    task_id = response.headers["Location"].rstrip("/").split("/")[-1]

    status = {}
    for _ in range(100):
        status = client.get(f"/api/task-status/{task_id}").get_json()
        if status.get("completed"):
            break
        time.sleep(0.02)

    assert status.get("overall_result") == "success", status
    history = client.get("/history")
    assert b"127.0.0.1" in history.data
    assert b"success" in history.data.lower()


def test_passwords_view_has_password_toggles_without_origin_selector(client):
    _login(client)
    response = client.get("/passwords")
    body = response.get_data(as_text=True)

    assert 'id="passwordOriginModal"' not in body
    assert 'name="origin"' not in body
    assert body.count("data-password-toggle") >= 2


def test_encrypt_rejects_invalid_origin(client):
    _login(client)
    response = _post(
        client,
        "/passwords",
        {
            "action": "encrypt",
            "passwords_textarea": "clave-uno",
            "master_password": "master-test",
            "origin": "inventado",
        },
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False
    assert "no válido" in response.get_json()["message"]


def test_encrypt_defaults_origin_to_manual(client, monkeypatch):
    _login(client)
    captured = {}

    def fake_encrypt(**kwargs):
        captured.update(kwargs)
        return True, "Contraseñas cifradas."

    monkeypatch.setattr(app_module.VaultManager, "encrypt_passwords", fake_encrypt)

    response = _post(
        client,
        "/passwords",
        {
            "action": "encrypt",
            "passwords_textarea": "clave-uno",
            "master_password": "master-test",
        },
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert captured["origin"] == "manual"


@pytest.mark.skipif(
    shutil.which(BIN_ANSIBLE_VAULT) is None,
    reason="ansible-vault no está instalado",
)
def test_delete_password_updates_vault(client):
    _login(client)
    db_path = app_module.get_db_path()
    vault_file = app_module.get_vault_file()
    ok, message = VaultManager.encrypt_passwords(
        ["clave-a-borrar"],
        "master-test",
        output_file=vault_file,
        db_path=db_path,
    )
    assert ok is True, message

    record = PasswordRecord.get_all_passwords(db_path=db_path)[0]
    response = _post(
        client,
        "/passwords",
        {
            "action": "delete",
            "password_id": record.id,
            "master_password": "master-test",
        },
        headers={"Accept": "application/json"},
    )
    payload = response.get_json()
    assert payload["success"] is True, payload
    assert PasswordRecord.get_password_count(db_path=db_path) == 0

    ok, _message, _entries = VaultManager.decrypt_vault_file(
        vault_file, "master-test"
    )
    assert ok is False  # el vault vacío se elimina


@pytest.mark.skipif(
    shutil.which(BIN_ANSIBLE_VAULT) is None,
    reason="ansible-vault no está instalado",
)
def test_delete_password_with_wrong_master_keeps_data(client):
    _login(client)
    db_path = app_module.get_db_path()
    vault_file = app_module.get_vault_file()
    ok, message = VaultManager.encrypt_passwords(
        ["clave-protegida"],
        "master-test",
        output_file=vault_file,
        db_path=db_path,
    )
    assert ok is True, message

    record = PasswordRecord.get_all_passwords(db_path=db_path)[0]
    response = _post(
        client,
        "/passwords",
        {
            "action": "delete",
            "password_id": record.id,
            "master_password": "incorrecta",
        },
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 400
    assert PasswordRecord.get_password_count(db_path=db_path) == 1

    ok, _message, entries = VaultManager.decrypt_vault_file(
        vault_file, "master-test"
    )
    assert ok is True
    assert [entry["password"] for entry in entries] == ["clave-protegida"]
