# services/job_queue.py
# Cola de trabajos acotada con cancelación cooperativa y apagado ordenado.
#
# Sustituye el patrón de «un hilo demonio por operación» sin introducir
# dependencias externas (Redis/Celery). El estado de cada tarea sigue viviendo
# en el proceso; la cola solo limita cuántas se ejecutan a la vez y cuántas
# pueden esperar.
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from services.metrics import percentile

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Se lanza cuando una operación detecta que fue cancelada."""


class JobQueueFull(RuntimeError):
    """La cola no admite más trabajos en este momento."""


class _Job:
    __slots__ = ("args", "cancel_event", "func", "kwargs", "task_id")

    def __init__(self, task_id: str, func: Callable[..., Any], args, kwargs):
        self.task_id = task_id
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.cancel_event = threading.Event()


class JobQueue:
    """Cola FIFO servida por un número fijo de hilos daemon."""

    def __init__(
        self,
        max_workers: int = 2,
        max_queue: int = 50,
        shutdown_grace: int = 10,
    ):
        self.max_workers = max(1, int(max_workers))
        self.max_queue = max(1, int(max_queue))
        self.shutdown_grace = max(0, int(shutdown_grace))
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=self.max_queue)
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        # Orden FIFO de los trabajos que aún esperan (los que ya están en
        # ejecución salen de esta lista). Permite mostrar la posición en la
        # cola sin inspeccionar las estructuras internas de ``queue.Queue``.
        self._pending_order: list[str] = []
        # Tiempo de espera de los trabajos que ya arrancaron, para métricas.
        self._enqueued_at: dict[str, float] = {}
        self._wait_samples: deque[float] = deque(maxlen=500)
        self._threads = []
        self._stopping = False
        for index in range(self.max_workers):
            thread = threading.Thread(
                target=self._worker,
                name=f"ansible-control-center-job-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    # ------------------------------------------------------------------
    # Envío / cancelación
    # ------------------------------------------------------------------
    def submit(
        self,
        task_id: str,
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Encola un trabajo. Lanza ``JobQueueFull`` si no hay hueco."""
        if self._stopping:
            raise JobQueueFull("El servicio está apagándose.")
        job = _Job(task_id, func, args, kwargs)
        # El trabajo recibe su propio evento de cancelación. Las funciones deben
        # aceptar ``cancel_event`` (o ``**kwargs``) para poder cancelarse de
        # forma cooperativa; incluso un trabajo cancelado antes de arrancar se
        # ejecuta para que cierre su estado como cancelado.
        job.kwargs.setdefault("cancel_event", job.cancel_event)
        with self._lock:
            if len(self._jobs) >= self.max_queue + self.max_workers:
                raise JobQueueFull("Hay demasiados trabajos en curso.")
            self._jobs[task_id] = job
            self._pending_order.append(task_id)
            self._enqueued_at[task_id] = time.monotonic()
        try:
            self._queue.put_nowait(job)
        except queue.Full as exc:
            with self._lock:
                self._jobs.pop(task_id, None)
                self._enqueued_at.pop(task_id, None)
                try:
                    self._pending_order.remove(task_id)
                except ValueError:
                    pass
            raise JobQueueFull("La cola de trabajos está llena.") from exc

    def cancel(self, task_id: str) -> bool:
        """Solicita cancelar un trabajo pendiente o en ejecución."""
        with self._lock:
            job = self._jobs.get(task_id)
        if job is None:
            return False
        job.cancel_event.set()
        return True

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(task_id)
        return bool(job and job.cancel_event.is_set())

    def cancel_event(self, task_id: str) -> threading.Event | None:
        with self._lock:
            job = self._jobs.get(task_id)
        return job.cancel_event if job else None

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            with self._lock:
                try:
                    self._pending_order.remove(item.task_id)
                except ValueError:
                    pass
                enqueued = self._enqueued_at.pop(item.task_id, None)
                if enqueued is not None:
                    self._wait_samples.append(max(0.0, time.monotonic() - enqueued))
            try:
                item.func(*item.args, **item.kwargs)
            except JobCancelled:
                logger.info("Trabajo %s cancelado", item.task_id)
            except Exception:  # pragma: no cover - salvaguarda
                logger.exception("Error no controlado en el trabajo %s", item.task_id)
            finally:
                with self._lock:
                    self._jobs.pop(item.task_id, None)
                self._queue.task_done()

    def shutdown(self, wait: bool = True) -> None:
        """Deja de aceptar trabajos y avisa a los pendientes."""
        if self._stopping:
            return
        self._stopping = True

        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel_event.set()

        for _ in self._threads:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                break

        if wait:
            for thread in self._threads:
                thread.join(timeout=self.shutdown_grace)

    def position(self, task_id: str) -> int | None:
        """Posición 1-based en la cola, o ``None`` si ya no espera."""
        with self._lock:
            try:
                return self._pending_order.index(task_id) + 1
            except ValueError:
                return None

    def wait_stats(self) -> dict[str, float]:
        """Espera en cola de los trabajos ya arrancados (segundos)."""
        with self._lock:
            samples = list(self._wait_samples)
        if not samples:
            return {
                "count": 0,
                "avg_seconds": 0.0,
                "p50_seconds": 0.0,
                "p95_seconds": 0.0,
                "max_seconds": 0.0,
            }
        return {
            "count": len(samples),
            "avg_seconds": round(sum(samples) / len(samples), 2),
            "p50_seconds": percentile(samples, 50),
            "p95_seconds": percentile(samples, 95),
            "max_seconds": round(max(samples), 2),
        }

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def running(self) -> int:
        with self._lock:
            return max(0, len(self._jobs) - len(self._pending_order))
