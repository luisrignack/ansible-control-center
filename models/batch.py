# models/batch.py
# Modelo SQLite para lotes de ejecución y resultados por host (fase P4).
#
# Un lote representa una operación completa sobre un grupo de equipos. Cada
# host del lote guarda su resultado, duración, estado previo y estado posterior
# para poder auditar qué ocurrió realmente en cada equipo.
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from config import resolve_db_path
from models.db import get_connection, init_schema

BATCH_STATUSES = {
    "running",
    "success",
    "partial",
    "failure",
    "preview",
    "error",
    "incomplete",
    "cancelled",
}
HOST_RESULTS = {"success", "failure", "skipped", "error", "preview"}

# Etiquetas legibles para la interfaz. Los estados internos nunca se muestran
# tal cual al operador.
STATUS_LABELS = {
    "running": "En curso",
    "success": "Correcto",
    "partial": "Parcial",
    "failure": "Fallido",
    "preview": "Vista previa",
    "error": "Error",
    "incomplete": "Incompleto",
    "cancelled": "Cancelado",
    "pending": "Pendiente",
}
HOST_RESULT_LABELS = {
    "success": "Correcto",
    "failure": "Fallido",
    "skipped": "Omitido",
    "error": "Error",
    "preview": "Vista previa",
}

_RESULT_COUNTERS = {
    "success": "success_count",
    "failure": "failure_count",
    "skipped": "skipped_count",
    "error": "error_count",
    "preview": "preview_count",
}


