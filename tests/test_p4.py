# tests/test_p4.py
# Pruebas de la fase P4: lotes, todos los hosts, contraseñas acotadas,
# vista previa, verificación e idempotencia registrada por host.
from __future__ import annotations

import io
import time

import pytest

import app as app_module
from acc import execution as acc_execution
from acc import runtime
from models.batch import ExecutionBatch
from models.db import init_schema
from models.password import PasswordRecord
from models.user import User


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


def _post(client, url, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload, **kwargs)


def _login(client):
    return _post(
        client, "/login", {"username": "admin", "password": "admin-password-123"}
    )


def _wait_for_task(client, task_id: str) -> dict:
    status: dict = {}
    for _ in range(200):
        status = client.get(f"/api/task-status/{task_id}").get_json()
        if status.get("completed"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"La tarea no terminó: {status}")


def _start_execution(client, mode: str, **overrides):
    data = {
        "target_ip": "10.0.0.0/24",
        "admin_user": "root",
        "password_origin": "temporal",
        "action": "TZ",
        "value": "UTC",
        "time_mode": "utc",
        "ntp_policy": "keep",
        "mode": mode,
        "password_file": (
            io.BytesIO(b"clave-uno\nclave-dos\n"),
            "passwords.txt",
        ),
    }
    data.update(overrides)
    response = _post(client, "/tareas", data=data, content_type="multipart/form-data")
    assert response.status_code == 302, response.data
    task_id = response.headers["Location"].rstrip("/").split("/")[-1]
    return task_id


def _ok(host, password=None, **extra):
    result = {
        "success": True,
        "host": host,
        "error": "",
        "before_state": {"timezone": "Etc/UTC", "ntp": True, "epoch": 100},
        "after_state": {"timezone": "UTC", "ntp": True, "epoch": 100},
        "changed": True,
        "verified": True,
        "warnings": "",
    }
    result.update(extra)
    return result


def test_batch_model_tracks_partial_results(tmp_path):
    db_path = str(tmp_path / "batch.db")
    init_schema(db_path)

    ExecutionBatch.create_batch(
        batch_id="batch-1",
        operator="admin",
        action="TZ",
        value="Europe/Madrid",
        target="10.0.0.0/24",
        time_mode="utc",
        ntp_policy="restore",
        total_hosts=3,
        status="running",
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="batch-1",
        host="10.0.0.1",
        result="success",
        duration_ms=12,
        before_state={"timezone": "UTC"},
        after_state={"timezone": "Europe/Madrid"},
        password_hash="abcd1234",
        password_origin="stored",
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="batch-1",
        host="10.0.0.2",
        result="failure",
        error="permiso denegado",
        duration_ms=30,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="batch-1",
        host="10.0.0.3",
        result="skipped",
        db_path=db_path,
    )

    batch = ExecutionBatch.finish_batch("batch-1", db_path=db_path)
    assert batch is not None
    assert batch.status == "partial"
    assert batch.dry_run is False
    assert batch.success_count == 1
    assert batch.failure_count == 1
    assert batch.skipped_count == 1
    assert batch.total_hosts == 3
    assert batch.duration_seconds is not None

    hosts = ExecutionBatch.get_host_results("batch-1", db_path=db_path)
    assert [host.host for host in hosts] == [
        "10.0.0.1",
        "10.0.0.2",
        "10.0.0.3",
    ]
    assert hosts[0].before_state == {"timezone": "UTC"}
    assert hosts[0].after_state == {"timezone": "Europe/Madrid"}
    assert hosts[1].error == "permiso denegado"


