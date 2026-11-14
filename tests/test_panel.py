"""Panel de control, portada y accesos a informes desde el catálogo."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import app as app_module
from acc import runtime
from models.batch import ExecutionBatch
from models.ops import HostGroup, InventoryHost, Schedule
from models.user import User


def _csrf_token(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _login(client, username="admin", password="admin-password-123"):
    token = _csrf_token(client)
    return client.post(
        "/login",
        data={"csrf_token": token, "username": username, "password": password},
    )


def _client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "panel.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    app_module.app.config.update(TESTING=True)
    return db_path, app_module.app.test_client()


def test_panel_requires_login(tmp_path, monkeypatch):
    _db_path, client = _client(tmp_path, monkeypatch)
    with client:
        response = client.get("/")
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_login_redirige_al_panel(tmp_path, monkeypatch):
    _db_path, client = _client(tmp_path, monkeypatch)
    with client:
        response = _login(client)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")


def test_panel_muestra_kpis_inventario_y_ejecucion(tmp_path, monkeypatch):
    db_path, client = _client(tmp_path, monkeypatch)
    group = HostGroup.create("Aulas", db_path=db_path)
    InventoryHost.add(group.id, "10.20.4.11", label="aula-03", db_path=db_path)
    InventoryHost.add(group.id, "10.20.4.12", label="aula-03", db_path=db_path)

    ExecutionBatch.create_batch(
        "batch-panel",
        "admin",
        "TZ",
        "America/Havana",
        "Aulas",
        total_hosts=2,
        status="running",
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        "batch-panel", "10.20.4.11", "success", db_path=db_path
    )
    Schedule.create(
        "NTP · Aulas",
        "zona-todas",
        datetime.now(UTC) + timedelta(hours=2),
        {},
        recurrence="daily",
        created_by="admin",
        db_path=db_path,
    )

    with client:
        _login(client)
        response = client.get("/")
        body = response.get_data(as_text=True)

    assert response.status_code == 200
    for label in (
        "Hosts gestionados",
        "Programaciones activas",
        "Éxito del último lote",
        "Alertas abiertas",
        "Inventario de hosts",
        "Ejecución en curso",
        "Alertas",
        "Próximas programaciones",
        "Nueva programación",
    ):
        assert label in body
    assert "10.20.4.11" in body
    assert "aula-03" in body
    assert "Ver todo" in body
    assert "batch-panel" in body


def test_menu_lateral_sin_rotulos_de_seccion(tmp_path, monkeypatch):
    _db_path, client = _client(tmp_path, monkeypatch)
    with client:
        _login(client)
        body = client.get("/").get_data(as_text=True)

    nav = body.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0]
    for item in (
        "Panel",
        "Tareas",
        "Ejecuciones",
        "Inventario",
        "Programaciones",
        "Solicitudes",
        "Credenciales",
        "Usuarios",
        "Sistema",
    ):
        assert item in nav
    for label in ("Operación", "Seguridad", "Administración"):
        assert label in nav
    assert "nav-label" in nav
    assert "Informes" not in nav
    assert "Auditoría" not in nav
    assert "Módulos" not in nav


def test_catalogo_muestra_opcion_de_informe_por_tarjeta(tmp_path, monkeypatch):
    _db_path, client = _client(tmp_path, monkeypatch)
    with client:
        _login(client)
        body = client.get("/tareas").get_data(as_text=True)

    assert 'href="/tarea/zona-todas/informe.pdf"' in body
    assert 'href="/tarea/apt-upgrade/migracion.pdf"' in body


def test_panel_alias_redirige_a_la_portada(tmp_path, monkeypatch):
    _db_path, client = _client(tmp_path, monkeypatch)
    with client:
        _login(client)
        response = client.get("/panel")
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")
