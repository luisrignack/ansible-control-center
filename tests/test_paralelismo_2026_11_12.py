# tests/test_paralelismo_2026_11_12.py
# Paralelismo acotado por lote y cupo global de playbooks simultáneos.
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import app as app_module
from models.batch import ExecutionBatch
from models.db import init_schema
from models.execution import ExecutionRecord


def _prepare_batch(
    tmp_path,
    monkeypatch,
    hosts,
    batch_id,
    *,
    concurrency,
    global_concurrency,
):
    """Base aislada, tarea y captura de resultados sin escribir por host."""
    db_path = str(tmp_path / f"{batch_id}.db")
    init_schema(db_path)
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    monkeypatch.setattr(app_module, "BATCH_HOST_CONCURRENCY", concurrency)
    monkeypatch.setattr(app_module, "GLOBAL_HOST_CONCURRENCY", global_concurrency)
    # El semáforo se crea de forma perezosa: se reinicia para que la prueba
    # use el cupo configurado y no el de una prueba anterior.
    monkeypatch.setattr(app_module, "_host_semaphore", None)
    results: list[dict] = []
    monkeypatch.setattr(
        ExecutionBatch,
        "add_host_result",
        staticmethod(lambda **kwargs: results.append(kwargs)),
    )
    monkeypatch.setattr(
        ExecutionRecord,
        "record_execution",
        staticmethod(lambda **kwargs: None),
    )
    task_id = app_module.create_task(owner="admin")
    log_lines = [f"Lote {batch_id}"]
    return db_path, task_id, results, log_lines


def _run_batch(
    task_id,
    batch_id,
    hosts,
    db_path,
    log_lines,
    *,
    deadline=None,
    cancel_event=None,
):
    return app_module._run_apply_batch(
        tid=task_id,
        batch_id=batch_id,
        hosts=hosts,
        admin_user="root",
        candidates=["clave-valida"],
        action="TZ",
        value="UTC",
        password_origin="temporal",
        time_mode="utc",
        ntp_policy="keep",
        ntp_servers=[],
        sync_rtc=False,
        force=False,
        deadline=deadline or (time.monotonic() + 60),
        db_path=db_path,
        log_lines=log_lines,
        cancel_event=cancel_event,
    )


class _ConcurrencyProbe:
    def __init__(self, delay: float = 0.08) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.lock = threading.Lock()

    def ping(self, **kwargs):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        return {"success": True}


def test_batch_parallelizes_hosts_up_to_the_configured_limit(
    tmp_path, monkeypatch
):
    hosts = [f"10.0.1.{index}" for index in range(1, 7)]
    db_path, task_id, results, log_lines = _prepare_batch(
        tmp_path,
        monkeypatch,
        hosts,
        "paralelo",
        concurrency=3,
        global_concurrency=3,
    )
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(app_module, "test_ansible_ping", probe.ping)
    monkeypatch.setattr(
        app_module,
        "apply_host",
        lambda **kwargs: {"success": True, "before_state": {}, "after_state": {}},
    )

    try:
        outcome = _run_batch(task_id, "paralelo", hosts, db_path, log_lines)
    finally:
        app_module.remove_task(task_id)

    assert outcome == "completed"
    # Se solapan hosts, pero nunca más que el cupo del lote.
    assert 2 <= probe.max_active <= 3
    assert probe.calls == len(hosts)
    # Aunque terminen en otro orden, cada host conserva su posición de entrada.
    positions = {item["host"]: item["position"] for item in results}
    assert positions == {
        host: index for index, host in enumerate(hosts, start=1)
    }
    assert all(item["result"] == "success" for item in results)


def test_host_results_keep_the_input_order_regardless_of_completion(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "orden.db")
    init_schema(db_path)
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    ExecutionBatch.create_batch(
        batch_id="orden",
        operator="admin",
        action="TZ",
        value="UTC",
        target="prueba",
        total_hosts=3,
        status="running",
        db_path=db_path,
    )
    # Se insertan en orden de finalización distinto al de entrada.
    for host, position in (("10.0.5.3", 3), ("10.0.5.1", 1), ("10.0.5.2", 2)):
        ExecutionBatch.add_host_result(
            batch_id="orden",
            host=host,
            result="success",
            position=position,
            db_path=db_path,
        )

    results = ExecutionBatch.get_host_results("orden", db_path=db_path)
    assert [item.host for item in results] == [
        "10.0.5.1",
        "10.0.5.2",
        "10.0.5.3",
    ]


