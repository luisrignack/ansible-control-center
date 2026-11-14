"""Metricas, auditoria, usuarios y ajustes (solo admin).
"""

from __future__ import annotations

import logging

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from acc import runtime
from acc.runtime import (
    apply_dry_run,
    load_ldap_access_setting,
    persist_dry_run,
    persist_ldap_access,
    request_metrics,
)
from acc.security import (
    _client_ip,
    _current_owner_id,
    _is_safe_redirect,
    _wants_json,
    roles_required,
)
from acc.task_state import (
    _active_task_count,
    _queue_snapshot,
)
from config import (
    ADMIN_PASSWORD_MIN_LENGTH,
    SLOW_REQUEST_MS,
)
from models.ops import (
    HostGroup,
    Schedule,
)
from models.user import (
    AuthEvent,
    User,
)
from services import ldap_auth
from services.metrics import summarize

logger = logging.getLogger("acc")


from acc.web.registry import RouteRegistry

app = RouteRegistry()


@app.route("/metricas")
@roles_required("admin")
def metricas():
    """Compatibilidad: el rendimiento vive ahora en Sistema → Rendimiento."""
    return redirect(url_for("sistema", tab="rendimiento"), code=301)


@app.route("/api/metricas")
@roles_required("admin")
def api_metricas():
    """Mismo snapshot en JSON para automatización y comprobaciones de SLO."""
    snapshot = request_metrics.snapshot()
    response = jsonify(
        {
            "window": request_metrics.window,
            "slow_request_ms": SLOW_REQUEST_MS,
            "summary": summarize(snapshot),
            "queue": _queue_snapshot(),
            "endpoints": snapshot,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/audit")
@roles_required("admin")
def audit():
    """Compatibilidad: la auditoría vive ahora en Sistema → Auditoría."""
    return redirect(url_for("sistema", tab="auditoria"), code=301)


@app.route("/usuarios")
@roles_required("admin")
def usuarios():
    """Lista de todas las cuentas: local y LDAPS (solo el admin entra)."""
    ldap_on = load_ldap_access_setting()
    users = User.all(db_path=runtime.get_db_path())
    last_events = AuthEvent.recent(limit=200, db_path=runtime.get_db_path())
    last_login: dict[str, str] = {}
    for event in last_events:
        event_kind = event["event"] or ""
        if event_kind in ("login_success", "ldap_first_login"):
            last_login.setdefault(event["username"], event["timestamp"])
    return render_template(
        "users.html",
        users=users,
        roles_in_ui=("viewer", "operator", "admin"),
        ldap_supported=ldap_on,
        ldap_configured=ldap_auth.ldap_configured(),
        last_login=last_login,
        password_min_length=ADMIN_PASSWORD_MIN_LENGTH,
    )


@app.route("/usuarios/ldaps", methods=["POST"])
@roles_required("admin")
def usuarios_ldaps():
    """Conmuta el acceso LDAPS: solo el admin local lo cambia."""
    if current_user.auth_source != "local":
        abort(403)

    if not ldap_auth.ldap_configured():
        flash(
            "El acceso LDAPS no está configurado (falta la URL o el bind "
            "del directorio en .env.docker).",
            "danger",
        )
        return redirect(url_for("usuarios"))

    enabled = not load_ldap_access_setting()
    # Al ACTIVAR, comprueba antes que el bind del directorio funciona: no
    # dejar ON un acceso que en cada login dirá "Servidor LDAP no disponible".
    if enabled:
        try:
            ldap_auth.test_search_bind()
        except ldap_auth.LdapError as exc:
            logger.error(
                "Activación de LDAPS abortada: bind de búsqueda falló: %s",
                exc,
            )
            AuthEvent.record(
                current_user.username,
                _client_ip(),
                "ldap_access_activate_failed",
                str(exc),
                db_path=runtime.get_db_path(),
            )
            flash(
                "No se pudo activar el acceso LDAPS: el directorio no "
                f"aceptó el binding de búsqueda ({exc}). Revisa la URL, el "
                "bind y la contraseña/CA en .env.docker e inténtalo de nuevo.",
                "danger",
            )
            return redirect(url_for("usuarios"))
    persist_ldap_access(enabled)
    AuthEvent.record(
        current_user.username,
        _client_ip(),
        "ldap_access_changed",
        f"El admin local '{current_user.username}' "
        + ("activó" if enabled else "desactivó")
        + " el acceso por LDAPS",
        db_path=runtime.get_db_path(),
    )
    flash(
        "Acceso por LDAPS activado." if enabled else "Acceso por LDAPS desactivado.",
        "success" if enabled else "info",
    )
    return redirect(url_for("usuarios"))


@app.route("/usuarios/rol", methods=["POST"])
@roles_required("admin")
def usuarios_rol():
    """El admin local asigna (o retira) roles a cualquier cuenta."""
    username = (request.form.get("username") or "").strip()
    next_username = (request.form.get("next_username") or "").strip()
    if not username:
        flash("Nombre de usuario no indicado.", "danger")
        return redirect(url_for("usuarios"))

    if username == current_user.username:
        flash("No puedes cambiar tu propio rol desde aquí.", "danger")
        return redirect(url_for("usuarios"))

    target = User.get_by_username(username, db_path=runtime.get_db_path())
    if not target:
        flash(f"El usuario '{username}' no existe.", "danger")
        return redirect(url_for("usuarios"))

    new_role = request.form.get("role") or ""
    # Salva siempre un admin LOCAL activo: un admin LDAPS (dependiente del
    # directorio) no puede dejar el sistema sin administración local.
    if (
        target.is_admin
        and target.is_active
        and target.auth_source == "local"
        and new_role != "admin"
        and User.count_active_local_admins_excluding(username, db_path=runtime.get_db_path())
        == 0
    ):
        flash(
            f"'{username}' es el último administrador local activo: no "
            "puedes retirarle el rol admin.",
            "danger",
        )
        return redirect(url_for("usuarios"))

    try:
        User.set_role(username, new_role, db_path=runtime.get_db_path())
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("usuarios"))

    AuthEvent.record(
        username,
        _client_ip(),
        "role_updated",
        f"El admin '{current_user.username}' fijó rol " f"'{new_role}'",
        db_path=runtime.get_db_path(),
    )
    flash(f"Rol de '{username}' actualizado.", "success")
    return redirect(
        url_for("usuarios") + (f"#{next_username}" if next_username else "")
    )


def _local_password_error(password: str, confirm: str) -> str | None:
    """Valida una contraseña local antes de guardar su hash."""
    if not password:
        return "La contraseña es obligatoria."
    if password != confirm:
        return "La contraseña y su confirmación no coinciden."
    if len(password) < ADMIN_PASSWORD_MIN_LENGTH:
        return (
            f"La contraseña debe tener al menos {ADMIN_PASSWORD_MIN_LENGTH} "
            "caracteres."
        )
    if len(password) > 256:
        return "La contraseña es demasiado larga (máximo 256 caracteres)."
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        return "La contraseña no puede contener caracteres de control."
    return None


@app.route("/usuarios/crear", methods=["POST"])
@roles_required("admin")
def usuarios_crear():
    """Crea una cuenta local con contraseña propia y rol explícito."""
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("password_confirm") or ""
    role = (request.form.get("role") or "viewer").strip()
    is_active = (request.form.get("active") or "").strip().lower() in {
        "1",
        "true",
        "si",
        "sí",
        "yes",
        "on",
    }

    password_error = _local_password_error(password, confirm)
    if password_error:
        flash(password_error, "danger")
        return redirect(url_for("usuarios"))

    try:
        user = User.create_local(
            username,
            password,
            role=role,
            is_active=is_active,
            db_path=runtime.get_db_path(),
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("usuarios"))

    AuthEvent.record(
        current_user.username,
        _client_ip(),
        "local_user_created",
        f"Creó la cuenta local '{user.username}' con rol '{user.role}'"
        + (" (inactiva)" if not user.is_active else ""),
        db_path=runtime.get_db_path(),
    )
    flash(
        f"Usuario local '{user.username}' creado con rol '{user.role}'.",
        "success",
    )
    return redirect(url_for("usuarios") + f"#{user.username}")


@app.route("/usuarios/password", methods=["POST"])
@roles_required("admin")
def usuarios_password():
    """Cambia la contraseña de una cuenta local (nunca de una LDAPS)."""
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    confirm = request.form.get("password_confirm") or ""
    if not username:
        flash("Nombre de usuario no indicado.", "danger")
        return redirect(url_for("usuarios"))

    password_error = _local_password_error(password, confirm)
    if password_error:
        flash(password_error, "danger")
        return redirect(url_for("usuarios"))

    try:
        User.set_password(username, password, db_path=runtime.get_db_path())
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("usuarios"))

    AuthEvent.record(
        current_user.username,
        _client_ip(),
        "password_changed",
        f"El admin '{current_user.username}' cambió la contraseña de '{username}'",
        db_path=runtime.get_db_path(),
    )
    flash(f"Contraseña de '{username}' actualizada.", "success")
    return redirect(url_for("usuarios") + f"#{username}")


