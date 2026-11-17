# tests/test_historial_privado_2026_11_16.py
# Historial privado por usuario.
#
# El historial de ejecuciones pasa a ser privado: cada usuario ve los lotes
# que lanzó y un administrador puede abrir la vista global con ``?all=1``.
# Los lotes heredados se atribuyen en la migración v20 por su ``operator`` o,
# en el caso de las programaciones, por el dueño de la programación.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import app as app_module
from acc import runtime
from acc.scheduling import _schedule_owner_id
from models import db as db_module
from models.batch import ExecutionBatch
from models.db import SCHEMA_VERSION, get_connection, init_schema
from models.ops import Schedule
from models.user import User
from services import scheduler as scheduler_module
from services.rate_limiter import LIMITER

PASSWORD = "clave-de-prueba-123"


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """Base de datos con un admin y dos operadores, y un cliente de pruebas."""
    db_path = str(tmp_path / "historial.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    init_schema(db_path)
    User.create_or_update("admin", PASSWORD, db_path=db_path, role="admin")
    User.create_or_update("alice", PASSWORD, db_path=db_path, role="operator")
    User.create_or_update("bob", PASSWORD, db_path=db_path, role="operator")
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        yield {"db_path": db_path, "client": client}


def _user_id(db_path: str, username: str) -> int:
    user = User.get_by_username(username, db_path=db_path)
    assert user is not None
    return int(user.id)


def _csrf(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token or ""


def _login(client, username: str) -> None:
    LIMITER.reset("login:127.0.0.1")
    token = _csrf(client)
    response = client.post(
        "/login",
        data={"username": username, "password": PASSWORD, "csrf_token": token},
    )
    assert response.status_code in (200, 302)


def _batch(
    db_path: str,
    batch_id: str,
    owner: str,
    *,
    task_id: str = "zona-todas",
    target: str = "10.0.0.1",
) -> str:
    user = User.get_by_username(owner, db_path=db_path)
    ExecutionBatch.create_batch(
        batch_id=batch_id,
        operator=owner,
        action="TZ",
        value="America/Caracas",
        target=target,
        total_hosts=1,
        status="running",
        task_id=task_id,
        owner_user_id=int(user.id) if user is not None else 0,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(batch_id, target, "success", db_path=db_path)
    ExecutionBatch.finish_batch(batch_id, status="success", db_path=db_path)
    return batch_id


def _pdf_text(response) -> str:
    return response.data.decode("cp1252", errors="replace")


# ---------------------------------------------------------------------------
# Migración v20: atribución del historial heredado
# ---------------------------------------------------------------------------
def test_migracion_v20_atribuye_lotes_por_operador(entorno):
    db_path = entorno["db_path"]
    _batch(db_path, "lote-alice", "alice")
    _batch(db_path, "lote-huerfano", "desconocido")
    # Simula una base anterior a la migración: sin dueño asignado.
    with get_connection(db_path) as conn:
        conn.execute("UPDATE execution_batches SET owner_user_id = 0")
        db_module._migrate_v20(conn, backfill=True)

    alice = ExecutionBatch.get_batch("lote-alice", db_path=db_path)
    huerfano = ExecutionBatch.get_batch("lote-huerfano", db_path=db_path)
    assert alice is not None and huerfano is not None
    assert alice.owner_user_id == _user_id(db_path, "alice")
    # Un operador que no existe como usuario no se inventa: queda sin dueño.
    assert huerfano.owner_user_id == 0


def test_migracion_v20_hereda_el_dueno_de_la_programacion(entorno):
    db_path = entorno["db_path"]
    alice_id = _user_id(db_path, "alice")
    Schedule.create(
        "rotación nocturna",
        "zona-todas",
        datetime.now(UTC) + timedelta(hours=1),
        {},
        created_by="alice",
        owner_user_id=alice_id,
        db_path=db_path,
    )
    _batch(db_path, "lote-agenda", "agenda:rotación nocturna")
    with get_connection(db_path) as conn:
        conn.execute("UPDATE execution_batches SET owner_user_id = 0")
        db_module._migrate_v20(conn, backfill=True)

    lote = ExecutionBatch.get_batch("lote-agenda", db_path=db_path)
    assert lote is not None
    assert lote.owner_user_id == alice_id


def test_migracion_v20_es_idempotente_y_crea_el_indice(entorno):
    db_path = entorno["db_path"]
    _batch(db_path, "lote-alice", "alice")
    alice_id = _user_id(db_path, "alice")
    with get_connection(db_path) as conn:
        indices = {
            row["name"]
            for row in conn.execute("PRAGMA index_list('execution_batches')")
        }
        assert "idx_execution_batches_owner" in indices
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert version == SCHEMA_VERSION

        # Simula una fila heredada y comprueba que sin ``backfill`` no se
        # recorre la tabla: la atribución queda para la primera migración.
        conn.execute("UPDATE execution_batches SET owner_user_id = 0")
        db_module._migrate_v20(conn, backfill=False)
        assert (
            conn.execute(
                "SELECT owner_user_id FROM execution_batches WHERE batch_id = ?",
                ("lote-alice",),
            ).fetchone()[0]
            == 0
        )

        db_module._migrate_v20(conn, backfill=True)
        assert (
            conn.execute(
                "SELECT owner_user_id FROM execution_batches WHERE batch_id = ?",
                ("lote-alice",),
            ).fetchone()[0]
            == alice_id
        )


def test_init_schema_no_retrocede_la_version(entorno):
    db_path = entorno["db_path"]
    init_schema(db_path)
    with get_connection(db_path) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Aislamiento del historial en la web
# ---------------------------------------------------------------------------
def test_historial_muestra_solo_lotes_propios(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "aaaaaaaa-alice", "alice", target="10.50.0.1")
    _batch(db_path, "bbbbbbbb-bob", "bob", target="10.60.0.1")

    _login(client, "alice")
    body = client.get("/ejecuciones", query_string={"tab": "historial"}).get_data(
        as_text=True
    )

    assert "aaaaaaaa" in body
    assert "bbbbbbbb" not in body


def test_detalle_de_lote_ajeno_devuelve_404(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "bbbbbbbb-bob", "bob")

    _login(client, "alice")
    response = client.get("/batches/bbbbbbbb-bob")

    # 404, no 403: no se confirma que el lote exista.
    assert response.status_code == 404


def test_pdf_de_lote_ajeno_devuelve_404(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "bbbbbbbb-bob", "bob")

    _login(client, "alice")
    response = client.get("/batches/bbbbbbbb-bob/migracion.pdf")

    assert response.status_code == 404
    assert response.mimetype != "application/pdf"


def test_informe_pdf_usa_el_ultimo_lote_propio(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "bbbbbbbb-bob", "bob", target="10.60.0.1")
    _batch(db_path, "aaaaaaaa-alice", "alice", target="10.50.0.1")

    _login(client, "alice")
    response = client.get("/tarea/zona-todas/informe.pdf")

    assert response.status_code == 200
    text = _pdf_text(response)
    assert "10.50.0.1" in text
    assert "10.60.0.1" not in text


def test_sin_lotes_propios_el_informe_no_cae_en_el_de_otro(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "bbbbbbbb-bob", "bob", target="10.60.0.1")

    _login(client, "alice")
    response = client.get("/tarea/zona-todas/informe.pdf", follow_redirects=False)

    assert response.status_code == 302
    assert "/tarea/zona-todas" in response.headers["Location"]


def test_exportacion_json_solo_incluye_lotes_propios(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "aaaaaaaa-alice", "alice")
    _batch(db_path, "bbbbbbbb-bob", "bob")

    _login(client, "alice")
    payload = client.get("/ejecuciones/exportar").get_json()

    assert [item["batch_id"] for item in payload["batches"]] == ["aaaaaaaa-alice"]
    hosts = payload["batches"][0]["hosts"]
    assert all(host.get("batch_id") is None for host in hosts)


def test_admin_ve_solo_lo_suyo_y_todo_con_all(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "bbbbbbbb-bob", "bob")
    _batch(db_path, "cccccccc-admin", "admin")

    _login(client, "admin")
    propio = client.get("/ejecuciones", query_string={"tab": "historial"}).get_data(
        as_text=True
    )
    global_view = client.get(
        "/ejecuciones", query_string={"tab": "historial", "all": "1"}
    ).get_data(as_text=True)

    assert "cccccccc" in propio
    assert "bbbbbbbb" not in propio
    assert "bbbbbbbb" in global_view
    assert "Vista global de administración" in global_view

    exportado = client.get("/ejecuciones/exportar", query_string={"all": "1"})
    ids = {item["batch_id"] for item in exportado.get_json()["batches"]}
    assert ids == {"bbbbbbbb-bob", "cccccccc-admin"}


def test_operador_no_puede_forzar_la_vista_global(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "bbbbbbbb-bob", "bob")

    _login(client, "alice")
    body = client.get("/ejecuciones", query_string={"all": "1"}).get_data(as_text=True)

    # ``?all=1`` solo se honra para administradores.
    assert "bbbbbbbb" not in body


def test_stats_se_acotan_al_usuario(entorno):
    db_path, client = entorno["db_path"], entorno["client"]
    _batch(db_path, "aaaaaaaa-alice", "alice")
    _batch(db_path, "bbbbbbbb-bob", "bob")

    _login(client, "alice")
    response = client.get("/")
    assert response.status_code == 200

    # El KPI de lotes registrados no suma el lote del otro operador.
    assert (
        ExecutionBatch.get_stats(
            db_path=db_path, owner_user_id=_user_id(db_path, "alice")
        )["batches"]
        == 1
    )
    assert (
        ExecutionBatch.get_stats(
            db_path=db_path, owner_user_id=_user_id(db_path, "bob")
        )["batches"]
        == 1
    )
    assert ExecutionBatch.get_stats(db_path=db_path)["batches"] == 2


def test_lote_programado_hereda_dueno_de_la_programacion(entorno, monkeypatch):
    db_path = entorno["db_path"]
    alice_id = _user_id(db_path, "alice")
    schedule = Schedule.create(
        "rotación",
        "contrasena",
        datetime.now(UTC) + timedelta(hours=1),
        {
            "target": "10.42.0.50",
            "target_mode": "manual",
            "task_id": "contrasena",
            "admin_user": "root",
            "action": "PASSWORD",
            "value": "ladmin",
            "ntp_policy": "keep",
            "master_password_enc": scheduler_module.encrypt_secret("clave-maestra"),
            "new_password_enc": scheduler_module.encrypt_secret("Clave-Nueva-123"),
        },
        created_by="alice",
        owner_user_id=alice_id,
        db_path=db_path,
    )
    calls: list[dict] = []

    class FakeQueue:
        def submit(self, queue_task_id, func, /, *args, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(runtime, "get_job_queue", lambda: FakeQueue())

    assert app_module.launch_scheduled(schedule.to_dict(), dict(schedule.params))

    assert calls and calls[0]["owner_user_id"] == alice_id


def test_schedule_owner_id_acepta_fila_y_dict():
    class Fila(dict):
        pass

    assert _schedule_owner_id(Fila(owner_user_id="7")) == 7
    assert _schedule_owner_id({"owner_user_id": None}) == 0
    assert _schedule_owner_id({}) == 0


# ---------------------------------------------------------------------------
# Compatibilidad del esquema
# ---------------------------------------------------------------------------
def test_la_columna_owner_user_id_es_obligatoria_con_valor_por_defecto(entorno):
    db_path = entorno["db_path"]
    with get_connection(db_path) as conn:
        columnas = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(execution_batches)")
        }
    assert columnas["owner_user_id"]["notnull"] == 1
    assert columnas["owner_user_id"]["dflt_value"] == "0"


def test_export_history_sin_filtro_incluye_todo(entorno):
    db_path = entorno["db_path"]
    _batch(db_path, "aaaaaaaa-alice", "alice")
    _batch(db_path, "bbbbbbbb-bob", "bob")

    todo = ExecutionBatch.export_history(db_path=db_path)
    solo_alice = ExecutionBatch.export_history(
        db_path=db_path, owner_user_id=_user_id(db_path, "alice")
    )

    assert {item["batch_id"] for item in todo} == {
        "aaaaaaaa-alice",
        "bbbbbbbb-bob",
    }
    assert [item["batch_id"] for item in solo_alice] == ["aaaaaaaa-alice"]
