"""Estado y configuracion de proceso: rutas, base de datos,
modo simulacion, vault y cola de trabajos.
"""

from __future__ import annotations

import logging
import threading

from flask import Flask

import config
import services.ansible_runner as ansible_runner_module
import services.password_tester as password_tester_module
from config import (
    DRY_RUN,
    GLOBAL_HOST_CONCURRENCY,
    JOB_MAX_WORKERS,
    JOB_QUEUE_SIZE,
    JOB_SHUTDOWN_GRACE_SECONDS,
    REQUEST_METRICS_WINDOW,
    resolve_db_path,
    resolve_vault_file,
)
from models.db import (
    get_setting,
    init_schema,
    set_setting,
)
from services.job_queue import JobQueue
from services.metrics import RequestMetrics

# Referencia a la aplicación Flask, fijada por ``acc.bootstrap`` para
# propagar ajustes de proceso (modo simulación) a ``app.config``.
app: Flask | None = None


def bind_app(flask_app: Flask) -> None:
    global app
    app = flask_app


logger = logging.getLogger("acc")

# Ajuste de interfaz (feature flag) expuesto como estado de proceso: así una
# sola asignación controla las rutas y el contexto de las plantillas.
UI_CONFIG_PANEL = config.UI_CONFIG_PANEL


_db_path_cache: str | None = None


request_metrics = RequestMetrics(window=REQUEST_METRICS_WINDOW)


_job_queue: JobQueue | None = None


def get_job_queue() -> JobQueue:
    global _job_queue
    if _job_queue is None:
        _job_queue = JobQueue(
            max_workers=JOB_MAX_WORKERS,
            max_queue=JOB_QUEUE_SIZE,
            shutdown_grace=JOB_SHUTDOWN_GRACE_SECONDS,
        )
    return _job_queue


_host_semaphore: threading.BoundedSemaphore | None = None


_host_semaphore_lock = threading.Lock()


def _get_host_semaphore() -> threading.BoundedSemaphore:
    global _host_semaphore
    if _host_semaphore is None:
        with _host_semaphore_lock:
            if _host_semaphore is None:
                _host_semaphore = threading.BoundedSemaphore(GLOBAL_HOST_CONCURRENCY)
    return _host_semaphore


def get_db_path() -> str:
    """Ruta de SQLite resuelta una sola vez por proceso."""
    global _db_path_cache
    if _db_path_cache is None:
        _db_path_cache = str(resolve_db_path())
    return _db_path_cache


DRY_RUN_SETTING_KEY = "dry_run"


LDAP_ACCESS_SETTING_KEY = "ldap_access_enabled"


_dry_run_lock = threading.Lock()


def is_dry_run() -> bool:
    """Estado actual del modo simulación en este proceso."""
    return bool(DRY_RUN)


def apply_dry_run(enabled: bool) -> None:
    """Propaga el modo simulación a todos los módulos que lo consultan.

    ``app``, ``services.ansible_runner`` y ``services.password_tester``
    importan la constante en su propio espacio de nombres; hay que actualizar
    las tres copias. Gunicorn se ejecuta con un único worker y varios hilos,
    así que el estado compartido en proceso es consistente.
    """
    global DRY_RUN
    value = bool(enabled)
    with _dry_run_lock:
        DRY_RUN = value
        ansible_runner_module.DRY_RUN = value
        password_tester_module.DRY_RUN = value
        if app is not None:
            app.config["DRY_RUN"] = value


def load_dry_run_setting() -> bool:
    """Recupera el modo simulación persistido o mantiene el valor de entorno."""
    stored = get_setting(DRY_RUN_SETTING_KEY, db_path=get_db_path())
    if stored is None:
        return DRY_RUN
    return str(stored).strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def persist_dry_run(enabled: bool) -> None:
    set_setting(
        DRY_RUN_SETTING_KEY,
        "1" if enabled else "0",
        db_path=get_db_path(),
    )


def load_ldap_access_setting() -> bool:
    """Estado del interruptor de acceso LDAPS (persistido en la BD).

    Antes del primer cambio desde la interfaz manda ``ACC_LDAP_ENABLED``
    (el valor de entorno); después de cualquier conmutación decide la BD,
    sin reinicios del contenedor.
    """
    stored = get_setting(LDAP_ACCESS_SETTING_KEY, db_path=get_db_path())
    if stored is None:
        return bool(config.LDAP_ENABLED)
    return str(stored).strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def persist_ldap_access(enabled: bool) -> None:
    set_setting(
        LDAP_ACCESS_SETTING_KEY,
        "1" if enabled else "0",
        db_path=get_db_path(),
    )


def init_db() -> None:
    """Crea o migra el esquema completo de SQLite (idempotente)."""
    init_schema(get_db_path())


def get_vault_file() -> str:
    """Ruta efectiva del vault (producción o fallback de desarrollo)."""
    return str(resolve_vault_file())
