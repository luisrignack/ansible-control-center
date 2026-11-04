# models/execution.py
# Modelo SQLite para el historial de ejecuciones de configuración de hora.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from config import resolve_db_path
from models.db import get_connection, init_schema


def _default_db_path() -> str:
    """Ruta de SQLite resuelta en tiempo de ejecución (producción o instance/)."""
    return str(resolve_db_path())


class ExecutionRecord:
    """Registro de una ejecución de configuración de hora."""

    def __init__(
        self,
        id: int,
        timestamp: datetime,
        action: str,
        value: str,
        host: str,
        result: str,
        password_hash: str,
        password_origin: str,
    ):
        self.id = id
        self.timestamp = timestamp
        self.action = action
        self.value = value
        self.host = host
        self.result = result
        self.password_hash = password_hash
        self.password_origin = password_origin

    def to_dict(self) -> Dict[str, Any]:
        """Convierte el registro a diccionario (sin la contraseña completa)."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "action": self.action,
            "value": self.value,
            "host": self.host,
            "result": self.result,
            "password_hash": self.password_hash,
            "password_origin": self.password_origin,
        }

    @staticmethod
    def init_db(db_path: str = None) -> None:
        """Crea o migra el esquema completo sin borrar datos existentes."""
        if db_path is None:
            db_path = _default_db_path()
        init_schema(db_path)

    @staticmethod
    def record_execution(
        action: str,
        value: str,
        host: str,
        result: str,
        password_hash: str,
        password_origin: str,
        db_path: str = None,
    ) -> "ExecutionRecord":
        """Registra una nueva ejecución en la base de datos.

        Un registro equivale a un host. Las estadísticas se calculan bajo
        demanda desde la propia tabla ``executions`` (ver ``get_stats``), por
        lo que no se mantiene una tabla de resúmenes que pueda desincronizarse.
        """
        if db_path is None:
            db_path = _default_db_path()

        timestamp = datetime.now(timezone.utc).isoformat()

        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO executions
                    (timestamp, action, value, host, result, password_hash, password_origin)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    action,
                    value,
                    host,
                    result,
                    password_hash,
                    password_origin,
                ),
            )
            execution_id = cursor.lastrowid

        return ExecutionRecord(
            id=execution_id,
            timestamp=datetime.fromisoformat(timestamp),
            action=action,
            value=value,
            host=host,
            result=result,
            password_hash=password_hash,
            password_origin=password_origin,
        )

    @staticmethod
    def get_all_executions(
        limit: int = 50, offset: int = 0, db_path: str = None
    ) -> List["ExecutionRecord"]:
        """Obtiene las ejecuciones ordenadas por fecha (más recientes primero)."""
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, action, value, host, result,
                       password_hash, password_origin
                FROM executions
                ORDER BY timestamp DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

        return [_row_to_record(row) for row in rows]

    @staticmethod
    def get_recent_executions(
        hours: int = 24, db_path: str = None
    ) -> List["ExecutionRecord"]:
        """Obtiene ejecuciones de las últimas N horas."""
        if db_path is None:
            db_path = _default_db_path()

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, action, value, host, result,
                       password_hash, password_origin
                FROM executions
                WHERE timestamp >= ?
                ORDER BY timestamp DESC, id DESC
                """,
                (since,),
            ).fetchall()

        return [_row_to_record(row) for row in rows]

    @staticmethod
    def get_stats(db_path: str = None) -> Dict[str, Any]:
        """Agrega el historial: total, éxitos, fallos y tasa de éxito.

        Se calcula directamente desde ``executions`` para que nunca quede
        desincronizado con los registros reales.
        """
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END), 0)
                        AS success,
                    COALESCE(SUM(CASE WHEN result = 'failure' THEN 1 ELSE 0 END), 0)
                        AS failure
                FROM executions
                """
            ).fetchone()

        total = int(row["total"])
        success = int(row["success"])
        failure = int(row["failure"])
        return {
            "total": total,
            "success": success,
            "failure": failure,
            "other": total - success - failure,
            "success_rate": round(success / total, 2) if total else 0.0,
        }

    @staticmethod
    def purge_old(days: int, db_path: str = None) -> int:
        """Elimina registros del historial heredado anteriores a ``days``."""
        if db_path is None:
            db_path = _default_db_path()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        ).isoformat()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM executions WHERE timestamp < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)


def _row_to_record(row) -> ExecutionRecord:
    """Convierte una fila SQLite en ``ExecutionRecord``."""
    return ExecutionRecord(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"])
        if row["timestamp"]
        else datetime.min,
        action=row["action"],
        value=row["value"],
        host=row["host"],
        result=row["result"],
        password_hash=row["password_hash"],
        password_origin=row["password_origin"],
    )


class ExecutionSummary:
    """Resumen agrupado de una ejecución.

    Se conserva por compatibilidad. El historial ya no escribe en la tabla
    heredada ``execution_summaries``: ``ExecutionRecord.get_stats()`` agrega
    los datos reales bajo demanda.
    """

    def __init__(
        self,
        execution_id: int,
        host_count: int,
        success_count: int,
        failure_count: int,
    ):
        self.execution_id = execution_id
        self.host_count = host_count
        self.success_count = success_count
        self.failure_count = failure_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "host_count": self.host_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": (
                round(self.success_count / self.host_count, 2)
                if self.host_count > 0
                else 0
            ),
        }