def test_execution_processes_all_hosts_and_retries_passwords(client, monkeypatch):
    _login(client)
    db_path = runtime.get_db_path()
    calls = []

    monkeypatch.setattr(
        acc_execution,
        "resolve_target",
        lambda _target: ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
    )
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )

    def fake_apply(host, password=None, **_kwargs):
        calls.append((host, password))
        if host == "10.0.0.1":
            return _ok(host, password)
        if host == "10.0.0.2":
            if password == "clave-dos":
                return _ok(host, password)
            return {
                "success": False,
                "host": host,
                "error": "la verificación falló",
                "before_state": {"timezone": "Etc/UTC"},
                "after_state": {"timezone": "Etc/UTC"},
            }
        return {
            "success": False,
            "host": host,
            "error": "no se pudo aplicar",
            "before_state": None,
            "after_state": None,
        }

    monkeypatch.setattr(acc_execution, "apply_host", fake_apply)

    task_id = _start_execution(client, "execute")
    status = _wait_for_task(client, task_id)

    assert status["overall_result"] == "partial", status
    assert status["batch_id"]

    batches = ExecutionBatch.get_batches(db_path=db_path)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.status == "partial"
    assert batch.total_hosts == 3
    assert batch.success_count == 2
    assert batch.failure_count == 1
    assert batch.operator == "admin"
    assert batch.time_mode == "utc"
    assert batch.ntp_policy == "keep"

    hosts = ExecutionBatch.get_host_results(batch.batch_id, db_path=db_path)
    assert [host.host for host in hosts] == [
        "10.0.0.1",
        "10.0.0.2",
        "10.0.0.3",
    ]
    assert hosts[0].result == "success"
    assert hosts[1].result == "success"
    assert hosts[1].password_hash == PasswordRecord.hash_short("clave-dos")
    assert hosts[2].result == "failure"
    assert hosts[2].error == "no se pudo aplicar"

    # El host 2 autenticó con la primera contraseña pero el playbook falló:
    # se probó la segunda. El host 3 también agotó todas las candidatas.
    # Los hosts se procesan en paralelo: el orden de las llamadas ya no es
    # determinista, pero cada host conserva sus reintentos.
    assert sorted(calls) == sorted(
        [
            ("10.0.0.1", "clave-uno"),
            ("10.0.0.2", "clave-uno"),
            ("10.0.0.2", "clave-dos"),
            ("10.0.0.3", "clave-uno"),
            ("10.0.0.3", "clave-dos"),
        ]
    )

    history = client.get("/ejecuciones", query_string={"tab": "historial"})
    assert history.status_code == 200
    assert b"10.0.0.0/24" in history.data
    assert b"partial" in history.data

    detail = client.get(f"/batches/{batch.batch_id}")
    assert detail.status_code == 200
    assert b"10.0.0.1" in detail.data
    assert b"10.0.0.3" in detail.data


def test_preview_inspects_without_applying_changes(client, monkeypatch):
    _login(client)
    db_path = runtime.get_db_path()

    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.10"])
    monkeypatch.setattr(
        acc_execution,
        "inspect_host",
        lambda host, **_kwargs: {
            "success": True,
            "host": host,
            "state": {
                "timezone": "Etc/UTC",
                "ntp": False,
                "epoch": 100,
                "local": "1970-01-01T00:01:40+0000",
            },
            "error": "",
        },
    )

    def must_not_apply(**_kwargs):
        raise AssertionError("La vista previa no debe ejecutar apply_host")

    monkeypatch.setattr(acc_execution, "apply_host", must_not_apply)

    task_id = _start_execution(
        client, "preview", ntp_policy="restore", value="Europe/Madrid"
    )
    status = _wait_for_task(client, task_id)

    assert status["overall_result"] == "preview", status
    batch = ExecutionBatch.get_batches(db_path=db_path)[0]
    assert batch.status == "preview"
    assert batch.preview_count == 1
    assert batch.success_count == 0

    hosts = ExecutionBatch.get_host_results(batch.batch_id, db_path=db_path)
    assert len(hosts) == 1
    assert hosts[0].result == "preview"
    assert hosts[0].before_state["timezone"] == "Etc/UTC"
    assert hosts[0].after_state["planned"] is True
    assert hosts[0].after_state["would_change"] is True


