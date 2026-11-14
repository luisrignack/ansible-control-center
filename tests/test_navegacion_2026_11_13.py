"""Navegación de la versión 0.9.5: secciones canónicas y compatibilidad.

Fija la nueva arquitectura de información (Operación/Seguridad/Administración)
y que las rutas antiguas sigan respondiendo con una redirección permanente.
"""

from __future__ import annotations

import pytest

import app as app_module
from acc import runtime
from models.user import User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "navegacion.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path, role="admin")
    User.create_or_update(
        "operador", "operador-password-123", db_path=db_path, role="operator"
    )
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _login(client, username="admin", password="admin-password-123"):
    with client.session_transaction() as sess:
        sess.clear()
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": _csrf(client),
        },
    )


@pytest.mark.parametrize(
    ("url", "marker"),
    [
        ("/", "Panel de control"),
        ("/tareas", "Catálogo"),
        ("/tareas?tab=ocultas", "Ocultas"),
        ("/ejecuciones", "Lotes en curso"),
        ("/ejecuciones?tab=historial", "Ejecuciones registradas"),
        ("/solicitudes", "Mis solicitudes"),
        ("/solicitudes?tab=aprobaciones", "Pendientes de decisión"),
        ("/credenciales", "Contraseñas candidatas"),
        ("/sistema?tab=auditoria", "Registro de autenticación"),
        ("/sistema?tab=rendimiento", "Latencia por ruta"),
        ("/sistema?tab=ajustes", "Modo simulación"),
        ("/sistema?tab=acerca-de", "Identidad institucional"),
        ("/cuenta", "Mi cuenta"),
        ("/programaciones/nueva", "Nueva programación"),
    ],
)
def test_paginas_canonicas_responden(client, url, marker):
    _login(client)
    response = client.get(url)
    assert response.status_code == 200, url
    assert marker in response.get_data(as_text=True), url


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/history", "/ejecuciones?tab=historial"),
        ("/modulos", "/tareas?tab=ocultas"),
        ("/aprobaciones", "/solicitudes?tab=aprobaciones"),
        ("/mis-solicitudes", "/solicitudes?tab=mias"),
        ("/passwords", "/credenciales"),
        ("/audit", "/sistema?tab=auditoria"),
        ("/metricas", "/sistema?tab=rendimiento"),
        ("/configuracion", "/sistema?tab=ajustes"),
        ("/acerca-de", "/sistema?tab=acerca-de"),
    ],
)
def test_rutas_antiguas_redirigen(client, old, new):
    _login(client)
    response = client.get(old)
    assert response.status_code == 301
    assert response.headers["Location"].endswith(new)


def test_alias_del_panel_redirige(client):
    _login(client)
    response = client.get("/panel")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_operador_no_ve_administracion(client):
    _login(client, "operador", "operador-password-123")
    body = client.get("/tareas").get_data(as_text=True)
    assert "/credenciales" in body
    assert "/solicitudes" in body
    assert "/usuarios" not in body
    assert "/sistema" not in body
    assert client.get("/sistema").status_code == 403


def test_programar_preselecciona_la_intencion(client):
    _login(client)
    body = client.get("/tarea/zona-todas?mode=schedule").get_data(as_text=True)
    assert '"schedule_intent": "schedule"' in body
