"""Reglas de seguridad operativa: criticidad y ventanas de mantenimiento.

La idea es proporcional al riesgo: un host ``critical`` solo se modifica dentro
de su ventana (o con una excepción explícita y auditada); un host ``high``
fuera de ventana genera aviso, pero no bloqueo.

Las ventanas se interpretan siempre en **UTC** para que coincidan con las
programaciones (que también se guardan en UTC). El día ISO es 1=lunes … 7=domingo.
Una ventana con ``inicio > fin`` cruza medianoche (p. ej. 22:00–06:00).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CRITICALITY_LEVELS = ("low", "medium", "high", "critical")
CRITICALITY_LABELS = {
    "low": "Baja",
    "medium": "Media",
    "high": "Alta",
    "critical": "Crítica",
}
DEFAULT_CRITICALITY = "medium"

_WEEKDAY_LABELS = {
    1: "Lun",
    2: "Mar",
    3: "Mié",
    4: "Jue",
    5: "Vie",
    6: "Sáb",
    7: "Dom",
}
_WEEKDAY_ALIASES = {
    "lun": 1,
    "mon": 1,
    "mar": 2,
    "tue": 2,
    "mie": 3,
    "mié": 3,
    "wed": 3,
    "jue": 4,
    "thu": 4,
    "vie": 5,
    "fri": 5,
    "sab": 6,
    "sáb": 6,
    "sat": 6,
    "dom": 7,
    "sun": 7,
}


def normalize_criticality(
    value: Any, default: str = DEFAULT_CRITICALITY
) -> str:
    """Normaliza un nivel de criticidad; lanza ``ValueError`` si no es válido."""
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw not in CRITICALITY_LEVELS:
        raise ValueError(
            "Criticidad no válida; usa low, medium, high o critical."
        )
    return raw


def normalize_criticality_override(value: Any) -> str:
    """Criticidad de un host: cadena vacía significa «heredar del grupo»."""
    raw = str(value or "").strip().lower()
    if raw in {"", "inherit", "heredar", "grupo"}:
        return ""
    return normalize_criticality(raw)


def parse_window_days(value: Any) -> Tuple[int, ...]:
    """Convierte ``"1,2,3"`` (o nombres) en días ISO ordenados y únicos."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = str(value).replace(";", ",").split(",")

    days = set()
    for part in parts:
        token = part.strip()
        if not token:
            continue
        if token.isdigit():
            day = int(token)
        else:
            day = _WEEKDAY_ALIASES.get(token.lower()[:3], 0)
        if day < 1 or day > 7:
            raise ValueError(
                "Día de ventana no válido; usa 1 (lunes) a 7 (domingo)."
            )
        days.add(day)
    return tuple(sorted(days))


def parse_window_time(value: Any) -> str:
    """Normaliza ``HH:MM``; cadena vacía si no hay valor."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Hora de ventana no válida; usa HH:MM (UTC).") from exc
    return parsed.strftime("%H:%M")


def window_error(
    days: Sequence[int], start: str, end: str
) -> Optional[str]:
    """Valida una ventana completa; ``None`` si es válida o está vacía."""
    if not days and not start and not end:
        return None
    if not days:
        return "La ventana de mantenimiento requiere al menos un día."
    if not start or not end:
        return "La ventana requiere hora de inicio y hora de fin."
    if start == end:
        return "La hora de inicio y la de fin no pueden ser iguales."
    return None


def format_days(days: Sequence[int]) -> str:
    return ", ".join(_WEEKDAY_LABELS.get(int(day), str(day)) for day in days)


def window_label(
    days: Sequence[int], start: str, end: str
) -> str:
    """Etiqueta legible; texto vacío si no hay ventana configurada."""
    if not days or not start or not end:
        return ""
    return f"{format_days(days)} {start}–{end} UTC"


def _as_utc(moment: Optional[datetime]) -> datetime:
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def is_window_open(
    days: Sequence[int],
    start: str,
    end: str,
    now: Optional[datetime] = None,
) -> Optional[bool]:
    """Indica si ``now`` cae dentro de la ventana; ``None`` si no hay ventana."""
    if not days or not start or not end:
        return None

    moment = _as_utc(now)
    start_time = datetime.strptime(start, "%H:%M").time()
    end_time = datetime.strptime(end, "%H:%M").time()
    current = moment.time().replace(second=0, microsecond=0)
    today = moment.isoweekday()
    day_set = {int(day) for day in days}

    if start_time <= end_time:
        return today in day_set and start_time <= current <= end_time

    # Ventana nocturna: empieza hoy y termina mañana, o empezó ayer.
    if current >= start_time:
        return today in day_set
    if current <= end_time:
        return (today - 1) in day_set or (today == 1 and 7 in day_set)
    return False


def next_window_open(
    days: Sequence[int],
    start: str,
    end: str,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Primera apertura futura de la ventana (búsqueda de hasta 8 días)."""
    if not days or not start or not end:
        return None

    moment = _as_utc(now)
    start_time = datetime.strptime(start, "%H:%M").time()
    day_set = {int(day) for day in days}

    for offset in range(0, 9):
        candidate_day = moment.date() + timedelta(days=offset)
        candidate = datetime.combine(
            candidate_day, start_time, tzinfo=timezone.utc
        )
        if candidate.isoweekday() not in day_set:
            continue
        if candidate > moment:
            return candidate
    return None


