"""Contrato del sistema de versiones.

La versión de la aplicación, el tag de Git y el tag de la imagen Docker deben
salir del mismo archivo ``VERSION``. Estas pruebas fijan esa fuente única y su
exposición en la configuración de Flask y en ``/acerca-de``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app as app_module
from acc import runtime, version
from models.user import User


def test_version_es_semver_y_coincide_con_el_archivo():
    raw = Path(app_module.config.BASE_DIR / "VERSION").read_text(encoding="utf-8")
    raw = raw.strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", raw)
    assert version.__version__ == raw


def test_create_app_publica_version_y_commit():
    assert app_module.app.config["APP_VERSION"] == version.__version__
    assert app_module.app.config["APP_BUILD_COMMIT"] == version.BUILD_COMMIT
    assert app_module.APP_VERSION == version.__version__
    assert app_module.APP_BUILD_COMMIT == version.BUILD_COMMIT


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
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


def _login(client):
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin-password-123",
            "csrf_token": _csrf_token(client),
        },
    )
    assert response.status_code in (302, 200)
    return response


def test_acerca_de_muestra_la_version(client):
    _login(client)
    response = client.get("/sistema", query_string={"tab": "acerca-de"})
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Versión" in body
    assert version.__version__ in body
