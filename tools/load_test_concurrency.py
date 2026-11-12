#!/usr/bin/env python3
"""Prueba de carga sintética del planificador de lotes.

Simula varios operadores enviando lotes a la vez por la misma ruta que usa la
aplicación (cola de trabajos, ``execution_batches``, reservas por host y
paralelismo acotado), pero sustituye Ansible por una latencia fija. No mide la
red ni los hosts reales: mide cuánto tarda el planificador en drenar el trabajo
y cuántos playbooks simultáneos llega a abrir.

Uso:
    .venv/bin/python tools/load_test_concurrency.py
    .venv/bin/python tools/load_test_concurrency.py --users 4 --hosts 20 \\
        --latency 0.2 --batch-concurrency 4 --global-concurrency 8

El resultado es orientativo. Para una prueba real, ejecuta lotes contra un
inventario de laboratorio y compara con ``/metricas``.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module
from models.batch import ExecutionBatch
from models.db import init_schema
from services.job_queue import JobQueue


class Probe:
    """Contabiliza hosts en vuelo y aplica una latencia fija."""

    def __init__(self, latency: float) -> None:
        self.latency = latency
        self.active = 0
        self.max_active = 0
        self.completed = 0
        self.lock = threading.Lock()

    def ping(self, **kwargs):
        # El ping real también tarda; se modela como una fracción del cambio.
        time.sleep(self.latency / 2)
        return {"success": True}

    def apply(self, **kwargs):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.latency)
            with self.lock:
                self.completed += 1
            return {
                "success": True,
                "before_state": {},
                "after_state": {},
            }
        finally:
            with self.lock:
                self.active -= 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=4)
    parser.add_argument("--hosts", type=int, default=20)
    parser.add_argument("--latency", type=float, default=0.2)
    parser.add_argument("--batch-concurrency", type=int, default=4)
    parser.add_argument("--global-concurrency", type=int, default=8)
    parser.add_argument("--job-workers", type=int, default=2)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="acc-load-") as temp_dir:
        db_path = str(Path(temp_dir) / "load.db")
        init_schema(db_path)
        app_module.get_db_path = lambda: db_path
        app_module.BATCH_HOST_CONCURRENCY = args.batch_concurrency
        app_module.GLOBAL_HOST_CONCURRENCY = args.global_concurrency
        app_module._host_semaphore = None

        probe = Probe(args.latency)
        app_module.test_ansible_ping = probe.ping
        app_module.apply_host = probe.apply

        queue = JobQueue(
            max_workers=args.job_workers,
            max_queue=max(4, args.users * 2),
            shutdown_grace=5,
        )
        waits: list[float] = []
        outcomes: list[str] = []
        errors: list[str] = []
        lock = threading.Lock()

        def run_batch(operator: str, batch_id: str, task_id: str, hosts: list[str],
                      submitted_at: float) -> None:
            started_at = time.monotonic()
            with lock:
                waits.append(started_at - submitted_at)
            try:
                outcome = app_module._run_apply_batch(
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
                    deadline=time.monotonic() + 600,
                    db_path=db_path,
                    log_lines=[f"Lote {batch_id}"],
                )
                ExecutionBatch.finish_batch(batch_id=batch_id, db_path=db_path)
                with lock:
                    outcomes.append(outcome)
            except Exception as exc:  # pragma: no cover - diagnóstico
                with lock:
                    errors.append(f"{operator}: {exc}")
            finally:
                app_module.remove_task(task_id)

        started = time.monotonic()
        try:
            for index in range(1, args.users + 1):
                operator = f"operador-{index}"
                batch_id = f"carga-{uuid.uuid4().hex[:8]}-{index}"
                task_id = app_module.create_task(owner=operator)
                hosts = [
                    f"10.20.{index}.{host}"
                    for host in range(1, args.hosts + 1)
                ]
                ExecutionBatch.create_batch(
                    batch_id=batch_id,
                    operator=operator,
                    action="TZ",
                    value="UTC",
                    target="laboratorio",
                    total_hosts=len(hosts),
                    status="running",
                    db_path=db_path,
                )
                submitted_at = time.monotonic()
                queue.submit(
                    task_id,
                    lambda cancel_event, operator=operator, batch_id=batch_id,
                    task_id=task_id, hosts=hosts, submitted_at=submitted_at: run_batch(
                        operator, batch_id, task_id, hosts, submitted_at
                    ),
                )
        finally:
            queue.shutdown(wait=True)
        elapsed = time.monotonic() - started

    total_hosts = args.users * args.hosts
    summary = {
        "users": args.users,
        "hosts_per_batch": args.hosts,
        "total_hosts": total_hosts,
        "simulated_latency_seconds": args.latency,
        "job_workers": args.job_workers,
        "batch_concurrency": args.batch_concurrency,
        "global_concurrency": args.global_concurrency,
        "max_playbooks_in_flight": probe.max_active,
        "wall_seconds": round(elapsed, 3),
        "hosts_per_second": round(total_hosts / elapsed, 1) if elapsed else 0,
        "queue_wait_seconds": {
            "mean": round(statistics.mean(waits), 3) if waits else 0,
            "max": round(max(waits), 3) if waits else 0,
        },
        "outcomes": outcomes,
        "errors": errors,
        "completed_playbooks": probe.completed,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors and len(outcomes) == args.users else 1


if __name__ == "__main__":
    raise SystemExit(main())
