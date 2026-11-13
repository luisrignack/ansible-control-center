# tests/test_sprint01.py
# Pruebas de los sprints 0 y 1 de la revisión técnica de 2026-11-03:
# contención de red, DST, cola de trabajos, bloqueo/roles, assets/CSP,
# bloqueo del vault, entorno mínimo de Ansible y estados de lote.
from __future__ import annotations

import io
import ipaddress
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import app as app_module
import config as config_module
from acc import runtime
from acc.web import auth as web_auth
from models.batch import ExecutionBatch
from models.db import SCHEMA_VERSION, init_schema
from models.user import AuthEvent, User
from services.ansible_runner import ansible_environment
from services.host_resolver import pin_target, resolve_target, validate_target
from services.job_queue import JobQueue, JobQueueFull
from services.time_utils import (
    LOCAL_TIME_AMBIGUOUS,
    LOCAL_TIME_MISSING,
    local_time_epoch,
    local_time_status,
    timezone_exists,
)
from services.vault_manager import _vault_lock


# ---------------------------------------------------------------------------
# Utilidades de cliente Flask
# ---------------------------------------------------------------------------
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


def _login(client, username="admin", password="admin-password-123"):
    return _post(client, "/login", {"username": username, "password": password})


def _execution_form(**overrides):
    data = {
        "target_ip": "10.0.0.0/24",
        "admin_user": "root",
        "password_origin": "temporal",
        "action": "TZ",
        "value": "UTC",
        "time_mode": "utc",
        "ntp_policy": "keep",
        "mode": "execute",
        "password_file": (io.BytesIO(b"clave-uno\n"), "passwords.txt"),
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# S1/S2: allowlist, loopback/metadatos y fijación de IP
# ---------------------------------------------------------------------------
def test_production_requires_allowlist(monkeypatch):
    monkeypatch.setattr(config_module, "IS_PRODUCTION", True)
    monkeypatch.setattr(config_module, "ALLOWED_NETWORKS", ())
    monkeypatch.setattr(config_module, "SECRET_KEY", "clave-explicita")
    monkeypatch.setattr(config_module, "HOST_KEY_CHECKING", True)

    with pytest.raises(RuntimeError, match="ALLOWED_NETWORKS"):
        config_module.validate_security_config()


def test_production_accepts_explicit_allowlist(monkeypatch):
    monkeypatch.setattr(config_module, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/8"),),
    )
    monkeypatch.setattr(config_module, "SECRET_KEY", "clave-explicita")
    monkeypatch.setattr(config_module, "HOST_KEY_CHECKING", True)
    monkeypatch.setattr(config_module, "SESSION_COOKIE_SECURE", True)

    config_module.validate_security_config()


def test_production_requires_secure_cookies(monkeypatch):
    monkeypatch.setattr(config_module, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/8"),),
    )
    monkeypatch.setattr(config_module, "SECRET_KEY", "clave-explicita")
    monkeypatch.setattr(config_module, "HOST_KEY_CHECKING", True)
    monkeypatch.setattr(config_module, "SESSION_COOKIE_SECURE", False)

    with pytest.raises(RuntimeError, match="ACC_COOKIE_SECURE"):
        config_module.validate_security_config()


def test_env_secret_reads_file_when_no_plain_value(tmp_path, monkeypatch):
    secret_file = tmp_path / "acc_secret_key"
    secret_file.write_text("clave-desde-fichero\n", encoding="utf-8")
    monkeypatch.delenv("ACC_SECRET_KEY", raising=False)
    monkeypatch.setenv("ACC_SECRET_KEY_FILE", str(secret_file))

    assert config_module._env_secret("ACC_SECRET_KEY") == "clave-desde-fichero"

    monkeypatch.setenv("ACC_SECRET_KEY", "clave-desde-entorno")
    assert config_module._env_secret("ACC_SECRET_KEY") == "clave-desde-entorno"


def test_loopback_and_metadata_are_blocked_even_if_allowlisted(monkeypatch):
    monkeypatch.setattr(config_module, "IS_PRODUCTION", True)
    monkeypatch.setattr(config_module, "ALLOW_LOOPBACK", True)
    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("169.254.0.0/16"),
        ),
    )

    assert resolve_target("127.0.0.1") == []
    assert resolve_target("169.254.169.254") == []
    assert validate_target("169.254.169.254") is not None


