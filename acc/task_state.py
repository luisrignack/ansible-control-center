"""Estado en memoria y persistido de las tareas, progreso SSE e
idempotencia de operaciones.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from acc import runtime
from acc.runtime import (
    is_dry_run,
)
from config import (
    AUTH_EVENT_RETENTION_DAYS,
    HISTORY_RETENTION_DAYS,
    JOB_MAX_WORKERS,
    JOB_RETENTION_HOURS,
    MAX_MEMORY_TASKS,
    MAX_TASK_STATE_ROWS,
    TASK_PERSIST_INTERVAL_SECONDS,
)
from models.batch import ExecutionBatch
from models.db import get_connection
from models.execution import ExecutionRecord
from models.user import AuthEvent
from services.host_locks import count_active_locks

logger = logging.getLogger("acc")


_tasks: dict[str, dict[str, Any]] = {}


_tasks_lock = threading.Lock()


_task_events: dict[str, threading.Event] = {}


_task_persist_times: dict[str, float] = {}


_task_persist_lock = threading.Lock()


_QUEUE_ETA_CACHE_SECONDS = 30


_queue_eta_cache: dict[str, tuple[float, float | None]] = {}


_queue_eta_lock = threading.Lock()


def _active_task_count() -> int:
    """Número de tareas pendientes o en ejecución en este proceso."""
    with _tasks_lock:
        return sum(
            1
            for task in _tasks.values()
            if not task.get("completed")
            and task.get("status") in {"pending", "running"}
        )


_OPERATION_ID_TTL = 3600


_MAX_OPERATION_IDS = 5000


TERMINAL_STATES = {
    "completed",
    "failed",
    "error",
    "found",
    "failure",
    "timeout",
    "cancelled",
    "incomplete",
}


def sse_event(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def sse_retry(retry_ms: int) -> str:
    return f"retry: {retry_ms}\n\n"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_task_persist_warned = False


def _persist_task(tid: str, task: dict[str, Any]) -> None:
    """Guarda una instantánea de la tarea en SQLite.

    El estado sigue viviendo en memoria para el SSE; SQLite es una copia de
    respaldo para que la página de progreso sobreviva a un reinicio. Un fallo
    de escritura no debe tumbar la ejecución.
    """
    global _task_persist_warned
    try:
        payload = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
        with get_connection(runtime.get_db_path()) as conn:
            conn.execute(
                """
                INSERT INTO task_state (task_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (tid, payload, _utc_now_iso()),
            )
    except Exception:
        if not _task_persist_warned:
            _task_persist_warned = True
            logger.exception("No se pudo persistir el estado de la tarea")


