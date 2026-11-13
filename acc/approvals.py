"""Parametros estables y payload de las solicitudes de aprobacion.
"""

from __future__ import annotations

from typing import Any

from config import (
    APPROVAL_REQUEST_TTL_HOURS,
    APPROVAL_TTL_MINUTES,
)
from models.ops import (
    APPROVAL_APPROVED,
    ApprovalRequest,
)


def _approval_detail(req: ApprovalRequest) -> str:
    """Resumen legible de los parámetros de una solicitud."""
    return ", ".join(
        f"{key}={str(value)[:60]}"
        for key, value in req.params.items()
        if key
        in {
            "target",
            "group_name",
            "admin_user",
            "action",
            "value",
            "mode",
        }
    )


def _approval_row(req: ApprovalRequest) -> dict[str, Any]:
    """Solicitud lista para plantilla, con su fecha límite y si es ejecutable."""
    data = req.to_dict()
    data.pop("payload", None)
    data["detail"] = _approval_detail(req)
    data["deadline"] = (
        req.approved_deadline(APPROVAL_TTL_MINUTES)
        if req.status == APPROVAL_APPROVED
        else req.requested_deadline(APPROVAL_REQUEST_TTL_HOURS)
    )
    # Las solicitudes anteriores a la v16 no guardan payload: se pueden
    # aprobar, pero la ejecución exige reenviar el formulario.
    data["executable"] = req.status == APPROVAL_APPROVED and bool(req.payload)
    return data


def _approval_params(form: dict[str, Any]) -> dict[str, Any]:
    """Parámetros estables de una ejecución, sin secretos, para aprobaciones.

    La nueva contraseña de la tarjeta ``PASSWORD`` tampoco se guarda: la
    aprobación solo identifica el usuario y el resto de parámetros.
    """
    return {
        key: value
        for key, value in form.items()
        if key
        not in {
            "master_password",
            "temporal_passwords",
            "new_password",
            "new_password_confirm",
        }
    }


def _approval_payload(form) -> dict[str, Any]:
    """Formulario original de la solicitud, apto para reejecutarla.

    Se guardan los campos tal cual llegaron (para reproducir el mismo hash de
    parámetros) excluyendo secretos, el token CSRF y el ``operation_id`` de la
    idempotencia, que se regenera en cada envío.
    """
    skip = {
        "csrf_token",
        "operation_id",
        "master_password",
        "new_password",
        "new_password_confirm",
        "password_file",
    }
    payload: dict[str, Any] = {}
    for key, value in form.items():
        if key in skip or not isinstance(value, str):
            continue
        payload[key] = value
    return payload
