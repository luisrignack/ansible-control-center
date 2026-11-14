"""Panel principal, tareas, progreso e informes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import quote

from flask import (
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    stream_with_context,
    url_for,
)
from flask_login import current_user, login_required

from acc import runtime
from acc.execution import handle_execution
from acc.safety import _safety_for_group
from acc.scheduling import _handle_schedule_from_task_form
from acc.security import (
    _can_view_task,
    _client_ip,
    _owner_scope,
    _task_viewer_actor,
    _wants_json,
    roles_required,
)
from acc.task_state import (
    _get_task,
    generate_progress_events,
    update_task,
)
from config import (
    APT_MIN_FREE_GB,
    APT_MIN_FREE_GB_CEILING,
    APT_MIN_FREE_GB_FLOOR,
    COMMON_TIMEZONES,
    DEFAULT_NTP_SERVERS,
    MAX_CONTENT_LENGTH,
    NTP_POLICIES,
    OS_PASSWORD_DEFAULT_USER,
    OS_PASSWORD_MAX_LENGTH,
    OS_PASSWORD_MIN_LENGTH,
    TARGET_ADMIN_USERNAME,
    TASK_REQUEST_EMAIL,
    TASK_REQUEST_SUBJECT,
)
from models.batch import (
    HOST_RESULT_LABELS,
    ExecutionBatch,
)
from models.db import get_connection
from models.ops import (
    APPROVAL_PENDING,
    SCHEDULE_DAILY,
    SCHEDULE_ONESHOT,
    SCHEDULE_WEEKLY,
    ApprovalRequest,
    HostGroup,
    Schedule,
)
from models.user import AuthEvent
from services import modules_panel, task_catalog
from services.host_locks import list_active_locks
from services.pdf_report import (
    build_execution_report,
    build_migration_report,
)
from services.safety import CRITICALITY_LABELS

logger = logging.getLogger("acc")


from acc.web.registry import RouteRegistry

app = RouteRegistry()


@app.route("/tareas", methods=["GET", "POST"])
@login_required
def tareas():
    if request.method == "POST":
        if request.form.get("mode", "").strip().lower() == "schedule":
            return _handle_schedule_from_task_form()
        return handle_execution()

    # La portada es el catálogo: solo tarjetas. Cada tarjeta enlaza a
    # /tarea/<task_id> para la vista de detalle de la tarea.
    # Contacto para pedir tareas nuevas (mailto: abre el cliente de correo
    # del sistema, p. ej. Thunderbird).
    contact_task_mailto = "mailto:{}?subject={}".format(
        TASK_REQUEST_EMAIL,
        quote(TASK_REQUEST_SUBJECT, safe=""),
    )
    hidden_ids = (
        modules_panel.hidden_task_ids(current_user.get_id(), db_path=runtime.get_db_path())
        if runtime.UI_CONFIG_PANEL
        else set()
    )
    visible_catalog, parked_catalog = modules_panel.split_catalog(
        task_catalog.TASK_CATALOG, hidden_ids
    )
    tab = "ocultas" if request.args.get("tab") == "ocultas" else "catalogo"
    return render_template(
        "tasks.html",
        task_catalog=visible_catalog,
        parked_catalog=parked_catalog,
        parked_count=len(parked_catalog),
        total_tasks=len(task_catalog.TASK_CATALOG),
        tab=tab,
        contact_task_mailto=contact_task_mailto,
    )


_RECURRENCE_LABELS = {
    SCHEDULE_ONESHOT: "Una vez",
    SCHEDULE_DAILY: "Diaria",
    SCHEDULE_WEEKLY: "Semanal",
}


def _short_moment(value) -> str:
    """Fecha/hora compacta en la zona local para las tablas del panel."""
    if not value:
        return "—"
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        return moment.astimezone().strftime("%d/%m · %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def _schedule_next_moment(schedule: Schedule) -> datetime | None:
    """Próxima ejecución prevista; las programaciones de una sola vez usan run_at."""
    if schedule.recurrence in (SCHEDULE_DAILY, SCHEDULE_WEEKLY):
        return schedule.next_run_from()
    try:
        moment = datetime.fromisoformat(schedule.run_at)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _panel_host_rows(
    db_path: str, owner_scope: int | None, limit: int = 8
) -> tuple[list[dict], int, int]:
    """Hosts del inventario con su último resultado conocido."""
    owner_filter = ""
    params: list = []
    if owner_scope is not None:
        owner_filter = "WHERE (g.owner_user_id = ? OR g.owner_user_id = 0)"
        params.append(int(owner_scope))
    with get_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT h.address, h.label, g.name AS group_name,
                   (SELECT eh.result FROM execution_hosts eh
                     WHERE eh.host = h.address
                     ORDER BY eh.created_at DESC, eh.id DESC LIMIT 1) AS last_result,
                   (SELECT eh.created_at FROM execution_hosts eh
                     WHERE eh.host = h.address
                     ORDER BY eh.created_at DESC, eh.id DESC LIMIT 1) AS last_run
            FROM inventory_hosts h
            JOIN inventory_groups g ON g.id = h.group_id
            {owner_filter}
            ORDER BY g.name COLLATE NOCASE, h.address
            LIMIT ?
            """,  # nosec B608 - el filtro es una constante, nunca entrada del usuario
            [*params, int(limit)],
        ).fetchall()
        total = conn.execute(
            f"""
            SELECT COUNT(*) FROM inventory_hosts h
            JOIN inventory_groups g ON g.id = h.group_id
            {owner_filter}
            """,  # nosec B608
            params,
        ).fetchone()[0]
        unexecuted = conn.execute(
            f"""
            SELECT COUNT(*) FROM inventory_hosts h
            JOIN inventory_groups g ON g.id = h.group_id
            {owner_filter}
            {"AND" if owner_filter else "WHERE"} NOT EXISTS (
                SELECT 1 FROM execution_hosts eh WHERE eh.host = h.address
            )
            """,  # nosec B608
            params,
        ).fetchone()[0]
    return (
        [
            {
                "address": row["address"],
                "label": row["label"],
                "group_name": row["group_name"],
                "result": row["last_result"],
                "result_label": HOST_RESULT_LABELS.get(
                    row["last_result"], "Sin datos"
                ),
                "last_run": _short_moment(row["last_run"]),
            }
            for row in rows
        ],
        int(total or 0),
        int(unexecuted or 0),
    )