def _load_persisted_task(tid: str) -> dict[str, Any] | None:
    """Recupera una tarea de SQLite y la vuelve a cachear en memoria."""
    try:
        with get_connection(runtime.get_db_path()) as conn:
            row = conn.execute(
                "SELECT payload FROM task_state WHERE task_id = ?", (tid,)
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    try:
        task = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(task, dict):
        return None

    with _tasks_lock:
        _tasks.setdefault(tid, task)
        return _tasks[tid]


def _refresh_task_from_db(tid: str, current: dict[str, Any]) -> dict[str, Any]:
    """Adopta la instantánea de SQLite si otra instancia la actualizó.

    La copia en memoria del proceso que ejecuta la tarea puede ser más nueva
    que la persistida (la consola se agrupa cada ``ACC_TASK_PERSIST_INTERVAL``
    segundos). Por eso solo se reemplaza cuando la marca ``updated_at`` de
    SQLite es estrictamente mayor.
    """
    try:
        with get_connection(runtime.get_db_path()) as conn:
            row = conn.execute(
                "SELECT payload FROM task_state WHERE task_id = ?", (tid,)
            ).fetchone()
    except Exception:  # noqa: BLE001
        return current
    if row is None:
        return current

    try:
        persisted = json.loads(row["payload"])
    except (TypeError, ValueError):
        return current
    if not isinstance(persisted, dict):
        return current

    persisted_updated = str(persisted.get("updated_at") or "")
    current_updated = str(current.get("updated_at") or "")
    if persisted_updated <= current_updated:
        return current

    with _tasks_lock:
        latest = _tasks.get(tid)
        if (
            latest is not None
            and str(latest.get("updated_at") or "") > persisted_updated
        ):
            return latest
        _tasks[tid] = persisted
        return persisted


def _queue_eta_seconds(action: str | None) -> float | None:
    """Mediana de duración de lotes recientes de la misma acción."""
    key = (action or "").strip().upper()
    now = time.monotonic()
    with _queue_eta_lock:
        cached = _queue_eta_cache.get(key)
        if cached is not None and now - cached[0] < _QUEUE_ETA_CACHE_SECONDS:
            return cached[1]
    try:
        durations = ExecutionBatch.recent_completed_durations(
            action=key or None, limit=20, db_path=runtime.get_db_path()
        )
    except Exception:
        logger.debug("No se pudo estimar la espera de cola", exc_info=True)
        durations = []
    estimate = statistics.median(durations) if durations else None
    with _queue_eta_lock:
        _queue_eta_cache[key] = (now, estimate)
    return estimate


def _format_queue_wait(seconds: float) -> str:
    """Texto corto para la estimación de espera."""
    value = max(0, round(seconds))
    if value >= 120:
        return f"~{value // 60} min"
    return f"~{value} s"


def _queue_snapshot() -> dict[str, Any]:
    """Estado de la cola para el panel de métricas y su API JSON."""
    now = datetime.now(UTC)
    with _tasks_lock:
        tasks = list(_tasks.values())
    pending = [
        task
        for task in tasks
        if not task.get("completed") and task.get("status") == "pending"
    ]
    running = [
        task
        for task in tasks
        if not task.get("completed") and task.get("status") == "running"
    ]
    oldest = 0.0
    for task in pending:
        try:
            created = datetime.fromisoformat(str(task.get("created_at") or ""))
        except (TypeError, ValueError):
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        oldest = max(oldest, (now - created).total_seconds())

    wait = (
        runtime._job_queue.wait_stats()
        if runtime._job_queue is not None
        else {
            "count": 0,
            "avg_seconds": 0.0,
            "p50_seconds": 0.0,
            "p95_seconds": 0.0,
            "max_seconds": 0.0,
        }
    )
    try:
        locks = count_active_locks(db_path=runtime.get_db_path())
    except Exception:
        logger.debug("No se pudieron contar las reservas de host", exc_info=True)
        locks = 0
    return {
        "depth": runtime._job_queue.pending if runtime._job_queue is not None else 0,
        "workers": JOB_MAX_WORKERS,
        "tasks_pending": len(pending),
        "tasks_running": len(running),
        "oldest_pending_seconds": round(oldest, 1),
        "active_host_locks": locks,
        "wait": wait,
    }


def _with_queue_position(
    tid: str, task: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Añade posición y espera estimada a una tarea que aún no arrancó.

    La posición cambia cuando otros trabajos arrancan, así que se calcula en
    cada lectura y no se persiste en SQLite. La espera se estima con la
    mediana de lotes recientes de la misma acción, repartida entre los puestos
    de trabajo disponibles.
    """
    if (
        not task
        or task.get("completed")
        or task.get("status") != "pending"
        or runtime._job_queue is None
    ):
        return task
    position = runtime._job_queue.position(tid)
    if position is None:
        return task
    decorated = {**task, "queue_position": position}

    eta_seconds: int | None = None
    estimate = _queue_eta_seconds(task.get("action"))
    if estimate:
        workers = max(1, JOB_MAX_WORKERS)
        batches_ahead = (position + workers - 1) // workers
        eta_seconds = round(estimate * batches_ahead)
        decorated["queue_eta_seconds"] = eta_seconds

    if not task.get("cancellation_requested"):
        detail = f"posición {position}"
        if eta_seconds:
            detail += f", espera {_format_queue_wait(eta_seconds)}"
        decorated["message"] = f"En cola ({detail})…"
    return decorated


def _get_task(tid: str, refresh: bool = False) -> dict[str, Any] | None:
    """Devuelve una tarea, leyendo SQLite cuando no está en este proceso.

    ``refresh=True`` consulta la instantánea persistida para que las lecturas
    (estado, progreso y cancelación) sean correctas aunque la petición caiga
    en un worker distinto del que ejecuta la tarea.
    """
    with _tasks_lock:
        task = _tasks.get(tid)
    if task is None:
        task = _load_persisted_task(tid)
    elif refresh:
        task = _refresh_task_from_db(tid, task)
    return _with_queue_position(tid, task)


def _delete_persisted_task(tid: str) -> None:
    try:
        with get_connection(runtime.get_db_path()) as conn:
            conn.execute("DELETE FROM task_state WHERE task_id = ?", (tid,))
    except Exception:  # noqa: BLE001
        logger.debug("No se pudo borrar la tarea persistida %s", tid)


def _trim_task_state_rows() -> None:
    """Acota la tabla ``task_state`` a las instantáneas más recientes.

    Sin este límite, un proceso de larga vida acumularía el estado completo de
    todas las operaciones históricas (incluida la consola) en SQLite.
    """
    try:
        with get_connection(runtime.get_db_path()) as conn:
            conn.execute(
                """
                DELETE FROM task_state
                WHERE task_id NOT IN (
                    SELECT task_id FROM task_state
                    ORDER BY updated_at DESC, task_id DESC
                    LIMIT ?
                )
                """,
                (MAX_TASK_STATE_ROWS,),
            )
    except Exception:
        logger.debug("No se pudo recortar task_state", exc_info=True)


def _purge_retention() -> None:
    """Aplica las políticas de retención al arrancar.

    Se ejecuta de forma perezosa y tolerante a fallos: una tabla ausente o una
    base de datos bloqueada no debe impedir que la aplicación arranque.
    """
    db_path = runtime.get_db_path()
    try:
        tasks = ExecutionBatch.purge_old(HISTORY_RETENTION_DAYS, db_path=db_path)
    except Exception:
        tasks = 0
        logger.exception("No se pudieron purgar lotes antiguos")
    try:
        records = ExecutionRecord.purge_old(HISTORY_RETENTION_DAYS, db_path=db_path)
    except Exception:
        records = 0
        logger.exception("No se pudo purgar el historial heredado")
    try:
        events = AuthEvent.purge_old(AUTH_EVENT_RETENTION_DAYS, db_path=db_path)
    except Exception:
        events = 0
        logger.exception("No se pudieron purgar eventos de autenticación")
    _trim_task_state_rows()
    if tasks or records or events:
        logger.info(
            "Retención aplicada: %s lote(s), %s registro(s) y %s evento(s) "
            "eliminados",
            tasks,
            records,
            events,
        )


def _recover_task_state(stale_minutes: int | None = None) -> None:
    """Purga tareas antiguas y cierra las que quedaron en curso.

    Al arrancar no puede haber ninguna tarea legítimamente en ejecución en este
    proceso, así que las instantáneas incompletas se marcan como error. Con
    ``stale_minutes`` solo se tocan las que llevan ese tiempo sin actualizarse:
    así un worker que arranca no cierra operaciones que otro worker sigue
    ejecutando.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=JOB_RETENTION_HOURS)).isoformat(
        timespec="seconds"
    )
    stale_cutoff = None
    if stale_minutes is not None:
        stale_cutoff = (
            datetime.now(UTC) - timedelta(minutes=int(stale_minutes))
        ).isoformat(timespec="seconds")

    with get_connection(runtime.get_db_path()) as conn:
        conn.execute("DELETE FROM task_state WHERE updated_at < ?", (cutoff,))
        rows = conn.execute(
            "SELECT task_id, payload, updated_at FROM task_state"
        ).fetchall()

    for row in rows:
        if stale_cutoff is not None and (row["updated_at"] or "") >= stale_cutoff:
            # Actualizada recientemente: puede ser otra instancia viva.
            continue
        try:
            task = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(task, dict) or task.get("completed"):
            continue
        task.update(
            {
                "status": "error",
                "message": "El proceso se reinició durante la ejecución.",
                "overall_result": "error",
                "completed": True,
                "updated_at": _utc_now_iso(),
            }
        )
        _persist_task(row["task_id"], task)


