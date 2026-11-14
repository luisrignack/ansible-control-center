"""Rutas de programaciones.
"""

from __future__ import annotations

from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from acc import runtime
from acc.safety import _safety_for_group
from acc.scheduling import (
    _schedulable_tasks,
    _update_schedule_from_form,
)
from acc.security import (
    _can_manage_owner,
    _owner_scope,
    _view_all_requested,
)
from acc.web.registry import RouteRegistry
from config import (
    APT_MIN_FREE_GB,
    APT_MIN_FREE_GB_CEILING,
    APT_MIN_FREE_GB_FLOOR,
    OS_PASSWORD_DEFAULT_USER,
    OS_PASSWORD_MAX_LENGTH,
    OS_PASSWORD_MIN_LENGTH,
    TARGET_ADMIN_USERNAME,
)
from models.ops import (
    HostGroup,
    Schedule,
)
from services import task_catalog
from services.wallpaper import remove_wallpaper

app = RouteRegistry()


@app.route("/programaciones", methods=["GET", "POST"])
@login_required
def programaciones():
    db_path = runtime.get_db_path()
    tasks = _schedulable_tasks()
    if request.method == "POST":
        if not current_user.has_role("admin"):
            abort(403)
        # ``op`` identifica la operación del formulario (activar/eliminar);
        # ``action`` es la acción operativa de la programación (TZ/TIME/APT).
        # Antes ambos usaban ``action`` y el formulario de alta enviaba dos
        # valores: la programación quedaba guardada con ``action="create"`` y
        # fallaba al dispararse.
        action = request.form.get("op", "").strip()
        if action == "toggle":
            schedule = (
                Schedule.get(request.form.get("schedule_id", "0"), db_path=db_path)
                if request.form.get("schedule_id", "").isdigit()
                else None
            )
            if schedule is None:
                flash("La programación no existe.", "warning")
            else:
                Schedule.set_enabled(schedule.id, not schedule.enabled, db_path=db_path)
                flash(
                    f"Programación '{schedule.name}' "
                    f"{'activada' if not schedule.enabled else 'desactivada'}.",
                    "success",
                )
        elif action == "delete":
            schedule = (
                Schedule.get(request.form.get("schedule_id", "0"), db_path=db_path)
                if request.form.get("schedule_id", "").isdigit()
                else None
            )
            if schedule is None:
                flash("La programación no existe.", "warning")
            else:
                remove_wallpaper((schedule.params or {}).get("wallpaper_path"))
                Schedule.delete(schedule.id, db_path=db_path)
                flash(f"Programación '{schedule.name}' eliminada.", "success")
        else:
            flash("Acción de programación no válida.", "warning")
        if request.form.get("view_all") == "1":
            return redirect(url_for("programaciones", all=1))
        return redirect(url_for("programaciones"))

    show_all = _view_all_requested()
    schedules = [
        s.to_dict()
        for s in Schedule.all(db_path=db_path, owner_user_id=_owner_scope())
    ]
    groups = [
        g.to_dict()
        for g in HostGroup.all(db_path=db_path, owner_user_id=_owner_scope())
    ]
    groups_by_id = {group["id"]: group for group in groups}
    for item in schedules:
        item["task_name"] = (
            tasks[item["task_id"]]["name"]
            if item["task_id"] in tasks
            else item["task_id"]
        )
        params = item.get("params") or {}
        group = groups_by_id.get(params.get("target_group_id"))
        item["group_name"] = group["name"] if group else ""
        item["group_criticality"] = group.get("criticality_label", "") if group else ""
        item["group_window"] = group.get("window_label", "") if group else ""
    # Las programaciones activas más próximas primero; las inactivas al final.
    schedules.sort(
        key=lambda item: (
            0 if item.get("enabled") else 1,
            item.get("run_at") or "9999-12-31T23:59",
        )
    )

    filter_group_raw = request.args.get("group", "").strip()
    filter_group_id = int(filter_group_raw) if filter_group_raw.isdigit() else None
    filter_group_name = ""
    if filter_group_id is not None:
        filter_group = groups_by_id.get(filter_group_id)
        filter_group_name = filter_group["name"] if filter_group else ""
        schedules = [
            item
            for item in schedules
            if str((item.get("params") or {}).get("target_group_id") or "")
            == str(filter_group_id)
        ]

    return render_template(
        "schedules.html",
        schedules=schedules,
        filter_group_id=filter_group_id,
        filter_group_name=filter_group_name,
        show_all=show_all,
        recurrence_labels={
            "once": "Una vez",
            "daily": "Diaria",
            "weekly": "Semanal",
        },
    )