def _default_db_path() -> str:
    """Ruta de SQLite resuelta en tiempo de ejecución (producción o instance/)."""
    return str(resolve_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _dumps_state(value: Any) -> str:
    """Serializa el estado de un host; ``None`` se guarda como cadena vacía."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _loads_state(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class HostResult:
    """Resultado de un host dentro de un lote."""

    def __init__(
        self,
        id: int,
        batch_id: str,
        host: str,
        result: str,
        error: str = "",
        duration_ms: int = 0,
        before_state: Any = None,
        after_state: Any = None,
        password_hash: str = "",
        password_origin: str = "",
        created_at: Optional[str] = None,
    ):
        self.id = id
        self.batch_id = batch_id
        self.host = host
        self.result = result
        self.error = error
        self.duration_ms = int(duration_ms or 0)
        self.before_state = before_state
        self.after_state = after_state
        self.password_hash = password_hash
        self.password_origin = password_origin
        self.created_at = _parse_datetime(created_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "host": self.host,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "password_hash": self.password_hash,
            "password_origin": self.password_origin,
            "created_at": self.created_at.isoformat()
            if self.created_at
            else None,
        }


class ExecutionBatch:
    """Lote de ejecución con contadores agregados por resultado."""

    def __init__(
        self,
        id: int,
        batch_id: str,
        created_at: Optional[datetime],
        finished_at: Optional[datetime],
        operator: str,
        action: str,
        value: str,
        target: str,
        time_mode: str,
        ntp_policy: str,
        status: str,
        total_hosts: int = 0,
        success_count: int = 0,
        failure_count: int = 0,
        skipped_count: int = 0,
        error_count: int = 0,
        preview_count: int = 0,
        dry_run: bool = False,
        admin_user: str = "",
        password_origin: str = "",
        ntp_servers: Optional[List[str]] = None,
        sync_rtc: bool = False,
        force: bool = False,
        local_fold: Optional[int] = None,
        time_zone: str = "",
    ):
        self.id = id
        self.batch_id = batch_id
        self.created_at = created_at
        self.finished_at = finished_at
        self.operator = operator
        self.action = action
        self.value = value
        self.target = target
        self.time_mode = time_mode
        self.ntp_policy = ntp_policy
        self.status = status
        self.total_hosts = int(total_hosts or 0)
        self.success_count = int(success_count or 0)
        self.failure_count = int(failure_count or 0)
        self.skipped_count = int(skipped_count or 0)
        self.error_count = int(error_count or 0)
        self.preview_count = int(preview_count or 0)
        self.dry_run = bool(dry_run)
        self.admin_user = admin_user or ""
        self.password_origin = password_origin or ""
        self.ntp_servers = list(ntp_servers or [])
        self.sync_rtc = bool(sync_rtc)
        self.force = bool(force)
        self.local_fold = local_fold
        self.time_zone = time_zone or ""

    @property
    def processed_count(self) -> int:
        return (
            self.success_count
            + self.failure_count
            + self.skipped_count
            + self.error_count
            + self.preview_count
        )

    @property
    def duration_seconds(self) -> Optional[float]:
        if not self.created_at or not self.finished_at:
            return None
        return round((self.finished_at - self.created_at).total_seconds(), 2)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "created_at": self.created_at.isoformat()
            if self.created_at
            else None,
            "finished_at": self.finished_at.isoformat()
            if self.finished_at
            else None,
            "operator": self.operator,
            "action": self.action,
            "value": self.value,
            "target": self.target,
            "time_mode": self.time_mode,
            "ntp_policy": self.ntp_policy,
            "status": self.status,
            "total_hosts": self.total_hosts,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "preview_count": self.preview_count,
            "processed_count": self.processed_count,
            "duration_seconds": self.duration_seconds,
            "dry_run": self.dry_run,
            "admin_user": self.admin_user,
            "password_origin": self.password_origin,
            "ntp_servers": self.ntp_servers,
            "sync_rtc": self.sync_rtc,
            "force": self.force,
            "local_fold": self.local_fold,
            "time_zone": self.time_zone,
        }

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    @staticmethod
    def init_db(db_path: str = None) -> None:
        """Crea o migra el esquema completo sin borrar datos existentes."""
        init_schema(db_path or _default_db_path())

    @staticmethod
    def get_batch(
        batch_id: str, db_path: str = None
    ) -> Optional["ExecutionBatch"]:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM execution_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        return _row_to_batch(row) if row else None

    @staticmethod
    def get_batches(
        limit: int = 50, offset: int = 0, db_path: str = None
    ) -> List["ExecutionBatch"]:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM execution_batches
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [_row_to_batch(row) for row in rows]

    @staticmethod
    def get_host_results(
        batch_id: str, db_path: str = None
    ) -> List[HostResult]:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM execution_hosts
                WHERE batch_id = ?
                ORDER BY id ASC
                """,
                (batch_id,),
            ).fetchall()
        return [_row_to_host_result(row) for row in rows]

    @staticmethod
    def get_stats(db_path: str = None) -> Dict[str, Any]:
        """Estadísticas globales de lotes y hosts.

        Los contadores de éxito/fallo son solo de ejecuciones reales: los
        lotes simulados (``dry_run=1``) se agregan aparte para no presentar
        una simulación como un cambio aplicado. ``preview`` cuenta las
        inspecciones reales de solo lectura.
        """
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            batches = int(
                conn.execute(
                    "SELECT COUNT(*) FROM execution_batches"
                ).fetchone()[0]
            )
            simulated_batches = int(
                conn.execute(
                    "SELECT COUNT(*) FROM execution_batches "
                    "WHERE dry_run = 1 AND status != 'preview'"
                ).fetchone()[0]
            )
            hosts = int(
                conn.execute(
                    "SELECT COUNT(*) FROM execution_hosts"
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT b.dry_run AS dry_run, h.result AS result,
                       COUNT(*) AS total
                FROM execution_hosts h
                JOIN execution_batches b ON b.batch_id = h.batch_id
                GROUP BY b.dry_run, h.result
                """
            ).fetchall()

        real = {
            row["result"]: int(row["total"])
            for row in rows
            if not row["dry_run"]
        }
        # Una vista previa es una inspección real aunque el lote se cree en
        # modo DRY_RUN; no debe contarse como cambio simulado.
        preview = sum(
            int(row["total"]) for row in rows if row["result"] == "preview"
        )
        simulated = sum(
            int(row["total"])
            for row in rows
            if row["dry_run"] and row["result"] != "preview"
        )
        return {
            "batches": batches,
            "simulated_batches": simulated_batches,
            "hosts": hosts,
            "success": real.get("success", 0),
            "failure": real.get("failure", 0),
            "skipped": real.get("skipped", 0),
            "error": real.get("error", 0),
            "preview": preview,
            "simulated": simulated,
        }

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------
    @staticmethod
    def create_batch(
        batch_id: str,
        operator: str,
        action: str,
        value: str,
        target: str,
        time_mode: str = "utc",
        ntp_policy: str = "keep",
        total_hosts: int = 0,
        status: str = "running",
        dry_run: bool = False,
        admin_user: str = "",
        password_origin: str = "",
        ntp_servers: Optional[List[str]] = None,
        sync_rtc: bool = False,
        force: bool = False,
        local_fold: Optional[int] = None,
        time_zone: str = "",
        db_path: str = None,
    ) -> "ExecutionBatch":
        if db_path is None:
            db_path = _default_db_path()
        if status not in BATCH_STATUSES:
            raise ValueError(f"Estado de lote no válido: {status}")

        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO execution_batches
                    (batch_id, created_at, operator, action, value, target,
                     time_mode, ntp_policy, status, total_hosts, dry_run,
                     admin_user, password_origin, ntp_servers, sync_rtc, force,
                     local_fold, time_zone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    _now(),
                    operator,
                    action,
                    value,
                    target,
                    time_mode,
                    ntp_policy,
                    status,
                    int(total_hosts or 0),
                    1 if dry_run else 0,
                    admin_user or "",
                    password_origin or "",
                    json.dumps(list(ntp_servers or []), ensure_ascii=False),
                    1 if sync_rtc else 0,
                    1 if force else 0,
                    int(local_fold) if local_fold in (0, 1) else None,
                    time_zone or "",
                ),
            )

        batch = ExecutionBatch.get_batch(batch_id, db_path=db_path)
        assert batch is not None
        return batch

    @staticmethod
    def add_host_result(
        batch_id: str,
        host: str,
        result: str,
        error: str = "",
        duration_ms: int = 0,
        before_state: Any = None,
        after_state: Any = None,
        password_hash: str = "",
        password_origin: str = "",
        db_path: str = None,
    ) -> HostResult:
        """Inserta el resultado de un host y actualiza el contador del lote."""
        if db_path is None:
            db_path = _default_db_path()
        if result not in HOST_RESULTS:
            raise ValueError(f"Resultado de host no válido: {result}")

        created_at = _now()
        counter = _RESULT_COUNTERS.get(result, "error_count")

        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO execution_hosts
                    (batch_id, host, result, error, duration_ms,
                     before_state, after_state, password_hash,
                     password_origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    host,
                    result,
                    error or "",
                    int(duration_ms or 0),
                    _dumps_state(before_state),
                    _dumps_state(after_state),
                    password_hash or "",
                    password_origin or "",
                    created_at,
                ),
            )
            host_result_id = cursor.lastrowid
            conn.execute(
                f"UPDATE execution_batches SET {counter} = {counter} + 1 "
                "WHERE batch_id = ?",
                (batch_id,),
            )

        return HostResult(
            id=host_result_id,
            batch_id=batch_id,
            host=host,
            result=result,
            error=error or "",
            duration_ms=int(duration_ms or 0),
            before_state=before_state,
            after_state=after_state,
            password_hash=password_hash or "",
            password_origin=password_origin or "",
            created_at=created_at,
        )

    @staticmethod
    def finish_batch(
        batch_id: str,
        status: Optional[str] = None,
        total_hosts: Optional[int] = None,
        db_path: str = None,
    ) -> Optional["ExecutionBatch"]:
        """Cierra el lote recalculando contadores y estado final."""
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
                        AS failure,
                    COALESCE(SUM(CASE WHEN result = 'skipped' THEN 1 ELSE 0 END), 0)
                        AS skipped,
                    COALESCE(SUM(CASE WHEN result = 'error' THEN 1 ELSE 0 END), 0)
                        AS errors,
                    COALESCE(SUM(CASE WHEN result = 'preview' THEN 1 ELSE 0 END), 0)
                        AS preview
                FROM execution_hosts
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()

            success = int(row["success"])
            failure = int(row["failure"])
            skipped = int(row["skipped"])
            errors = int(row["errors"])
            preview = int(row["preview"])
            processed = success + failure + skipped + errors + preview

            if status is None:
                status = _derive_status(
                    success, failure, skipped, errors, preview, processed
                )
            if status not in BATCH_STATUSES:
                raise ValueError(f"Estado de lote no válido: {status}")

            final_total = int(total_hosts) if total_hosts is not None else processed
            conn.execute(
                """
                UPDATE execution_batches SET
                    finished_at = ?,
                    status = ?,
                    total_hosts = ?,
                    success_count = ?,
                    failure_count = ?,
                    skipped_count = ?,
                    error_count = ?,
                    preview_count = ?
                WHERE batch_id = ?
                """,
                (
                    _now(),
                    status,
                    final_total,
                    success,
                    failure,
                    skipped,
                    errors,
                    preview,
                    batch_id,
                ),
            )

        return ExecutionBatch.get_batch(batch_id, db_path=db_path)

    @staticmethod
    def reconcile_orphans(db_path: str = None) -> int:
        """Marca como error los lotes que quedaron ``running`` tras un reinicio.

        Los hilos de ejecución son daemon y el estado vive en memoria: si el
        proceso muere, el lote no puede cerrarse solo. Al arrancar se cierran
        los huérfanos para que el historial no muestre lotes eternamente en
        curso. Devuelve cuántos lotes se reconciliaron.
        """
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE execution_batches
                SET status = 'error',
                    finished_at = COALESCE(finished_at, ?)
                WHERE status = 'running'
                """,
                (_now(),),
            )
            return int(cursor.rowcount or 0)

    @staticmethod
    def purge_old(days: int, db_path: str = None) -> int:
        """Elimina lotes (y sus hosts por cascada) anteriores a ``days``."""
        if db_path is None:
            db_path = _default_db_path()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        ).isoformat()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM execution_batches WHERE created_at < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)