@app.route("/")
@login_required
def panel():
    """Panel de control: inventario, ejecuciones, programaciones y alertas."""
    db_path = runtime.get_db_path()
    owner_scope = _owner_scope()

    host_rows, total_hosts, unexecuted_hosts = _panel_host_rows(
        db_path, owner_scope
    )
    groups = HostGroup.all(db_path=db_path, owner_user_id=owner_scope)

    schedules = Schedule.all(db_path=db_path, owner_user_id=owner_scope)
    active_schedules = [schedule for schedule in schedules if schedule.enabled]
    upcoming = sorted(
        active_schedules,
        key=lambda item: _schedule_next_moment(item)
        or datetime.max.replace(tzinfo=UTC),
    )[:3]
    task_names = {task["id"]: task["name"] for task in task_catalog.TASK_CATALOG}
    upcoming_schedules = [
        {
            "id": schedule.id,
            "name": schedule.name,
            "task_name": task_names.get(schedule.task_id, schedule.task_id),
            "next_run": _short_moment(_schedule_next_moment(schedule)),
            "recurrence": _RECURRENCE_LABELS.get(
                schedule.recurrence, schedule.recurrence
            ),
        }
        for schedule in upcoming
    ]

    batches = ExecutionBatch.get_batches(limit=20, db_path=db_path)
    running_batch = next(
        (batch for batch in batches if batch.status == "running"), None
    )
    latest_batch = next(
        (batch for batch in batches if batch.status != "running"), None
    )

    success_pct = None
    success_foot = "Todavía no hay lotes finalizados"
    if latest_batch is not None and latest_batch.total_hosts:
        success_pct = round(
            100 * latest_batch.success_count / latest_batch.total_hosts
        )
        success_foot = (
            f"{latest_batch.success_count} de {latest_batch.total_hosts} hosts"
        )
    elif latest_batch is not None:
        success_foot = "El último lote no procesó hosts"

    alerts: list[dict] = []
    if current_user.has_role("admin", "operator"):
        if current_user.has_role("admin"):
            pending = ApprovalRequest.list_status(
                APPROVAL_PENDING, limit=50, db_path=db_path
            )
        else:
            pending = [
                request
                for request in ApprovalRequest.list_for_requester(
                    current_user.username, limit=50, db_path=db_path
                )
                if request.status == APPROVAL_PENDING
            ]
        if pending:
            alerts.append(
                {
                    "level": "warn",
                    "title": "Aprobaciones pendientes",
                    "text": f"{len(pending)} solicitud(es) esperan revisión.",
                    "href": url_for(
                        "solicitudes",
                        tab="aprobaciones"
                        if current_user.has_role("admin")
                        else "mias",
                    ),
                }
            )

    if latest_batch is not None:
        failed = latest_batch.failure_count + latest_batch.error_count
        attention = latest_batch.attention_count
        if failed:
            alerts.append(
                {
                    "level": "err",
                    "title": "Fallos en el último lote",
                    "text": f"{failed} host(s) con fallo o error en "
                    f"«{latest_batch.target}».",
                    "href": url_for(
                        "batch_detail", batch_id=latest_batch.batch_id
                    ),
                }
            )
        if attention:
            alerts.append(
                {
                    "level": "warn",
                    "title": "Hosts que requieren atención",
                    "text": f"{attention} host(s) necesitan revisión manual.",
                    "href": url_for(
                        "batch_detail", batch_id=latest_batch.batch_id
                    ),
                }
            )

    for schedule in active_schedules:
        if schedule.last_error:
            alerts.append(
                {
                    "level": "err",
                    "title": f"Programación «{schedule.name}»",
                    "text": schedule.last_error,
                    "href": url_for("programaciones"),
                }
            )
            if len(alerts) >= 5:
                break

    if unexecuted_hosts:
        alerts.append(
            {
                "level": "warn",
                "title": "Hosts sin ejecuciones",
                "text": f"{unexecuted_hosts} host(s) del inventario aún no "
                "tienen resultados.",
                "href": url_for("inventario"),
            }
        )

    running_progress = None
    if running_batch is not None:
        processed = running_batch.processed_count
        total = running_batch.total_hosts or 0
        running_progress = {
            "batch_id": running_batch.batch_id,
            "target": running_batch.target,
            "processed": processed,
            "total": total,
            "percent": min(100, round(100 * processed / total)) if total else 0,
            "operator": running_batch.operator,
        }

    return render_template(
        "panel.html",
        total_hosts=total_hosts,
        groups_count=len(groups),
        active_schedules_count=len(active_schedules),
        success_pct=success_pct,
        success_foot=success_foot,
        latest_batch=latest_batch,
        alerts=alerts,
        alert_count=len(alerts),
        host_rows=host_rows,
        running=running_progress,
        upcoming_schedules=upcoming_schedules,
        active_schedules_total=len(active_schedules),
    )


