"""Datos ficticios para revisar la interfaz con un volumen realista.

El sembrado está pensado para demos y capturas, no para producción con datos
reales. Todo lo que crea lleva una marca reconocible:

* grupos y programaciones con el prefijo ``Demo · ``;
* lotes con operadores ``demo.*`` y ``batch_id`` que empieza por ``demo-``;
* solicitudes de aprobación de ``demo.*``;
* eventos de auditoría con ``[demo]`` en el detalle.

``purge_demo_data`` elimina únicamente esas filas y deja intacto el resto de
la base. Las programaciones se crean **inactivas** para que el planificador
nunca las ejecute contra direcciones ficticias.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from models.batch import ExecutionBatch
from models.db import get_connection
from models.ops import ApprovalRequest, HostGroup, Schedule

DEMO_PREFIX = "Demo · "
DEMO_OPERATOR_PREFIX = "demo."
DEMO_BATCH_PREFIX = "demo-"

# (nombre, descripción, plataforma, criticidad, días, inicio, fin, nota)
_DEMO_GROUPS = (
    (
        "Demo · Sucursal Vedado",
        "Estaciones de atención al público de la sucursal Vedado.",
        "linux",
        "high",
        "1,2,3,4,5",
        "08:00",
        "12:00",
        "Ventana de mantenimiento aprobada por la dirección.",
    ),
    (
        "Demo · Sucursal Centro",
        "Puntos de venta y cajas de la sucursal Centro.",
        "linux",
        "medium",
        "2,4",
        "13:00",
        "17:00",
        "Cambios coordinados con el turno de la tarde.",
    ),
    (
        "Demo · Aulas Facultad",
        "Laboratorios docentes y aulas de computación.",
        "linux",
        "low",
        "",
        "",
        "",
        "",
    ),
    (
        "Demo · Oficina Windows",
        "Equipos de escritorio con Windows de la oficina central.",
        "windows",
        "critical",
        "6",
        "07:00",
        "10:00",
        "Solo ventanas de fin de semana.",
    ),
)

_DEMO_HOSTS = {
    "Demo · Sucursal Vedado": (
        ("10.99.10.11", "caja-01"),
        ("10.99.10.12", "caja-02"),
        ("10.99.10.13", "caja-03"),
        ("10.99.10.14", "backoffice-01"),
        ("10.99.10.15", "backoffice-02"),
        ("10.99.10.16", "kiosco-01"),
    ),
    "Demo · Sucursal Centro": (
        ("10.99.20.11", "caja-centro-01"),
        ("10.99.20.12", "caja-centro-02"),
        ("10.99.20.13", "caja-centro-03"),
        ("10.99.20.14", "supervisor"),
    ),
    "Demo · Aulas Facultad": (
        ("10.99.30.21", "aula-101"),
        ("10.99.30.22", "aula-102"),
        ("10.99.30.23", "aula-201"),
        ("10.99.30.24", "aula-202"),
        ("10.99.30.25", "laboratorio-1"),
        ("10.99.30.26", "laboratorio-2"),
    ),
    "Demo · Oficina Windows": (
        ("10.99.40.31", "pc-direccion"),
        ("10.99.40.32", "pc-contabilidad"),
        ("10.99.40.33", "pc-rrhh"),
        ("10.99.40.34", "pc-secretaria"),
    ),
}

# (días atrás, operador, task_id, acción, valor, objetivo, estado, resultados)
_DEMO_BATCHES = (
    (
        0.001,
        "demo.maria",
        "zona-todas",
        "TZ",
        "America/Caracas",
        "10.99.10.0/24",
        "running",
        ["success", "success", "success"],
    ),
    (
        1,
        "demo.maria",
        "zona-todas",
        "TZ",
        "America/Caracas",
        "inventory:vedado",
        "success",
        ["success"] * 6,
    ),
    (
        2,
        "demo.luis",
        "apt-upgrade",
        "APT",
        "seguridad",
        "10.99.20.0/24",
        "partial",
        ["success", "success", "success", "failure", "attention"],
    ),
    (
        3,
        "demo.maria",
        "tapiz",
        "WALLPAPER",
        "institucional",
        "10.99.30.0/24",
        "success",
        ["success"] * 6,
    ),
    (
        5,
        "demo.carla",
        "firewall",
        "FIREWALL",
        "activar",
        "10.99.40.0/24",
        "failure",
        ["failure"] * 4,
    ),
    (
        7,
        "demo.luis",
        "zona-todas",
        "TIME",
        "2026-11-07 09:00",
        "10.99.10.11-10.99.10.16",
        "success",
        ["success"] * 6,
    ),
    (
        9,
        "demo.maria",
        "contrasena",
        "PASSWORD",
        "soporte",
        "10.99.20.0/24",
        "error",
        ["error", "error", "failure"],
    ),
    (
        11,
        "demo.carla",
        "apt-upgrade",
        "APT",
        "mantenimiento",
        "10.99.30.0/24",
        "preview",
        ["preview"] * 6,
    ),
    (
        13,
        "demo.luis",
        "zona-todas",
        "TZ",
        "America/Caracas",
        "10.99.10.0/24",
        "success",
        ["success"] * 6,
    ),
    (
        15,
        "demo.maria",
        "tapiz",
        "WALLPAPER",
        "institucional",
        "10.99.20.0/24",
        "cancelled",
        ["success", "skipped", "skipped", "skipped"],
    ),
    (
        17,
        "demo.carla",
        "zona-todas",
        "TIME",
        "2026-10-28 08:30",
        "10.99.40.0/24",
        "incomplete",
        ["skipped", "skipped", "skipped"],
    ),
    (
        19,
        "demo.luis",
        "zona-todas",
        "TZ",
        "America/Caracas",
        "10.99.30.0/24",
        "success",
        ["success"] * 5,
    ),
    (
        21,
        "demo.maria",
        "firewall",
        "FIREWALL",
        "desactivar",
        "10.99.10.0/24",
        "partial",
        ["success", "success", "success", "failure"],
    ),
    (
        24,
        "demo.carla",
        "apt-upgrade",
        "APT",
        "seguridad",
        "10.99.40.0/24",
        "success",
        ["success"] * 4,
    ),
)

_DEMO_EVENTS = (
    (
        0.04,
        "demo.maria",
        "10.99.20.11",
        "login_success",
        "[demo] Acceso desde la red corporativa",
    ),
    (
        0.20,
        "demo.luis",
        "10.99.20.12",
        "login_success",
        "[demo] Acceso desde la red corporativa",
    ),
    (
        0.30,
        "demo.carla",
        "10.99.20.13",
        "login_failure",
        "[demo] Contraseña incorrecta (1/5)",
    ),
    (
        1.10,
        "demo.maria",
        "10.99.20.11",
        "dry_run_on",
        "[demo] Simulación activada para revisar el lote",
    ),
    (1.12, "demo.maria", "10.99.20.11", "dry_run_off", "[demo] Simulación desactivada"),
    (
        2.40,
        "demo.luis",
        "10.99.20.12",
        "login_success",
        "[demo] Acceso tras cambiar la contraseña",
    ),
    (
        4.00,
        "demo.carla",
        "10.99.20.13",
        "account_locked",
        "[demo] Cuenta bloqueada temporalmente",
    ),
    (6.25, "demo.maria", "10.99.20.11", "login_success", "[demo] Acceso desde VPN"),
)


def _default_db_path() -> str:
    from acc import runtime

    return str(runtime.get_db_path())


def demo_data_present(db_path: str | None = None) -> bool:
    """Indica si la base ya contiene datos de demostración."""
    db_path = db_path or _default_db_path()
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM inventory_groups WHERE name LIKE ?) +
                (SELECT COUNT(*) FROM execution_batches WHERE operator LIKE ?) +
                (SELECT COUNT(*) FROM schedules WHERE name LIKE ?) AS total
            """,
            (f"{DEMO_PREFIX}%", f"{DEMO_OPERATOR_PREFIX}%", f"{DEMO_PREFIX}%"),
        ).fetchone()
    return bool(row["total"])


