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
from services.safety import (
    DEFAULT_CRITICALITY,
    normalize_criticality,
    normalize_criticality_override,
    parse_window_days,
    parse_window_time,
    window_error,
)


class OSFlavor:
    """Sistemas operativos soportados por el inventario."""
    LINUX = "linux"
    WINDOWS = "windows"


# Mantener el orden estable para listas en UI/mensajes.
OS_FLAVORS = (OSFlavor.LINUX, OSFlavor.WINDOWS)


class OSConnection:
    """Tipos de conexión de Ansible por plataforma."""
    SSH = "ssh"
    WINRM = "winrm"


OS_CONNECTIONS = (OSConnection.SSH, OSConnection.WINRM)


def _default_db_path() -> str:
    from config import resolve_db_path

    return str(resolve_db_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _criticality_label(value: str) -> str:
    from services.safety import CRITICALITY_LABELS

    return CRITICALITY_LABELS.get(str(value or "").lower(), "—")


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
        criticality: str = DEFAULT_CRITICALITY,
        window_days: str = "",
        window_start: str = "",
        window_end: str = "",
        window_note: str = "",
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.os_flavor = os_flavor
        self.connection = connection
        self.ssh_key_path = ssh_key_path
        self.created_at = created_at
        self.host_count = host_count
        self.criticality = criticality or DEFAULT_CRITICALITY
        self.window_days = window_days or ""
        self.window_start = window_start or ""
        self.window_end = window_end or ""
        self.window_note = window_note or ""

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
            "criticality": self.criticality,
            "criticality_label": _criticality_label(self.criticality),
            "window_days": self.window_days,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "window_note": self.window_note,
            "window_label": self.window_label,
        }

    @property
    def window_label(self) -> str:
        from services.safety import window_label

        return window_label(
            parse_window_days(self.window_days),
            self.window_start,
            self.window_end,
        )

    @staticmethod
    def _row_to_group(row) -> "HostGroup":
        keys = row.keys()
        return HostGroup(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            os_flavor=row["os_flavor"],
            connection=row["connection"],
            ssh_key_path=row["ssh_key_path"],
            created_at=row["created_at"],
            host_count=row["host_count"] if "host_count" in keys else 0,
            criticality=(
                row["criticality"]
                if "criticality" in keys
                else DEFAULT_CRITICALITY
            ),
            window_days=row["window_days"] if "window_days" in keys else "",
            window_start=(
                row["window_start"] if "window_start" in keys else ""
            ),
            window_end=row["window_end"] if "window_end" in keys else "",
            window_note=row["window_note"] if "window_note" in keys else "",
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
        connection: str = "",
        db_path: str = None,
        criticality: str = DEFAULT_CRITICALITY,
        window_days: str = "",
        window_start: str = "",
        window_end: str = "",
        window_note: str = "",
    ) -> "HostGroup":
        if db_path is None:
            db_path = _default_db_path()
        name = (name or "").strip()
        os_flavor = (os_flavor or "linux").strip().lower()
        if not name or len(name) > 80:
            raise ValueError("El nombre del grupo es obligatorio (máx. 80).")
        if os_flavor not in OS_FLAVORS:
            raise ValueError("Plataforma no soportada.")
        # La conexión se deriva de la plataforma salvo que se fuerce.
        connection = (connection or "").strip().lower()
        if not connection:
            connection = (
                "winrm" if os_flavor == OSFlavor.WINDOWS else "ssh"
            )
        if connection not in OS_CONNECTIONS:
            raise ValueError("Tipo de conexión no soportado.")

        criticality = normalize_criticality(criticality)
        days = parse_window_days(window_days)
        start = parse_window_time(window_start)
        end = parse_window_time(window_end)
        safety_error = window_error(days, start, end)
        if safety_error:
            raise ValueError(safety_error)

        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO inventory_groups
                    (name, description, os_flavor, connection, created_at,
                     criticality, window_days, window_start, window_end,
                     window_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    (description or "").strip()[:300],
                    os_flavor,
                    connection,
                    _now(),
                    criticality,
                    ",".join(str(day) for day in days),
                    start,
                    end,
                    (window_note or "").strip()[:200],
                ),
            )
        return cls.get(cursor.lastrowid, db_path=db_path)

    @classmethod
    def set_safety(
        cls,
        group_id: int,
        criticality: str,
        window_days: str = "",
        window_start: str = "",
        window_end: str = "",
        window_note: str = "",
        db_path: str = None,
    ) -> None:
        """Actualiza criticidad y ventana de un grupo existente."""
        if db_path is None:
            db_path = _default_db_path()
        criticality = normalize_criticality(criticality)
        days = parse_window_days(window_days)
        start = parse_window_time(window_start)
        end = parse_window_time(window_end)
        safety_error = window_error(days, start, end)
        if safety_error:
            raise ValueError(safety_error)
        with get_connection(db_path) as conn:
            conn.execute(
                """
                UPDATE inventory_groups
                SET criticality = ?, window_days = ?, window_start = ?,
                    window_end = ?, window_note = ?
                WHERE id = ?
                """,
                (
                    criticality,
                    ",".join(str(day) for day in days),
                    start,
                    end,
                    (window_note or "").strip()[:200],
                    int(group_id),
                ),
            )

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
        criticality: str = "",
    ) -> None:
        self.id = id
        self.group_id = group_id
        self.address = address
        self.label = label
        self.created_at = created_at
        self.criticality = criticality or ""

    @staticmethod
    def from_row(row) -> "InventoryHost":
        keys = row.keys()
        return InventoryHost(
            id=row["id"],
            group_id=row["group_id"],
            address=row["address"],
            label=row["label"],
            created_at=row["created_at"],
            criticality=(
                row["criticality"] if "criticality" in keys else ""
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "address": self.address,
            "label": self.label,
            "criticality": self.criticality,
        }

    @classmethod
    def add(
        cls,
        group_id: int,
        address: str,
        label: str = "",
        db_path: str = None,
        criticality: str = "",
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
        try:
            criticality = normalize_criticality_override(criticality)
        except ValueError as exc:
            return None, str(exc)
        with get_connection(db_path) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO inventory_hosts
                        (group_id, address, label, created_at, criticality)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(group_id),
                        str(_ip.ip_address(pinned)),
                        (label or "").strip()[:80],
                        _now(),
                        criticality,
                    ),
                )
            except Exception as exc:  # duplicado u otro error
                return None, f"No se pudo añadir el host: {exc}"
        return (
            InventoryHost(
                cursor.lastrowid, group_id, pinned, label, criticality=criticality
            ),
            None,
        )

    @classmethod
    def set_criticality(
        cls, host_id: int, criticality: str, db_path: str = None
    ) -> None:
        if db_path is None:
            db_path = _default_db_path()
        override = normalize_criticality_override(criticality)
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE inventory_hosts SET criticality = ? WHERE id = ?",
                (override, int(host_id)),
            )

    @classmethod
    def find_by_address(
        cls, address: str, db_path: str = None
    ) -> Tuple[Optional["InventoryHost"], Optional["HostGroup"]]:
        """Busca un host por IP y devuelve también su grupo (si existe)."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM inventory_hosts WHERE address = ? LIMIT 1",
                (str(address or "").strip(),),
            ).fetchone()
        if row is None:
            return None, None
        host = cls.from_row(row)
        return host, HostGroup.get(host.group_id, db_path=db_path)

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
    def claim(
        cls,
        schedule_id: int,
        owner: str = "",
        lease_seconds: int = 300,
        db_path: str = None,
    ) -> bool:
        """Reserva atómicamente una programación vencida para un proceso.

        Solo el proceso que consigue el ``UPDATE`` lanza el lote. La reserva
        caduca para que un proceso caído no bloquee la programación para
        siempre. Debe llamarse antes de leer/descifrar parámetros y de
        :meth:`mark_run`, que limpia la reserva.
        """
        if db_path is None:
            db_path = _default_db_path()
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=int(lease_seconds))).isoformat(
            timespec="seconds"
        )
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE schedules
                SET claimed_at = ?, claimed_by = ?
                WHERE id = ?
                  AND enabled = 1
                  AND run_at <= ?
                  AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (
                    now.isoformat(timespec="seconds"),
                    (owner or "")[:120],
                    int(schedule_id),
                    now.isoformat(timespec="seconds"),
                    cutoff,
                ),
            )
            return int(cursor.rowcount or 0) == 1

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
        if row is None:  # pragma: no cover - el INSERT acaba de ejecutarse
            raise RuntimeError(
                f"No se pudo recuperar la programación creada: {name}"
            )
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
        retry_delay_seconds: int = 0,
        db_path: str = None,
    ) -> None:
        """Registra el disparo de una programación y calcula la siguiente.

        Un error transitorio (por ejemplo, la cola llena) se registra pero no
        desactiva la programación. Para no reintentar en bucle cada escaneo se
        puede indicar ``retry_delay_seconds``: la ``run_at`` se pospone ese
        tiempo conservando la programación activa. Los errores permanentes
        deben seguir pasando ``disable=True``.
        """
        if db_path is None:
            db_path = _default_db_path()
        enabled = 0 if disable else 1
        scheduled_at: Optional[str] = None
        if next_run is not None:
            scheduled_at = next_run.isoformat(timespec="seconds")
        elif retry_delay_seconds > 0:
            retry_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(retry_delay_seconds)
            )
            scheduled_at = retry_at.isoformat(timespec="seconds")
        with get_connection(db_path) as conn:
            if scheduled_at is not None:
                conn.execute(
                    """
                    UPDATE schedules
                    SET last_run = ?, last_batch = ?, last_error = ?,
                        run_at = ?, enabled = ?, claimed_at = NULL,
                        claimed_by = NULL
                    WHERE id = ?
                    """,
                    (
                        _now(),
                        batch_id or "",
                        (error or "")[:300],
                        scheduled_at,
                        enabled,
                        int(schedule_id),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE schedules
                    SET last_run = ?, last_batch = ?, last_error = ?,
                        enabled = ?, claimed_at = NULL, claimed_by = NULL
                    WHERE id = ?
                    """,
                    (
                        _now(),
                        batch_id or "",
                        (error or "")[:300],
                        enabled,
                        int(schedule_id),
                    ),
                )

    def next_run_from(self, base: Optional[datetime] = None) -> Optional[datetime]:
        """Próxima ejecución tras un disparo, según la recurrencia.

        La base nunca puede ser anterior a ``ahora``: si la aplicación estuvo
        caída varios días, no se encadenan ejecuciones históricas una tras
        otra. Las ejecuciones perdidas se consolidan en un único disparo y la
        siguiente queda siempre en el futuro.
        """
        if self.recurrence not in (SCHEDULE_DAILY, SCHEDULE_WEEKLY):
            return None
        moment = base or _parse_utc(self.run_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = max(moment, datetime.now(timezone.utc))
        if self.recurrence == SCHEDULE_DAILY:
            return moment + timedelta(days=1)
        return moment + timedelta(days=7)


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
APPROVAL_RESERVED = "reserved"
APPROVAL_REJECTED = "rejected"
APPROVAL_CONSUMED = "consumed"

# Una reserva abandonada (proceso caído entre reservar y confirmar) vuelve a
# ``approved`` pasado este tiempo.
APPROVAL_RESERVATION_LEASE_MINUTES = 10


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
        if got is None:  # pragma: no cover - el INSERT acaba de ejecutarse
            raise RuntimeError(
                f"No se pudo recuperar la aprobación creada: {task_id}"
            )
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
        if req.requested_by and req.requested_by == approver:
            return None, (
                "El aprobador debe ser distinto del solicitante "
                "(two-person rule)."
            )
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, approver = ?, decided_at = ?, note = ?
                WHERE id = ? AND status = ? AND consumed_at IS NULL
                """,
                (
                    APPROVAL_APPROVED if approve else APPROVAL_REJECTED,
                    approver,
                    _now(),
                    (note or "").strip()[:300],
                    int(request_id),
                    APPROVAL_PENDING,
                ),
            )
            if cursor.rowcount != 1:
                return None, "La solicitud ya fue decidida."
        return cls.get(request_id, db_path=db_path), None

    @classmethod
    def reserve_if_approved(
        cls,
        task_id: str,
        params: Dict[str, Any],
        executor: str,
        approves_by_differs: bool,
        ttl_minutes: int,
        db_path: str = None,
    ) -> Tuple[Optional[int], Optional[str]]:
        """Reserva una aprobación válida sin consumirla todavía.

        Devuelve ``(request_id, None)`` si la reserva se adquirió, o
        ``(None, error)`` si no hay ninguna aprobación válida. El flujo
        correcto es: reservar → encolar → :meth:`confirm_reserved`; si el
        encolado falla, :meth:`release_reserved` devuelve la aprobación a
        ``approved`` para no gastar una autorización de un solo uso.

        La transición ``approved`` → ``reserved`` es atómica: dos envíos
        concurrentes no pueden reservar la misma fila.
        """
        if db_path is None:
            db_path = _default_db_path()
        wanted = params_hash(params)
        now = datetime.now(timezone.utc)
        lease_cutoff = (
            now - timedelta(minutes=APPROVAL_RESERVATION_LEASE_MINUTES)
        ).isoformat(timespec="seconds")
        # Las reservas abandonadas vuelven a estar disponibles.
        with get_connection(db_path) as conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, consumed_at = NULL
                WHERE status = ? AND consumed_at < ?
                """,
                (APPROVAL_APPROVED, APPROVAL_RESERVED, lease_cutoff),
            )
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
                elif req.status == APPROVAL_CONSUMED:
                    best_error = (
                        "La aprobación ya se usó; solicita otra."
                    )
                continue
            if req.requested_by and req.approver == req.requested_by:
                best_error = (
                    "La aprobación no es válida: el aprobador debe ser "
                    "distinto del solicitante (two-person rule)."
                )
                continue
            if approves_by_differs and req.approver == executor:
                best_error = (
                    "La aprobación debe venir de un administrador distinto "
                    "del ejecutor (two-person rule)."
                )
                continue
            decided = _parse_utc(req.decided_at or req.requested_at)
            if now > decided + timedelta(minutes=ttl_minutes):
                best_error = "La aprobación ha expirado; solicita una nueva."
                continue
            if req.consumed_at:
                best_error = "La aprobación ya se usó; solicita otra."
                continue
            with get_connection(db_path) as conn:
                cursor = conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, consumed_at = ?
                    WHERE id = ? AND status = ? AND consumed_at IS NULL
                    """,
                    (
                        APPROVAL_RESERVED,
                        now.isoformat(timespec="seconds"),
                        int(req.id),
                        APPROVAL_APPROVED,
                    ),
                )
                if cursor.rowcount != 1:
                    best_error = (
                        "La aprobación ya se usó; solicita otra."
                    )
                    continue
            return int(req.id), None
        return None, best_error

    @classmethod
    def confirm_reserved(
        cls, request_id: int, db_path: str = None
    ) -> bool:
        """Convierte una reserva en consumo definitivo (un solo uso)."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, consumed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    APPROVAL_CONSUMED,
                    _now(),
                    int(request_id),
                    APPROVAL_RESERVED,
                ),
            )
            return int(cursor.rowcount or 0) == 1

    @classmethod
    def release_reserved(
        cls, request_id: int, db_path: str = None
    ) -> bool:
        """Devuelve una reserva a ``approved`` si el envío no llegó a encolarse."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, consumed_at = NULL
                WHERE id = ? AND status = ?
                """,
                (APPROVAL_APPROVED, int(request_id), APPROVAL_RESERVED),
            )
            return int(cursor.rowcount or 0) == 1

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
        """Reserva y consume una aprobación válida en una sola llamada.

        Se conserva como API compatible para los flujos que no necesitan
        separar reserva y confirmación (por ejemplo, pruebas o scripts). El
        flujo web usa ``reserve_if_approved`` + ``confirm_reserved`` para no
        gastar la aprobación si el encolado falla.
        """
        request_id, error = cls.reserve_if_approved(
            task_id,
            params,
            executor=executor,
            approves_by_differs=approves_by_differs,
            ttl_minutes=ttl_minutes,
            db_path=db_path,
        )
        if request_id is None:
            return False, error
        if not cls.confirm_reserved(request_id, db_path=db_path):
            return False, "La aprobación ya se usó; solicita otra."
        return True, None
