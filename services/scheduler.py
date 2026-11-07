"""Programador de ejecuciones (ventanas de mantenimiento).

Un hilo de fondo consulta cada pocos segundos la tabla ``schedules`` y, para
las programaciones vencidas, invoca un callback que el propio ``app.py``
instala: ese callback envía el lote a la misma cola de trabajos que la
ejecución manual (con su presupuesto de tiempo, best-effort y persistencia).

Las credenciales: la contraseña maestra del vault necesaria para operar
desatendidamente se recibe **cifrada** con Fernet; la clave vive en
``SECRETS_DIR/scheduler_password.key`` (0600). Nunca se guarda en claro en la
base de datos y sin la clave del archivo el disparo falla de forma segura.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:  # POSIX (Linux/macOS); en otros sistemas el scheduler sigue sin lock.
    import fcntl
except ImportError:  # pragma: no cover - plataformas sin fcntl
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("acc")

SCAN_INTERVAL = 15.0
# Espera antes de reintentar una programación tras un error transitorio
# (cola llena, fallo inesperado del lanzador). No se desactiva la agenda.
SCHEDULE_RETRY_DELAY_SECONDS = 300
# Tiempo que una fila queda reservada por un proceso antes de que otro pueda
# reclamarla si este muere.
SCHEDULE_CLAIM_LEASE_SECONDS = 300

_thread: Optional[threading.Thread] = None
_no_stop = threading.Event()
_lock_fd: Optional[int] = None
_lock_path: Optional[Path] = None


def password_key_file() -> Path:
    from config import SECRETS_DIR

    return Path(SECRETS_DIR) / "scheduler_password.key"


def _fernet():
    """Devuelve el cifrador Fernet, creando la clave con permisos 0600.

    La clave se crea de forma atómica con ``O_EXCL`` y ``0600``; si otro
    proceso se adelanta, se reutiliza la clave ya escrita. Nunca se deja una
    ventana en la que el fichero sea legible por otros usuarios.
    """
    from cryptography.fernet import Fernet

    key_file = password_key_file()
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(
                key_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            # Otro proceso/worker creó la clave entre la comprobación y el
            # open: se usa la existente.
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
    try:
        # Normaliza permisos también si la clave venía de una versión previa.
        key_file.chmod(0o600)
    except OSError:  # pragma: no cover - sistemas sin permisos POSIX
        pass
    return Fernet(key_file.read_bytes())


def encrypt_secret(secret: str) -> str:
    """Cifra una cadena para guardarla en ``schedules.params``."""
    if not secret:
        return ""
    return _fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    """Descifra lo generado por ``encrypt_secret``; cadena vacía si falla."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        logger.exception("No se pudo descifrar la credencial programada")
        return ""


def scan_once(
    db_path: str,
    launch: Callable[[Any, Dict[str, Any]], bool],
    max_launch: int = 10,
) -> int:
    """Revisa programaciones vencidas y dispara las que correspondan.

    ``launch(schedule, params)`` decide si realmente arranca el lote (ya se
    encarga de llamar a ``Schedule.mark_run``). Devuelve cuántas se lanzaron.

    Una programación cuya tarea no está activa en el catálogo se desactiva con
    un error registrado en lugar de quedarse reintentando para siempre.
    """
    from models.ops import Schedule, _parse_utc
    from services.task_catalog import get_task

    launched = 0
    for schedule in Schedule.due_rows(db_path):
        if launched >= max_launch:
            break
        # Solo un proceso puede reclamar la fila. El claim caduca, así que un
        # proceso caído no bloquea la programación para siempre.
        if not Schedule.claim(
            schedule["id"],
            owner=str(os.getpid()),
            lease_seconds=SCHEDULE_CLAIM_LEASE_SECONDS,
            db_path=db_path,
        ):
            continue
        try:
            params = dict(json.loads(schedule["params"] or "{}"))
        except Exception:
            params = {}

        task = get_task(schedule["task_id"])
        if task is None or task.get("status") != "active":
            Schedule.mark_run(
                schedule["id"],
                error="La tarea ya no está activa en el catálogo.",
                disable=True,
                db_path=db_path,
            )
            continue

        try:
            if launch(schedule, params):
                launched += 1
        except Exception as exc:  # pragma: no cover - salvaguarda
            logger.exception("Programación %s falló al lanzar", schedule["id"])
            # Un fallo del lanzador puede ser transitorio (por ejemplo, la
            # cola de trabajos). Se registra y se reintenta con espera en
            # lugar de apagar la programación de forma permanente.
            Schedule.mark_run(
                schedule["id"],
                error=str(exc),
                retry_delay_seconds=SCHEDULE_RETRY_DELAY_SECONDS,
                db_path=db_path,
            )
    return launched