def test_time_mode_and_ntp_policy_reach_ansible(
    client, monkeypatch, task_hora_multiaccion
):
    _login(client)
    db_path = runtime.get_db_path()
    received = {}

    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.20"])
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )

    def fake_apply(host, **kwargs):
        received.update(kwargs)
        return _ok(host)

    monkeypatch.setattr(acc_execution, "apply_host", fake_apply)

    task_id = _start_execution(
        client,
        "execute",
        action="TIME",
        value="2026-11-03T10:30",
        time_mode="local",
        time_zone="Europe/Madrid",
        ntp_policy="configure",
        ntp_servers="pool.ntp.org, time.cloudflare.com",
    )
    status = _wait_for_task(client, task_id)
    assert status["overall_result"] == "success", status

    assert received["time_mode"] == "local"
    assert received["ntp_policy"] == "configure"
    assert received["ntp_servers"] == [
        "pool.ntp.org",
        "time.cloudflare.com",
    ]
    assert received["target_epoch"] is not None
    assert received["local_fold"] == 0
    assert received["time_zone"] == "Europe/Madrid"

    batch = ExecutionBatch.get_batches(db_path=db_path)[0]
    assert batch.action == "TIME"
    assert batch.time_mode == "local"
    assert batch.ntp_policy == "configure"
    assert batch.time_zone == "Europe/Madrid"


def test_live_console_streams_redacts_and_compacts_markers(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.30"])
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )

    def fake_apply(host, line_callback=None, **_kwargs):
        assert line_callback is not None
        line_callback("PLAY [hora]")
        line_callback("TASK [aplicar]")
        line_callback("password=clave-uno")
        line_callback("HORA_STATE_B64=eyJ0ZXN0IjogMX0=")
        return _ok(host)

    monkeypatch.setattr(acc_execution, "apply_host", fake_apply)

    task_id = _start_execution(client, "execute")
    page = client.get(f"/progress/{task_id}")
    assert page.status_code == 200
    assert b"Salida de Ansible en vivo" in page.data

    status = _wait_for_task(client, task_id)
    console = "\n".join(status["console"])
    assert "PLAY [hora]" in console
    assert "TASK [aplicar]" in console
    assert "clave-uno" not in console
    assert "***" in console
    assert "[marcador de estado del host recibido]" in console
    assert any("Playbook y verificación OK" in line for line in status["console"])


