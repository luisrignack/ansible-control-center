"""Pruebas de humo de la aplicación web (fases P0/P2)."""
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


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert b"Acceso Administrativo" in response.data


def test_protected_route_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_login_and_dashboard(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Formulario de" in dashboard.data


def test_invalid_credentials(client):
    response = client.post(
        "/login",
        data={"username": "admin", "password": "incorrecta"},
        follow_redirects=True,
    )
    assert b"Usuario o contrase" in response.data


def test_404_page(client):
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
    response = client.get("/ruta-que-no-existe")
    assert response.status_code == 404
    assert b"404" in response.data


def test_index_validation(client):
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
    response = client.post(
        "/",
        data={
            "target_ip": "no-es-una-ip",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "TZ",
            "value": "UTC",
        },
        follow_redirects=True,
    )
    assert b"IP o rango CIDR" in response.data


def test_temporal_txt_requires_txt_file(client):
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
    response = client.post(
        "/",
        data={
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
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
    response = client.post(
        "/",
        data={
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


@pytest.mark.skipif(
    shutil.which(BIN_ANSIBLE_VAULT) is None,
    reason="ansible-vault no está instalado",
)
def test_delete_password_updates_vault(client):
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
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
    response = client.post(
        "/passwords",
        data={
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
    client.post(
        "/login",
        data={"username": "admin", "password": "admin-password-123"},
    )
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
    response = client.post(
        "/passwords",
        data={
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
