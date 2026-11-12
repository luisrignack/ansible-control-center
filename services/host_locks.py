# services/host_locks.py
# Reservas por host para evitar playbooks simultáneos sobre el mismo equipo.
#
# Dos lotes distintos podían apuntar a la misma IP y aplicarse a la vez: el
# último en escribir ganaba, ambos podían registrar éxito y, en acciones como
# contraseña o firewall, el estado final no coincidía con ninguna auditoría.
# La reserva vive en SQLite para sobrevivir a reinicios y compartirse entre
# procesos; caduca sola para que una caída no bloquee el host indefinidamente.
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import HOST_LOCK_TTL_SECONDS, resolve_db_path
from models.db import get_connection

logger = logging.getLogger(__name__)


def _db_path(db_path: Optional[str]) -> str:
    return db_path or str(resolve_db_path())


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def acquire_host_lock(
    host: str,
    batch_id: str,
    owner: str = "",
    db_path: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
) -> bool:
    """Intenta reservar ``host`` para ``batch_id``.

    Devuelve ``True`` si la reserva es nueva o si la anterior había caducado.
    ``INSERT OR IGNORE`` y el ``UPDATE`` condicionado por ``expires_at`` hacen
    la decisión atómica: con varios hilos o procesos solo uno gana.
    """
    ttl = int(ttl_seconds if ttl_seconds is not None else HOST_LOCK_TTL_SECONDS)
    now = datetime.now(timezone.utc)
    now_iso = _iso(now)
    expires_iso = _iso(now + timedelta(seconds=ttl))

    with get_connection(_db_path(db_path)) as conn:
        # Limpieza oportunista: las reservas caducadas no bloquean y no deben
        # acumularse entre reinicios.
        conn.execute("DELETE FROM host_locks WHERE expires_at < ?", (now_iso,))
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO host_locks
                (host, batch_id, owner, acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (host, batch_id, owner, now_iso, expires_iso),
        )
        if cursor.rowcount == 1:
            return True
        cursor = conn.execute(
            """
            UPDATE host_locks
            SET batch_id = ?, owner = ?, acquired_at = ?, expires_at = ?
            WHERE host = ? AND expires_at < ?
            """,
            (batch_id, owner, now_iso, expires_iso, host, now_iso),
        )
        return cursor.rowcount == 1


def refresh_host_lock(
    host: str,
    batch_id: str,
    db_path: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
) -> bool:
    """Renueva la reserva mientras el lote sigue trabajando en el host.

    Se llama antes de cada intento de credencial: un host con varias
    contraseñas candidatas puede tardar más que el TTL inicial.
    """
    ttl = int(ttl_seconds if ttl_seconds is not None else HOST_LOCK_TTL_SECONDS)
    expires_iso = _iso(datetime.now(timezone.utc) + timedelta(seconds=ttl))
    with get_connection(_db_path(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE host_locks SET expires_at = ? WHERE host = ? AND batch_id = ?",
            (expires_iso, host, batch_id),
        )
        return cursor.rowcount == 1


def release_host_lock(
    host: str,
    batch_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Libera la reserva del host.

    Si se indica ``batch_id``, solo borra la reserva propia: una reserva
    caducada que otro lote ya tomó no debe liberarse por error.
    """
    with get_connection(_db_path(db_path)) as conn:
        if batch_id is None:
            cursor = conn.execute("DELETE FROM host_locks WHERE host = ?", (host,))
        else:
            cursor = conn.execute(
                "DELETE FROM host_locks WHERE host = ? AND batch_id = ?",
                (host, batch_id),
            )
        return cursor.rowcount == 1


def release_batch_locks(batch_id: str, db_path: Optional[str] = None) -> int:
    """Libera todas las reservas de un lote (cierre o error inesperado)."""
    with get_connection(_db_path(db_path)) as conn:
        cursor = conn.execute(
            "DELETE FROM host_locks WHERE batch_id = ?", (batch_id,)
        )
        return int(cursor.rowcount or 0)


def is_host_locked(host: str, db_path: Optional[str] = None) -> bool:
    """Indica si hay una reserva vigente para el host (diagnóstico y avisos)."""
    now_iso = _iso(datetime.now(timezone.utc))
    with get_connection(_db_path(db_path)) as conn:
        row = conn.execute(
            "SELECT 1 FROM host_locks WHERE host = ? AND expires_at >= ?",
            (host, now_iso),
        ).fetchone()
    return row is not None