def purge_demo_data(db_path: str | None = None) -> dict[str, int]:
    """Elimina solo las filas creadas por ``seed_demo_data``."""
    db_path = db_path or _default_db_path()
    batch_like = f"{DEMO_BATCH_PREFIX}%"
    with get_connection(db_path) as conn:
        batches = conn.execute(
            "DELETE FROM execution_batches WHERE batch_id LIKE ? OR operator LIKE ?",
            (batch_like, f"{DEMO_OPERATOR_PREFIX}%"),
        ).rowcount
        approvals = conn.execute(
            "DELETE FROM approvals WHERE requested_by LIKE ?",
            (f"{DEMO_OPERATOR_PREFIX}%",),
        ).rowcount
        schedules = conn.execute(
            "DELETE FROM schedules WHERE name LIKE ?", (f"{DEMO_PREFIX}%",)
        ).rowcount
        # ``inventory_hosts`` cae por la clave foránea ON DELETE CASCADE.
        groups = conn.execute(
            "DELETE FROM inventory_groups WHERE name LIKE ?", (f"{DEMO_PREFIX}%",)
        ).rowcount
        events = conn.execute(
            "DELETE FROM auth_events WHERE username LIKE ? OR detail LIKE ?",
            (f"{DEMO_OPERATOR_PREFIX}%", "%[demo]%"),
        ).rowcount
    return {
        "batches": int(batches or 0),
        "approvals": int(approvals or 0),
        "schedules": int(schedules or 0),
        "groups": int(groups or 0),
        "events": int(events or 0),
    }


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _insert_group_hosts(db_path: str, group: HostGroup, hosts) -> int:
    now = _iso(datetime.now(UTC))
    with get_connection(db_path) as conn:
        for address, label in hosts:
            conn.execute(
                """
                INSERT INTO inventory_hosts
                    (group_id, address, label, created_at, criticality)
                VALUES (?, ?, ?, ?, '')
                ON CONFLICT(group_id, address) DO NOTHING
                """,
                (group.id, address, label, now),
            )
    return len(hosts)


