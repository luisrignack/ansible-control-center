"""Pruebas de la tarjeta de actualización del sistema (APT, 2026-11-07).

La tarjeta ya está operativa: valida la política de migración, el umbral de
espacio, la selección de playbooks y la clasificación ``attention`` en lotes e
informes.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

import app as app_module
import services.ansible_runner as runner
from models.batch import ExecutionBatch
from models.db import get_connection, init_schema
from services.pdf_report import build_execution_report, build_migration_report
from services.task_catalog import TASK_CATALOG, get_task

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "ansible" / "apt_upgrade.yml"
INSPECT_PLAYBOOK = ROOT / "ansible" / "apt_estado.yml"
FACTS = ROOT / "ansible" / "tasks" / "apt_facts.yml"


def test_apt_card_is_active_and_restricted_to_admin():
    task = get_task("apt-upgrade")
    assert task is not None
    assert task["status"] == "active"
    assert task["platforms"] == ["linux"]
    assert task["actions"] == ["APT"]
    assert task["min_role"] == "admin"
    assert task["playbook"] == "apt_upgrade.yml"
    assert task["schedulable"] is True


def test_apt_card_is_active_alongside_the_other_operational_tasks():
    active = [task for task in TASK_CATALOG if task["status"] == "active"]
    assert [task["id"] for task in active] == [
        "zona-todas",
        "contrasena",
        "tapiz",
        "apt-upgrade",
    ]


def test_apt_playbook_uses_safe_apt_get_upgrade():
    data = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    tasks = data[0]["tasks"]

    apt_upgrade = next(
        item
        for item in tasks
        if item.get("name") == "Aplicar la actualización segura"
    )
    module = apt_upgrade["ansible.builtin.apt"]
    assert module["upgrade"] == "safe"
    assert module["update_cache"] is False
    assert "force-confold" in module["dpkg_options"]
    assert module["lock_timeout"] == 300
    assert module["autoremove"] is False

    for item in tasks:
        module_data = item.get("ansible.builtin.apt")
        if isinstance(module_data, dict):
            assert module_data.get("upgrade") not in {
                "dist-upgrade",
                "full-upgrade",
            }
        assert "ansible.builtin.shell" not in item


def test_apt_playbook_sets_noninteractive_environment_and_policy_blocks():
    data = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    play = data[0]
    assert play["environment"]["DEBIAN_FRONTEND"] == "noninteractive"
    serialized = PLAYBOOK.read_text(encoding="utf-8")
    assert "/var/run/reboot-required" in serialized
    assert "APT_VERIFIED=True" in serialized
    assert "APT_SKIP=True" in serialized
    assert "Omitir la actualización por política" in serialized
    assert "blocked_os" in serialized
    assert "min_free_gb" in serialized


def test_apt_facts_are_shared_and_read_only():
    facts = FACTS.read_text(encoding="utf-8")
    assert "apt-get -s upgrade" in facts
    assert "APT_BLOCKED" in facts
    assert "APT_SPACE_OK" in facts
    assert "APT_DISK_FREE_BYTES" in facts
    assert "update_cache" not in facts
    assert "do-release-upgrade" not in facts

    inspect = INSPECT_PLAYBOOK.read_text(encoding="utf-8")
    assert "tasks/apt_facts.yml" in inspect
    assert "APT_STATE_B64" in inspect
    assert "update_cache" not in inspect
    assert "ansible.builtin.apt:" not in inspect


def test_apt_default_policy_blocks_ubuntu_20_family():
    from config import APT_BLOCKED_OS

    assert "ubuntu:20.04" in APT_BLOCKED_OS
    assert "linuxmint:20" in APT_BLOCKED_OS
    assert "linuxlite:5" in APT_BLOCKED_OS
    assert "mx:19" in APT_BLOCKED_OS


def test_parse_min_free_gb_boundaries():
    assert app_module._parse_min_free_gb("")[0] == pytest.approx(
        app_module.APT_MIN_FREE_GB
    )
    value, error = app_module._parse_min_free_gb("4,5")
    assert error is None
    assert value == pytest.approx(4.5)

    _, error = app_module._parse_min_free_gb("abc")
    assert error

    _, error = app_module._parse_min_free_gb("0")
    assert error

    _, error = app_module._parse_min_free_gb("999")
    assert error


def test_validate_apt_operation_normalizes_safe_and_threshold():
    validated, errors = app_module._validate_operation_core(
        task=get_task("apt-upgrade"),
        task_id="apt-upgrade",
        admin_user="root",
        action="APT",
        value="peligroso",
        ntp_policy="configure",
        ntp_servers_raw="pool.ntp.org",
        min_free_gb_raw="3.5",
    )
    assert errors == []
    assert validated["action"] == "APT"
    assert validated["value"] == "safe"
    assert validated["ntp_policy"] == "keep"
    assert validated["ntp_servers"] == []
    assert validated["min_free_gb"] == pytest.approx(3.5)


def test_validate_apt_rejects_invalid_threshold():
    validated, errors = app_module._validate_operation_core(
        task=get_task("apt-upgrade"),
        task_id="apt-upgrade",
        admin_user="root",
        action="APT",
        value="",
        ntp_policy="keep",
        ntp_servers_raw="",
        min_free_gb_raw="-3",
    )
    assert validated is None
    assert any("umbral" in error.lower() for error in errors)


def test_inspect_apt_decodes_state(monkeypatch):
    state = {
        "os_id": "ubuntu",
        "os_version": "22.04",
        "hostname": "estacion-1",
        "pending": 4,
        "attention": False,
        "attention_kind": "",
        "space_ok": True,
    }
    encoded = base64.b64encode(json.dumps(state).encode()).decode()

    class Process:
        returncode = 0
        stdout = f"APT_STATE_B64={encoded}\n"

    captured = {}

    def fake_invoke(**kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    result = runner.inspect_apt(
        host="10.0.0.10",
        user="root",
        password="clave",
        min_free_gb=3,
    )

    assert captured["playbook_path"] == str(runner.APT_INSPECT_PLAYBOOK_FILE)
    assert captured["extra_vars"]["min_free_gb"] == 3
    assert result["success"] is True
    assert result["state"]["hostname"] == "estacion-1"


def test_apply_host_selects_apt_playbook_and_reports_migration(monkeypatch):
    state = {
        "os_id": "ubuntu",
        "os_version": "20.04",
        "os_pretty": "Ubuntu 20.04.6 LTS",
        "hostname": "antigua",
        "attention": True,
        "attention_kind": "migration",
        "attention_reason": "Distribución bloqueada: requiere migración.",
        "blocked": True,
        "space_ok": True,
    }
    encoded = base64.b64encode(json.dumps(state).encode()).decode()

    class Process:
        returncode = 0
        stdout = (
            f"APT_BEFORE_B64={encoded}\n"
            f"APT_STATE_B64={encoded}\n"
            "APT_SKIP=True\n"
            "APT_CHANGED=False\n"
            "APT_VERIFIED=True\n"
        )

    captured = {}

    def fake_invoke(**kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    result = runner.apply_host(
        host="10.0.0.20",
        user="root",
        action="APT",
        value="safe",
        password="clave",
        min_free_gb=4,
    )

    assert captured["playbook_path"] == str(runner.APT_UPGRADE_PLAYBOOK_FILE)
    extra = captured["extra_vars"]
    assert extra["accion"] == "APT"
    assert extra["valor"] == "safe"
    assert extra["min_free_gb"] == 4
    assert result["success"] is True
    assert result["attention"] is True
    assert result["attention_kind"] == "migration"
    assert result["policy_skipped"] is True


def test_apply_host_reports_space_attention(monkeypatch):
    state = {
        "hostname": "sin-espacio",
        "attention": True,
        "attention_kind": "space",
        "attention_reason": "Espacio insuficiente: 1.00 GiB libres.",
        "blocked": False,
        "space_ok": False,
    }
    encoded = base64.b64encode(json.dumps(state).encode()).decode()

    class Process:
        returncode = 0
        stdout = (
            f"APT_BEFORE_B64={encoded}\n"
            f"APT_STATE_B64={encoded}\n"
            "APT_SKIP=True\n"
            "APT_CHANGED=False\n"
            "APT_VERIFIED=True\n"
        )

    monkeypatch.setattr(
        runner, "_invoke_playbook", lambda **kwargs: Process()
    )
    monkeypatch.setattr(runner, "DRY_RUN", False)

    result = runner.apply_host(
        host="10.0.0.21",
        user="root",
        action="APT",
        value="safe",
        password="clave",
    )
    assert result["attention"] is True
    assert result["attention_kind"] == "space"
    assert "Espacio insuficiente" in result["attention_reason"]


def test_batch_attention_counts_as_partial(tmp_path):
    db_path = str(tmp_path / "apt-batch.db")
    init_schema(db_path)
    batch = ExecutionBatch.create_batch(
        batch_id="apt-attention-batch",
        operator="tester",
        action="APT",
        value="safe",
        target="10.0.0.0/24",
        time_mode="utc",
        ntp_policy="keep",
        total_hosts=2,
        status="running",
        task_id="apt-upgrade",
        min_free_gb=2,
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id=batch.batch_id,
        host="10.0.0.30",
        result="attention",
        error="Requiere migración de distribución.",
        after_state={"attention_kind": "migration"},
        db_path=db_path,
    )
    ExecutionBatch.add_host_result(
        batch_id=batch.batch_id,
        host="10.0.0.31",
        result="success",
        db_path=db_path,
    )
    finished = ExecutionBatch.finish_batch(
        batch_id=batch.batch_id, total_hosts=2, db_path=db_path
    )
    assert finished.attention_count == 1
    assert finished.success_count == 1
    assert finished.min_free_gb == pytest.approx(2.0)
    assert finished.status == "partial"


def test_schema_v15_adds_attention_and_threshold(tmp_path):
    db_path = str(tmp_path / "schema.db")
    init_schema(db_path)
    with get_connection(db_path) as conn:
        batch_cols = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(execution_batches)"
            ).fetchall()
        }
    assert "attention_count" in batch_cols
    assert "min_free_gb" in batch_cols


def _pdf_text(pdf_bytes: bytes) -> str:
    return pdf_bytes.decode("latin-1")


def test_general_report_includes_space_attention_and_migration_report_is_separate():
    batch = SimpleNamespace(
        batch_id="apt-pdf-batch-0001",
        task_id="apt-upgrade",
        created_at=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc),
        finished_at=None,
        operator="tester",
        action="APT",
        value="safe",
        target="10.0.0.0/24",
        time_mode="utc",
        ntp_policy="keep",
        admin_user="root",
        password_origin="stored",
        dry_run=False,
        status="partial",
        total_hosts=2,
        success_count=0,
        failure_count=0,
        skipped_count=0,
        error_count=0,
        preview_count=0,
        attention_count=2,
        duration_seconds=12,
        min_free_gb=2,
    )
    migration = SimpleNamespace(
        host="10.0.0.40",
        result="attention",
        error="Distribución bloqueada: Ubuntu 20.04 requiere migración.",
        created_at=None,
        after_state={
            "hostname": "migrar",
            "os_pretty": "Ubuntu 20.04.6 LTS",
            "attention_kind": "migration",
            "attention_reason": "Distribución bloqueada.",
            "ram_mb": 4096,
            "cpu_cores": 2,
            "cpu_model": "Intel",
            "disk_total_gb": 40.0,
            "disk_free_gb": 20.0,
        },
    )
    space = SimpleNamespace(
        host="10.0.0.41",
        result="attention",
        error="Espacio insuficiente: 1.00 GiB libres.",
        created_at=None,
        after_state={
            "hostname": "sin-espacio",
            "os_pretty": "Ubuntu 22.04 LTS",
            "attention_kind": "space",
            "attention_reason": "Espacio insuficiente: 1.00 GiB libres.",
        },
    )

    general = _pdf_text(
        build_execution_report(batch, [migration, space], task_name="APT")
    )
    assert "10.0.0.41" in general
    assert "sin-espacio" in general
    assert "Espacio insuficiente" in general
    assert "Requiere" in general
    assert "atención" in general

    migration_pdf = _pdf_text(
        build_migration_report(batch, [migration, space], task_name="APT")
    )
    assert "10.0.0.40" in migration_pdf
    assert "migrar" in migration_pdf
    assert "25 GB" in migration_pdf
    assert "10.0.0.41" not in migration_pdf


def test_apt_task_page_has_execution_form_and_threshold(client):
    _login(client)
    response = client.get("/tarea/apt-upgrade")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Actualización segura del sistema (APT)" in body
    assert 'name="min_free_gb"' in body
    assert "Formulario de" in body or "execForm" in body


def _login(client):
    from models.user import User

    client.get("/login")
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "admin",
            "password": "admin-password-123",
        },
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "app.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    app_module.init_db()
    from models.user import User

    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client