def _acquire_instance_lock() -> bool:
    """Toma un lock exclusivo de proceso para el scheduler.

    Con varios workers de Gunicorn todos importan ``app.py`` y podrían
    arrancar su propio hilo. Solo el primero en tomar el lock programa
    ejecuciones; los demás lo dejan en manos del líder. El lock se libera al
    terminar el proceso.
    """
    global _lock_fd, _lock_path
    if _lock_fd is not None:
        return True
    from config import resolve_db_path

    if fcntl is None:
        logger.warning(
            "fcntl no está disponible; no se puede garantizar un único "
            "programador. Ejecuta el scheduler en un proceso dedicado."
        )
        return True

    # El lock vive junto a la base de datos: es la misma ruta para todos los
    # workers, exista o no el directorio de secretos de producción.
    try:
        path = Path(resolve_db_path()).parent / "scheduler.lock"
    except RuntimeError as exc:
        logger.warning("No se puede resolver la ruta del lock: %s", exc)
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        logger.warning(
            "No se pudo abrir el lock del scheduler (%s): %s. No se inicia.",
            path,
            exc,
        )
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        logger.info(
            "Otro proceso ya ejecuta el scheduler; este proceso no lo inicia."
        )
        return False
    try:
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
    except OSError:  # pragma: no cover - metadato best-effort
        pass
    _lock_fd = fd
    _lock_path = path
    return True


def release_instance_lock() -> None:
    """Libera el lock del scheduler (útil en apagados ordenados y pruebas)."""
    global _lock_fd, _lock_path
    if _lock_fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover
        pass
    try:
        os.close(_lock_fd)
    except OSError:  # pragma: no cover
        pass
    _lock_fd = None
    _lock_path = None


def start(
    launch: Callable[[Any, Dict[str, Any]], bool],
    db_path_provider: Callable[[], str],
    interval: float = SCAN_INTERVAL,
) -> Optional[threading.Thread]:
    """Arranca el hilo de revisión si corresponde y no está ya corriendo.

    Respeta ``ACC_SCHEDULER_ENABLED``, ``ACC_ROLE`` y el lock de instancia.
    Devuelve el hilo, o ``None`` si este proceso no debe programar.
    """
    from config import PROCESS_ROLE, SCHEDULER_ENABLED

    if not SCHEDULER_ENABLED:
        logger.info("Scheduler desactivado por ACC_SCHEDULER_ENABLED=0.")
        return None
    if PROCESS_ROLE in {"web", "cli"}:
        logger.info(
            "Scheduler no iniciado en este proceso (ACC_ROLE=%s).",
            PROCESS_ROLE,
        )
        return None
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    if not _acquire_instance_lock():
        return None
    _no_stop.clear()

    def _loop() -> None:
        while not _no_stop.is_set():
            try:
                scan_once(db_path_provider(), launch)
            except Exception:  # pragma: no cover - salvaguarda
                logger.exception("El programador falló una revisión")
            _no_stop.wait(interval)

    _thread = threading.Thread(target=_loop, name="acc-schedules", daemon=True)
    _thread.start()
    return _thread


def stop(timeout: float = 5.0) -> None:
    """Detiene el hilo del scheduler y libera el lock de instancia."""
    global _thread
    _no_stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    _thread = None
    release_instance_lock()