@app.route("/usuarios/eliminar", methods=["POST"])
@roles_required("admin")
def usuarios_eliminar():
    """Elimina una cuenta local y transfiere sus datos al admin actual.

    Las cuentas LDAPS no se borran aquí: su identidad vive en el directorio.
    Las programaciones y grupos privados del usuario eliminado pasan al
    administrador que ejecuta el borrado para no quedar huérfanos ni
    exponerse automáticamente a todos los usuarios.
    """
    username = (request.form.get("username") or "").strip()
    confirmation = (request.form.get("confirm_username") or "").strip()
    if not username:
        flash("Nombre de usuario no indicado.", "danger")
        return redirect(url_for("usuarios"))
    if confirmation != username:
        flash(
            "Para eliminar la cuenta escribe su nombre exacto en la confirmación.",
            "danger",
        )
        return redirect(url_for("usuarios"))

    if username == current_user.username:
        flash("No puedes eliminar tu propia cuenta.", "danger")
        return redirect(url_for("usuarios"))

    target = User.get_by_username(username, db_path=runtime.get_db_path())
    if target is None:
        flash(f"El usuario '{username}' no existe.", "danger")
        return redirect(url_for("usuarios"))
    if target.auth_source != "local":
        flash(
            "Las cuentas LDAPS se gestionan en el directorio corporativo. "
            "Puedes desactivarlas aquí para bloquear su acceso.",
            "danger",
        )
        return redirect(url_for("usuarios"))

    if (
        target.is_admin
        and target.is_active
        and User.count_active_local_admins_excluding(username, db_path=runtime.get_db_path())
        == 0
    ):
        flash(
            f"'{username}' es el último administrador local activo: no "
            "puedes eliminarlo.",
            "danger",
        )
        return redirect(url_for("usuarios"))

    owner_id = _current_owner_id()
    moved_schedules = Schedule.reassign_owner(
        target.id, owner_id, db_path=runtime.get_db_path()
    )
    moved_groups = HostGroup.reassign_owner(target.id, owner_id, db_path=runtime.get_db_path())
    try:
        deleted = User.delete_local(username, db_path=runtime.get_db_path())
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("usuarios"))
    if not deleted:
        flash(f"El usuario '{username}' no existe.", "danger")
        return redirect(url_for("usuarios"))

    AuthEvent.record(
        current_user.username,
        _client_ip(),
        "local_user_deleted",
        f"Eliminó la cuenta local '{username}'; transfirió "
        f"{moved_schedules} programación(es) y {moved_groups} grupo(s)",
        db_path=runtime.get_db_path(),
    )
    detail = ""
    if moved_schedules or moved_groups:
        detail = (
            f" Se transfirieron a tu cuenta {moved_schedules} programación(es) "
            f"y {moved_groups} grupo(s) de inventario."
        )
    flash(f"Usuario local '{username}' eliminado.{detail}", "success")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/estado", methods=["POST"])