def _result_payload(
    result: str, action: str, value: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    before: dict[str, Any] = {"status": "pendiente"}
    after: dict[str, Any] = {"status": "aplicado"}
    if action == "TZ":
        before = {"timezone": "UTC", "ntp": "sin sincronizar"}
        after = {"timezone": value, "ntp": "sincronizado"}
    elif action == "TIME":
        before = {"hora": "desviada +3m 12s", "rtc": "local"}
        after = {"hora": "sincronizada", "rtc": "UTC"}
    elif action == "WALLPAPER":
        before = {"tapiz": "predeterminado"}
        after = {"tapiz": value}
    elif action == "APT":
        before = {"paquetes_actualizables": 37, "reinicio": "no"}
        after = {
            "paquetes_actualizables": 0,
            "reinicio": "sí" if result == "success" else "pendiente",
        }
    elif action == "PASSWORD":
        before = {"usuario": value, "hash": "sha512"}
        after = {"usuario": value, "hash": "sha512", "rotada": True}
    elif action == "FIREWALL":
        before = {"ufw": "inactivo" if value == "activar" else "activo"}
        after = {"ufw": "activo" if value == "activar" else "inactivo"}
    if result == "failure":
        after = {"status": "sin cambios", "motivo": "host inaccesible"}
    elif result == "error":
        after = {"status": "error", "motivo": "tiempo de espera agotado"}
    elif result == "skipped":
        after = {"status": "omitido", "motivo": "presupuesto de la ventana agotado"}
    elif result == "preview":
        before = {"status": "actual"}
        after = {"status": "cambiaría"}
    elif result == "attention":
        after = {"status": "aplicado", "aviso": "requiere reinicio manual"}
    return before, after


def _hosts_for_batch(index: int, count: int) -> list[str]:
    """Direcciones ficticias estables por lote, dentro de 10.99.0.0/16."""
    subnet = 10 + (index % 4) * 10
    return [f"10.99.{subnet}.{50 + index * 3 + offset}" for offset in range(count)]


def _create_demo_batches(
    db_path: str, group_ids: list[int], with_running: bool = True
) -> tuple[int, int]:
    specs = tuple(
        item for item in _DEMO_BATCHES if with_running or item[6] != "running"
    )
    host_total = 0
    now = datetime.now(UTC)
    for index, (
        days_ago,
        operator,
        task_id,
        action,
        value,
        target,
        status,
        results,
    ) in enumerate(specs):
        if target == "inventory:vedado":
            target = f"inventory:{group_ids[0]}"
        batch_id = f"{DEMO_BATCH_PREFIX}{uuid.uuid4().hex[:10]}"
        created = now - timedelta(days=days_ago, minutes=index * 7 + 3)
        total = 6 if status == "running" else len(results)
        running = status == "running"
        ExecutionBatch.create_batch(
            batch_id=batch_id,
            operator=operator,
            action=action,
            value=value,
            target=target,
            time_mode="utc",
            ntp_policy="sync" if action in {"TZ", "TIME"} else "keep",
            total_hosts=total,
            status="running" if running else "success",
            dry_run=(index == 8),
            admin_user="demo.admin",
            password_origin="vault" if action == "PASSWORD" else "",
            ntp_servers=(
                ["ntp.etecsa.cu", "pool.ntp.org"] if action in {"TZ", "TIME"} else []
            ),
            sync_rtc=action == "TIME",
            time_zone=value if action == "TZ" else "",
            task_id=task_id,
            db_path=db_path,
        )
        hosts = _hosts_for_batch(index, total)
        for position, (host, result) in enumerate(
            zip(hosts, results, strict=False), start=1
        ):
            before, after = _result_payload(result, action, value)
            error = ""
            if result == "failure":
                error = "Connection timed out"
            elif result == "error":
                error = "ansible: UNREACHABLE"
            ExecutionBatch.add_host_result(
                batch_id,
                host,
                result,
                error=error,
                duration_ms=850 + position * 137,
                before_state=before,
                after_state=after,
                password_hash=(
                    "demo" if action == "PASSWORD" and result == "success" else ""
                ),
                password_origin="vault" if action == "PASSWORD" else "",
                position=position,
                db_path=db_path,
            )
            host_total += 1
        if not running:
            ExecutionBatch.finish_batch(batch_id, status=status, db_path=db_path)

        finished = created + timedelta(minutes=2 + index % 5, seconds=17 + index)
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE execution_batches SET created_at = ?, finished_at = ? WHERE batch_id = ?",
                (_iso(created), None if running else _iso(finished), batch_id),
            )
            conn.execute(
                "UPDATE execution_hosts SET created_at = ? WHERE batch_id = ?",
                (_iso(created + timedelta(seconds=20)), batch_id),
            )
    return len(_DEMO_BATCHES), host_total


