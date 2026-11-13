"""Consola de ejecucion: lineas saneadas, progreso y omision de hosts.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from acc.operations import _state_bool
from acc.task_state import update_task
from models.batch import ExecutionBatch

_batch_console_lock = threading.RLock()


def _append_log(lines: list[str], message: str, limit: int = 200) -> None:
    """Añade una línea al log de progreso manteniendo un tamaño acotado."""
    with _batch_console_lock:
        lines.append(message)
        if len(lines) > limit:
            del lines[: len(lines) - limit]


_CONSOLE_MAX_LINES = 300


_CONSOLE_MAX_LINE_LENGTH = 500


_STATE_B64_RE = re.compile(r"(?:HORA|TAPIZ|APT)_[A-Z_]+_B64=")


_STATE_JSON_STATE_RE = re.compile(r"(?:HORA|TAPIZ|APT)_(?:STATE|BEFORE)=\{")


def _sanitize_console_line(line: str, secrets: list[str]) -> str:
    """Normaliza una línea de Ansible y oculta secretos conocidos.

    También compacta los marcadores base64 de estado, que son muy largos y no
    aportan nada a la consola: el estado ya se muestra en el detalle del lote.
    """
    text = line.replace("\r", "").replace("\n", "")
    text = "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32)
    for secret in secrets:
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    if _STATE_B64_RE.search(text) or _STATE_JSON_STATE_RE.search(text):
        return "[marcador de estado del host recibido]"
    text = text.rstrip()
    if len(text) > _CONSOLE_MAX_LINE_LENGTH:
        text = text[:_CONSOLE_MAX_LINE_LENGTH] + "…"
    return text


def _trim_console(console: list[str]) -> None:
    if len(console) > _CONSOLE_MAX_LINES:
        del console[: len(console) - _CONSOLE_MAX_LINES]


def _append_console(console: list[str], tid: str, message: str, host: str = "") -> None:
    """Añade una línea de resumen a la consola y actualiza la tarea."""
    text = f"[{host}] {message}" if host else message
    with _batch_console_lock:
        console.append(text)
        _trim_console(console)
        snapshot = list(console)
    update_task(tid, {"console": snapshot})


def _make_console_callback(tid: str, host: str, console: list[str], secrets: list[str]):
    """Crea el callback que recibe la salida de Ansible en tiempo real."""
    last_update = [0.0]

    def callback(raw_line: str) -> None:
        text = _sanitize_console_line(raw_line, secrets)
        if not text:
            return
        with _batch_console_lock:
            console.append(f"[{host}] {text}")
            _trim_console(console)
            snapshot = list(console)
        # No actualizar en cada línea: el SSE reenvía el estado completo.
        now = time.monotonic()
        if now - last_update[0] >= 0.25:
            last_update[0] = now
            update_task(tid, {"console": snapshot})

    return callback


def _progress_for(index: int, total: int, start: int = 12, span: int = 85) -> int:
    """Progreso proporcional al número de hosts ya procesados."""
    if total <= 0:
        return start
    return min(start + int(index / total * span), start + span)


def _skip_remaining_hosts(
    tid: str,
    batch_id: str,
    hosts: list[str],
    db_path: str,
    log_lines: list[str],
    console_lines: list[str],
    reason: str = "Presupuesto de tiempo del lote agotado.",
) -> None:
    """Registra como omitidos los hosts que no se llegaron a procesar.

    Así el lote cierra con todos sus hosts contabilizados en vez de quedar
    abierto indefinidamente o perder los pendientes.
    """
    for host in hosts:
        ExecutionBatch.add_host_result(
            batch_id=batch_id,
            host=host,
            result="skipped",
            error=reason,
            db_path=db_path,
        )
        _append_log(log_lines, f"OMITIDO {host}: {reason}")
        _append_console(console_lines, tid, f"OMITIDO: {reason}", host=host)
    update_task(tid, {"log": list(log_lines), "console": list(console_lines)})


def _ntp_keep_warning(action: str, ntp_policy: str, state: Any) -> str:
    """Aviso cuando ``TIME`` se aplica con NTP activo y política ``keep``."""
    if (
        action == "TIME"
        and ntp_policy == "keep"
        and isinstance(state, dict)
        and _state_bool(state.get("ntp"))
    ):
        return (
            "TIME con NTP activo y política keep: el reloj puede volver a "
            "sincronizarse. Usa off, restore o configure."
        )
    return ""
