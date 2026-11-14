"""Rutas de solicitudes y aprobaciones.
"""

from __future__ import annotations

import uuid

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user

from acc import runtime
from acc.approvals import (
    _approval_detail,
    _approval_row,
)
from acc.execution import handle_execution
from acc.security import roles_required
from acc.web.registry import RouteRegistry
from config import (
    APPROVAL_REQUEST_TTL_HOURS,
    APPROVAL_TTL_MINUTES,
)
from models.ops import (
    APPROVAL_APPROVED,
    APPROVAL_EXPIRED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ApprovalRequest,
)
from services import task_catalog

app = RouteRegistry()


@app.route("/solicitudes", methods=["GET"])
@roles_required("operator")
def solicitudes():
    """Solicitudes propias y, para administradores, la bandeja de aprobación."""
    db_path = runtime.get_db_path()
    ApprovalRequest.expire_stale(
        APPROVAL_REQUEST_TTL_HOURS, APPROVAL_TTL_MINUTES, db_path=db_path
    )
    username = getattr(current_user, "username", "") or ""
    requests = [
        _approval_row(req)
        for req in ApprovalRequest.list_for_requester(username, db_path=db_path)
    ]
    pending: list[dict] = []
    decided: list[dict] = []
    if current_user.has_role("admin"):
        pending = [
            _approval_row(req)
            for req in ApprovalRequest.list_status(
                APPROVAL_PENDING, db_path=db_path
            )
        ]
        decided = [
            _approval_row(req)
            for req in ApprovalRequest.list_status(
                APPROVAL_APPROVED, db_path=db_path
            )
            + ApprovalRequest.list_status(
                APPROVAL_REJECTED, db_path=db_path
            )
            + ApprovalRequest.list_status(
                APPROVAL_EXPIRED, db_path=db_path
            )
        ]
    requested_tab = request.args.get("tab", "mias")
    tab = "aprobaciones" if requested_tab == "aprobaciones" else "mias"
    if tab == "aprobaciones" and not current_user.has_role("admin"):
        tab = "mias"
    return render_template(
        "solicitudes.html",
        tab=tab,
        requests=requests,
        pending=pending,
        decided=decided,
        request_ttl_hours=APPROVAL_REQUEST_TTL_HOURS,
        approval_ttl_minutes=APPROVAL_TTL_MINUTES,
    )


@app.route("/mis-solicitudes", methods=["GET"])
@roles_required("operator")
def mis_solicitudes():
    """Compatibilidad: Mis solicitudes vive en Solicitudes → Mis solicitudes."""
    return redirect(url_for("solicitudes", tab="mias"), code=301)


@app.route("/mis-solicitudes/<int:request_id>/ejecutar", methods=["GET", "POST"])
@app.route("/solicitudes/<int:request_id>/ejecutar", methods=["GET", "POST"])
@roles_required("operator")
def ejecutar_aprobacion(request_id):
    """Ejecuta una solicitud propia ya aprobada sin repetir el formulario.

    Los parámetros se recuperan del payload guardado al crear la solicitud
    (sin secretos); solo se piden la contraseña maestra del vault y, en el
    cambio de contraseña, la contraseña nueva.
    """
    db_path = runtime.get_db_path()
    ApprovalRequest.expire_stale(
        APPROVAL_REQUEST_TTL_HOURS, APPROVAL_TTL_MINUTES, db_path=db_path
    )
    req = ApprovalRequest.get(request_id, db_path=db_path)
    username = getattr(current_user, "username", "") or ""
    if req is None or req.requested_by != username:
        flash("La solicitud no existe o no pertenece a tu usuario.", "warning")
        return redirect(url_for("solicitudes", tab="mias"))
    if req.status != APPROVAL_APPROVED:
        flash(
            "Solo se puede ejecutar una solicitud aprobada y vigente "
            f"(estado actual: {req.status}).",
            "warning",
        )
        return redirect(url_for("solicitudes", tab="mias"))
    if not req.payload:
        flash(
            "Esta solicitud se creó antes de habilitar la ejecución directa y "
            "no guarda sus parámetros. Vuelve a enviar el formulario: la "
            "aprobación vigente se aplicará al reenviarlo.",
            "warning",
        )
        return redirect(url_for("solicitudes", tab="mias"))
    if request.method == "GET":
        return render_template(
            "ejecutar_aprobacion.html",
            req=_approval_row(req),
            task=task_catalog.get_task(req.task_id),
            detail=_approval_detail(req),
            needs_password=req.params.get("action") == "PASSWORD",
            uses_vault=req.params.get("origin", "stored") == "stored",
            uses_file=req.params.get("origin") == "temporal",
            approval_ttl_minutes=APPROVAL_TTL_MINUTES,
        )
    # Reenvío: mismos parámetros de la solicitud aprobada (el hash coincide),
    # secretos aportados ahora y nuevos tokens de idempotencia/CSRF.
    submitted = dict(req.payload or {})
    submitted["mode"] = "execute"
    submitted["operation_id"] = uuid.uuid4().hex
    if not submitted.get("task"):
        submitted["task"] = req.task_id
    submitted.setdefault("password_origin", req.params.get("origin", "stored"))
    for name in ("master_password", "new_password", "new_password_confirm"):
        if name in request.form:
            submitted[name] = request.form.get(name, "")
    return handle_execution(
        default_mode="execute", form_data=submitted, files=request.files
    )


@app.route("/aprobaciones", methods=["GET", "POST"])
@roles_required("admin")
def aprobaciones():
    db_path = runtime.get_db_path()
    if request.method == "POST":
        action = request.form.get("action", "").strip()
        request_id = request.form.get("request_id", "").strip()
        if not request_id.isdigit():
            flash("Solicitud no válida.", "warning")
            return redirect(url_for("solicitudes", tab="aprobaciones"))
        if action == "approve":
            decided, decide_error = ApprovalRequest.decide(
                int(request_id),
                approver=getattr(current_user, "username", ""),
                approve=True,
                note=request.form.get("note", ""),
                ttl_hours=APPROVAL_REQUEST_TTL_HOURS,
                db_path=db_path,
            )
            if decide_error or decided is None:
                flash(decide_error or "No se pudo aprobar.", "danger")
            else:
                flash(
                    f"Solicitud #{decided.id} aprobada: el solicitante puede "
                    "ejecutarla desde Mis solicitudes durante "
                    f"{APPROVAL_TTL_MINUTES // 60} h.",
                    "success",
                )
        elif action == "reject":
            decided, decide_error = ApprovalRequest.decide(
                int(request_id),
                approver=getattr(current_user, "username", ""),
                approve=False,
                note=request.form.get("note", ""),
                ttl_hours=APPROVAL_REQUEST_TTL_HOURS,
                db_path=db_path,
            )
            if decide_error or decided is None:
                flash(decide_error or "No se pudo rechazar.", "danger")
            else:
                flash(f"Solicitud #{decided.id} rechazada.", "info")
        else:
            flash("Acción de aprobación no válida.", "warning")
        return redirect(url_for("solicitudes", tab="aprobaciones"))

    return redirect(url_for("solicitudes", tab="aprobaciones"), code=301)

def register(flask_app) -> None:
    app.apply(flask_app)

