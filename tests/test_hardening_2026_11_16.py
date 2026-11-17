# tests/test_hardening_2026_11_16.py
# Pruebas de las mejoras de hardening: cadena de auditoría, estado
# interrumpido, backup de SQLite, inventario CSV, métricas Prometheus y
# catálogo declarativo.
from __future__ import annotations

import io
import json
import os

import pytest

import app as app_module
from acc import runtime
from acc.task_state import _recover_task_state
from models.batch import ExecutionBatch
from models.db import (
    backup_database,
    get_connection,
    init_schema,
    integrity_check,
)
from models.ops import HostGroup, InventoryHost
from models.user import AuthEvent, User
from services.rate_limiter import LIMITER

PASSWORD = "clave-de-prueba-123"


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hardening.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    init_schema(db_path)
    User.create_or_update("admin", PASSWORD, db_path=db_path, role="admin")
    User.create_or_update("alice", PASSWORD, db_path=db_path, role="operator")
    User.create_or_update("vero", PASSWORD, db_path=db_path, role="viewer")
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        yield {"db_path": db_path, "client": client}


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token or ""


def _login(client, username: str, password: str = PASSWORD) -> None:
    LIMITER.reset("login:127.0.0.1")
    token = _csrf(client)
    response = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
    )
    assert response.status_code in (200, 302)


# ---------------------------------------------------------------------------
# Cadena de auditoría
# ---------------------------------------------------------------------------
def test_audit_chain_valida_y_detecta_manipulacion(entorno):
    db_path = entorno["db_path"]
    AuthEvent.record("alice", "10.0.0.5", "login_ok", "local", db_path=db_path)
    AuthEvent.record("alice", "10.0.0.5", "task_cancel", "tarea=abc", db_path=db_path)
    AuthEvent.record("admin", "10.0.0.6", "role_change", "alice=admin", db_path=db_path)

    result = AuthEvent.verify_chain(db_path=db_path, secret="secreto")
    assert result["ok"] is True
    assert result["events"] == 3
    assert result["hashed"] == 3
    assert result["head"]

    with get_connection(db_path) as conn:
        conn.execute("UPDATE auth_events SET detail = 'otra cosa' WHERE id = 2")

    broken = AuthEvent.verify_chain(db_path=db_path)
    assert broken["ok"] is False
    assert broken["broken_at"] == 2
    assert "hash" in broken["reason"]


def test_audit_chain_detecta_borrado_intermedio(entorno):
    db_path = entorno["db_path"]
    for idx in range(3):
        AuthEvent.record("alice", "10.0.0.5", "login_failed", str(idx), db_path=db_path)

    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM auth_events WHERE id = 2")

    result = AuthEvent.verify_chain(db_path=db_path)
    assert result["ok"] is False
    assert "enlace" in result["reason"]


def test_audit_checkpoint_firmado_y_truncamiento(entorno):
    db_path = entorno["db_path"]
    for idx in range(2):
        AuthEvent.record("alice", "10.0.0.5", "login_ok", str(idx), db_path=db_path)
    checkpoint = AuthEvent.create_checkpoint("secreto", note="prueba", db_path=db_path)
    assert checkpoint["upto_event_id"] == 2

    result = AuthEvent.verify_chain(db_path=db_path, secret="secreto")
    assert result["ok"] is True
    assert result["checkpoint"]["verified_signature"] is True

    # Firma de otro secreto: la cadena sigue bien, pero el ancla no es válida.
    otra = AuthEvent.verify_chain(db_path=db_path, secret="distinto")
    assert otra["ok"] is True
    assert otra["checkpoint"]["verified_signature"] is False

    # Borrar la cola deja un checkpoint que apunta a eventos inexistentes.
    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM auth_events WHERE id = 2")
    truncado = AuthEvent.verify_chain(db_path=db_path, secret="secreto")
    assert truncado["ok"] is False
    assert "checkpoint" in truncado["reason"]