@app.route("/panel")
@login_required
def panel_alias():
    """Compatibilidad con el antiguo acceso directo al panel."""
    return redirect(url_for("panel"))


@app.route("/ejecuciones")
@login_required
def ejecuciones():
    """Ejecuciones en curso e historial de lotes en una sola sección."""
    db_path = runtime.get_db_path()
    batches = ExecutionBatch.get_batches(limit=100, db_path=db_path)
    running_batches = [batch for batch in batches if batch.status == "running"]
    history_batches = [batch for batch in batches if batch.status != "running"]
    tab = "historial" if request.args.get("tab") == "historial" else "curso"
    return render_template(
        "ejecuciones.html",
        running_batches=running_batches,
        batches=history_batches,
        stats=ExecutionBatch.get_stats(db_path=db_path),
        tab=tab,
    )


@app.route("/configuracion")
@login_required
def configuracion():
    """Compatibilidad: los ajustes viven ahora en Sistema → Ajustes."""
    return redirect(url_for("sistema", tab="ajustes"), code=301)


@app.route("/modulos")
@login_required
def modulos():
    """Compatibilidad: las tareas ocultas viven en Tareas → Ocultas."""
    if not runtime.UI_CONFIG_PANEL:
        abort(404)
    return redirect(url_for("tareas", tab="ocultas"), code=301)