def _derive_status(
    success: int,
    failure: int,
    skipped: int,
    errors: int,
    preview: int,
    processed: int,
) -> str:
    """Calcula el estado global a partir de los resultados por host."""
    if preview > 0:
        return "preview"
    if processed > 0 and success == processed:
        return "success"
    if success > 0:
        return "partial"
    # Un lote cuyos hosts no llegaron a procesarse (presupuesto agotado,
    # cancelación) es incompleto, no un fallo técnico de los hosts.
    if processed > 0 and skipped == processed:
        return "incomplete"
    if errors > 0 and failure == 0 and skipped == 0:
        return "error"
    return "failure"


def _row_to_batch(row) -> ExecutionBatch:
    return ExecutionBatch(
        id=row["id"],
        batch_id=row["batch_id"],
        created_at=_parse_datetime(row["created_at"]),
        finished_at=_parse_datetime(row["finished_at"]),
        operator=row["operator"],
        action=row["action"],
        value=row["value"],
        target=row["target"],
        time_mode=row["time_mode"],
        ntp_policy=row["ntp_policy"],
        status=row["status"],
        total_hosts=row["total_hosts"],
        success_count=row["success_count"],
        failure_count=row["failure_count"],
        skipped_count=row["skipped_count"],
        error_count=row["error_count"],
        preview_count=row["preview_count"],
        dry_run=bool(row["dry_run"]),
        admin_user=row["admin_user"],
        password_origin=row["password_origin"],
        ntp_servers=json.loads(row["ntp_servers"] or "[]"),
        sync_rtc=bool(row["sync_rtc"]),
        force=bool(row["force"]),
        local_fold=row["local_fold"],
        time_zone=row["time_zone"],
    )


def _row_to_host_result(row) -> HostResult:
    return HostResult(
        id=row["id"],
        batch_id=row["batch_id"],
        host=row["host"],
        result=row["result"],
        error=row["error"],
        duration_ms=row["duration_ms"],
        before_state=_loads_state(row["before_state"]),
        after_state=_loads_state(row["after_state"]),
        password_hash=row["password_hash"],
        password_origin=row["password_origin"],
        created_at=row["created_at"],
    )