def test_hostname_is_resolved_once_and_pinned_to_literal_ip(monkeypatch):
    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/8"),),
    )
    calls = []

    def fake_resolution(name):
        calls.append(name)
        return (name, [], ["10.20.30.40"])

    monkeypatch.setattr(socket, "gethostbyname_ex", fake_resolution)

    pinned, error = pin_target("servidor.ejemplo")

    assert error is None
    assert pinned == "10.20.30.40"
    assert calls == ["servidor.ejemplo"]


def test_hostname_with_any_blocked_address_is_rejected(monkeypatch):
    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/8"),),
    )
    monkeypatch.setattr(
        socket,
        "gethostbyname_ex",
        lambda name: (name, [], ["10.20.30.40", "127.0.0.1"]),
    )

    pinned, error = pin_target("servidor.ejemplo")

    assert pinned is None
    assert "permitidas" in error


# ---------------------------------------------------------------------------
# T4/T5: DST y validación real con zoneinfo
# ---------------------------------------------------------------------------
def test_invented_timezone_is_rejected():
    assert timezone_exists("UTC")
    assert timezone_exists("Europe/Madrid")
    assert not timezone_exists("America/Atlantida")


def test_nonexistent_local_time_during_spring_gap_is_rejected():
    status, message = local_time_status("2026-03-29T02:30", "Europe/Madrid")
    assert status == LOCAL_TIME_MISSING
    assert "no existe" in message


def test_ambiguous_local_time_requires_explicit_fold():
    status, message = local_time_status("2026-10-25T02:30", "Europe/Madrid")
    assert status == LOCAL_TIME_AMBIGUOUS
    assert "ambigua" in message

    assert local_time_status("2026-10-25T02:30", "Europe/Madrid", fold=0)[0] == "ok"
    assert local_time_status("2026-10-25T02:30", "Europe/Madrid", fold=1)[0] == "ok"


def test_fold_selects_two_different_instants():
    first = local_time_epoch("2026-10-25T02:30", "Europe/Madrid", fold=0)
    second = local_time_epoch("2026-10-25T02:30", "Europe/Madrid", fold=1)
    assert first is not None and second is not None
    assert second - first == 3600


def test_form_rejects_dst_gap(client):
    _login(client)
    response = _post(
        client,
        "/",
        _execution_form(
            action="TIME",
            value="2026-03-29T02:30",
            time_mode="local",
            time_zone="Europe/Madrid",
            ntp_policy="configure",
            ntp_servers="pool.ntp.org",
        ),
        follow_redirects=True,
    )
    assert "no existe" in response.get_data(as_text=True)


def test_form_rejects_ambiguous_time_without_fold(client):
    _login(client)
    response = _post(
        client,
        "/",
        _execution_form(
            action="TIME",
            value="2026-10-25T02:30",
            time_mode="local",
            time_zone="Europe/Madrid",
            ntp_policy="configure",
            ntp_servers="pool.ntp.org",
            time_fold="auto",
        ),
        follow_redirects=True,
    )
    assert "ambigua" in response.get_data(as_text=True)


def test_form_rejects_unknown_timezone(client):
    _login(client)
    response = _post(
        client,
        "/",
        _execution_form(
            action="TIME",
            value="2026-01-01T10:00",
            time_mode="local",
            time_zone="America/Atlantida",
            ntp_policy="configure",
            ntp_servers="pool.ntp.org",
        ),
        follow_redirects=True,
    )
    assert "Zona horaria desconocida" in response.get_data(as_text=True)


def test_time_with_ntp_keep_is_rejected_on_execute(client):
    _login(client)
    response = _post(
        client,
        "/",
        _execution_form(
            action="TIME",
            value="2026-01-01T10:00",
            time_mode="utc",
            ntp_policy="keep",
        ),
        follow_redirects=True,
    )
    assert "política keep no está permitido" in response.get_data(as_text=True)


def test_ntp_configure_matches_normalized_server_set():
    state = {
        "timezone": "UTC",
        "ntp": True,
        "epoch": 1000,
        "ntp_servers": ["b.example", "a.example"],
    }
    assert (
        app_module._would_change(
            state,
            "TZ",
            "UTC",
            "utc",
            "configure",
            False,
            ntp_servers=["a.example", "b.example"],
        )
        is False
    )
    assert (
        app_module._would_change(
            state,
            "TZ",
            "UTC",
            "utc",
            "configure",
            False,
            ntp_servers=["otro.example"],
        )
        is True
    )


