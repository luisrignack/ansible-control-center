# tests/test_concurrencia_2026_11_12.py
# Concurrencia entre operadores: reserva por host, caducidad y posición en cola.
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import app as app_module
from acc import execution as acc_execution
from acc import runtime
from models.batch import ExecutionBatch
from models.db import get_connection, init_schema
from services import ansible_runner
from services.host_locks import (
    acquire_host_lock,
    count_active_locks,
    is_host_locked,
    list_active_locks,
    release_batch_locks,
    release_host_lock,
)
from services.job_queue import JobQueue


def _run_batch(
    tmp_path,
    monkeypatch,
    hosts,
    batch_id="lote-1",
    candidates=None,
):
    """Prepara base, tarea y lote para ejercitar ``_run_apply_batch``."""
    db_path = str(tmp_path / "concurrencia.db")
    init_schema(db_path)
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    task_id = app_module.create_task(owner="admin")
    ExecutionBatch.create_batch(
        batch_id=batch_id,
        operator="admin",
        action="TZ",
        value="UTC",
        target="prueba",
        total_hosts=len(hosts),
        status="running",
        db_path=db_path,
    )
    try:
        outcome = app_module._run_apply_batch(
            tid=task_id,
            batch_id=batch_id,
            hosts=hosts,
            admin_user="root",
            candidates=list(candidates or []),
            action="TZ",
            value="UTC",
            password_origin="temporal",
            time_mode="utc",
            ntp_policy="keep",
            ntp_servers=[],
            sync_rtc=False,
            force=False,
            deadline=time.monotonic() + 60,
            db_path=db_path,
            log_lines=[f"Lote {batch_id}"],
        )
    finally:
        # No dejar tareas activas en memoria: otras pruebas (por ejemplo, el
        # interruptor de DRY RUN) comprueban que no hay operaciones en curso.
        app_module.remove_task(task_id)
    return db_path, outcome


# ---------------------------------------------------------------------------
# Reserva por host
# ---------------------------------------------------------------------------
def test_host_lock_is_exclusive_and_only_owner_releases(tmp_path):
    db_path = str(tmp_path / "locks.db")
    init_schema(db_path)

    assert acquire_host_lock("10.0.0.5", "lote-a", db_path=db_path) is True
    assert is_host_locked("10.0.0.5", db_path=db_path) is True

    # Otro lote no puede tomar el host mientras la reserva esté vigente.
    assert acquire_host_lock("10.0.0.5", "lote-b", db_path=db_path) is False
    # Tampoco puede liberar una reserva ajena.
    assert release_host_lock("10.0.0.5", "lote-b", db_path=db_path) is False
    assert is_host_locked("10.0.0.5", db_path=db_path) is True

    assert count_active_locks(db_path=db_path) == 1
    active = list_active_locks(db_path=db_path)
    assert [lock["host"] for lock in active] == ["10.0.0.5"]
    assert active[0]["batch_id"] == "lote-a"

    assert release_host_lock("10.0.0.5", "lote-a", db_path=db_path) is True
    assert acquire_host_lock("10.0.0.5", "lote-b", db_path=db_path) is True
    release_batch_locks("lote-b", db_path=db_path)
    assert count_active_locks(db_path=db_path) == 0


def test_only_one_concurrent_acquire_wins(tmp_path):
    db_path = str(tmp_path / "locks.db")
    init_schema(db_path)
    barrier = threading.Barrier(4)
    results: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def contender(index: int) -> None:
        barrier.wait()
        acquired = acquire_host_lock("10.0.0.6", f"lote-{index}", db_path=db_path)
        with lock:
            results.append((index, acquired))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(contender, range(4)))

    assert sum(acquired for _, acquired in results) == 1
    # La reserva ganadora sigue vigente y sale con release_batch_locks.
    winner = next(index for index, acquired in results if acquired)
    assert is_host_locked("10.0.0.6", db_path=db_path) is True
    release_batch_locks(f"lote-{winner}", db_path=db_path)
    assert is_host_locked("10.0.0.6", db_path=db_path) is False


def test_expired_lock_can_be_taken_by_another_batch(tmp_path):
    db_path = str(tmp_path / "locks.db")
    init_schema(db_path)

    assert acquire_host_lock("10.0.0.7", "lote-a", db_path=db_path) is True
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE host_locks SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE host = ?",
            ("10.0.0.7",),
        )

    assert acquire_host_lock("10.0.0.7", "lote-b", db_path=db_path) is True
    # El lote original ya no es dueño: no puede liberar la reserva nueva.
    assert release_host_lock("10.0.0.7", "lote-a", db_path=db_path) is False
    assert is_host_locked("10.0.0.7", db_path=db_path) is True
    assert release_host_lock("10.0.0.7", "lote-b", db_path=db_path) is True


