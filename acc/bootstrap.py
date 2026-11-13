"""Fabrica de la aplicacion Flask y configuracion de arranque.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
from datetime import timedelta

from flask import Flask

import config
from acc import runtime
from acc.cli import register_cli
from acc.extensions import login_manager
from acc.runtime import (
    apply_dry_run,
    init_db,
    load_dry_run_setting,
)
from acc.scheduling import launch_scheduled
from acc.task_state import (
    _purge_retention,
    _recover_task_state,
)
from acc.version import BUILD_COMMIT, __version__
from config import (
    CSRF_ENABLED,
    CSRF_TIME_LIMIT_SECONDS,
    DEBUG,
    IS_PRODUCTION,
    MAX_CONTENT_LENGTH,
    PERMANENT_SESSION_LIFETIME_SECONDS,
    PROCESS_ROLE,
    RECOVERY_STALE_MINUTES,
    REMEMBER_COOKIE_DURATION_SECONDS,
    REMEMBER_COOKIE_HTTPONLY,
    REMEMBER_COOKIE_SAMESITE,
    REMEMBER_COOKIE_SECURE,
    RETENTION_INTERVAL_HOURS,
    SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
    TRUST_PROXY,
    ensure_directories,
    get_or_create_secret_key,
    setup_logging,
    validate_security_config,
)
from models.batch import ExecutionBatch
from services import scheduler

logger = logging.getLogger("acc")


def _shutdown_job_queue() -> None:
    """Espera brevemente a los trabajos en curso al terminar el proceso."""
    if runtime._job_queue is not None:
        runtime._job_queue.shutdown(wait=True)


def _is_cli_invocation() -> bool:
    """Detecta si el proceso es el CLI de Flask (no el servidor web)."""
    argv = [str(part).lower() for part in sys.argv]
    if not argv:
        return False
    programa = os.path.basename(argv[0])
    if programa in {"flask", "flask.exe"}:
        return True
    return len(argv) >= 3 and argv[1] == "-m" and argv[2] == "flask"


def configure_app(flask_app: Flask) -> Flask:
    # Falla pronto si la configuración de producción no es segura (allowlist,
    # clave de sesión y verificación de claves SSH).
    validate_security_config()
    flask_app.config.update(
        SECRET_KEY=get_or_create_secret_key(),
        DEBUG=DEBUG,
        SESSION_COOKIE_HTTPONLY=SESSION_COOKIE_HTTPONLY,
        SESSION_COOKIE_SAMESITE=SESSION_COOKIE_SAMESITE,
        SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
        REMEMBER_COOKIE_HTTPONLY=REMEMBER_COOKIE_HTTPONLY,
        REMEMBER_COOKIE_SAMESITE=REMEMBER_COOKIE_SAMESITE,
        REMEMBER_COOKIE_SECURE=REMEMBER_COOKIE_SECURE,
        REMEMBER_COOKIE_DURATION=timedelta(seconds=REMEMBER_COOKIE_DURATION_SECONDS),
        PERMANENT_SESSION_LIFETIME=timedelta(
            seconds=PERMANENT_SESSION_LIFETIME_SECONDS
        ),
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        CSRF_ENABLED=CSRF_ENABLED,
        CSRF_TIME_LIMIT=CSRF_TIME_LIMIT_SECONDS,
        APP_VERSION=__version__,
        APP_BUILD_COMMIT=BUILD_COMMIT,
    )

    if TRUST_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix

        # Flask tipa ``wsgi_app`` como método; ProxyFix es el envoltorio oficial.
        flask_app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            flask_app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0
        )

    setup_logging()
    logger.info(
        "Ansible Control Center %s%s",
        __version__,
        f" (commit {BUILD_COMMIT})" if BUILD_COMMIT else "",
    )
    try:
        ensure_directories()
    except RuntimeError as exc:
        if IS_PRODUCTION:
            # En producción no se degrada a instance/: la base y los secretos
            # deben estar en las rutas respaldadas.
            raise
        logger.warning(
            "No se pudieron crear los directorios de producción (%s); "
            "se usará el directorio local instance/ para desarrollo.",
            exc,
        )

    login_manager.init_app(flask_app)
    init_db()
    try:
        # El modo simulación elegido en la interfaz sobrevive a los reinicios.
        apply_dry_run(load_dry_run_setting())
    except Exception:  # pragma: no cover - no impedir el arranque
        logger.exception("No se pudo cargar el modo simulación persistido")

    cli_mode = _is_cli_invocation()
    if not cli_mode and PROCESS_ROLE != "cli":
        try:
            _purge_retention()
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudo aplicar la retención al arrancar")
        try:
            # Solo tareas estancadas: un worker no debe cerrar operaciones que
            # otro worker sigue actualizando.
            _recover_task_state(stale_minutes=RECOVERY_STALE_MINUTES)
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudieron recuperar las tareas persistidas")
        try:
            reconciled = ExecutionBatch.reconcile_orphans(
                db_path=runtime.get_db_path(),
                stale_minutes=RECOVERY_STALE_MINUTES,
            )
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudieron reconciliar lotes huérfanos")
        else:
            if reconciled:
                logger.warning(
                    "Se marcaron %s lote(s) huérfano(s) como error al arrancar",
                    reconciled,
                )
    register_cli(flask_app)

    # Programador de ejecuciones desatendidas. Un hilo daemon revisa la tabla
    # ``schedules`` y lanza los lotes por la misma cola que la operación
    # manual. El lock de instancia evita que varios workers programen a la vez
    # y ``ACC_ROLE=web`` permite delegarlo a un proceso dedicado.
    if not cli_mode:
        try:
            scheduler.start(
                launch_scheduled,
                lambda: str(runtime.get_db_path()),
                maintenance=_purge_retention,
                maintenance_interval=RETENTION_INTERVAL_HOURS * 3600,
            )
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudo iniciar el programador de ejecuciones")

    # Apagado ordenado. El ``atexit`` se registra una sola vez por proceso; si
    # la cola nunca se creó, el callback no hace nada.
    atexit.register(_shutdown_job_queue)
    return flask_app


# Modulos de rutas en el mismo orden en que se declaraban en ``app.py``: el
# mapa de URLs y el orden de los hooks se mantienen estables.
_WEB_MODULES = (
    "acc.web.hooks",
    "acc.web.auth",
    "acc.web.tasks",
    "acc.web.inventory",
    "acc.web.schedules",
    "acc.web.approvals",
    "acc.web.passwords",
    "acc.web.admin",
    "acc.web.errors",
)


def _register_web(flask_app: Flask) -> None:
    """Aplica rutas, hooks y manipuladores de error de ``acc.web``."""
    import importlib

    for name in _WEB_MODULES:
        module = importlib.import_module(name)
        module.register(flask_app)


def create_app() -> Flask:
    """Crea la aplicación Flask completa (rutas + configuración de arranque).

    ``root_path`` apunta a la raíz del proyecto para que ``templates/`` y
    ``static/`` se resuelvan igual que cuando la aplicación vivía en
    ``app.py``.
    """
    flask_app = Flask("app", root_path=str(config.BASE_DIR))
    runtime.bind_app(flask_app)
    _register_web(flask_app)
    return configure_app(flask_app)