@app.route("/modulos/<task_id>/ocultar", methods=["POST"])
@login_required
def modulo_ocultar(task_id: str):
    if not runtime.UI_CONFIG_PANEL:
        abort(404)
    task = task_catalog.get_task(task_id)
    if task is None:
        abort(404)
    modules_panel.set_task_hidden(
        current_user.get_id(), task_id, True, db_path=runtime.get_db_path()
    )
    flash(f"«{task['name']}» se movió a Tareas → Ocultas.", "info")
    return redirect(url_for('tareas'))


@app.route("/modulos/<task_id>/mostrar", methods=["POST"])
@login_required
def modulo_mostrar(task_id: str):
    if not runtime.UI_CONFIG_PANEL:
        abort(404)
    task = task_catalog.get_task(task_id)
    if task is None:
        abort(404)
    modules_panel.set_task_hidden(
        current_user.get_id(), task_id, False, db_path=runtime.get_db_path()
    )
    flash(f"«{task['name']}» vuelve al catálogo.", "success")
    return redirect(url_for("tareas", tab="ocultas"))


@app.route("/modulos/restaurar", methods=["POST"])
@login_required
def modulos_restaurar():
    if not runtime.UI_CONFIG_PANEL:
        abort(404)
    modules_panel.restore_all(current_user.get_id(), db_path=runtime.get_db_path())
    flash("Todas las tareas vuelven al catálogo.", "success")
    return redirect(url_for("tareas", tab="ocultas"))


@app.route("/tarea/<task_id>", methods=["GET"])
@login_required
def task_view(task_id):
    task = task_catalog.get_task(task_id)
    if task is None:
        flash("La tarea pedida no existe en el catálogo.", "warning")
        return redirect(url_for('tareas'))

    prefill = {
        key: request.args.get(key, "")
        for key in (
            "target_ip",
            "admin_user",
            "action",
            "value",
            "time_mode",
            "time_zone",
            "time_fold",
            "ntp_policy",
            "ntp_servers",
            "sync_rtc",
            "force",
            "min_free_gb",
            "target_user",
            "mode",
        )
    }
    prefill["operation_id"] = uuid.uuid4().hex
    prefill["schedule_intent"] = (
        "schedule" if prefill.get("mode") == "schedule" else "now"
    )
    if not prefill.get("target_user"):
        if prefill.get("action", "").upper() == "PASSWORD" and prefill.get("value"):
            prefill["target_user"] = prefill["value"]
        else:
            prefill["target_user"] = OS_PASSWORD_DEFAULT_USER
    # Si la tarea corresponde a una acción específica, solo se ofrecen las
    # tareas activas. Los grupos del inventario alimentan el selector de
    # objetivo por grupo.
    inventory_groups = []
    for group in HostGroup.all(
        db_path=runtime.get_db_path(), owner_user_id=_owner_scope()
    ):
        data = group.to_dict()
        data["safety"] = _safety_for_group(group)
        inventory_groups.append(data)
    locked_hosts = [
        lock["host"] for lock in list_active_locks(limit=20, db_path=runtime.get_db_path())
    ]
    return render_template(
        "index.html",
        locked_hosts=locked_hosts,
        common_timezones=COMMON_TIMEZONES,
        ntp_policies=NTP_POLICIES,
        default_ntp_servers=", ".join(DEFAULT_NTP_SERVERS),
        default_admin_user=TARGET_ADMIN_USERNAME,
        prefill=prefill,
        selected_task=task,
        inventory_groups=inventory_groups,
        criticality_labels=CRITICALITY_LABELS,
        max_upload_bytes=MAX_CONTENT_LENGTH,
        default_min_free_gb=APT_MIN_FREE_GB,
        min_free_gb_floor=APT_MIN_FREE_GB_FLOOR,
        min_free_gb_ceiling=APT_MIN_FREE_GB_CEILING,
        os_password_default_user=OS_PASSWORD_DEFAULT_USER,
        os_password_min_length=OS_PASSWORD_MIN_LENGTH,
        os_password_max_length=OS_PASSWORD_MAX_LENGTH,
    )