def create_task(owner: str = "") -> str:
    """Crea una tarea en memoria y en SQLite.

    ``owner`` es el usuario que la lanzó. Se usa para que un operador solo
    pueda cancelar sus propias operaciones; un administrador puede cancelar
    cualquiera. Las ejecuciones programadas no tienen dueño humano y quedan
    reservadas a los administradores.
    """
    task_id = str(uuid.uuid4())
    task = {
        "status": "pending",
        "progress": 0,
        "message": "En cola…",
        "log": [],
        "console": [],
        "host": "",
        "password_hash": "",
        "overall_result": "unknown",
        "completed": False,
        "dry_run": is_dry_run(),
        "owner": owner or "",
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
    }
    with _tasks_lock:
        _tasks[task_id] = task
        _task_events[task_id] = threading.Event()
        # Acota el estado en memoria: las tareas cerradas más antiguas se
        # descartan primero. La instantánea SQLite sigue disponible para el
        # historial reciente.
        if len(_tasks) > MAX_MEMORY_TASKS:
            removable = [
                key
                for key, value in _tasks.items()
                if key != task_id and value.get("completed")
            ]
            for key in removable[: max(0, len(_tasks) - MAX_MEMORY_TASKS)]:
                _tasks.pop(key, None)
                _task_events.pop(key, None)
                with _task_persist_lock:
                    _task_persist_times.pop(key, None)
    _persist_task(task_id, task)
    _trim_task_state_rows()
    return task_id