def test_ntp_configure_tolerates_extra_system_servers():
    """Tras aplicar, el sistema puede conservar servidores propios."""
    state = {
        "timezone": "UTC",
        "ntp": True,
        "epoch": 1000,
        "ntp_servers": ["a.example", "b.example", "extra.example"],
    }

    assert (
        app_module._would_change(
            state,
            "TZ",
            "UTC",
            "utc",
            "configure",
            False,
            ntp_servers=["a.example", "b.example"],
        )
        is False
    )


# ---------------------------------------------------------------------------
# A1: cola acotada, cancelación y apagado ordenado
# ---------------------------------------------------------------------------
def test_job_queue_bounds_concurrency_and_cancels():
    job_queue = JobQueue(max_workers=1, max_queue=1, shutdown_grace=2)
    started = threading.Event()
    release = threading.Event()
    queued_ran = threading.Event()

    def first_job(cancel_event):
        started.set()
        release.wait(5)

    def queued_job(cancel_event):
        queued_ran.set()

    job_queue.submit("primero", first_job)
    assert started.wait(2)
    job_queue.submit("segundo", queued_job)

    with pytest.raises(JobQueueFull):
        job_queue.submit("tercero", queued_job)

    assert job_queue.cancel("segundo") is True
    release.set()
    job_queue.shutdown(wait=True)

    assert queued_ran.wait(2)
    with pytest.raises(JobQueueFull):
        job_queue.submit("cuarto", queued_job)


def test_job_queue_cancels_a_running_job():
    job_queue = JobQueue(max_workers=1, max_queue=1, shutdown_grace=2)
    started = threading.Event()
    cancelled = threading.Event()

    def blocking_job(cancel_event):
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.01)
        cancelled.set()

    job_queue.submit("cancelable", blocking_job)
    assert started.wait(2)
    assert job_queue.cancel("cancelable") is True
    assert cancelled.wait(2)
    job_queue.shutdown(wait=True)


# ---------------------------------------------------------------------------
# S3: roles, bloqueo progresivo y auditoría de login
# ---------------------------------------------------------------------------
def test_viewer_is_read_only(client, tmp_path):
    db_path = runtime.get_db_path()
    User.create_or_update(
        "viewer", "viewer-password-123", db_path=db_path, role="viewer"
    )
    assert _login(client, "viewer", "viewer-password-123").status_code == 302

    assert client.get("/").status_code == 200
    assert client.get("/history").status_code == 200
    assert client.get("/audit").status_code == 403
    assert _post(client, "/execute", _execution_form()).status_code == 403


def test_operator_cannot_read_audit(client):
    db_path = runtime.get_db_path()
    User.create_or_update(
        "operator", "operator-password-123", db_path=db_path, role="operator"
    )
    assert _login(client, "operator", "operator-password-123").status_code == 302
    assert client.get("/audit").status_code == 403
    # Puede enviar operaciones: un formulario inválido se rechaza con
    # redirección, no con 403 (no se lanza ningún trabajo real).
    response = _post(client, "/execute", {"admin_user": "root"})
    assert response.status_code == 302


def test_admin_can_read_audit(client):
    _login(client)
    AuthEvent.record(
        "admin",
        "127.0.0.1",
        "login_success",
        "Prueba",
        db_path=runtime.get_db_path(),
    )
    response = client.get("/audit")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Auditoría de acceso" in body
    assert "Acceso correcto" in body


def test_account_is_locked_after_repeated_failures(client, monkeypatch):
    db_path = runtime.get_db_path()
    User.create_or_update("bloqueado", "correcta-123", db_path=db_path, role="viewer")
    monkeypatch.setattr(web_auth, "LOGIN_LOCKOUT_THRESHOLD", 2)

    for _ in range(2):
        _post(
            client,
            "/login",
            {"username": "bloqueado", "password": "incorrecta"},
        )

    response = _post(
        client,
        "/login",
        {"username": "bloqueado", "password": "correcta-123"},
        follow_redirects=True,
    )
    assert "bloqueada" in response.get_data(as_text=True)

    events = AuthEvent.recent(limit=20, db_path=db_path)
    kinds = {event["event"] for event in events}
    assert "login_failure" in kinds
    assert "account_locked" in kinds
    assert "login_locked" in kinds