@app.route("/tarea/<task_id>/informe.pdf", methods=["GET"])
@login_required
def task_report_pdf(task_id: str):
    """Informe PDF de la última corrida de una tarea del catálogo."""
    task = task_catalog.get_task(task_id)
    if task is None:
        abort(404)

    db_path = runtime.get_db_path()
    batch = ExecutionBatch.get_latest_by_task(task_id, db_path=db_path)
    report_task_name = task["name"]
    if batch is None:
        # Solo se recurre al último lote global si es anterior a la migración
        # v9 (no guarda ``task_id``). Nunca se presenta el lote de otra tarea
        # como si fuera el de esta; en ese caso el informe se etiqueta como
        # "última corrida general".
        batches = ExecutionBatch.get_batches(limit=1, db_path=db_path)
        candidate = batches[0] if batches else None
        if candidate is not None and not candidate.task_id:
            batch = candidate
            report_task_name = f"{task['name']} (última corrida general)"
    if batch is None:
        flash(
            "Esta tarea aún no tiene ejecuciones registradas: lanza una "
            "corrida para poder generar el informe.",
            "info",
        )
        return redirect(url_for("task_view", task_id=task_id))

    host_results = ExecutionBatch.get_host_results(batch.batch_id, db_path=db_path)
    pdf_bytes = build_execution_report(
        batch,
        host_results,
        task_name=report_task_name,
    )
    filename = f"informe-{task_id}-{batch.batch_id[:8]}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/tarea/<task_id>/migracion.pdf", methods=["GET"])
@login_required
def task_migration_pdf(task_id: str):
    """Informe PDF de estaciones que requieren migración (última corrida)."""
    task = task_catalog.get_task(task_id)
    if task is None:
        abort(404)
    db_path = runtime.get_db_path()
    batch = ExecutionBatch.get_latest_by_task(task_id, db_path=db_path)
    if batch is None:
        flash(
            "Esta tarea aún no tiene ejecuciones registradas: lanza una "
            "corrida para poder generar el informe de migración.",
            "info",
        )
        return redirect(url_for("task_view", task_id=task_id))
    host_results = ExecutionBatch.get_host_results(batch.batch_id, db_path=db_path)
    pdf_bytes = build_migration_report(
        batch,
        host_results,
        task_name=task["name"],
    )
    filename = f"migracion-{task_id}-{batch.batch_id[:8]}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/batches/<batch_id>/migracion.pdf", methods=["GET"])
@login_required
def batch_migration_pdf(batch_id: str):
    """Informe de migración de un lote concreto."""
    batch = ExecutionBatch.get_batch(batch_id, db_path=runtime.get_db_path())
    if batch is None:
        abort(404)
    host_results = ExecutionBatch.get_host_results(batch_id, db_path=runtime.get_db_path())
    pdf_bytes = build_migration_report(
        batch,
        host_results,
        task_name=batch.task_id or "",
    )
    filename = f"migracion-{batch.batch_id[:8]}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.route("/preview", methods=["POST"])
@roles_required("operator")
def preview_execution():
    """Alias explícito para lanzar una vista previa (no cambia hosts)."""
    return handle_execution(default_mode="preview")


@app.route("/execute", methods=["POST"])
@roles_required("operator")
def execute_execution():
    """Alias explícito para lanzar una ejecución sobre todos los hosts."""
    return handle_execution(default_mode="execute")


@app.route("/progress/<task_id>")
@login_required
def progress_page(task_id: str):
    task = _get_task(task_id, refresh=True)
    if task is None or not _can_view_task(task):
        abort(404)
    actor = _task_viewer_actor()
    can_cancel = current_user.has_role("admin") or str(task.get("owner") or "") == actor
    return render_template(
        "progress.html", task_id=task_id, task=task, can_cancel=can_cancel
    )