def test_apply_batch_skips_a_host_locked_by_another_batch(tmp_path, monkeypatch):
    host = "10.0.0.8"
    db_path = str(tmp_path / "concurrencia.db")
    init_schema(db_path)
    assert acquire_host_lock(host, "otro-lote", db_path=db_path) is True

    # No se debe ejecutar ningún playbook: el ping ni siquiera se llama.
    called = []
    monkeypatch.setattr(
        acc_execution, "test_ansible_ping", lambda **kwargs: called.append(kwargs)
    )

    db_path, outcome = _run_batch(
        tmp_path, monkeypatch, [host], batch_id="lote-ocupado"
    )

    results = ExecutionBatch.get_host_results("lote-ocupado", db_path=db_path)
    assert outcome == "completed"
    assert len(results) == 1
    assert results[0].result == "skipped"
    assert "Otra operación en curso" in results[0].error
    assert called == []
    # La reserva ajena sigue intacta y solo la libera su dueño.
    assert is_host_locked(host, db_path=db_path) is True
    release_host_lock(host, "otro-lote", db_path=db_path)


def test_apply_batch_releases_the_host_lock_after_success(tmp_path, monkeypatch):
    host = "10.0.0.9"
    monkeypatch.setattr(
        acc_execution, "test_ansible_ping", lambda **kwargs: {"success": True}
    )
    monkeypatch.setattr(
        acc_execution,
        "apply_host",
        lambda **kwargs: {"success": True, "before_state": {}, "after_state": {}},
    )

    db_path, outcome = _run_batch(
        tmp_path,
        monkeypatch,
        [host],
        batch_id="lote-ok",
        candidates=["clave-valida"],
    )

    results = ExecutionBatch.get_host_results("lote-ok", db_path=db_path)
    assert outcome == "completed"
    assert [result.result for result in results] == ["success"]
    assert is_host_locked(host, db_path=db_path) is False


# ---------------------------------------------------------------------------
# Estado y posición en la cola
# ---------------------------------------------------------------------------
def test_job_queue_reports_position_while_pending(tmp_path, monkeypatch):
    job_queue = JobQueue(max_workers=1, max_queue=2, shutdown_grace=2)
    started = threading.Event()
    release = threading.Event()

    def blocking(cancel_event):
        started.set()
        release.wait(5)

    job_queue.submit("primero", blocking)
    assert started.wait(2)
    job_queue.submit("segundo", lambda cancel_event: None)

    assert job_queue.position("segundo") == 1
    assert job_queue.position("primero") is None

    # La vista de la tarea añade la posición sin persistirla.
    monkeypatch.setattr(runtime, "_job_queue", job_queue)
    task = {"status": "pending", "completed": False, "message": "En cola…"}
    decorated = app_module._with_queue_position("segundo", task)
    assert decorated["queue_position"] == 1
    assert "posición 1" in decorated["message"]

    release.set()
    deadline = time.monotonic() + 5
    while job_queue.position("segundo") is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job_queue.position("segundo") is None
    job_queue.shutdown(wait=True)


def test_queue_eta_uses_median_of_recent_batches(tmp_path, monkeypatch):
    db_path = str(tmp_path / "eta.db")
    init_schema(db_path)
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)

    for index, seconds in enumerate((10, 20, 30)):
        batch_id = f"eta-{index}"
        ExecutionBatch.create_batch(
            batch_id=batch_id,
            operator="admin",
            action="TZ",
            value="UTC",
            target="prueba",
            total_hosts=1,
            status="running",
            db_path=db_path,
        )
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE execution_batches SET created_at = "
                "datetime('now', ?) WHERE batch_id = ?",
                (f"-{seconds} seconds", batch_id),
            )
        ExecutionBatch.add_host_result(
            batch_id=batch_id,
            host="10.0.0.1",
            result="success",
            duration_ms=100,
            db_path=db_path,
        )
        ExecutionBatch.finish_batch(batch_id=batch_id, db_path=db_path)

    app_module._queue_eta_cache.clear()
    try:
        # La duración incluye el tiempo real transcurrido desde created_at.
        assert 18 <= app_module._queue_eta_seconds("TZ") <= 23
        # Sin acción concreta se usa la mediana global.
        assert 18 <= app_module._queue_eta_seconds("") <= 23
    finally:
        app_module._queue_eta_cache.clear()


def test_job_queue_records_wait_time():
    job_queue = JobQueue(max_workers=1, max_queue=2, shutdown_grace=2)
    started = threading.Event()
    release = threading.Event()

    def blocking(cancel_event):
        started.set()
        release.wait(5)

    job_queue.submit("primero", blocking)
    assert started.wait(2)
    job_queue.submit("segundo", lambda cancel_event: None)
    release.set()

    deadline = time.monotonic() + 5
    while job_queue.wait_stats()["count"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stats = job_queue.wait_stats()
    assert stats["count"] == 1
    assert stats["max_seconds"] >= 0
    job_queue.shutdown(wait=True)


def test_apply_host_respects_an_explicit_dry_run_override(monkeypatch):
    # El lote fija el modo al arrancar; no depende del global en ese momento.
    monkeypatch.setattr(ansible_runner, "DRY_RUN", False)
    result = ansible_runner.apply_host(
        host="10.0.0.1",
        user="root",
        action="TZ",
        value="UTC",
        password="clave",
        dry_run=True,
    )
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["simulated"] is True


def test_queue_position_is_not_added_to_running_tasks(monkeypatch):
    job_queue = JobQueue(max_workers=1, max_queue=1, shutdown_grace=1)
    monkeypatch.setattr(runtime, "_job_queue", job_queue)

    running = {"status": "running", "completed": False}
    assert app_module._with_queue_position("x", running) is running

    job_queue.shutdown(wait=True)