def test_audit_export_incluye_eventos_y_cadena(entorno):
    db_path = entorno["db_path"]
    AuthEvent.record("alice", "10.0.0.5", "login_ok", "local", db_path=db_path)
    events = AuthEvent.export_events(db_path=db_path)
    assert len(events) == 1
    assert events[0]["event"] == "login_ok"
    assert len(events[0]["event_hash"]) == 64
    json.dumps(events)  # serializable


# ---------------------------------------------------------------------------
# Estado interrumpido
# ---------------------------------------------------------------------------
def test_tarea_interrumpida_se_distingue_de_un_fallo(entorno):
    task_id = app_module.create_task()
    app_module.update_task(task_id, {"status": "running", "message": "En curso"})
    with app_module._tasks_lock:
        app_module._tasks.pop(task_id, None)

    _recover_task_state()

    task = app_module._get_task(task_id)
    assert task is not None
    assert task["status"] == "error"
    assert task["interrupted"] is True
    assert task["overall_result"] == "interrupted"
    assert "desconocido" in task["message"]


def test_lote_huerfano_se_marca_interrumpido(entorno):
    db_path = entorno["db_path"]
    ExecutionBatch.create_batch(
        batch_id="orphan-1",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.1",
        status="running",
        db_path=db_path,
    )
    assert ExecutionBatch.reconcile_orphans(db_path=db_path) == 1
    batch = ExecutionBatch.get_batch("orphan-1", db_path=db_path)
    assert batch is not None
    assert batch.status == "error"
    assert batch.interrupted is True


# ---------------------------------------------------------------------------
# SQLite: integridad y backup consistente
# ---------------------------------------------------------------------------
def test_backup_db_es_consistente_y_privado(entorno, tmp_path):
    db_path = entorno["db_path"]
    User.create_or_update("bob", PASSWORD, db_path=db_path, role="operator")
    target = tmp_path / "copias" / "acc-backup.db"

    result = backup_database(db_path, target)

    assert result == target
    assert target.exists()
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert integrity_check(str(target), quick=True)["ok"] is True
    with get_connection(str(target)) as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE username = 'bob'"
        ).fetchone()
    assert row is not None


def test_integrity_check_detecta_base_corrupta(tmp_path):
    corrupt = tmp_path / "corrupta.db"
    corrupt.write_bytes(b"esto no es una base de datos SQLite" * 50)
    result = integrity_check(str(corrupt), quick=True)
    assert result["ok"] is False
    assert result["details"]


