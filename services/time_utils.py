# services/time_utils.py
# Utilidades de zona horaria basadas en ``zoneinfo``.
#
# Se usan para validar que una zona existe de verdad (no solo que su nombre
# tenga forma válida) y para detectar horas locales inexistentes o ambiguas
# en los cambios de horario de verano (DST).
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Estados posibles al validar una hora de pared local.
LOCAL_TIME_OK = "ok"
LOCAL_TIME_MISSING = "missing"
LOCAL_TIME_AMBIGUOUS = "ambiguous"
LOCAL_TIME_INVALID = "invalid"


def timezone_exists(name: str) -> bool:
    """Indica si ``name`` es una zona horaria IANA conocida por el sistema."""
    value = (name or "").strip()
    if not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def parse_local_datetime(value: str) -> Optional[datetime]:
    """Convierte ``AAAA-MM-DDTHH:MM`` en un ``datetime`` naive."""
    try:
        return datetime.strptime(value.replace(" ", "T"), "%Y-%m-%dT%H:%M")
    except (AttributeError, TypeError, ValueError):
        return None


def local_time_status(
    value: str,
    timezone_name: str,
    fold: Optional[int] = None,
) -> Tuple[str, str]:
    """Clasifica una hora local en ``ok``, ``missing`` o ``ambiguous``.

    ``fold`` solo es necesario para desambiguar el segundo pase de una hora
    repetida (otoño). Devuelve ``(estado, mensaje)`` con un mensaje listo para
    mostrar al operador.
    """
    parsed = parse_local_datetime(value)
    if parsed is None:
        return LOCAL_TIME_INVALID, "Formato de hora inválido; use AAAA-MM-DDTHH:MM."
    if not timezone_exists(timezone_name):
        return LOCAL_TIME_INVALID, f"Zona horaria desconocida: {timezone_name}."

    tz = ZoneInfo(timezone_name)
    first = parsed.replace(tzinfo=tz, fold=0)
    second = parsed.replace(tzinfo=tz, fold=1)

    # Si al pasar a UTC y volver a la zona la hora cambia, estamos ante un
    # hueco de primavera: esa hora de pared no existe en el calendario local.
    roundtrip = first.astimezone(ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None)
    if roundtrip != parsed:
        return (
            LOCAL_TIME_MISSING,
            f"La hora {value} no existe en {timezone_name} por el cambio de "
            "horario de verano. Elige otra hora.",
        )

    if first.utcoffset() != second.utcoffset():
        if fold not in (0, 1):
            return (
                LOCAL_TIME_AMBIGUOUS,
                f"La hora {value} es ambigua en {timezone_name} (se repite por "
                "el cambio de horario). Indica si corresponde a la primera o a "
                "la segunda ocurrencia.",
            )
        return LOCAL_TIME_OK, ""

    return LOCAL_TIME_OK, ""


def local_time_epoch(
    value: str,
    timezone_name: str,
    fold: int = 0,
) -> Optional[int]:
    """Convierte una hora de pared local en epoch usando ``zoneinfo``.

    Devuelve ``None`` si la hora o la zona no son válidas. Se usa tanto en la
    vista previa como en la ejecución para que ambas calculen exactamente el
    mismo instante, sin depender de la base de datos horaria del host.
    """
    parsed = parse_local_datetime(value)
    if parsed is None or not timezone_exists(timezone_name):
        return None
    try:
        aware = parsed.replace(
            tzinfo=ZoneInfo(timezone_name), fold=1 if fold == 1 else 0
        )
        return int(aware.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def utc_time_epoch(value: str) -> Optional[int]:
    """Convierte una hora UTC ``AAAA-MM-DDTHH:MM`` en epoch."""
    parsed = parse_local_datetime(value)
    if parsed is None:
        return None
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())