def test_global_limit_is_shared_by_two_concurrent_batches(
    tmp_path, monkeypatch
):
    hosts_a = [f"10.0.2.{index}" for index in range(1, 5)]
    hosts_b = [f"10.0.2.{index}" for index in range(11, 15)]
    db_path, task_a, results_a, logs_a = _prepare_batch(
        tmp_path,
        monkeypatch,
        hosts_a,
        "lote-a",
        concurrency=2,
        global_concurrency=2,
    )
    # La segunda tarea comparte la misma base y el mismo semáforo global.
    task_b = app_module.create_task(owner="admin")
    results_b: list[dict] = []
    original_add = ExecutionBatch.add_host_result

    def record(**kwargs):
        target = results_a if kwargs["batch_id"] == "lote-a" else results_b
        target.append(kwargs)

    monkeypatch.setattr(
        ExecutionBatch, "add_host_result", staticmethod(record)
    )
    assert original_add is not None

    probe = _ConcurrencyProbe(delay=0.05)
    monkeypatch.setattr(app_module, "test_ansible_ping", probe.ping)
    monkeypatch.setattr(
        app_module,
        "apply_host",
        lambda **kwargs: {"success": True, "before_state": {}, "after_state": {}},
    )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    _run_batch, task_a, "lote-a", hosts_a, db_path, logs_a
                ),
                pool.submit(
                    _run_batch,
                    task_b,
                    "lote-b",
                    hosts_b,
                    db_path,
                    [f"Lote lote-b"],
                ),
            ]
            outcomes = [future.result() for future in futures]
    finally:
        app_module.remove_task(task_a)
        app_module.remove_task(task_b)

    assert outcomes == ["completed", "completed"]
    # Dos lotes de 2 hosts comparten un cupo global de 2.
    assert 2 <= probe.max_active <= 2
    assert probe.calls == len(hosts_a) + len(hosts_b)
    assert len(results_a) == len(hosts_a)
    assert len(results_b) == len(hosts_b)


def test_parallel_batch_skips_everything_when_already_cancelled(
    tmp_path, monkeypatch
):
    hosts = [f"10.0.3.{index}" for index in range(1, 5)]
    db_path, task_id, results, log_lines = _prepare_batch(
        tmp_path,
        monkeypatch,
        hosts,
        "cancelado",
        concurrency=2,
        global_concurrency=2,
    )
    event = threading.Event()
    event.set()
    called = []
    monkeypatch.setattr(
        app_module, "test_ansible_ping", lambda **kwargs: called.append(kwargs)
    )

    try:
        outcome = _run_batch(
            task_id,
            "cancelado",
            hosts,
            db_path,
            log_lines,
            cancel_event=event,
        )
    finally:
        app_module.remove_task(task_id)

    assert outcome == "cancelled"
    assert called == []
    assert len(results) == len(hosts)
    assert all(item["result"] == "skipped" for item in results)
    assert all("Cancelado" in item["error"] for item in results)


def test_parallel_batch_skips_pending_hosts_when_budget_is_spent(
    tmp_path, monkeypatch
):
    hosts = [f"10.0.4.{index}" for index in range(1, 4)]
    db_path, task_id, results, log_lines = _prepare_batch(
        tmp_path,
        monkeypatch,
        hosts,
        "sin-tiempo",
        concurrency=2,
        global_concurrency=2,
    )
    called = []
    monkeypatch.setattr(
        app_module, "test_ansible_ping", lambda **kwargs: called.append(kwargs)
    )

    try:
        outcome = _run_batch(
            task_id,
            "sin-tiempo",
            hosts,
            db_path,
            log_lines,
            deadline=time.monotonic() - 1,
        )
    finally:
        app_module.remove_task(task_id)

    assert outcome == "incomplete"
    assert called == []
    assert len(results) == len(hosts)
    assert all(item["result"] == "skipped" for item in results)
