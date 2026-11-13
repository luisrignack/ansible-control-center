"""Programaciones: validacion de formularios y lanzamiento.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user

from acc import runtime
from acc.approvals import _approval_params
from acc.execution import execute_with_progress
from acc.operations import (
    _form_bool,
    _validate_scheduled_params,
)
from acc.safety import (
    _safety_for_group,
    _safety_for_target,
)
from acc.security import (
    _authenticated,
    _current_owner_id,
)
from acc.task_state import (
    create_task,
    remove_task,
    update_task,
)
from models.ops import (
    HostGroup,
    Schedule,
)
from services import scheduler, task_catalog
from services.host_resolver import pin_target
from services.job_queue import JobQueueFull
from services.wallpaper import (
    clone_wallpaper_for_run,
    remote_wallpaper_uri,
    remove_wallpaper,
    save_wallpaper,
)

logger = logging.getLogger("acc")


def _schedulable_tasks() -> dict[str, dict[str, Any]]:
    """Tareas activas que admiten ejecución desatendida."""
    return {
        task["id"]: task
        for task in task_catalog.TASK_CATALOG
        if task.get("status") == "active" and task.get("schedulable", True)
    }


def _validate_schedule_form(
    tasks: dict[str, dict[str, Any]],
    db_path: str,
    form,
    *,
    files=None,
    existing: Schedule | None = None,
    owner_user_id: int = 0,
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """Valida los campos comunes de alta y edición de programaciones.

    Devuelve ``(datos, errores, avisos)``. Los datos incluyen nombre, tarea,
    fecha, recurrencia, parámetros operativos y, cuando corresponde, el
    secreto nuevo en claro para que el llamador lo cifre antes de guardarlo.
    En el alta se exige la contraseña maestra; en la edición se conserva la
    que ya estaba cifrada.
    """
    errors: list[str] = []
    warnings: list[str] = []
    name = form.get("name", "").strip()
    task_id = form.get("task_id", "").strip()
    run_at_raw = form.get("run_at", "").strip()
    recurrence = form.get("recurrence", "once").strip().lower() or "once"
    if task_id not in tasks:
        return None, ["Selecciona una tarea activa del catálogo."], warnings
    task = tasks[task_id]
    try:
        run_at = datetime.strptime(run_at_raw, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
    except ValueError:
        return (
            None,
            [
                (
                    "Fecha/hora inválida; usa el selector o el formato "
                    "AAAA-MM-DDTHH:MM (hora UTC)."
                )
            ],
            warnings,
        )
    master_password = form.get("master_password", "")
    if existing is None and not master_password:
        return (
            None,
            [
                (
                    "La contraseña maestra del vault es obligatoria para operar "
                    "desatendidamente."
                )
            ],
            warnings,
        )
    target_mode = form.get("target_mode", "manual").strip().lower() or "manual"
    target_group_id = None
    schedule_group = None
    pinned_ip = None
    if target_mode == "group":
        gid = form.get("target_group", "").strip()
        if gid.isdigit():
            schedule_group = HostGroup.get(gid, db_path=db_path)
        if schedule_group is None:
            return None, ["Selecciona un grupo del inventario válido."], warnings
        if (
            schedule_group.owner_user_id not in {0, int(owner_user_id or 0)}
            and not (_authenticated() and current_user.has_role("admin"))
        ):
            return (
                None,
                [
                    (
                        "El grupo seleccionado pertenece a otro usuario. Elige "
                        "uno de tu inventario o un grupo compartido."
                    )
                ],
                warnings,
            )
        target_group_id = int(gid)
    else:
        pinned_ip, ip_error = pin_target(form.get("target_ip", "").strip())
        if ip_error or not pinned_ip:
            return None, [ip_error or "IP objetivo no válida."], warnings

    action = form.get("action", "").strip().upper()
    task_actions = task.get("actions") or ["TZ", "TIME"]
    if not action:
        action = str(task_actions[0]).upper() if len(task_actions) == 1 else "TZ"
    target_user_raw = form.get("target_user", "")
    new_password = form.get("new_password", "")
    new_password_confirm = form.get("new_password_confirm", "")
    value = form.get("value", "")
    if action == "FIREWALL":
        # El motor guarda la operación completa en ``value``; el formulario
        # la separa en operación + IP para mayor claridad.
        firewall_op = form.get("firewall_op", "").strip().lower()
        firewall_ip = form.get("firewall_ip", "").strip()
        value = f"allow:{firewall_ip}" if firewall_op == "allow" else firewall_op

    existing_params = (existing.params or {}) if existing is not None else {}
    # En la edición de una contraseña ya programada se permite dejarla en
    # blanco para conservar el secreto cifrado existente.
    require_password = action == "PASSWORD" and not existing_params.get(
        "new_password_enc"
    )

    # Mismas reglas que la ejecución manual: una programación no puede
    # saltarse la política de hora/NTP, usar una acción que la tarea no admite
    # ni apuntar a un grupo con otra plataforma.
    validated, schedule_errors = _validate_scheduled_params(
        task,
        form.get("admin_user", ""),
        action,
        value,
        form.get("ntp_policy", "keep"),
        form.get("ntp_servers", ""),
        platform=schedule_group.os_flavor if schedule_group else "",
        group_name=schedule_group.name if schedule_group else "",
        min_free_gb_raw=form.get("min_free_gb", ""),
        target_user_raw=target_user_raw,
        new_password=new_password,
        new_password_confirm=new_password_confirm,
        require_password=require_password,
    )
    if schedule_errors or validated is None:
        return (
            None,
            schedule_errors or ["No se pudo validar la programación."],
            warnings,
        )
    # Las programaciones no admiten excepción: si el horario elegido cae
    # fuera de la ventana del grupo, se rechaza en vez de ejecutar hosts
    # críticos sin supervisión.
    if schedule_group is not None:
        schedule_safety = _safety_for_group(schedule_group, now=run_at)
    else:
        schedule_safety = _safety_for_target(None, pinned_ip, now=run_at)
    if schedule_safety["blocked"]:
        return (
            None,
            [
                "No se puede programar fuera de la ventana de mantenimiento: "
                + schedule_safety["reasons"][0]
            ],
            warnings,
        )
    warnings.extend(schedule_safety["warnings"])
    if task.get("destructive"):
        warnings.append(
            f"La tarea '{task['name']}' es destructiva: se ejecutará "
            "desatendida sin aprobación manual. Revisa el objetivo y la "
            "ventana de mantenimiento."
        )

    # Imagen del tapiz: la nueva imagen se guarda en el directorio persistente
    # de programaciones; si la edición no sube una nueva, se conserva la que
    # ya tenía la programación.
    wallpaper_path = ""
    wallpaper_name = ""
    wallpaper_ext = ""
    if validated["action"] == "WALLPAPER":
        upload = files.get("wallpaper_file") if files is not None else None
        has_upload = bool(upload is not None and getattr(upload, "filename", ""))
        if has_upload:
            try:
                saved_wallpaper = save_wallpaper(upload, persistent=True)
            except ValueError as exc:
                return None, [str(exc)], warnings
            wallpaper_path = saved_wallpaper["path"]
            wallpaper_name = saved_wallpaper["name"]
            wallpaper_ext = saved_wallpaper["ext"]
        else:
            wallpaper_path = str(existing_params.get("wallpaper_path") or "")
            wallpaper_name = str(existing_params.get("wallpaper_name") or "tapiz")
            wallpaper_ext = str(existing_params.get("wallpaper_ext") or "")
            if not wallpaper_path or not os.path.isfile(wallpaper_path):
                return (
                    None,
                    [
                        (
                            "Selecciona una imagen para el tapiz. La programación "
                            "no conserva una imagen válida."
                        )
                    ],
                    warnings,
                )
            if not wallpaper_ext:
                wallpaper_ext = os.path.splitext(wallpaper_path)[1].lstrip(".")

    schedule_params = _approval_params(
        {
            "target": (
                f"inventory:{target_group_id}"
                if target_mode == "group"
                else pinned_ip
            ),
            "target_mode": target_mode,
            "target_group_id": target_group_id,
            "task_id": task_id,
            "task_name": task["name"],
            "admin_user": validated["admin_user"],
            "origin": "stored",
            "action": validated["action"],
            "value": (
                wallpaper_name
                if validated["action"] == "WALLPAPER"
                else validated["value"]
            ),
            "time_mode": "utc",
            "time_zone": "",
            "time_fold": None,
            "ntp_policy": validated["ntp_policy"],
            "ntp_servers": validated["ntp_servers"],
            "sync_rtc": _form_bool("sync_rtc", form),
            "force": _form_bool("force", form),
            "min_free_gb": validated["min_free_gb"],
            "wallpaper_path": wallpaper_path,
            "wallpaper_name": wallpaper_name,
            "wallpaper_ext": wallpaper_ext,
        }
    )
    return {
        "name": name,
        "task_id": task_id,
        "run_at": run_at,
        "recurrence": recurrence,
        "params": schedule_params,
        "master_password": master_password,
        "new_password": (
            new_password if validated["action"] == "PASSWORD" else ""
        ),
    }, errors, warnings


def _create_schedule_from_form(
    tasks: dict[str, dict[str, Any]],
    db_path: str,
    form,
    *,
    files=None,
) -> tuple[str | None, list[str], list[str]]:
    """Valida y crea una programación desde un formulario.

    Devuelve ``(nombre_creado, errores, avisos)``. No redirige ni escribe
    flashes para que lo usen el formulario de ejecución y las pruebas sin
    duplicar validaciones. Los secretos se cifran antes de persistirlos.
    """
    owner_user_id = _current_owner_id()
    data, errors, warnings = _validate_schedule_form(
        tasks, db_path, form, files=files, owner_user_id=owner_user_id
    )
    if data is None:
        return None, errors, warnings
    params = {
        **data["params"],
        "master_password_enc": scheduler.encrypt_secret(data["master_password"]),
    }
    if data["new_password"]:
        params["new_password_enc"] = scheduler.encrypt_secret(data["new_password"])
    try:
        Schedule.create(
            data["name"],
            data["task_id"],
            data["run_at"],
            params,
            recurrence=data["recurrence"],
            created_by=getattr(current_user, "username", "") or "",
            owner_user_id=owner_user_id,
            db_path=db_path,
        )
    except ValueError as exc:
        # No dejar una imagen persistente huérfana si el alta falla.
        remove_wallpaper(data["params"].get("wallpaper_path"))
        return None, [str(exc)], warnings
    return data["name"], errors, warnings


def _update_schedule_from_form(
    schedule: Schedule,
    tasks: dict[str, dict[str, Any]],
    db_path: str,
    form,
    *,
    files=None,
) -> tuple[str | None, list[str], list[str]]:
    """Valida y actualiza una programación conservando sus secretos cifrados."""
    data, errors, warnings = _validate_schedule_form(
        tasks,
        db_path,
        form,
        files=files,
        existing=schedule,
        owner_user_id=schedule.owner_user_id,
    )
    if data is None:
        return None, errors, warnings
    old_params = schedule.params or {}
    encrypted = old_params.get("master_password_enc")
    if not encrypted:
        remove_wallpaper(data["params"].get("wallpaper_path"))
        return (
            None,
            [
                (
                    "La programación no conserva la contraseña cifrada. Elimínala "
                    "y vuelve a crearla desde la tarjeta de la tarea."
                )
            ],
            warnings,
        )
    params = {**data["params"], "master_password_enc": encrypted}
    action = data["params"].get("action", "")
    if action == "PASSWORD":
        if data["new_password"]:
            params["new_password_enc"] = scheduler.encrypt_secret(
                data["new_password"]
            )
        else:
            existing_secret = old_params.get("new_password_enc")
            if not existing_secret:
                remove_wallpaper(data["params"].get("wallpaper_path"))
                return (
                    None,
                    [
                        (
                            "Indica la nueva contraseña: esta programación no "
                            "conserva un secreto cifrado."
                        )
                    ],
                    warnings,
                )
            params["new_password_enc"] = existing_secret
    else:
        params.pop("new_password_enc", None)
    try:
        Schedule.update(
            schedule.id,
            data["name"],
            data["run_at"],
            params,
            recurrence=data["recurrence"],
            db_path=db_path,
        )
    except ValueError as exc:
        new_wallpaper = data["params"].get("wallpaper_path")
        if new_wallpaper and new_wallpaper != old_params.get("wallpaper_path"):
            remove_wallpaper(new_wallpaper)
        return None, [str(exc)], warnings
    old_wallpaper = old_params.get("wallpaper_path")
    new_wallpaper = data["params"].get("wallpaper_path")
    if old_wallpaper and old_wallpaper != new_wallpaper:
        remove_wallpaper(old_wallpaper)
    return data["name"], errors, warnings


def _handle_schedule_from_task_form():
    """Crea una programación desde el modo «Programar» del formulario."""
    if not current_user.has_role("admin"):
        abort(403)
    form = request.form
    task_id = form.get("task", "").strip()
    if form.get("password_origin", "stored").strip() != "stored":
        flash(
            "Las programaciones usan credenciales guardadas. Cambia el origen "
            "de contraseñas a «Contraseñas guardadas en el sistema».",
            "danger",
        )
        return redirect(url_for("task_view", task_id=task_id))
    schedule_form = {
        "name": form.get("schedule_name", "").strip(),
        "task_id": task_id,
        "run_at": form.get("run_at", "").strip(),
        "recurrence": form.get("recurrence", "once").strip().lower(),
        "target_mode": form.get("target_mode", "manual"),
        "target_group": form.get("target_group", ""),
        "target_ip": form.get("target_ip", ""),
        "admin_user": form.get("admin_user", ""),
        "master_password": form.get("master_password", ""),
        "action": form.get("action", ""),
        "value": form.get("value", ""),
        "ntp_policy": form.get("ntp_policy", "keep"),
        "ntp_servers": form.get("ntp_servers", ""),
        "min_free_gb": form.get("min_free_gb", ""),
        "sync_rtc": form.get("sync_rtc", ""),
        "force": form.get("force", ""),
        # Campos propios de cada tarjeta; la validación común los ignora
        # cuando la acción no los usa.
        "target_user": form.get("target_user", ""),
        "new_password": form.get("new_password", ""),
        "new_password_confirm": form.get("new_password_confirm", ""),
        "firewall_op": form.get("firewall_op", ""),
        "firewall_ip": form.get("firewall_ip", ""),
    }
    created, errors, warnings = _create_schedule_from_form(
        _schedulable_tasks(),
        runtime.get_db_path(),
        schedule_form,
        files=request.files,
    )
    for error in errors:
        flash(error, "danger")
    for warning in warnings:
        flash(warning, "warning")
    if created is None:
        return redirect(url_for("task_view", task_id=task_id))
    flash(
        f"Programación '{created}' creada. Se ejecutará sola (UTC) mientras "
        "esté activa.",
        "success",
    )
    return redirect(url_for("programaciones"))


def launch_scheduled(schedule, params: dict[str, Any]) -> bool:
    """Dispara una programación vencida purando la cola de trabajos normal.

    ``params`` lleva los parámetros guardados (sin secretos en claro) y la
    contraseña maestra cifrada. Registra el disparo y calcula la siguiente
    ejecución según la recurrencia (una vez se desactiva tras el disparo).
    """
    db_path = runtime.get_db_path()
    master_password = scheduler.decrypt_secret(params.get("master_password_enc", ""))
    if not master_password:
        Schedule.mark_run(
            schedule["id"],
            error="Contraseña maestra no descifrable; la clave de cifrado "
            "cambió o falta.",
            disable=True,
            db_path=db_path,
        )
        return False

    action = str(params.get("action") or "TZ").upper()
    scheduled_new_password = ""
    if action == "PASSWORD":
        scheduled_new_password = scheduler.decrypt_secret(
            params.get("new_password_enc", "")
        )
        if not scheduled_new_password:
            Schedule.mark_run(
                schedule["id"],
                error="La nueva contraseña programada no se puede descifrar; "
                "edita la programación y vuelve a indicarla.",
                disable=True,
                db_path=db_path,
            )
            return False

    # Defensa en profundidad: una fila antigua o editada a mano no debe
    # saltarse las reglas que la ejecución manual aplica. La plataforma se
    # resuelve con el grupo actual: si cambió, el disparo se rechaza.
    target_group = None
    if (
        params.get("target_mode") == "group"
        and params.get("target_group_id") is not None
    ):
        target_group = HostGroup.get(params["target_group_id"], db_path=db_path)
    validated, errors = _validate_scheduled_params(
        task_catalog.get_task(schedule["task_id"]),
        params.get("admin_user", ""),
        params.get("action", "TZ"),
        params.get("value", ""),
        params.get("ntp_policy", "keep"),
        ",".join(str(server) for server in (params.get("ntp_servers") or [])),
        platform=target_group.os_flavor if target_group else "",
        group_name=target_group.name if target_group else "",
        min_free_gb_raw=str(params.get("min_free_gb", "")),
        target_user_raw=params.get("value", "") if action == "PASSWORD" else "",
        new_password=scheduled_new_password,
        new_password_confirm=scheduled_new_password,
        require_password=action == "PASSWORD",
    )
    if errors or validated is None:
        Schedule.mark_run(
            schedule["id"],
            error="; ".join(errors)[:300],
            disable=True,
            db_path=db_path,
        )
        return False

    target = params.get("target", "")
    if params.get("target_mode") == "group":
        if params.get("target_group_id") is None or target_group is None:
            Schedule.mark_run(
                schedule["id"],
                error="La programación apuntaba a un grupo eliminado.",
                disable=True,
                db_path=db_path,
            )
            return False
        target = f"inventory:{params['target_group_id']}"

    # Las programaciones no tienen excepción: si al vencer caen fuera de la
    # ventana, se posponen hasta la próxima apertura sin desactivarse.
    schedule_safety = _safety_for_target(
        params.get("target_group_id") if target_group is not None else None,
        target,
    )
    if schedule_safety["blocked"]:
        delay = 3600
        next_open = schedule_safety.get("next_open")
        if next_open:
            try:
                delta = datetime.fromisoformat(next_open) - datetime.now(UTC)
                delay = max(60, int(delta.total_seconds()))
            except ValueError:
                pass
        Schedule.mark_run(
            schedule["id"],
            error=(
                "Pospuesta por ventana de mantenimiento: "
                + schedule_safety["reasons"][0]
            )[:300],
            retry_delay_seconds=delay,
            db_path=db_path,
        )
        return False

    # La imagen programada se conserva para futuras ejecuciones: se copia a un
    # temporal que el propio lote borrará al terminar.
    wallpaper_run_path = ""
    wallpaper_uri = ""
    wallpaper_name = str(params.get("wallpaper_name") or "")
    if validated["action"] == "WALLPAPER":
        try:
            wallpaper_run_path = clone_wallpaper_for_run(
                params.get("wallpaper_path")
            )
        except (OSError, ValueError) as exc:
            Schedule.mark_run(
                schedule["id"],
                error=str(exc)[:300],
                disable=True,
                db_path=db_path,
            )
            return False
        wallpaper_ext = str(params.get("wallpaper_ext") or "") or (
            os.path.splitext(wallpaper_run_path)[1].lstrip(".")
        )
        wallpaper_uri = remote_wallpaper_uri(wallpaper_ext)

    tid = create_task()
    update_task(
        tid,
        {
            "status": "pending",
            "progress": 0,
            "message": "Lanzada por programación; en cola…",
            "mode": "execute",
            "action": validated["action"],
            "completed": False,
        },
    )
    try:
        runtime.get_job_queue().submit(
            tid,
            execute_with_progress,
            tid,
            target,
            validated["admin_user"],
            "stored",
            master_password,
            validated["action"],
            validated["value"],
            [],
            mode="execute",
            time_mode=params.get("time_mode", "utc"),
            time_zone=params.get("time_zone", ""),
            time_fold=params.get("time_fold"),
            ntp_policy=validated["ntp_policy"],
            ntp_servers=validated["ntp_servers"],
            sync_rtc=bool(params.get("sync_rtc")),
            force=bool(params.get("force")),
            operator=f"agenda:{schedule['name']}",
            task_id=schedule["task_id"],
            min_free_gb=validated.get("min_free_gb"),
            wallpaper_path=wallpaper_run_path,
            wallpaper_uri=wallpaper_uri,
            wallpaper_name=wallpaper_name,
            new_password=scheduled_new_password,
        )
    except JobQueueFull as exc:
        remove_task(tid)
        remove_wallpaper(wallpaper_run_path)
        # Fallo transitorio de capacidad: no se desactiva la programación; se
        # pospone el siguiente intento para no martillear la cola.
        Schedule.mark_run(
            schedule["id"],
            error=f"Cola llena: {str(exc)[:120]}",
            retry_delay_seconds=scheduler.SCHEDULE_RETRY_DELAY_SECONDS,
            db_path=db_path,
        )
        return False
    except Exception:
        remove_wallpaper(wallpaper_run_path)
        raise

    schedule_row = Schedule.get(schedule["id"], db_path=db_path)
    next_run = schedule_row.next_run_from() if schedule_row else None
    Schedule.mark_run(
        schedule["id"],
        batch_id="",
        error="",
        next_run=next_run,
        disable=next_run is None,
        db_path=db_path,
    )
    logger.info(
        "Programación '%s' lanzada como tarea %s",
        schedule["name"],
        tid,
    )
    return True