@roles_required("admin")
def usuarios_estado():
    """Activa o desactiva una cuenta (bloquea el acceso, sin borrar datos)."""
    username = (request.form.get("username") or "").strip()
    is_active = (request.form.get("active") or "").strip().lower() in {
        "1",
        "true",
        "si",
        "sí",
        "yes",
        "on",
    }
    if not username:
        flash("Nombre de usuario no indicado.", "danger")
        return redirect(url_for("usuarios"))

    if username == current_user.username:
        flash("No puedes desactivar tu propia cuenta.", "danger")
        return redirect(url_for("usuarios"))

    target = User.get_by_username(username, db_path=runtime.get_db_path())
    if target is None:
        flash(f"El usuario '{username}' no existe.", "danger")
        return redirect(url_for("usuarios"))

    # Salva siempre un admin LOCAL activo: un admin LDAPS no puede
    # desactivar la única cuenta de administración local del sistema.
    if (
        not is_active
        and target.is_admin
        and target.is_active
        and target.auth_source == "local"
        and User.count_active_local_admins_excluding(username, db_path=runtime.get_db_path())
        == 0
    ):
        flash(
            f"'{username}' es el último administrador local activo: no "
            "puedes desactivarlo.",
            "danger",
        )
        return redirect(url_for("usuarios"))

    User.set_active(username, is_active, db_path=runtime.get_db_path())
    AuthEvent.record(
        username,
        _client_ip(),
        "account_state_changed",
        f"El admin '{current_user.username}' "
        + ("activó" if is_active else "desactivó")
        + " la cuenta",
        db_path=runtime.get_db_path(),
    )
    flash(
        f"Cuenta de '{username}' " + ("activada." if is_active else "desactivada."),
        "success",
    )
    return redirect(url_for("usuarios"))