def evaluate_safety(
    *,
    group_name: str = "",
    group_criticality: str = DEFAULT_CRITICALITY,
    hosts: Optional[Iterable[Dict[str, Any]]] = None,
    window_days: Any = "",
    window_start: Any = "",
    window_end: Any = "",
    window_note: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Evalúa criticidad y ventana de un objetivo.

    Devuelve un diccionario estable con ``blocked``, ``level`` (ok/warn/block),
    ``reasons``, ``warnings``, listas de hosts críticos/altos y la etiqueta de
    la ventana. No lanza por datos heredados inválidos: los degrada a aviso.
    """
    try:
        days = parse_window_days(window_days)
        start = parse_window_time(window_start)
        end = parse_window_time(window_end)
        configured = bool(days and start and end)
        label = window_label(days, start, end) if configured else ""
        open_now = is_window_open(days, start, end, now) if configured else None
    except ValueError:
        days, start, end = (), "", ""
        configured = False
        label = ""
        open_now = None

    try:
        base_level = normalize_criticality(group_criticality)
    except ValueError:
        base_level = DEFAULT_CRITICALITY

    critical_hosts: List[str] = []
    high_hosts: List[str] = []
    for host in hosts or []:
        address = str(host.get("address") or "")
        raw_level = host.get("criticality") or base_level
        try:
            level = normalize_criticality(raw_level, default=base_level)
        except ValueError:
            level = base_level
        if level == "critical":
            critical_hosts.append(address)
        elif level == "high":
            high_hosts.append(address)

    reasons: List[str] = []
    warnings: List[str] = []
    blocked = False
    next_open = None
    objective = f"el grupo '{group_name}'" if group_name else "el objetivo"

    if critical_hosts:
        if not configured:
            warnings.append(
                f"{len(critical_hosts)} host(s) crítico(s) en {objective} "
                "sin ventana de mantenimiento definida."
            )
        elif not open_now:
            blocked = True
            next_open = next_window_open(days, start, end, now)
            detail = (
                f" Próxima apertura: {next_open.isoformat(timespec='minutes')}."
                if next_open
                else ""
            )
            reasons.append(
                f"{len(critical_hosts)} host(s) crítico(s) en {objective} "
                f"fuera de la ventana {label}.{detail}"
            )

    if high_hosts and configured and not open_now:
        warnings.append(
            f"{len(high_hosts)} host(s) de criticidad alta en {objective} "
            f"fuera de la ventana {label}."
        )
    elif high_hosts and not configured:
        warnings.append(
            f"{len(high_hosts)} host(s) de criticidad alta en {objective} "
            "sin ventana de mantenimiento definida."
        )

    if configured and not critical_hosts and not high_hosts and not open_now:
        warnings.append(
            f"{objective.capitalize()} está fuera de la ventana {label}, "
            "pero no contiene hosts críticos ni de criticidad alta."
        )

    if blocked:
        level = "block"
    elif warnings:
        level = "warn"
    else:
        level = "ok"

    return {
        "blocked": blocked,
        "level": level,
        "reasons": reasons,
        "warnings": warnings,
        "critical_hosts": critical_hosts,
        "high_hosts": high_hosts,
        "group_criticality": base_level,
        "window_configured": configured,
        "window_label": label,
        "window_note": str(window_note or ""),
        "inside_window": open_now,
        "next_open": (
            next_open.isoformat(timespec="minutes") if next_open else ""
        ),
        "summary": (
            reasons[0]
            if reasons
            else (warnings[0] if warnings else "Dentro de la política de criticidad y ventana.")
        ),
    }
