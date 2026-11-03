# tests/test_p4.py
# Pruebas de la fase P4: lotes, todos los hosts, todas las contraseñas,
# vista previa, verificación e idempotencia registrada por host.
from __future__ import annotations

import io
import time

import pytest

import app as app_module
from models.batch import ExecutionBatch
from models.db import init_schema
from models.password import PasswordRecord
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
    response = _post(
        client, "/", data=data, content_type="multipart/form-data"
    )
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


def test_execution_processes_all_hosts_and_retries_passwords(
    client, monkeypatch
):
    _login(client)
    db_path = app_module.get_db_path()
    calls = []

    monkeypatch.setattr(
        app_module,
        "resolve_target",
        lambda _target: ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
    )
    monkeypatch.setattr(
        app_module,
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

    monkeypatch.setattr(app_module, "apply_host", fake_apply)

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
    assert calls == [
        ("10.0.0.1", "clave-uno"),
        ("10.0.0.2", "clave-uno"),
        ("10.0.0.2", "clave-dos"),
        ("10.0.0.3", "clave-uno"),
        ("10.0.0.3", "clave-dos"),
    ]

    history = client.get("/history")
    assert history.status_code == 200
    assert b"10.0.0.0/24" in history.data
    assert b"partial" in history.data

    detail = client.get(f"/batches/{batch.batch_id}")
    assert detail.status_code == 200
    assert b"10.0.0.1" in detail.data
    assert b"10.0.0.3" in detail.data


def test_preview_inspects_without_applying_changes(client, monkeypatch):
    _login(client)
    db_path = app_module.get_db_path()

    monkeypatch.setattr(
        app_module, "resolve_target", lambda _target: ["10.0.0.10"]
    )
    monkeypatch.setattr(
        app_module,
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

    monkeypatch.setattr(app_module, "apply_host", must_not_apply)

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


def test_time_mode_and_ntp_policy_reach_ansible(client, monkeypatch):
    _login(client)
    db_path = app_module.get_db_path()
    received = {}

    monkeypatch.setattr(
        app_module, "resolve_target", lambda _target: ["10.0.0.20"]
    )
    monkeypatch.setattr(
        app_module,
        "test_ansible_ping",
        lambda **_kwargs: {"success": True, "error": ""},
    )

    def fake_apply(host, **kwargs):
        received.update(kwargs)
        return _ok(host)

    monkeypatch.setattr(app_module, "apply_host", fake_apply)

    task_id = _start_execution(
        client,
        "execute",
        action="TIME",
        value="2026-11-03T10:30",
        time_mode="local",
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

    batch = ExecutionBatch.get_batches(db_path=db_path)[0]
    assert batch.action == "TIME"
    assert batch.time_mode == "local"
    assert batch.ntp_policy == "configure"
