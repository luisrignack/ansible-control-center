"""Notificación por correo de resultados de lotes (opcional).

Configuración por entorno (ver ``config.py``):

- ``ACC_SMTP_HOST``      host SMTP; vacío desactiva el notificador.
- ``ACC_SMTP_PORT``      puerto (25 por defecto, 587 típico con STARTTLS).
- ``ACC_SMTP_USER`` / ``ACC_SMTP_PASSWORD``  credenciales opcionales.
- ``ACC_SMTP_FROM``      remitente.
- ``ACC_SMTP_STARTTLS``  activar STARTTLS (por defecto en 587).
- ``ACC_NOTIFY_TO``      lista de destinatarios separados por comas.
- ``ACC_NOTIFY_LEVEL``   ``off`` (por defecto), ``failures`` o ``all``.

Nunca lanza excepciones hacia el flujo del lote: un fallo de correo se
registra en el log y no debe afectar el resultado de la operación.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Optional

logger = logging.getLogger("acc")


def smtp_enabled() -> bool:
    from config import NOTIFY_TO, SMTP_HOST

    return bool(SMTP_HOST) and bool(NOTIFY_TO)


def notify_configured_level() -> str:
    from config import NOTIFY_LEVEL

    return NOTIFY_LEVEL if NOTIFY_LEVEL in {"off", "failures", "all"} else "off"


def _send(subject: str, body: str) -> Optional[str]:
    """Envía el correo. Devuelve ``None`` o una cadena con el error."""
    from config import (
        NOTIFY_TO,
        SMTP_FROM,
        SMTP_HOST,
        SMTP_PASSWORD,
        SMTP_PORT,
        SMTP_STARTTLS,
        SMTP_USER,
    )

    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = SMTP_FROM or SMTP_USER or "acc@localhost"
        message["To"] = ", ".join(NOTIFY_TO)
        message.set_content(body)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            if SMTP_STARTTLS:
                smtp.starttls(context=ssl.create_default_context())
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD or "")
            smtp.send_message(message)
    except Exception as exc:
        logger.warning("No se pudo enviar la notificación por correo: %s", exc)
        return str(exc)
    return None


def batch_has_issues(batch: Any) -> bool:
    """Indica si el lote presenta algo que merece aviso (fallo, error u omisión)."""
    return bool(
        batch
        and (
            getattr(batch, "failure_count", 0)
            or getattr(batch, "error_count", 0)
            or getattr(batch, "skipped_count", 0)
        )
    )


def _summary(batch: Any) -> str:
    parts = {
        "ok": getattr(batch, "success_count", 0),
        "fallo": getattr(batch, "failure_count", 0),
        "error": getattr(batch, "error_count", 0),
        "omitidos": getattr(batch, "skipped_count", 0),
        "previsualizados": getattr(batch, "preview_count", 0),
    }
    summary = ", ".join(
        f"{count} {label}" for label, count in parts.items() if count
    )
    return summary or "sin host"


def notify_batch_result(batch: Any, host_results: Any) -> Optional[str]:
    """Envía el resumen del lote si la configuración lo pide.

    ``batch`` es un ``ExecutionBatch`` y ``host_results`` la lista de
    ``HostResult`` ya consultada por el flujo del lote (o el resultado de
    ``ExecutionBatch.get_host_results``). Devuelve ``None`` cuando no hay
    nada que enviar o el envío sale bien; devuelve el error si el SMTP falló.
    """
    if batch is None:
        return None
    level = notify_configured_level()
    if level == "off" or not smtp_enabled():
        return None
    is_preview = getattr(batch, "status", "") == "preview"
    if is_preview and level != "all":
        return None
    if level == "failures" and not batch_has_issues(batch):
        return None

    label = "Vista previa" if is_preview else "Lote"
    subject = (
        f"[ACC] {label} {getattr(batch, 'batch_id', '?')[:8]}"
        f" — {_summary(batch)}"
    )
    lines = [
        f"{label}: {_summary(batch)}",
        f"Operador: {getattr(batch, 'operator', '—')}",
        f"Objetivo: {getattr(batch, 'target', '—')}",
        f"Acción: {getattr(batch, 'action', '—')}"
        f" → {getattr(batch, 'value', '—') or '—'}",
        f"Simulación: {'sí' if getattr(batch, 'dry_run', False) else 'no'}",
    ]
    problem_lines = [
        f"  {getattr(res, 'host', '?')}: {getattr(res, 'result', '?')}"
        f" — {getattr(res, 'error', '')}"[:200]
        for res in (host_results or [])
        if getattr(res, "result", "") in {"failure", "error", "skipped"}
    ]
    if problem_lines:
        lines.append("")
        lines.append("Hosts con problema:")
        lines.extend(problem_lines[:50])
    return _send(subject, "\n".join(lines))
