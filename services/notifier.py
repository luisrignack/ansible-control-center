"""Notificación por correo de resultados de lotes (opcional).

Configuración por entorno (ver ``config.py``):

- ``ACC_SMTP_HOST``      host SMTP; vacío desactiva el notificador.
- ``ACC_SMTP_PORT``      puerto (25 por defecto, 587 típico con STARTTLS).
- ``ACC_SMTP_USER`` / ``ACC_SMTP_PASSWORD``  credenciales opcionales.
- ``ACC_SMTP_FROM``      remitente.
- ``ACC_SMTP_STARTTLS``  activar STARTTLS (por defecto en 587).
- ``ACC_SMTP_SSL``       TLS implícito tipo SMTPS (por defecto en 465).
- ``ACC_NOTIFY_TO``      lista de destinatarios separados por comas.
- ``ACC_NOTIFY_LEVEL``   ``off`` (por defecto), ``failures`` o ``all``.

Con credenciales SMTP, el envío exige cifrado: si no hay STARTTLS ni TLS
implícito se rechaza y se devuelve el motivo, para no enviar la contraseña en
claro. El fallo se registra en el log y en la consola del lote; nunca tumba la
ejecución.

Nunca lanza excepciones hacia el flujo del lote: un fallo de correo se
registra en el log y no debe afectar el resultado de la operación.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger("acc")


def smtp_enabled() -> bool:
    from config import NOTIFY_TO, SMTP_HOST

    return bool(SMTP_HOST) and bool(NOTIFY_TO)


def notify_configured_level() -> str:
    from config import NOTIFY_LEVEL

    return NOTIFY_LEVEL if NOTIFY_LEVEL in {"off", "failures", "all"} else "off"


def _send(subject: str, body: str) -> str | None:
    """Envía el correo. Devuelve ``None`` o una cadena con el error."""
    from config import (
        NOTIFY_TO,
        SMTP_FROM,
        SMTP_HOST,
        SMTP_PASSWORD,
        SMTP_PORT,
        SMTP_SSL,
        SMTP_STARTTLS,
        SMTP_USER,
    )

    # Fail-closed: con credenciales, la conexión debe ir cifrada. Sin esta
    # comprobación, un puerto 25 con usuario enviaría la contraseña en claro.
    if SMTP_USER and not (SMTP_STARTTLS or SMTP_SSL):
        error = (
            "credenciales SMTP sin cifrado: activa ACC_SMTP_STARTTLS "
            "(587) o ACC_SMTP_SSL (465)"
        )
        logger.warning("No se envía la notificación: %s", error)
        return error

    try:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = SMTP_FROM or SMTP_USER or "acc@localhost"
        message["To"] = ", ".join(NOTIFY_TO)
        message.set_content(body)

        context = ssl.create_default_context()
        client: Any
        if SMTP_SSL:
            client = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=context)
        else:
            client = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        with client as smtp:
            if SMTP_STARTTLS and not SMTP_SSL:
                smtp.starttls(context=context)
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD or "")
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo enviar la notificación por correo: %s", exc)
        return str(exc)
    return None


def batch_has_issues(batch: Any) -> bool:
    """Indica si el lote presenta algo que merece aviso (fallo, error, omisión o atención)."""
    return bool(
        batch
        and (
            getattr(batch, "failure_count", 0)
            or getattr(batch, "error_count", 0)
            or getattr(batch, "skipped_count", 0)
            or getattr(batch, "attention_count", 0)
        )
    )


def _summary(batch: Any) -> str:
    parts = {
        "ok": getattr(batch, "success_count", 0),
        "fallo": getattr(batch, "failure_count", 0),
        "error": getattr(batch, "error_count", 0),
        "omitidos": getattr(batch, "skipped_count", 0),
        "requieren atención": getattr(batch, "attention_count", 0),
        "previsualizados": getattr(batch, "preview_count", 0),
    }
    summary = ", ".join(f"{count} {label}" for label, count in parts.items() if count)
    return summary or "sin host"


def notify_batch_result(batch: Any, host_results: Any) -> str | None:
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
        f"[ACC] {label} {getattr(batch, 'batch_id', '?')[:8]}" f" — {_summary(batch)}"
    )
    lines = [
        f"{label}: {_summary(batch)}",
        f"Operador: {getattr(batch, 'operator', '—')}",
        f"Objetivo: {getattr(batch, 'target', '—')}",
        f"Acción: {getattr(batch, 'action', '—')}"
        + f" → {getattr(batch, 'value', '—') or '—'}",
        f"Simulación: {'sí' if getattr(batch, 'dry_run', False) else 'no'}",
    ]
    problem_lines = [
        f"  {getattr(res, 'host', '?')}: {getattr(res, 'result', '?')}"
        f" — {getattr(res, 'error', '')}"[:200]
        for res in (host_results or [])
        if getattr(res, "result", "") in {"failure", "error", "skipped", "attention"}
    ]
    if problem_lines:
        lines.append("")
        lines.append("Hosts con problema:")
        lines.extend(problem_lines[:50])
    return _send(subject, "\n".join(lines))