# ---------------------------------------------------------------------------
# S7: assets locales, CSP estricta y ausencia de estilos inline
# ---------------------------------------------------------------------------
def test_csp_is_strict_and_assets_are_local(client):
    response = client.get("/login")
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert "style-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "http://" not in csp and "https://" not in csp
    assert "cdn" not in csp.lower()

    body = response.get_data(as_text=True)
    assert "/static/vendor/bootstrap/bootstrap.min.css" in body
    assert "/static/vendor/bootstrap-icons/bootstrap-icons.css" in body
    assert "/static/css/app.css" in body
    assert "fonts.googleapis" not in body
    assert "cdn." not in body

    root = Path(app_module.app.root_path)
    assert (root / "static/vendor/bootstrap/bootstrap.min.css").is_file()
    assert (root / "static/vendor/bootstrap/bootstrap.bundle.min.js").is_file()
    assert (root / "static/css/app.css").is_file()


def test_templates_have_no_inline_style_or_cdn():
    templates = Path(app_module.app.root_path) / "templates"
    for path in templates.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "style=" not in text, path
        assert "fonts.googleapis" not in text, path
        assert "cdn." not in text, path
        assert "unsafe-inline" not in text, path


# ---------------------------------------------------------------------------
# S5/S8: lock del vault y entorno mínimo de Ansible
# ---------------------------------------------------------------------------
def test_vault_lock_serializes_writers(tmp_path):
    vault = str(tmp_path / "passwords_vault.yml")
    first_inside = threading.Event()
    release_first = threading.Event()
    attempting_second = threading.Event()
    second_inside = threading.Event()

    def first():
        with _vault_lock(vault):
            first_inside.set()
            release_first.wait(5)

    def second():
        assert first_inside.wait(2)
        attempting_second.set()
        with _vault_lock(vault):
            second_inside.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()

    assert attempting_second.wait(2)
    # El segundo escritor debe estar bloqueado mientras el primero mantiene
    # el lock; no puede entrar antes de que se libere.
    assert not second_inside.is_set()

    release_first.set()
    for thread in threads:
        thread.join(timeout=5)
    assert second_inside.is_set()


def test_ansible_environment_does_not_leak_app_secrets(monkeypatch):
    monkeypatch.setenv("ACC_SECRET_KEY", "no-debe-filtrarse")
    monkeypatch.setenv("ACC_PASSWORD_PEPPER", "tampoco")
    monkeypatch.setenv("SECRETO_EXTERNO", "tampoco")
    monkeypatch.setenv("ANSIBLE_TEST_VAR", "permitida")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))

    env = ansible_environment()

    assert env["ANSIBLE_TEST_VAR"] == "permitida"
    assert env["ANSIBLE_NOCOLOR"] == "1"
    assert env["ANSIBLE_CONFIG"]
    assert "ACC_SECRET_KEY" not in env
    assert "ACC_PASSWORD_PEPPER" not in env
    assert "SECRETO_EXTERNO" not in env


# ---------------------------------------------------------------------------
# T7/D4: estados de lote y protección de esquema futuro
# ---------------------------------------------------------------------------
def test_batch_time_zone_and_cancelled_status(tmp_path):
    db_path = str(tmp_path / "batch.db")
    init_schema(db_path)

    ExecutionBatch.create_batch(
        batch_id="cancelado-1",
        operator="admin",
        action="TIME",
        value="2026-10-25T02:30",
        target="10.0.0.1",
        time_mode="local",
        ntp_policy="configure",
        total_hosts=2,
        status="running",
        local_fold=1,
        time_zone="Europe/Madrid",
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="cancelado-1",
        host="10.0.0.1",
        result="skipped",
        db_path=db_path,
    )
    batch = ExecutionBatch.finish_batch(
        batch_id="cancelado-1", status="cancelled", db_path=db_path
    )

    assert batch is not None
    assert batch.status == "cancelled"
    assert batch.time_zone == "Europe/Madrid"
    assert batch.local_fold == 1


def test_all_skipped_batch_is_incomplete_not_failure(tmp_path):
    db_path = str(tmp_path / "batch.db")
    init_schema(db_path)

    ExecutionBatch.create_batch(
        batch_id="incompleto-1",
        operator="admin",
        action="TZ",
        value="UTC",
        target="10.0.0.1",
        total_hosts=1,
        status="running",
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id="incompleto-1",
        host="10.0.0.1",
        result="skipped",
        db_path=db_path,
    )
    batch = ExecutionBatch.finish_batch(batch_id="incompleto-1", db_path=db_path)

    assert batch is not None
    assert batch.status == "incomplete"


def test_init_schema_aborts_on_future_version(tmp_path):
    db_path = str(tmp_path / "futuro.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="más nuevo"):
        init_schema(db_path)