@app.route("/progress/<task_id>/events")
@login_required
def progress_sse(task_id: str):
    task = _get_task(task_id, refresh=True)
    if task is None or not _can_view_task(task):
        abort(404)
    response = Response(
        stream_with_context(generate_progress_events(task_id)),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/task-status/<task_id>")
@login_required
def api_task_status(task_id: str):
    task = _get_task(task_id, refresh=True)
    if task is None or not _can_view_task(task):
        # Mismo resultado que una tarea inexistente: no se confirma que el
        # identificador pertenece a otro usuario.
        response = jsonify({"status": "not_found", "exists": False})
    else:
        response = jsonify({**task, "exists": True})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/tasks/<task_id>/cancel", methods=["POST"])
@roles_required("operator")
def cancel_task(task_id: str):
    """Solicita la cancelación cooperativa de una operación en curso.

    Un operador solo puede cancelar las operaciones que lanzó; un
    administrador puede cancelar cualquiera. Las tareas sin dueño (por
    ejemplo, una programación desatendida) quedan reservadas a los
    administradores.
    """
    task = _get_task(task_id, refresh=True)
    if task is None:
        abort(404)

    actor = getattr(current_user, "username", "") or str(current_user.get_id())
    owner = str(task.get("owner") or "")
    if not current_user.has_role("admin") and owner != actor:
        AuthEvent.record(
            actor,
            _client_ip(),
            "task_cancel_denied",
            f"tarea={task_id} dueño={owner or '(ninguno)'}",
            db_path=runtime.get_db_path(),
        )
        message = "Solo puedes cancelar las operaciones que lanzaste tú."
        if _wants_json():
            return jsonify({"success": False, "message": message}), 403
        flash(message, "danger")
        return redirect(url_for("progress_page", task_id=task_id))

    if task.get("completed") or task.get("cancellation_requested"):
        message = "La operación ya terminó o ya se estaba cancelando."
        if _wants_json():
            return jsonify({"success": True, "message": message}), 200
        flash(message, "info")
        return redirect(url_for("progress_page", task_id=task_id))

    requested = runtime.get_job_queue().cancel(task_id)
    if not requested:
        message = "La operación ya no está en ejecución."
        if _wants_json():
            return jsonify({"success": False, "message": message}), 409
        flash(message, "warning")
        return redirect(url_for("progress_page", task_id=task_id))

    update_task(
        task_id,
        {
            "cancellation_requested": True,
            "message": "Cancelación solicitada; se detendrá al terminar el host actual…",
        },
    )
    AuthEvent.record(
        actor,
        _client_ip(),
        "task_cancel",
        f"tarea={task_id} dueño={owner or '(ninguno)'}",
        db_path=runtime.get_db_path(),
    )
    logger.info("Cancelación solicitada para la tarea %s por %s", task_id, actor)
    if _wants_json():
        return (
            jsonify(
                {
                    "success": True,
                    "status": "cancelling",
                    "message": "Cancelación solicitada.",
                }
            ),
            202,
        )
    flash("Cancelación solicitada.", "info")
    return redirect(url_for("progress_page", task_id=task_id))


@app.route("/batches/<batch_id>")
@login_required
def batch_detail(batch_id: str):
    batch = ExecutionBatch.get_batch(batch_id, db_path=runtime.get_db_path())
    if batch is None:
        abort(404)
    host_results = ExecutionBatch.get_host_results(batch_id, db_path=runtime.get_db_path())
    duration_stats = ExecutionBatch.get_duration_stats(batch_id, db_path=runtime.get_db_path())
    return render_template(
        "batch.html",
        batch=batch,
        host_results=host_results,
        duration_stats=duration_stats,
    )


@app.route("/preview/<batch_id>")
@login_required
def preview_detail(batch_id: str):
    """Alias de la vista de lote para enlaces de vista previa."""
    return redirect(url_for("batch_detail", batch_id=batch_id))


@app.route("/acerca-de", methods=["GET"])
@login_required
def acerca_de():
    """Compatibilidad: la información institucional vive en Sistema → Acerca de."""
    return redirect(url_for("sistema", tab="acerca-de"), code=301)


def register(flask_app) -> None:
    app.apply(flask_app)