def update_task(tid: str, updates: dict[str, Any]) -> None:
    with _tasks_lock:
        task = _tasks.get(tid)
        if task is None:
            return
        task.update(updates)
        task["updated_at"] = _utc_now_iso()
        snapshot = dict(task)
        event = _task_events.get(tid)

    # Los cambios de estado y la finalización se guardan siempre. La consola
    # puede generar decenas de líneas por segundo: se agrupan para no escribir
    # en SQLite en cada una. Un fallo de escritura no interrumpe la operación.
    force_persist = bool(
        updates.get("completed")
        or "status" in updates
        or "cancellation_requested" in updates
    )
    now = time.monotonic()
    should_persist = force_persist
    with _task_persist_lock:
        # ``None`` significa "aún no persistido por update_task": la primera
        # actualización se escribe siempre. Comparar contra 0.0 dependía de que
        # time.monotonic() superase el intervalo, lo que fallaba en hosts con
        # menos de una hora de uptime.
        last = _task_persist_times.get(tid)
        if (
            last is None
            or TASK_PERSIST_INTERVAL_SECONDS <= 0
            or now - last >= TASK_PERSIST_INTERVAL_SECONDS
        ):
            should_persist = True
        if should_persist:
            _task_persist_times[tid] = now
    if should_persist:
        _persist_task(tid, snapshot)
    if event is not None:
        event.set()


def remove_task(tid: str) -> None:
    with _tasks_lock:
        _tasks.pop(tid, None)
        _task_events.pop(tid, None)
    with _task_persist_lock:
        _task_persist_times.pop(tid, None)
    _delete_persisted_task(tid)


