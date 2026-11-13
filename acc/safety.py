"""Criticidad y ventana de mantenimiento antes de ejecutar.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import flash, jsonify, redirect, request, url_for
from flask_login import current_user

from acc import runtime
from acc.operations import _form_bool
from acc.security import (
    _client_ip,
    _wants_json,
)
from models.ops import (
    HostGroup,
    InventoryHost,
)
from models.user import AuthEvent
from services.safety import evaluate_safety

logger = logging.getLogger("acc")


def _safety_for_group(group, now=None, hosts=None):
    """Evalúa la política de un grupo con su lista actual de hosts."""
    db_path = runtime.get_db_path()
    if hosts is None:
        hosts = [
            {"address": host.address, "criticality": host.criticality}
            for host in HostGroup.hosts_of(group.id, db_path=db_path)
        ]
    return evaluate_safety(
        group_name=group.name,
        group_criticality=group.criticality,
        hosts=hosts,
        window_days=group.window_days,
        window_start=group.window_start,
        window_end=group.window_end,
        window_note=group.window_note,
        now=now,
    )


def _safety_for_target(target_group_id, target, now=None):
    """Evalúa un objetivo por grupo o por IP manual clasificada."""
    db_path = runtime.get_db_path()
    if target_group_id is not None:
        group = HostGroup.get(target_group_id, db_path=db_path)
        if group is not None:
            return _safety_for_group(group, now=now)

    text = str(target or "").strip()
    if not text.startswith("inventory:"):
        addresses = [part.strip() for part in text.split(",") if part.strip()]
        if len(addresses) == 1:
            try:
                import ipaddress as _ip

                _ip.ip_address(addresses[0])
            except ValueError:
                pass
            else:
                host, group = InventoryHost.find_by_address(
                    addresses[0], db_path=db_path
                )
                if host is not None and group is not None:
                    return _safety_for_group(
                        group,
                        now=now,
                        hosts=[
                            {
                                "address": host.address,
                                "criticality": host.criticality,
                            }
                        ],
                    )

    result = evaluate_safety(now=now)
    result["warnings"].append(
        "El objetivo manual no está clasificado en el inventario; no se "
        "aplica criticidad ni ventana de mantenimiento."
    )
    result["level"] = "warn"
    result["summary"] = result["warnings"][0]
    return result


def _safety_gate(form: dict[str, Any], operator: str, mode: str):
    """Aplica criticidad y ventana antes de encolar una ejecución real.

    La vista previa nunca se bloquea (no modifica nada). Una ejecución real
    sobre hosts ``critical`` fuera de ventana solo continúa con una excepción
    explícita de un administrador, con motivo y auditoría. Devuelve una
    respuesta Flask si hay que detener el envío; ``None`` si puede seguir.
    """
    safety = _safety_for_target(form.get("target_group_id"), form["target"])
    override_requested = _form_bool("safety_override")
    reason = request.form.get("safety_reason", "").strip()

    if safety["blocked"] and mode == "execute":
        if not override_requested:
            AuthEvent.record(
                operator,
                _client_ip(),
                "safety_blocked",
                f"{form['task_id']} sobre {form['target']}: "
                + " | ".join(safety["reasons"]),
                db_path=runtime.get_db_path(),
            )
            for text in safety["reasons"]:
                flash(text, "danger")
            flash(
                "La operación no se envió. Espera a la ventana de "
                "mantenimiento o solicita una excepción a un administrador.",
                "warning",
            )
            if _wants_json():
                return (
                    jsonify(
                        {
                            "status": "blocked",
                            "errors": safety["reasons"],
                            "next_open": safety.get("next_open", ""),
                        }
                    ),
                    403,
                )
            return redirect(url_for("task_view", task_id=form["task_id"]))
        if not current_user.has_role("admin"):
            flash(
                "Solo un administrador puede autorizar una excepción de "
                "ventana de mantenimiento.",
                "danger",
            )
            if _wants_json():
                return (
                    jsonify(
                        {
                            "status": "blocked",
                            "errors": ["Se requiere rol admin para la excepción."],
                        }
                    ),
                    403,
                )
            return redirect(url_for("task_view", task_id=form["task_id"]))
        if len(reason) < 10:
            flash(
                "La excepción requiere un motivo de al menos 10 caracteres.",
                "danger",
            )
            if _wants_json():
                return (
                    jsonify(
                        {
                            "status": "blocked",
                            "errors": ["Motivo de excepción demasiado corto."],
                        }
                    ),
                    403,
                )
            return redirect(url_for("task_view", task_id=form["task_id"]))
        AuthEvent.record(
            operator,
            _client_ip(),
            "safety_override",
            f"{form['task_id']} sobre {form['target']}: {reason} | "
            + " | ".join(safety["reasons"]),
            db_path=runtime.get_db_path(),
        )
        logger.warning(
            "Excepción de ventana autorizada por %s sobre %s: %s",
            operator,
            form["target"],
            reason,
        )
        flash(
            "Excepción de ventana autorizada y auditada. La operación "
            "continuará fuera de la ventana de mantenimiento.",
            "warning",
        )
        return None

    if safety["blocked"] and mode != "execute":
        for text in safety["reasons"]:
            flash(f"Vista previa permitida: {text}", "warning")
    for text in safety["warnings"]:
        flash(text, "warning")
    return None
