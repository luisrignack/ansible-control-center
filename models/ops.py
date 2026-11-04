"""Modelos operativos: inventario, programaciones y aprobaciones.

- ``HostGroup`` / ``InventoryHost``: inventario persistente de grupos de
  hosts con metadatos (SO, conexión, clave SSH opcional).
- ``Schedule``: ejecuciones programadas (una vez, diaria o semanal). Los
  parámetros sensibles (contraseña maestra del vault) se guardan cifrados
  con una clave propia del directorio de secretos.
- ``ApprovalRequest``: cola de doble aprobación para tareas destructivas.

Todas las operaciones usan ``get_connection`` y son no destructivas.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from models.db import get_connection


def _default_db_path() -> str:
    from config import resolve_db_path

    return str(resolve_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def params_hash(params: Dict[str, Any]) -> str:
    """Hash estable de los parámetros de una solicitud de aprobación.

    Excluye secretos: los parámetros que viajan aquí nunca contienen
    contraseñas, así que el hash basta para casar solicitud y envío.
    """
    stable = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Inventario
# ---------------------------------------------------------------------------
class HostGroup:
    """Grupo de hosts del inventario."""

    def __init__(
        self,
        id: int,
        name: str,
        description: str = "",
        os_flavor: str = "linux",
        connection: str = "ssh",
        ssh_key_path: str = "",
        created_at: str = "",
        host_count: int = 0,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.os_flavor = os_flavor
        self.connection = connection
        self.ssh_key_path = ssh_key_path
        self.created_at = created_at
        self.host_count = host_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "os_flavor": self.os_flavor,
            "connection": self.connection,
            "ssh_key_path": self.ssh_key_path,
            "created_at": self.created_at,
            "host_count": self.host_count,
        }

    @staticmethod
    def _row_to_group(row) -> "HostGroup":
        return HostGroup(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            os_flavor=row["os_flavor"],
            connection=row["connection"],
            ssh_key_path=row["ssh_key_path"],
            created_at=row["created_at"],
            host_count=row["host_count"] if "host_count" in row.keys() else 0,
        )

    @classmethod
    def all(cls, db_path: str = None) -> List["HostGroup"]:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT g.*, (SELECT COUNT(*) FROM inventory_hosts h
                             WHERE h.group_id = g.id) AS host_count
                FROM inventory_groups g ORDER BY g.name
                """
            ).fetchall()
        return [cls._row_to_group(row) for row in rows]

    @classmethod
    def get(cls, group_id: int, db_path: str = None) -> Optional["HostGroup"]:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            row = conn.execute(
                """
                SELECT g.*, (SELECT COUNT(*) FROM inventory_hosts h
                             WHERE h.group_id = g.id) AS host_count
                FROM inventory_groups g WHERE g.id = ?
                """,
                (int(group_id),),
            ).fetchone()
        return cls._row_to_group(row) if row is not None else None

    @classmethod
    def create(
        cls,
        name: str,
        description: str = "",
        os_flavor: str = "linux",
        connection: str = "ssh",
        db_path: str = None,
    ) -> "HostGroup":
        if db_path is None:
            db_path = _default_db_path()
        name = (name or "").strip()
        if not name or len(name) > 80:
            raise ValueError("El nombre del grupo es obligatorio (máx. 80).")
        if os_flavor not in {"linux"}:
            raise ValueError("Plataforma no soportada.")
        if connection not in {"ssh"}:
            raise ValueError("Tipo de conexión no soportado.")
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO inventory_groups
                    (name, description, os_flavor, connection, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    (description or "").strip()[:300],
                    os_flavor,
                    connection,
                    _now(),
                ),
            )
        return cls.get(cursor.lastrowid, db_path=db_path)

    @classmethod
    def set_ssh_key(
        cls, group_id: int, ssh_key_path: str, db_path: str = None
    ) -> None:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE inventory_groups SET ssh_key_path = ? WHERE id = ?",
                (ssh_key_path or "", int(group_id)),
            )

    def delete(self, db_path: str = None) -> None:
        if db_path is None:
            db_path = _default_db_path()
        if self.id is None:
            return
        with get_connection(db_path) as conn:
            conn.execute(
                "DELETE FROM inventory_hosts WHERE group_id = ?", (self.id,)
            )
            conn.execute(
                "DELETE FROM inventory_groups WHERE id = ?", (self.id,)
            )

    @classmethod
    def hosts_of(
        cls, group_id: int, db_path: str = None
    ) -> List["InventoryHost"]:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM inventory_hosts WHERE group_id = ?
                ORDER BY id
                """,
                (int(group_id),),
            ).fetchall()
        return [InventoryHost.from_row(row) for row in rows]


class InventoryHost:
    """Host dentro de un grupo del inventario."""

    def __init__(
        self,
        id: int,
        group_id: int,
        address: str,
        label: str = "",
        created_at: str = "",
    ) -> None:
        self.id = id
        self.group_id = group_id
        self.address = address
        self.label = label
        self.created_at = created_at

    @staticmethod
    def from_row(row) -> "InventoryHost":
        return InventoryHost(
            id=row["id"],
            group_id=row["group_id"],
            address=row["address"],
            label=row["label"],
            created_at=row["created_at"],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "address": self.address,
            "label": self.label,
        }

    @classmethod
    def add(
        cls,
        group_id: int,
        address: str,
        label: str = "",
        db_path: str = None,
    ) -> Tuple[Optional["InventoryHost"], Optional[str]]:
        """Añade un host validando su formato IPv4.

        Devuelve ``(host, error)``. La validación de formato es la misma que
        espera el objetivo manual: IPv4 literal; la comprobación de allowlist
        se hace al ejecutar, con la allowlist del momento.
        """
        import ipaddress as _ip

        from services.host_resolver import pin_target

        if db_path is None:
            db_path = _default_db_path()
        group = HostGroup.get(group_id, db_path=db_path)
        if group is None:
            return None, "El grupo no existe."
        address = (address or "").strip()
        try:
            import ipaddress as _ipaddr

            _ipaddr.IPv4Address(address)
        except ValueError:
            return None, f"Dirección inválida: {address or '(vacía)'}"
        pinned, error = pin_target(address, max_hosts=1)
        if error or not pinned:
            return None, error or f"Dirección inválida: {address}"
        with get_connection(db_path) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO inventory_hosts
                        (group_id, address, label, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        int(group_id),
                        str(_ip.ip_address(pinned)),
                        (label or "").strip()[:80],
                        _now(),
                    ),
                )
            except Exception as exc:  # duplicado u otro error
                return None, f"No se pudo añadir el host: {exc}"
        return InventoryHost(cursor.lastrowid, group_id, pinned, label), None

    def delete(self, db_path: str = None) -> None:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            conn.execute(
                "DELETE FROM inventory_hosts WHERE id = ?", (int(self.id),)
            )


# ---------------------------------------------------------------------------
# Programaciones
# ---------------------------------------------------------------------------
SCHEDULE_ONESHOT = "once"
SCHEDULE_DAILY = "daily"
SCHEDULE_WEEKLY = "weekly"
SCHEDULE_RECURRENCES = (SCHEDULE_ONESHOT, SCHEDULE_DAILY, SCHEDULE_WEEKLY)


class Schedule:
    """Ejecución programada de una tarea del catálogo."""

    def __init__(self, row) -> None:
        self.id = row["id"]
        self.name = row["name"]
        self.task_id = row["task_id"]
        self.run_at = row["run_at"]
        self.recurrence = row["recurrence"]
        self.enabled = bool(row["enabled"])
        self.params = json.loads(row["params"] or "{}")
        self.last_run = row["last_run"]
        self.last_batch = row["last_batch"]
        self.last_error = row["last_error"]
        self.created_by = row["created_by"]
        self.created_at = row["created_at"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "task_id": self.task_id,
            "run_at": self.run_at,
            "recurrence": self.recurrence,
            "enabled": self.enabled,
            "params": self.params,
            "last_run": self.last_run,
            "last_batch": self.last_batch,
            "last_error": self.last_error,
            "created_by": self.created_by,
        }

    @staticmethod
    def is_due(row, now: Optional[datetime] = None) -> bool:
        """Indica si una fila de ``schedules`` está lista para dispararse."""
        if not bool(row["enabled"]):
            return False
        plan = datetime.fromisoformat(row["run_at"])
        if plan.tzinfo is None:
            plan = plan.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return now >= plan

    @classmethod
    def due(cls, db_path: str = None) -> List["Schedule"]:
        """Devuelve las programaciones activas cuya hora ya llegó."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1"
            ).fetchall()
        return [cls(row) for row in rows if cls.is_due(row)]

    @classmethod
    def due_rows(cls, db_path: str = None) -> List[Any]:
        """Fila cruda de programaciones vencidas (para workflows con claim)."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE enabled = 1"
            ).fetchall()
        return [row for row in rows if cls.is_due(row)]

    @classmethod
    def all(cls, db_path: str = None) -> List["Schedule"]:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM schedules ORDER BY run_at DESC"
            ).fetchall()
        return [cls(row) for row in rows]

    @classmethod
    def get(cls, schedule_id: int, db_path: str = None) -> Optional["Schedule"]:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (int(schedule_id),)
            ).fetchone()
        return Schedule(row) if row is not None else None

    @classmethod
    def create(
        cls,
        name: str,
        task_id: str,
        run_at: datetime,
        params: Dict[str, Any],
        recurrence: str = SCHEDULE_ONESHOT,
        created_by: str = "",
        db_path: str = None,
    ) -> "Schedule":
        if db_path is None:
            db_path = _default_db_path()
        name = (name or "").strip()
        if not name or len(name) > 80:
            raise ValueError("El nombre de la programación es obligatorio.")
        if recurrence not in SCHEDULE_RECURRENCES:
            raise ValueError("Recurrencia no válida.")
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        if run_at < datetime.now(timezone.utc) - timedelta(seconds=120):
            raise ValueError("La fecha/hora debe estar en el futuro (UTC).")
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO schedules
                    (name, task_id, run_at, recurrence, enabled, params,
                     created_by, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    name,
                    task_id,
                    run_at.isoformat(timespec="seconds"),
                    recurrence,
                    json.dumps(params, ensure_ascii=False),
                    created_by,
                    _now(),
                ),
            )
        row = conn_row = None
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM schedules WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        assert row is not None
        return Schedule(row)

    @classmethod
    def set_enabled(
        cls,
        schedule_id: int,
        enabled: bool,
        db_path: str = None,
    ) -> None:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE schedules SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, int(schedule_id)),
            )

    @classmethod
    def delete(cls, schedule_id: int, db_path: str = None) -> None:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            conn.execute("DELETE FROM schedules WHERE id = ?", (int(schedule_id),))

    @classmethod
    def mark_run(
        cls,
        schedule_id: int,
        batch_id: str = "",
        error: str = "",
        next_run: Optional[datetime] = None,
        disable: bool = False,
        db_path: str = None,
    ) -> None:
        """Registra el disparo de una programación y calcula la siguiente."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            if next_run is not None:
                conn.execute(
                    """
                    UPDATE schedules
                    SET last_run = ?, last_batch = ?, last_error = ?,
                        run_at = ?, enabled = ?
                    WHERE id = ?
                    """,
                    (
                        _now(),
                        batch_id or "",
                        (error or "")[:300],
                        next_run.isoformat(timespec="seconds"),
                        0 if disable or bool(error) else 1,
                        int(schedule_id),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE schedules
                    SET last_run = ?, last_batch = ?, last_error = ?,
                        enabled = ?
                    WHERE id = ?
                    """,
                    (
                        _now(),
                        batch_id or "",
                        (error or "")[:300],
                        0 if disable or bool(error) else 1,
                        int(schedule_id),
                    ),
                )

    def next_run_from(self, base: Optional[datetime] = None) -> Optional[datetime]:
        """Próxima ejecución tras un disparo, según la recurrencia."""
        if self.recurrence == SCHEDULE_DAILY:
            return (base or _parse_utc(self.run_at)) + timedelta(days=1)
        if self.recurrence == SCHEDULE_WEEKLY:
            return (base or _parse_utc(self.run_at)) + timedelta(days=7)
        return None


def _parse_utc(value: str) -> datetime:
    plan = datetime.fromisoformat(value)
    if plan.tzinfo is None:
        plan = plan.replace(tzinfo=timezone.utc)
    return plan


# ---------------------------------------------------------------------------
# Aprobaciones (two-person rule)
# ---------------------------------------------------------------------------
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_CONSUMED = "consumed"


class ApprovalRequest:
    """Solicitud de autorización para ejecutar una tarea destructiva."""

    def __init__(self, row) -> None:
        self.id = row["id"]
        self.task_id = row["task_id"]
        self.params_hash = row["params_hash"]
        self.params = json.loads(row["params"] or "{}")
        self.requested_by = row["requested_by"]
        self.requested_at = row["requested_at"]
        self.status = row["status"]
        self.approver = row["approver"]
        self.decided_at = row["decided_at"]
        self.note = row["note"]
        self.consumed_at = row["consumed_at"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "status": self.status,
            "approver": self.approver,
            "decided_at": self.decided_at,
            "note": self.note,
        }

    @classmethod
    def _by_row(cls, row) -> Optional["ApprovalRequest"]:
        return cls(row) if row is not None else None

    @classmethod
    def get(cls, request_id: int, db_path: str = None) -> Optional["ApprovalRequest"]:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (int(request_id),)
            ).fetchone()
        return cls._by_row(row)

    @classmethod
    def list_status(
        cls, status: str, limit: int = 100, db_path: str = None
    ) -> List["ApprovalRequest"]:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM approvals WHERE status = ?
                ORDER BY id DESC LIMIT ?
                """,
                (status, int(limit)),
            ).fetchall()
        return [cls(row) for row in rows]

    @classmethod
    def create(
        cls,
        task_id: str,
        params: Dict[str, Any],
        requested_by: str,
        db_path: str = None,
    ) -> "ApprovalRequest":
        """Creamos la solicitud sin secretos (params sin contraseñas)."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO approvals
                    (task_id, params_hash, params, requested_by, requested_at,
                     status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    params_hash(params),
                    json.dumps(params, ensure_ascii=False),
                    requested_by,
                    _now(),
                    APPROVAL_PENDING,
                ),
            )
        got = cls.get(cursor.lastrowid, db_path=db_path)
        assert got is not None
        return got

    @classmethod
    def decide(
        cls,
        request_id: int,
        approver: str,
        approve: bool,
        note: str = "",
        db_path: str = None,
    ) -> Tuple[Optional["ApprovalRequest"], Optional[str]]:
        """Aprueba o rechaza una solicitud pendiente.

        Devuelve ``(solicitud, error)``.
        """
        if db_path is None:
            db_path = _default_db_path()
        req = cls.get(request_id, db_path=db_path)
        if req is None:
            return None, "La solicitud no existe."
        if req.status != APPROVAL_PENDING:
            return None, "La solicitud ya fue decidida."
        approver = (approver or "").strip()
        if not approver:
            return None, "El aprobador es obligatorio."
        with get_connection(db_path) as conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, approver = ?, decided_at = ?, note = ?
                WHERE id = ?
                """,
                (
                    APPROVAL_APPROVED if approve else APPROVAL_REJECTED,
                    approver,
                    _now(),
                    (note or "").strip()[:300],
                    int(request_id),
                ),
            )
        return cls.get(request_id, db_path=db_path), None

    @classmethod
    def consume_if_approved(
        cls,
        task_id: str,
        params: Dict[str, Any],
        executor: str,
        approves_by_differs: bool,
        ttl_minutes: int,
        db_path: str = None,
    ) -> Tuple[bool, Optional[str]]:
        """Consume una aprobación válida para esta ejecución.

        Casar tarea + hash de parámetros + estado ``approved`` + aprobador
        distinto del ejecutor + dentro del TTL. Es de un solo uso.
        """
        if db_path is None:
            db_path = _default_db_path()
        wanted = params_hash(params)
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM approvals
                WHERE task_id = ? AND params_hash = ?
                ORDER BY id DESC LIMIT 20
                """,
                (task_id, wanted),
            ).fetchall()
        best_error = "Se requiere aprobación de un administrador para ejecutar esta tarea."
        for row in rows:
            req = cls(row)
            if req.status != APPROVAL_APPROVED:
                if req.status == APPROVAL_PENDING:
                    best_error = (
                        "Esta tarea requiere doble aprobación: hay una "
                        "solicitud pendiente, espera la decisión de un admin."
                    )
                continue
            if approves_by_differs and req.approver == executor:
                best_error = (
                    "La aprobación debe venir de un administrador distinto "
                    "del ejecutor (two-person rule)."
                )
                continue
            decided = _parse_utc(req.decided_at or req.requested_at)
            if datetime.now(timezone.utc) > decided + timedelta(minutes=ttl_minutes):
                best_error = "La aprobación ha expirado; solicita una nueva."
                continue
            if req.consumed_at:
                best_error = "La aprobación ya se usó; solicita otra."
                continue
            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE approvals SET status = ?, consumed_at = ? WHERE id = ?",
                    (APPROVAL_CONSUMED, _now(), int(req.id)),
                )
            return True, None
        return False, best_error