def _claim_operation_id(operation_id: str) -> str | None:
    """Reserva un envío idempotente en SQLite.

    Devuelve ``None`` si la operación es nueva. Si ya existía, devuelve el
    ``task_id`` asociado o ``""`` mientras el primer envío aún se está creando.
    ``INSERT OR IGNORE`` hace la reserva atómica incluso con varios workers.
    """
    if not operation_id:
        return None

    now = datetime.now(UTC)
    cutoff = (now - timedelta(seconds=_OPERATION_ID_TTL)).isoformat(timespec="seconds")
    with get_connection(runtime.get_db_path()) as conn:
        conn.execute("DELETE FROM operation_ids WHERE created_at < ?", (cutoff,))
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO operation_ids
                (operation_id, created_at, task_id)
            VALUES (?, ?, '')
            """,
            (operation_id, now.isoformat(timespec="seconds")),
        )
        if cursor.rowcount == 1:
            # Acota la tabla sin tocar las reservas recientes.
            total = int(
                conn.execute("SELECT COUNT(*) FROM operation_ids").fetchone()[0]
            )
            if total > _MAX_OPERATION_IDS:
                conn.execute(
                    """
                    DELETE FROM operation_ids WHERE operation_id IN (
                        SELECT operation_id FROM operation_ids
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                    """,
                    (total - _MAX_OPERATION_IDS,),
                )
            return None
        row = conn.execute(
            "SELECT task_id FROM operation_ids WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    return row["task_id"] if row is not None else ""


def _complete_operation_id(operation_id: str, tid: str) -> None:
    """Asocia el task_id definitivo a un envío ya reservado."""
    if not operation_id:
        return
    with get_connection(runtime.get_db_path()) as conn:
        conn.execute(
            "UPDATE operation_ids SET task_id = ? WHERE operation_id = ?",
            (tid, operation_id),
        )


def _release_operation_id(operation_id: str) -> None:
    """Libera una reserva que no llegó a crear ninguna tarea.

    Se usa cuando la aprobación falta o la cola está llena, para que el
    operador pueda reintentar con el mismo ``operation_id`` sin esperar al TTL.
    """
    if not operation_id:
        return
    with get_connection(runtime.get_db_path()) as conn:
        conn.execute(
            "DELETE FROM operation_ids WHERE operation_id = ?",
            (operation_id,),
        )


def _task_version(task: dict[str, Any]) -> tuple:
    """Huella barata para evitar serializar el estado sin cambios.

    Se comparan campos que cambian en cada actualización relevante; así una
    conexión SSE ociosa no reenvía la tarea completa cada segundo.
    """
    console = task.get("console") or []
    log = task.get("log") or []
    return (
        task.get("updated_at"),
        task.get("status"),
        task.get("completed"),
        task.get("progress"),
        task.get("message"),
        len(console),
        len(log),
        console[-1] if console else "",
        log[-1] if log else "",
    )


def generate_progress_events(task_id: str):
    yield sse_retry(1500)
    start = time.time()
    max_wait = 90
    last_version: tuple | None = None

    while True:
        # Si la tarea la ejecuta otro proceso no existe evento local: se
        # refresca desde SQLite para que el SSE funcione con varios workers.
        with _tasks_lock:
            event = _task_events.get(task_id)
        task = _get_task(task_id, refresh=event is None)

        if task is None:
            yield sse_event(
                "completed",
                json.dumps({"status": "not_found", "completed": True}),
            )
            return

        version = _task_version(task)
        if version != last_version:
            yield sse_event("progress", json.dumps(task, ensure_ascii=False))
            last_version = version

        if task.get("status") in TERMINAL_STATES or task.get("completed"):
            yield sse_event("completed", json.dumps(task, ensure_ascii=False))
            return

        if time.time() - start >= max_wait:
            yield sse_event(
                "timeout",
                json.dumps({**task, "timeout": True}, ensure_ascii=False),
            )
            return

        # Espera a que update_task señale el evento; si no hay evento local,
        # sondea SQLite una vez por segundo (coste acotado por conexión).
        if event is not None:
            event.wait(1.0)
            event.clear()
        else:
            time.sleep(1)
