"""Purga de demostración sin borrados colaterales (2026-11-14).

La versión anterior borraba por patrones amplios (``operator LIKE 'demo.%'``,
``detail LIKE '%[demo]%'``). Un operador real llamado ``demo.fulano`` o un
evento real que mencionara «[demo]» desaparecían al ejecutar ``--purge``.
Ahora se enumeran los identificadores exactos que creó el sembrado.
"""

from __future__ import annotations

import pytest

import config
from models.batch import ExecutionBatch
from models.db import get_connection, init_schema
from models.ops import HostGroup
from models.user import AuthEvent
from services.demo_data import (
    DEMO_GROUP_NAMES,
    demo_data_present,
    purge_demo_data,
    seed_demo_data,
)


def _make_batch(db_path: str, batch_id: str, operator: str) -> None:
    ExecutionBatch.create_batch(
        batch_id,
        operator,
        "TZ",
        "America/Caracas",
        "10.0.0.0/24",
        total_hosts=1,
        status="success",
        db_path=db_path,
    )


def test_purge_keeps_real_operator_with_demo_prefix(tmp_path):
    db_path = str(tmp_path / "seed.db")
    init_schema(db_path)
    _make_batch(db_path, "20261114-120000-abcd", "demo.fulano")
    seed_demo_data(db_path, with_running=False)

    purge_demo_data(db_path)

    assert ExecutionBatch.get_batch("20261114-120000-abcd", db_path=db_path) is not None
    with get_connection(db_path) as conn:
        demo_batches = conn.execute(
            "SELECT COUNT(*) FROM execution_batches WHERE batch_id LIKE 'demo-%'"
        ).fetchone()[0]
    assert demo_batches == 0


def test_purge_keeps_manual_group_with_demo_like_name(tmp_path):
    db_path = str(tmp_path / "seed.db")
    init_schema(db_path)
    real = HostGroup.create("Demo · Producción real", db_path=db_path)
    seed_demo_data(db_path, with_running=False)

    purge_demo_data(db_path)

    assert HostGroup.get(real.id, db_path=db_path) is not None
    names = {group.name for group in HostGroup.all(db_path=db_path)}
    assert names == {"Demo · Producción real"}
    assert "Demo · Sucursal Vedado" in DEMO_GROUP_NAMES


def test_purge_keeps_real_event_without_demo_marker(tmp_path):
    db_path = str(tmp_path / "seed.db")
    init_schema(db_path)
    AuthEvent.record(
        "demo.maria",
        "10.99.20.11",
        "login_success",
        "Inicio de sesión real de la operadora",
        db_path=db_path,
    )
    seed_demo_data(db_path, with_running=False)

    purge_demo_data(db_path)

    events = AuthEvent.recent(limit=50, db_path=db_path)
    assert [event["detail"] for event in events] == [
        "Inicio de sesión real de la operadora"
    ]


def test_seed_refuses_production_without_force(tmp_path, monkeypatch):
    db_path = str(tmp_path / "seed.db")
    init_schema(db_path)
    monkeypatch.setattr(config, "IS_PRODUCTION", True)

    with pytest.raises(RuntimeError, match="production"):
        seed_demo_data(db_path)

    # La purga sí se permite: solo elimina datos ficticios.
    assert purge_demo_data(db_path)["groups"] == 0

    summary = seed_demo_data(db_path, with_running=False, allow_production=True)
    assert summary["groups"] == 4
    assert demo_data_present(db_path)


def test_cli_reports_production_refusal(tmp_path, monkeypatch):
    import app as app_module
    from acc import runtime

    db_path = str(tmp_path / "seed.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    init_schema(db_path)

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["seed-demo", "--yes", "--no-running"])
    assert result.exit_code != 0
    assert "production" in result.output
