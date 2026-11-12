# tests/test_paralelismo_2026_11_12.py
# Paralelismo acotado por lote y cupo global de playbooks simultáneos.
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import app as app_module
from models.batch import ExecutionBatch
from models.db import SCHEMA_VERSION, get_connection, init_schema
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


def test_batch_parallelizes_hosts_up_to_the_configured_limit(tmp_path, monkeypatch):
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
    assert positions == {host: index for index, host in enumerate(hosts, start=1)}
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


def test_global_limit_is_shared_by_two_concurrent_batches(tmp_path, monkeypatch):
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

    monkeypatch.setattr(ExecutionBatch, "add_host_result", staticmethod(record))
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
                pool.submit(_run_batch, task_a, "lote-a", hosts_a, db_path, logs_a),
                pool.submit(
                    _run_batch,
                    task_b,
                    "lote-b",
                    hosts_b,
                    db_path,
                    ["Lote lote-b"],
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


def test_parallel_batch_skips_everything_when_already_cancelled(tmp_path, monkeypatch):
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


def _wait_until(predicate, timeout=5.0):
    """Espera activa acotada para no depender de sleeps largos."""
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_cancel_while_a_host_is_running_skips_waiting_hosts(tmp_path, monkeypatch):
    hosts = [f"10.0.6.{index}" for index in range(1, 4)]
    db_path, task_id, results, log_lines = _prepare_batch(
        tmp_path,
        monkeypatch,
        hosts,
        "cancelacion-en-vuelo",
        concurrency=2,
        global_concurrency=1,
    )
    cancel_event = threading.Event()
    started = threading.Event()
    release = threading.Event()
    called = []

    def ping(host, **kwargs):
        called.append(host)
        if host == hosts[0]:
            started.set()
            assert release.wait(5), "el worker en vuelo no fue liberado"
        return {"success": True}

    def apply_after_cancel(**kwargs):
        if cancel_event.is_set():
            return {
                "success": False,
                "error": "Operación cancelada por el usuario.",
            }
        return {"success": True, "before_state": {}, "after_state": {}}

    monkeypatch.setattr(app_module, "test_ansible_ping", ping)
    monkeypatch.setattr(app_module, "apply_host", apply_after_cancel)

    outcome = []
    runner = threading.Thread(
        target=lambda: outcome.append(
            _run_batch(
                task_id,
                "cancelacion-en-vuelo",
                hosts,
                db_path,
                log_lines,
                cancel_event=cancel_event,
            )
        )
    )
    try:
        runner.start()
        assert started.wait(5), "el primer host no llegó a ejecutarse"
        cancel_event.set()
        # Los hosts en espera deben observar la cancelación y omitirse antes
        # de que el host en vuelo libere el cupo global.
        assert _wait_until(
            lambda: len(results) == 2
        ), "los hosts en espera no se omitieron a tiempo"
        release.set()
        runner.join(5)
        assert not runner.is_alive()
    finally:
        cancel_event.set()
        release.set()
        app_module.remove_task(task_id)

    assert outcome == ["cancelled"]
    # Ningún host nuevo empieza después de la cancelación.
    assert called == [hosts[0]]
    by_host = {item["host"]: item for item in results}
    assert by_host[hosts[0]]["result"] == "failure"
    assert all(by_host[host]["result"] == "skipped" for host in hosts[1:])


def test_deadline_while_a_host_is_running_skips_waiting_hosts(tmp_path, monkeypatch):
    hosts = [f"10.0.7.{index}" for index in range(1, 4)]
    db_path, task_id, results, log_lines = _prepare_batch(
        tmp_path,
        monkeypatch,
        hosts,
        "deadline-en-vuelo",
        concurrency=2,
        global_concurrency=1,
    )
    started = threading.Event()
    release = threading.Event()
    called = []

    def ping(host, **kwargs):
        called.append(host)
        if host == hosts[0]:
            started.set()
            assert release.wait(5), "el worker en vuelo no fue liberado"
        return {"success": True}

    monkeypatch.setattr(app_module, "test_ansible_ping", ping)
    monkeypatch.setattr(
        app_module,
        "apply_host",
        lambda **kwargs: {
            "success": True,
            "before_state": {},
            "after_state": {},
        },
    )
    deadline = time.monotonic() + 0.3
    outcome = []
    runner = threading.Thread(
        target=lambda: outcome.append(
            _run_batch(
                task_id,
                "deadline-en-vuelo",
                hosts,
                db_path,
                log_lines,
                deadline=deadline,
            )
        )
    )
    try:
        runner.start()
        assert started.wait(5), "el primer host no llegó a ejecutarse"
        assert _wait_until(lambda: time.monotonic() >= deadline + 0.05)
        # El host en vuelo puede terminar, pero los pendientes no deben empezar.
        assert _wait_until(
            lambda: len(results) == 2
        ), "los hosts pendientes no se omitieron al vencer el plazo"
        release.set()
        runner.join(5)
        assert not runner.is_alive()
    finally:
        release.set()
        app_module.remove_task(task_id)

    assert outcome == ["incomplete"]
    assert called == [hosts[0]]
    by_host = {item["host"]: item for item in results}
    assert by_host[hosts[0]]["result"] == "success"
    assert all(by_host[host]["result"] == "skipped" for host in hosts[1:])


def test_worker_failure_releases_the_shared_slot_for_the_next_batch(
    tmp_path, monkeypatch
):
    db_path, task_id, results, log_lines = _prepare_batch(
        tmp_path,
        monkeypatch,
        ["10.0.8.1"],
        "fallo-interno",
        concurrency=1,
        global_concurrency=1,
    )
    task_after = app_module.create_task(owner="admin")

    def failing_apply(**kwargs):
        raise RuntimeError("fallo simulado del playbook")

    monkeypatch.setattr(
        app_module, "test_ansible_ping", lambda **kwargs: {"success": True}
    )
    monkeypatch.setattr(app_module, "apply_host", failing_apply)
    try:
        first = _run_batch(task_id, "fallo-interno", ["10.0.8.1"], db_path, log_lines)
        monkeypatch.setattr(
            app_module,
            "apply_host",
            lambda **kwargs: {
                "success": True,
                "before_state": {},
                "after_state": {},
            },
        )
        second = _run_batch(
            task_after,
            "post-fallo",
            ["10.0.8.2"],
            db_path,
            ["Lote post-fallo"],
            deadline=time.monotonic() + 1,
        )
    finally:
        app_module.remove_task(task_id)
        app_module.remove_task(task_after)

    assert first == "completed"
    assert results[0]["result"] == "failure"
    # Si el cupo global se hubiera fugado, el segundo lote vencería el plazo.
    assert second == "completed"
    assert results[1]["result"] == "success"


def test_migration_v18_backfills_positions_and_keeps_them_idempotent(
    tmp_path,
):
    db_path = str(tmp_path / "migracion-v18.db")
    init_schema(db_path)
    hosts = ["10.0.9.3", "10.0.9.1", "10.0.9.2"]
    ExecutionBatch.create_batch(
        batch_id="legacy-v17",
        operator="admin",
        action="TZ",
        value="UTC",
        target="prueba",
        total_hosts=len(hosts),
        status="running",
        db_path=db_path,
    )
    for host in hosts:
        ExecutionBatch.add_host_result(
            batch_id="legacy-v17",
            host=host,
            result="success",
            db_path=db_path,
        )

    # Simula una base v17: sin la columna y con la versión anterior.
    with get_connection(db_path) as conn:
        conn.execute("ALTER TABLE execution_hosts DROP COLUMN position")
        conn.execute("PRAGMA user_version = 17")

    init_schema(db_path)

    with get_connection(db_path) as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        rows = conn.execute(
            "SELECT id, position FROM execution_hosts ORDER BY id"
        ).fetchall()
    assert [row["position"] for row in rows] == [row["id"] for row in rows]
    assert [
        item.host
        for item in ExecutionBatch.get_host_results("legacy-v17", db_path=db_path)
    ] == hosts

    # Una segunda pasada no reordena ni pisa posiciones ya asignadas.
    with get_connection(db_path) as conn:
        first_id = conn.execute("SELECT MIN(id) FROM execution_hosts").fetchone()[0]
        conn.execute(
            "UPDATE execution_hosts SET position = 99 WHERE id = ?", (first_id,)
        )
        conn.execute(
            "UPDATE execution_hosts SET position = 0 WHERE id != ?", (first_id,)
        )
    init_schema(db_path)
    with get_connection(db_path) as conn:
        positions = [
            row["position"]
            for row in conn.execute("SELECT position FROM execution_hosts ORDER BY id")
        ]
    assert positions[0] == 99
    assert all(position > 0 for position in positions)


def test_parallel_batch_skips_pending_hosts_when_budget_is_spent(tmp_path, monkeypatch):
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