def test_preview_failure_is_not_counted_as_real_error(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.55"])
    monkeypatch.setattr(
        acc_execution,
        "inspect_host",
        lambda **_kwargs: {"success": False, "error": "Sin conexión"},
    )

    task_id = _start_execution(client, "preview")
    status = _wait_for_task(client, task_id)

    assert status["overall_result"] == "preview"
    batch = ExecutionBatch.get_batch(
        status["batch_id"], db_path=runtime.get_db_path()
    )
    assert batch is not None
    assert batch.status == "preview"
    assert batch.error_count == 1

    stats = ExecutionBatch.get_stats(db_path=runtime.get_db_path())
    assert stats["error"] == 0
    assert stats["failure"] == 0


def test_dry_run_visible_and_persisted(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.40"])
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )
    monkeypatch.setattr(
        acc_execution,
        "apply_host",
        lambda host, **_kwargs: _ok(host),
    )
    monkeypatch.setattr(runtime, "DRY_RUN", True)

    task_id = _start_execution(client, "execute")
    page = client.get(f"/progress/{task_id}")
    assert b"DRY RUN" in page.data

    status = _wait_for_task(client, task_id)
    assert status["dry_run"] is True

    batch = ExecutionBatch.get_batch(
        status["batch_id"], db_path=runtime.get_db_path()
    )
    assert batch is not None
    assert batch.dry_run is True

    detail = client.get(f"/batches/{status['batch_id']}")
    assert b"DRY RUN" in detail.data


def test_real_execution_is_not_marked_as_dry_run(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.41"])
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )
    monkeypatch.setattr(
        acc_execution,
        "apply_host",
        lambda host, **_kwargs: _ok(host),
    )
    monkeypatch.setattr(runtime, "DRY_RUN", False)

    task_id = _start_execution(client, "execute")
    status = _wait_for_task(client, task_id)

    assert status["dry_run"] is False
    batch = ExecutionBatch.get_batch(
        status["batch_id"], db_path=runtime.get_db_path()
    )
    assert batch is not None
    assert batch.dry_run is False
    assert b"DRY RUN" not in client.get(f"/batches/{status['batch_id']}").data


def test_task_status_json_is_not_cached(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.70"])
    monkeypatch.setattr(
        acc_execution,
        "inspect_host",
        lambda host, **_kwargs: {
            "success": True,
            "host": host,
            "state": {"timezone": "UTC", "ntp": False, "epoch": 100},
            "error": "",
        },
    )
    task_id = _start_execution(client, "preview")
    _wait_for_task(client, task_id)

    response = client.get(f"/api/task-status/{task_id}")
    assert response.headers["Cache-Control"] == "no-store"


def test_preview_inspects_for_real_even_in_dry_run(client, monkeypatch):
    _login(client)
    seen = {}
    monkeypatch.setattr(runtime, "DRY_RUN", True)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.71"])

    def fake_inspect(host, **kwargs):
        seen["force_real"] = kwargs.get("force_real")
        return {
            "success": True,
            "host": host,
            "state": {"timezone": "UTC", "ntp": False, "epoch": 100},
            "error": "",
        }

    monkeypatch.setattr(acc_execution, "inspect_host", fake_inspect)

    task_id = _start_execution(client, "preview")
    status = _wait_for_task(client, task_id)

    # La simulación no debe ocultar que el host no existe o no autentica.
    assert seen["force_real"] is True
    assert status["overall_result"] == "preview"
    batch = ExecutionBatch.get_batch(
        status["batch_id"], db_path=runtime.get_db_path()
    )
    assert batch is not None
    assert batch.status == "preview"
    # Una vista previa es una inspección real, no una ejecución simulada.
    assert batch.dry_run is False


def test_password_attempts_per_host_are_capped(client, monkeypatch):
    _login(client)
    calls = []
    monkeypatch.setattr(acc_execution, "MAX_PASSWORD_ATTEMPTS", 1)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.72"])
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )

    def fake_apply(host, password=None, **_kwargs):
        calls.append(password)
        return {
            "success": False,
            "host": host,
            "error": "la verificación falló",
            "before_state": None,
            "after_state": None,
        }

    monkeypatch.setattr(acc_execution, "apply_host", fake_apply)

    task_id = _start_execution(client, "execute")
    status = _wait_for_task(client, task_id)

    assert calls == ["clave-uno"]
    batch = ExecutionBatch.get_batch(
        status["batch_id"], db_path=runtime.get_db_path()
    )
    assert batch is not None
    assert batch.failure_count == 1


def test_batch_time_budget_skips_remaining_hosts(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(acc_execution, "BATCH_TIME_BUDGET_SECONDS", 0)
    monkeypatch.setattr(
        acc_execution,
        "resolve_target",
        lambda _target: ["10.0.0.73", "10.0.0.74"],
    )

    def must_not_run(**_kwargs):
        raise AssertionError("No debe procesarse ningún host sin presupuesto")

    monkeypatch.setattr(acc_execution, "test_ansible_ping", must_not_run)
    monkeypatch.setattr(acc_execution, "apply_host", must_not_run)

    task_id = _start_execution(client, "execute")
    status = _wait_for_task(client, task_id)

    assert status["overall_result"] == "incomplete", status
    batch = ExecutionBatch.get_batch(
        status["batch_id"], db_path=runtime.get_db_path()
    )
    assert batch is not None
    assert batch.status == "incomplete"
    assert batch.total_hosts == 2
    assert batch.skipped_count == 2
    assert batch.success_count == 0


def test_restore_does_not_add_net_ntp_change():
    state = {"timezone": "UTC", "ntp": True, "epoch": 1000}

    # TZ ya correcta + restore: NTP vuelve a su estado previo, sin cambio neto.
    assert (
        app_module._would_change(state, "TZ", "UTC", "utc", "restore", False) is False
    )
    # off sí exige apagar NTP.
    assert app_module._would_change(state, "TZ", "UTC", "utc", "off", False) is True
    # configure exige activar NTP cuando está apagado.
    assert (
        app_module._would_change(
            {"timezone": "UTC", "ntp": False, "epoch": 1000},
            "TZ",
            "UTC",
            "utc",
            "configure",
            False,
        )
        is True
    )
    # configure reescribe la configuración NTP: sin la lista de servidores en
    # el estado no se puede prometer que no habrá cambios.
    assert (
        app_module._would_change(
            {"timezone": "UTC", "ntp": True, "epoch": 1000},
            "TZ",
            "UTC",
            "utc",
            "configure",
            False,
            ntp_servers=["pool.ntp.org"],
        )
        is True
    )
    # Un "False" serializado por Ansible no debe tratarse como NTP activo.
    assert (
        app_module._would_change(
            {"timezone": "UTC", "ntp": "False", "epoch": 1000},
            "TZ",
            "UTC",
            "utc",
            "off",
            False,
        )
        is False
    )


def test_preview_warns_about_time_with_keep_and_active_ntp(
    client, monkeypatch, task_hora_multiaccion
):
    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.42"])
    monkeypatch.setattr(
        acc_execution,
        "inspect_host",
        lambda host, **_kwargs: {
            "success": True,
            "host": host,
            "state": {
                "timezone": "UTC",
                "ntp": True,
                "epoch": 100,
                "local": "2026-01-01T00:00:00+0000",
            },
            "error": "",
        },
    )

    task_id = _start_execution(
        client,
        "preview",
        action="TIME",
        value="2026-01-01T00:00",
        time_mode="utc",
        ntp_policy="keep",
    )
    status = _wait_for_task(client, task_id)

    assert any("Advertencia" in line for line in status["log"])
    assert any("política keep" in line for line in status["console"])


def test_login_cookies_are_hardened(client):
    response = _post(
        client,
        "/login",
        {
            "username": "admin",
            "password": "admin-password-123",
            "remember": "1",
        },
    )
    assert response.status_code == 302

    cookies = response.headers.getlist("Set-Cookie")
    session_cookie = next(c for c in cookies if c.startswith("session="))
    remember_cookie = next(c for c in cookies if c.startswith("remember_token="))

    # La sesión es permanente y la cookie no es accesible desde JavaScript.
    assert "HttpOnly" in session_cookie
    assert "SameSite=Lax" in session_cookie
    assert "Expires=" in session_cookie
    assert "Expires=" in remember_cookie
    assert app_module.app.config["REMEMBER_COOKIE_DURATION"].total_seconds() == 604800
    assert "HttpOnly" in remember_cookie
    assert "SameSite=Lax" in remember_cookie
    assert "Secure" not in session_cookie


def test_reconcile_orphan_running_batches(tmp_path):
    db_path = str(tmp_path / "orphans.db")
    init_schema(db_path)

    ExecutionBatch.create_batch(
        batch_id="running-1",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.1",
        status="running",
        db_path=db_path,
    )
    ExecutionBatch.create_batch(
        batch_id="preview-1",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.1",
        status="preview",
        db_path=db_path,
    )

    assert ExecutionBatch.reconcile_orphans(db_path=db_path) == 1

    running = ExecutionBatch.get_batch("running-1", db_path=db_path)
    preview = ExecutionBatch.get_batch("preview-1", db_path=db_path)
    assert running is not None and running.status == "error"
    assert running.finished_at is not None
    assert preview is not None and preview.status == "preview"
    assert preview.finished_at is None

    # Es idempotente: ya no quedan lotes en curso.
    assert ExecutionBatch.reconcile_orphans(db_path=db_path) == 0


def test_duplicate_submission_is_idempotent(client, monkeypatch, task_hora_multiaccion):
    import re

    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.50"])
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )
    monkeypatch.setattr(acc_execution, "apply_host", lambda host, **_kwargs: _ok(host))

    page = client.get("/tarea/zona-todas")
    match = re.search(rb'name="operation_id" value="([0-9a-f]+)"', page.data)
    assert match is not None
    operation_id = match.group(1).decode()

    first_task = _start_execution(client, "execute", operation_id=operation_id)
    second_task = _start_execution(client, "execute", operation_id=operation_id)

    assert second_task == first_task
    _wait_for_task(client, first_task)
    batches = ExecutionBatch.get_batches(db_path=runtime.get_db_path())
    assert len(batches) == 1


def test_operation_ids_persist_in_sqlite(client):
    """La reserva sobrevive a reinicios y no depende de la memoria."""
    operation_id = "operacion-persistente"
    assert app_module._claim_operation_id(operation_id) is None
    app_module._complete_operation_id(operation_id, "tarea-1234")

    with app_module.get_connection(runtime.get_db_path()) as conn:
        row = conn.execute(
            "SELECT task_id FROM operation_ids WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    assert row is not None and row["task_id"] == "tarea-1234"

    # No hay caché en memoria que borrar: la segunda consulta va a SQLite.
    assert app_module._claim_operation_id(operation_id) == "tarea-1234"
    app_module._release_operation_id(operation_id)
    assert app_module._claim_operation_id(operation_id) is None


def test_task_state_survives_memory_loss(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(acc_execution, "resolve_target", lambda _target: ["10.0.0.60"])
    monkeypatch.setattr(
        acc_execution,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )
    monkeypatch.setattr(acc_execution, "apply_host", lambda host, **_kwargs: _ok(host))

    task_id = _start_execution(client, "execute")
    status = _wait_for_task(client, task_id)
    assert status["completed"] is True

    # Simula un reinicio del proceso: la memoria se pierde.
    with app_module._tasks_lock:
        app_module._tasks.pop(task_id, None)

    recovered = client.get(f"/api/task-status/{task_id}")
    assert recovered.status_code == 200
    data = recovered.get_json()
    assert data["completed"] is True
    assert data["batch_id"] == status["batch_id"]
    assert client.get(f"/progress/{task_id}").status_code == 200


def test_incomplete_tasks_are_closed_after_restart(client):
    task_id = app_module.create_task()
    app_module.update_task(task_id, {"status": "running", "message": "En curso"})
    with app_module._tasks_lock:
        app_module._tasks.pop(task_id, None)

    app_module._recover_task_state()

    task = app_module._get_task(task_id)
    assert task is not None
    assert task["completed"] is True
    assert task["status"] == "error"
    assert "reinició" in task["message"]


def test_healthz_reports_database_status(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


@pytest.fixture()
def task_hora_multiaccion(monkeypatch):
    """Tarea TIME/NTP temporal para cubrir el motor no expuesto en catálogo.

    La tarjeta visible es solo la de zona horaria fija; el motor de hora
    exacta/NTP sigue vivo en ``cambiar_hora.yml`` y se valida aquí con una
    tarea equivalente que solo existe dentro de cada prueba.
    """
    import services.task_catalog as catalog

    task = {
        "id": "hora",
        "name": "Hora y zona horaria (test)",
        "icon": "bi-clock",
        "tone": "blue",
        "summary": "Acción TIME y NTP para pruebas del moteur horario.",
        "playbook": "cambiar_hora.yml",
        "status": catalog.STATUS_ACTIVE,
        "min_role": "operator",
        "destructive": False,
        "platforms": ["linux"],
        "actions": ["TZ", "TIME"],
        "chips": ["Test"],
        "steps": [],
    }
    catalog._register_test_task(task, monkeypatch)
    return task