@app.route("/settings/dry-run", methods=["POST"])
@roles_required("admin")
def update_dry_run():
    """Activa o desactiva el modo simulación desde la interfaz."""
    payload = request.get_json(silent=True)
    if request.is_json:
        raw = (payload or {}).get("enabled")
    elif request.form.get("dry_run_form") == "1":
        # Formulario sin JavaScript: una casilla desmarcada no envía
        # ``enabled``, así que su ausencia significa "desactivado".
        raw = request.form.get("enabled", "0")
    else:
        raw = None

    if isinstance(raw, bool):
        enabled = raw
    else:
        normalized = str(raw).strip().lower() if raw is not None else ""
        if normalized in {"1", "true", "yes", "on", "si", "sí"}:
            enabled = True
        elif normalized in {"0", "false", "no", "off"}:
            enabled = False
        else:
            message = "Valor de simulación no válido."
            if _wants_json():
                return jsonify({"success": False, "message": message}), 400
            flash(message, "danger")
            return redirect(url_for("panel"))

    if _active_task_count():
        message = (
            "No se puede cambiar el modo mientras hay operaciones en curso. "
            "Espera a que terminen e inténtalo de nuevo."
        )
        if _wants_json():
            return jsonify({"success": False, "message": message}), 409
        flash(message, "warning")
        return redirect(url_for("panel"))

    try:
        persist_dry_run(enabled)
        apply_dry_run(enabled)
    except Exception:
        logger.exception("No se pudo actualizar el modo simulación")
        message = "No se pudo guardar el cambio de modo."
        if _wants_json():
            return jsonify({"success": False, "message": message}), 500
        flash(message, "danger")
        return redirect(url_for("panel"))

    message = (
        "Modo simulación activado: no se aplicarán cambios en los equipos."
        if enabled
        else "Ejecución real activada: las operaciones modificarán los equipos."
    )
    logger.info(
        "Modo simulación %s por %s desde %s",
        "activado" if enabled else "desactivado",
        current_user.username,
        _client_ip(),
    )
    AuthEvent.record(
        username=current_user.username,
        ip=_client_ip(),
        event="dry_run_on" if enabled else "dry_run_off",
        detail="Cambio desde la interfaz web",
        db_path=runtime.get_db_path(),
    )

    if _wants_json():
        return jsonify(
            {
                "success": True,
                "dry_run": enabled,
                "message": message,
            }
        )

    flash(message, "info")
    target = request.form.get("next", "")
    return redirect(target if _is_safe_redirect(target) else url_for("panel"))

def register(flask_app) -> None:
    app.apply(flask_app)

