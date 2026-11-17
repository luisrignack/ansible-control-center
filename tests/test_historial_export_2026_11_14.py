"""Exportación y copia previa del historial (2026-11-14).

El borrado del historial era irreversible y no dejaba rastro de los lotes
eliminados. Ahora la ruta de borrado escribe una copia JSON en
``data/backups/`` antes de tocar la base y, si no puede escribirla, no borra.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app as app_module
from acc import runtime
from models.batch import ExecutionBatch
from models.db import init_schema
from models.user import User


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "historial.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    init_schema(db_path)
    User.create_or_update("admin", "admin-password-123", db_path=db_path, role="admin")
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def _login(client) -> None:
    with client.session_transaction() as sess:
        sess.clear()
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


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        return sess.get("_csrf_token")


def _admin_id(db_path: str) -> int:
    """Identificador del admin de la fixture: dueño de los lotes de prueba.

    Las pruebas de modelo usan una base vacía sin usuarios; en ese caso el
    lote queda sin dueño (``0``), igual que una fila antigua.
    """
    user = User.get_by_username("admin", db_path=db_path)
    return int(user.id) if user is not None else 0


def _make_batch(db_path: str, batch_id: str, status: str = "success") -> None:
    ExecutionBatch.create_batch(
        batch_id,
        "admin",
        "TZ",
        "America/Caracas",
        "10.0.0.0/24",
        total_hosts=1,
        status="running",
        owner_user_id=_admin_id(db_path),
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(batch_id, "10.0.0.10", "success", db_path=db_path)
    if status != "running":
        ExecutionBatch.finish_batch(batch_id, status=status, db_path=db_path)


def test_exportar_historial_descarga_json(client):
    db_path = runtime.get_db_path()
    _make_batch(db_path, "lote-export-1")
    _login(client)

    response = client.get("/ejecuciones/exportar")

    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"
    payload = json.loads(response.get_data(as_text=True))
    assert [batch["batch_id"] for batch in payload["batches"]] == ["lote-export-1"]
    hosts = payload["batches"][0]["hosts"]
    assert hosts and hosts[0]["host"] == "10.0.0.10"
    assert "id" not in hosts[0]


def test_exportar_exige_sesion(client):
    response = client.get("/ejecuciones/exportar")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_borrar_historial_guarda_copia_previa(client):
    db_path = runtime.get_db_path()
    _make_batch(db_path, "lote-borrar-1")
    _login(client)

    response = client.post(
        "/ejecuciones/borrar",
        data={"confirmar": "BORRAR", "csrf_token": _csrf(client)},
    )
    assert response.status_code == 302
    assert ExecutionBatch.get_batch("lote-borrar-1", db_path=db_path) is None

    backups = sorted((Path(db_path).parent / "backups").glob("historial-*.json"))
    assert len(backups) == 1
    payload = json.loads(backups[0].read_text(encoding="utf-8"))
    assert payload["source_db"] == db_path
    assert [batch["batch_id"] for batch in payload["batches"]] == ["lote-borrar-1"]
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_si_la_copia_falla_no_se_borra_nada(client, monkeypatch):
    db_path = runtime.get_db_path()
    _make_batch(db_path, "lote-protegido-1")
    _login(client)

    def _fail(*args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(ExecutionBatch, "backup_history", staticmethod(_fail))

    response = client.post(
        "/ejecuciones/borrar",
        data={"confirmar": "BORRAR", "csrf_token": _csrf(client)},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "No se ha borrado nada" in response.get_data(as_text=True)
    assert ExecutionBatch.get_batch("lote-protegido-1", db_path=db_path) is not None


def test_export_history_no_incluye_ids_internos(tmp_path):
    db_path = str(tmp_path / "export.db")
    init_schema(db_path)
    _make_batch(db_path, "lote-sin-id")

    history = ExecutionBatch.export_history(db_path=db_path)

    assert len(history) == 1
    assert "id" not in history[0]
    assert history[0]["batch_id"] == "lote-sin-id"
    assert "batch_id" not in history[0]["hosts"][0]