def _create_demo_schedules(db_path: str, group_ids: list[int], batch_id: str) -> int:
    now = datetime.now(UTC)
    specs = (
        (
            "Demo · Zona horaria sucursales",
            "zona-todas",
            now + timedelta(days=1, hours=2),
            "once",
            {
                "target": f"inventory:{group_ids[0]}",
                "target_group_id": group_ids[0],
                "target_mode": "group",
                "admin_user": "root",
                "action": "TZ",
                "value": "America/Caracas",
                "ntp_policy": "sync",
            },
        ),
        (
            "Demo · Tapiz institucional aulas",
            "tapiz",
            now + timedelta(days=2, hours=1),
            "weekly",
            {
                "target": f"inventory:{group_ids[2]}",
                "target_group_id": group_ids[2],
                "target_mode": "group",
                "action": "WALLPAPER",
                "value": "institucional",
            },
        ),
        (
            "Demo · Actualización APT centro",
            "apt-upgrade",
            now + timedelta(days=3),
            "daily",
            {
                "target": f"inventory:{group_ids[1]}",
                "target_group_id": group_ids[1],
                "target_mode": "group",
                "action": "APT",
                "value": "seguridad",
                "min_free_gb": 2.0,
            },
        ),
        (
            "Demo · Firewall oficina Windows",
            "firewall",
            now + timedelta(days=5, hours=4),
            "weekly",
            {
                "target": f"inventory:{group_ids[3]}",
                "target_group_id": group_ids[3],
                "target_mode": "group",
                "action": "FIREWALL",
                "value": "activar",
            },
        ),
    )
    count = 0
    for name, task_id, run_at, recurrence, params in specs:
        schedule = Schedule.create(
            name,
            task_id,
            run_at,
            params,
            recurrence=recurrence,
            created_by="demo.maria",
            db_path=db_path,
        )
        # Inactivas: el planificador no debe tocar direcciones ficticias.
        Schedule.set_enabled(schedule.id, False, db_path=db_path)
        count += 1
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE schedules SET last_run = ?, last_batch = ? WHERE name = ?",
            (
                _iso(now - timedelta(days=1, hours=3)),
                batch_id,
                "Demo · Zona horaria sucursales",
            ),
        )
    return count