# ---------------------------------------------------------------------------
# Historial global: enlaces con all=1
# ---------------------------------------------------------------------------
def _batch(db_path: str, batch_id: str, owner: str) -> None:
    user = User.get_by_username(owner, db_path=db_path)
    ExecutionBatch.create_batch(
        batch_id=batch_id,
        operator=owner,
        action="TZ",
        value="UTC",
        target="10.0.0.1",
        total_hosts=1,
        status="running",
        owner_user_id=int(user.id) if user is not None else 0,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(batch_id, "10.0.0.1", "success", db_path=db_path)
    ExecutionBatch.finish_batch(batch_id, status="success", db_path=db_path)


def test_admin_abre_el_detalle_del_lote_desde_la_vista_global(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "bbbbbbbb-bob", "admin")  # el lote es de admin
    _batch(db_path, "cccccccc-alice", "alice")

    _login(client, "admin")
    body = client.get(
        "/ejecuciones", query_string={"tab": "historial", "all": "1"}
    ).get_data(as_text=True)
    # El enlace de la vista global conserva el alcance global.
    assert "all=1" in body
    assert client.get("/batches/cccccccc-alice", query_string={"all": "1"}).status_code == 200
    # Sin pedir la vista global, el lote ajeno sigue oculto.
    assert client.get("/batches/cccccccc-alice").status_code == 404


# ---------------------------------------------------------------------------
# Inventario CSV
# ---------------------------------------------------------------------------
def test_inventario_exporta_csv(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    alice = User.get_by_username("alice", db_path=db_path)
    group = HostGroup.create(
        "Oficina", db_path=db_path, owner_user_id=int(alice.id)
    )
    InventoryHost.add(group.id, "10.20.0.1", label="puesto 1", db_path=db_path)

    _login(client, "alice")
    response = client.get("/inventario/exportar.csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert "10.20.0.1" in body
    assert "Oficina" in body
    assert "no-store" in response.headers["Cache-Control"]


def test_inventario_importa_csv_sin_borrar(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    alice = User.get_by_username("alice", db_path=db_path)
    group = HostGroup.create("Sucursal", db_path=db_path, owner_user_id=int(alice.id))

    _login(client, "alice")
    token = _csrf(client)
    csv_data = (
        "address,label,criticality\n"
        "10.30.0.1,caja 1,high\n"
        "10.30.0.2,caja 2,\n"
        "no-es-ip,rota,\n"
        "10.30.0.1,duplicada,\n"
    )
    response = client.post(
        "/inventario/importar",
        data={
            "csrf_token": token,
            "group_id": str(group.id),
            "csv_file": (io.BytesIO(csv_data.encode()), "hosts.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "2 host(s) añadido(s)" in body

    hosts = HostGroup.hosts_of(group.id, db_path=db_path)
    assert {host.address for host in hosts} == {"10.30.0.1", "10.30.0.2"}


def test_viewer_no_puede_importar_csv(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    alice = User.get_by_username("alice", db_path=db_path)
    group = HostGroup.create("Privado", db_path=db_path, owner_user_id=int(alice.id))

    _login(client, "vero")
    token = _csrf(client)
    response = client.post(
        "/inventario/importar",
        data={
            "csrf_token": token,
            "group_id": str(group.id),
            "csv_file": (io.BytesIO(b"address\n10.40.0.1\n"), "hosts.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Métricas Prometheus y catálogo
# ---------------------------------------------------------------------------
def test_metrics_requiere_autenticacion(entorno):
    client = entorno["client"]
    response = client.get("/metrics")
    assert response.status_code == 403


def test_metrics_prometheus_para_admin(entorno):
    client = entorno["client"]
    _login(client, "admin")
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "acc_info{" in body
    assert "acc_jobs_pending" in body
    assert "acc_batches_total" in body
    assert response.headers["Cache-Control"] == "no-store"


def test_metrics_prometheus_con_token(entorno, monkeypatch):
    from acc.web import admin as admin_module

    monkeypatch.setattr(admin_module, "PROMETHEUS_TOKEN", "token-de-scrape")
    client = entorno["client"]
    assert client.get("/metrics").status_code == 403
    ok = client.get("/metrics", headers={"Authorization": "Bearer token-de-scrape"})
    assert ok.status_code == 200
    denied = client.get("/metrics", headers={"Authorization": "Bearer otro"})
    assert denied.status_code == 403


def test_api_catalogo_filtra_por_rol(entorno):
    client = entorno["client"]
    _login(client, "admin")
    payload = client.get("/api/catalogo").get_json()
    ids = {task["id"] for task in payload["tasks"]}
    assert "contrasena" in ids
    contrasena = next(task for task in payload["tasks"] if task["id"] == "contrasena")
    assert contrasena["requires_approval"] is True
    assert "playbook" not in contrasena

    # Cerrar sesión de admin (el logout exige POST y CSRF) y entrar como viewer.
    client.post("/logout", data={"csrf_token": _csrf(client)})
    _login(client, "vero")
    viewer_payload = client.get("/api/catalogo").get_json()
    # Un viewer no recibe tareas que exigen operator/admin.
    assert viewer_payload["tasks"] == []


def test_api_catalogo_requiere_sesion(entorno):
    assert entorno["client"].get("/api/catalogo").status_code in (302, 401)
