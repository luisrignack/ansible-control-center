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
    current_app,
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
from models.batch import ExecutionBatch
from models.ops import HostGroup
from models.user import AuthEvent
from services import modules_panel, task_catalog
from services.host_locks import list_active_locks
from services.pdf_report import (
    build_execution_report,
    build_migration_report,
    format_spanish_datetime,
)
from services.safety import CRITICALITY_LABELS

logger = logging.getLogger("acc")


from acc.web.registry import RouteRegistry

app = RouteRegistry()


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
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
    return render_template(
        "tasks.html",
        task_catalog=visible_catalog,
        parked_count=len(parked_catalog),
        contact_task_mailto=contact_task_mailto,
    )


@app.route("/configuracion")
@login_required
def configuracion():
    """Entrada del grupo Configuración: abre la primera pestaña disponible."""
    if not runtime.UI_CONFIG_PANEL:
        abort(404)
    return redirect(url_for("inventario"))


@app.route("/modulos")
@login_required
def modulos():
    """Vista de módulos: tarjetas que el usuario quitó del panel principal."""
    if not runtime.UI_CONFIG_PANEL:
        abort(404)
    hidden_ids = modules_panel.hidden_task_ids(
        current_user.get_id(), db_path=runtime.get_db_path()
    )
    _, parked_catalog = modules_panel.split_catalog(task_catalog.TASK_CATALOG, hidden_ids)
    return render_template(
        "modules.html",
        hidden_tasks=parked_catalog,
        total_tasks=len(task_catalog.TASK_CATALOG),
    )


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
    flash(f"«{task['name']}» se movió a Módulos.", "info")
    return redirect(url_for("index"))


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
    flash(f"«{task['name']}» vuelve al panel principal.", "success")
    return redirect(url_for("modulos"))


@app.route("/modulos/restaurar", methods=["POST"])
@login_required
def modulos_restaurar():
    if not runtime.UI_CONFIG_PANEL:
        abort(404)
    modules_panel.restore_all(current_user.get_id(), db_path=runtime.get_db_path())
    flash("Todas las tareas vuelven al panel principal.", "success")
    return redirect(url_for("modulos"))


@app.route("/tarea/<task_id>", methods=["GET"])
@login_required
def task_view(task_id):
    task = task_catalog.get_task(task_id)
    if task is None:
        flash("La tarea pedida no existe en el catálogo.", "warning")
        return redirect(url_for("index"))

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
        )
    }
    prefill["operation_id"] = uuid.uuid4().hex
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
    """Página institucional con la fecha del host (hora local del servidor)."""
    host_date = format_spanish_datetime(datetime.now(UTC), with_time=True)
    return render_template(
        "about.html",
        host_date=host_date,
        app_version=current_app.config.get("APP_VERSION", "desconocida"),
        build_commit=current_app.config.get("APP_BUILD_COMMIT", ""),
    )

def register(flask_app) -> None:
    app.apply(flask_app)