def _create_demo_approvals(db_path: str, group_ids: list[int]) -> int:
    now = datetime.now(UTC)
    pending = (
        (
            "zona-todas",
            {
                "target": f"inventory:{group_ids[0]}",
                "target_group_id": group_ids[0],
                "target_mode": "group",
                "admin_user": "root",
                "action": "TZ",
                "value": "America/Caracas",
                "ntp_policy": "sync",
            },
        ),
        (
            "firewall",
            {
                "target": f"inventory:{group_ids[3]}",
                "target_group_id": group_ids[3],
                "target_mode": "group",
                "action": "FIREWALL",
                "value": "activar",
            },
        ),
    )
    created = []
    for task_id, params in pending:
        created.append(
            ApprovalRequest.create(
                task_id,
                params,
                "demo.maria",
                payload={"target": params["target"], "demo": True},
                db_path=db_path,
            )
        )
    # Una decisión histórica para que la pestaña «Decididas» no esté vacía.
    decided = ApprovalRequest.create(
        "contrasena",
        {
            "target": "10.99.20.11",
            "target_mode": "manual",
            "admin_user": "root",
            "action": "PASSWORD",
            "target_user": "soporte",
        },
        "demo.luis",
        payload={"target": "10.99.20.11", "demo": True},
        db_path=db_path,
    )
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE approvals SET status = 'approved', approver = 'admin', "
            "decided_at = ?, note = 'Aprobada en la ventana del martes.' WHERE id = ?",
            (_iso(now - timedelta(days=2)), decided.id),
        )
    rejected = ApprovalRequest.create(
        "tapiz",
        {
            "target": "10.99.30.21",
            "target_mode": "manual",
            "action": "WALLPAPER",
            "value": "institucional",
        },
        "demo.carla",
        payload={"target": "10.99.30.21", "demo": True},
        db_path=db_path,
    )
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE approvals SET status = 'rejected', approver = 'admin', "
            "decided_at = ?, note = 'Fuera de la ventana autorizada.' WHERE id = ?",
            (_iso(now - timedelta(days=4)), rejected.id),
        )
    return len(created) + 2


def _create_demo_events(db_path: str) -> int:
    now = datetime.now(UTC)
    with get_connection(db_path) as conn:
        # Del más antiguo al más reciente para que el orden por ``id DESC``
        # de la auditoría coincida con el orden temporal.
        for days_ago, username, ip, event, detail in reversed(_DEMO_EVENTS):
            conn.execute(
                "INSERT INTO auth_events (timestamp, username, ip, event, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (_iso(now - timedelta(days=days_ago)), username, ip, event, detail),
            )
    return len(_DEMO_EVENTS)


def seed_demo_data(
    db_path: str | None = None,
    with_running: bool = True,
    reset: bool = False,
) -> dict[str, Any]:
    """Inserta el conjunto de datos ficticios y devuelve el resumen.

    Si ya existen datos de demostración y ``reset`` es falso, lanza
    ``ValueError`` para no duplicarlos. ``with_running`` permite omitir el
    lote en curso (por ejemplo, en una base recién creada para capturas).
    """
    db_path = db_path or _default_db_path()
    if demo_data_present(db_path):
        if not reset:
            raise ValueError(
                "Ya hay datos de demostración. Usa --reset para recrearlos "
                "o --purge para eliminarlos."
            )
        purge_demo_data(db_path)

    groups: dict[str, HostGroup] = {}
    host_total = 0
    for (
        name,
        description,
        os_flavor,
        criticality,
        days,
        start,
        end,
        note,
    ) in _DEMO_GROUPS:
        group = HostGroup.create(
            name,
            description,
            os_flavor=os_flavor,
            criticality=criticality,
            window_days=days,
            window_start=start,
            window_end=end,
            window_note=note,
            db_path=db_path,
        )
        groups[name] = group
        host_total += _insert_group_hosts(db_path, group, _DEMO_HOSTS[name])

    group_ids = [group.id for group in groups.values()]
    batch_total, host_results = _create_demo_batches(
        db_path, group_ids, with_running=with_running
    )
    first_batch = ExecutionBatch.get_batches(limit=1, db_path=db_path)
    batch_id = first_batch[0].batch_id if first_batch else ""
    schedule_total = _create_demo_schedules(db_path, group_ids, batch_id)
    approval_total = _create_demo_approvals(db_path, group_ids)
    event_total = _create_demo_events(db_path)
    return {
        "groups": len(groups),
        "hosts": host_total,
        "batches": batch_total,
        "host_results": host_results,
        "schedules": schedule_total,
        "approvals": approval_total,
        "events": event_total,
    }