@app.route("/programaciones/nueva")
@login_required
def programacion_nueva():
    """Selector de tarea para crear una programación real.

    Antes «Nueva programación» llevaba a la lista; ahora abre este paso
    previo y cada tarjeta continúa en la tarea con la opción «Programar»
    ya seleccionada.
    """
    if not current_user.has_role("admin"):
        abort(403)
    schedulable = _schedulable_tasks()
    catalog = {task["id"]: task for task in task_catalog.TASK_CATALOG}
    tasks = []
    for task_id, task in schedulable.items():
        meta = catalog.get(task_id, {})
        tasks.append(
            {
                "id": task_id,
                "name": task["name"],
                "summary": meta.get("summary", ""),
                "icon": meta.get("icon", "bi-lightning-charge"),
                "chips": meta.get("chips", []),
                "actions": task.get("actions") or [],
            }
        )
    return render_template("programacion_nueva.html", tasks=tasks)


@app.route("/programaciones/<int:schedule_id>/editar", methods=["GET", "POST"])
@login_required
def schedule_edit(schedule_id: int):
    """Edita una programación existente conservando su secreto cifrado."""
    if not current_user.has_role("admin"):
        abort(403)
    db_path = runtime.get_db_path()
    schedule = Schedule.get(schedule_id, db_path=db_path)
    if schedule is None:
        flash("La programación no existe.", "warning")
        return redirect(url_for("programaciones"))
    if not _can_manage_owner(schedule.owner_user_id):
        abort(403)

    tasks = _schedulable_tasks()
    if schedule.task_id not in tasks:
        flash(
            f"La tarea '{schedule.task_id}' ya no está disponible para "
            "programar. Elimina esta programación o vuelve a crearla desde "
            "el catálogo.",
            "warning",
        )
        return redirect(url_for("programaciones"))
    errors: list[str] = []
    warnings: list[str] = []
    values: dict[str, Any] = {}
    if request.method == "POST":
        updated, errors, warnings = _update_schedule_from_form(
            schedule, tasks, db_path, request.form, files=request.files
        )
        if updated is not None:
            for warning in warnings:
                flash(warning, "warning")
            flash(f"Programación '{updated}' actualizada.", "success")
            return redirect(url_for("programaciones"))
        values = dict(request.form)
    if not values:
        params = schedule.params or {}
        target_mode = str(params.get("target_mode") or "manual")
        action = str(params.get("action") or "TZ").upper()
        stored_value = str(params.get("value") or "")
        target_user = stored_value if action == "PASSWORD" else OS_PASSWORD_DEFAULT_USER
        firewall_op = "enable"
        firewall_ip = ""
        if action == "FIREWALL":
            if stored_value in {"enable", "disable"}:
                firewall_op = stored_value
            elif stored_value.startswith("allow:"):
                firewall_op = "allow"
                firewall_ip = stored_value.split(":", 1)[1]
        values = {
            "name": schedule.name,
            "task_id": schedule.task_id,
            "run_at": (schedule.run_at or "")[:16],
            "recurrence": schedule.recurrence,
            "target_mode": target_mode,
            "target_ip": params.get("target") if target_mode != "group" else "",
            "target_group": str(params.get("target_group_id") or ""),
            "admin_user": params.get("admin_user") or TARGET_ADMIN_USERNAME,
            "action": action,
            "value": stored_value,
            "target_user": target_user,
            "firewall_op": firewall_op,
            "firewall_ip": firewall_ip,
            "ntp_policy": params.get("ntp_policy") or "keep",
            "ntp_servers": ", ".join(params.get("ntp_servers") or []),
            "min_free_gb": params.get("min_free_gb") or APT_MIN_FREE_GB,
            "sync_rtc": bool(params.get("sync_rtc")),
            "force": bool(params.get("force")),
        }
    params = schedule.params or {}
    has_wallpaper = bool(params.get("wallpaper_path"))
    has_new_password_secret = bool(params.get("new_password_enc"))
    wallpaper_name = str(params.get("wallpaper_name") or "")

    inventory_groups = []
    group_scope = None if current_user.has_role("admin") else schedule.owner_user_id
    for group in HostGroup.all(db_path=db_path, owner_user_id=group_scope):
        data = group.to_dict()
        data["safety"] = _safety_for_group(group)
        inventory_groups.append(data)
    return render_template(
        "schedule_edit.html",
        schedule=schedule,
        values=values,
        errors=errors,
        warnings=warnings,
        tasks=[
            {
                "id": key,
                "name": value["name"],
                "actions": value.get("actions") or [],
                "fixed_value": value.get("fixed_value", ""),
            }
            for key, value in tasks.items()
        ],
        inventory_groups=inventory_groups,
        recurrence_labels={
            "once": "Una vez",
            "daily": "Diaria",
            "weekly": "Semanal",
        },
        default_min_free_gb=APT_MIN_FREE_GB,
        min_free_gb_floor=APT_MIN_FREE_GB_FLOOR,
        min_free_gb_ceiling=APT_MIN_FREE_GB_CEILING,
        has_wallpaper=has_wallpaper,
        wallpaper_name=wallpaper_name,
        has_new_password_secret=has_new_password_secret,
        os_password_default_user=OS_PASSWORD_DEFAULT_USER,
        os_password_min_length=OS_PASSWORD_MIN_LENGTH,
        os_password_max_length=OS_PASSWORD_MAX_LENGTH,
    )

def register(flask_app) -> None:
    app.apply(flask_app)

