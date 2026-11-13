"""Rutas y utilidades de autenticacion.
"""

from __future__ import annotations

import logging

from flask import flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

import config
from acc import runtime
from acc.extensions import login_manager
from acc.runtime import (
    load_ldap_access_setting,
)
from acc.security import (
    _check_rate_limit,
    _client_ip,
    _is_safe_redirect,
    _rate_limited_response,
)
from config import (
    LOGIN_LOCKOUT_MINUTES,
    LOGIN_LOCKOUT_THRESHOLD,
    LOGIN_RATE_LIMIT,
    LOGIN_RATE_WINDOW,
)
from models.db import get_connection
from models.user import (
    AuthEvent,
    User,
)
from services import ldap_auth
from services.csrf import rotate_csrf_token
from services.rate_limiter import LIMITER

logger = logging.getLogger("acc")


from acc.web.registry import RouteRegistry

app = RouteRegistry()


@login_manager.user_loader
def load_user(user_id: str):
    try:
        user = User.get_by_id(int(user_id), db_path=runtime.get_db_path())
    except (TypeError, ValueError):
        return None
    # Una cuenta desactivada pierde el acceso de inmediato, aunque conserve la
    # cookie de sesión o la cookie "remember" de Flask-Login.
    if user is None or not user.is_active:
        return None
    return user


@app.route("/healthz")
def healthz():
    """Comprobación de salud para Docker/balanceadores (sin autenticación).

    Solo confirma que el proceso responde y que SQLite es accesible; no expone
    configuración ni datos.
    """
    try:
        with get_connection(runtime.get_db_path()) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        logger.exception("Healthcheck: base de datos no disponible")
        response = jsonify({"status": "error", "database": "unavailable"})
        response.status_code = 503
    else:
        response = jsonify({"status": "ok", "database": "ok"})
    response.headers["Cache-Control"] = "no-store"
    return response


def _finish_session_login(user: User, client_ip: str, event: str, detail: str):
    """Completa el flujo común tras validar credenciales (local o LDAPS)."""
    user.register_success(db_path=runtime.get_db_path())
    AuthEvent.record(
        user.username,
        client_ip,
        event,
        detail,
        db_path=runtime.get_db_path(),
    )
    LIMITER.reset(f"login:{client_ip}")
    # Evita fijación de sesión: se descarta cualquier estado previo antes de
    # emitir la cookie autenticada.
    session.clear()
    rotate_csrf_token()
    session.permanent = True
    login_user(user, remember=bool(request.form.get("remember")))
    next_url = request.args.get("next") or request.form.get("next")
    return redirect(
        next_url if next_url and _is_safe_redirect(next_url) else url_for("index")
    )


def _register_login_failure(
    user: User | None, username: str, client_ip: str, detail: str = ""
) -> None:
    """Registra un intento fallido (bloqueo, evento y flash)."""
    locked = False
    if user is not None:
        try:
            locked = user.register_failure(
                LOGIN_LOCKOUT_THRESHOLD,
                LOGIN_LOCKOUT_MINUTES,
                db_path=runtime.get_db_path(),
            )
        except Exception:  # pragma: no cover - no tumbar el login
            logger.exception("No se pudo registrar el fallo de login")
    AuthEvent.record(
        username,
        client_ip,
        "account_locked" if locked else "login_failure",
        ("Cuenta bloqueada por intentos fallidos" if locked else detail)
        or "Credenciales incorrectas",
        db_path=runtime.get_db_path(),
    )
    logger.warning("Intento de login fallido para usuario=%r", username)
    if locked:
        flash(
            "Demasiados intentos fallidos: la cuenta quedó bloqueada "
            f"durante {LOGIN_LOCKOUT_MINUTES} minutos.",
            "danger",
        )
    else:
        flash("Usuario o contraseña incorrectos.", "danger")


def _try_ldap_login(
    username: str, password: str, user: User | None, client_ip: str
) -> None:
    """Intenta el login corporativo LDAPS para el usuario tipeado.

    Solo se llama cuando la cuenta local no valida contraseñas (o no
    existe) y LDAPS está habilitado. Devuelve la respuesta de la sesión
    creada o ``None``.
    """
    # Una cuenta local preexistente con ese nombre nunca se autentica vía
    # LDAP: evita que un uid del directorio tome un rol local (p. ej. admin).
    # El mensaje al usuario es genérico (no revela qué nombres son cuentas
    # locales); el detalle queda en el evento de auditoría y el log.
    if user is not None and user.auth_source == "local":
        AuthEvent.record(
            username,
            client_ip,
            "login_rejected_local_account",
            "El nombre corresponde a una cuenta local; se negó el login LDAP",
            db_path=runtime.get_db_path(),
        )
        logger.info("Login LDAP rechazado: %r es una cuenta local", username)
        flash("Usuario o contraseña incorrectos.", "danger")
        return

    if user is not None and not user.is_active:
        AuthEvent.record(
            username,
            client_ip,
            "login_disabled",
            "Cuenta LDAP desactivada por el admin",
            db_path=runtime.get_db_path(),
        )
        flash(
            "La cuenta está desactivada. Pide al administrador que la "
            "active de nuevo.",
            "danger",
        )
        return

    try:
        ldap_user = ldap_auth.authenticate(username, password)
    except ldap_auth.LdapError as exc:
        logger.error("LDAPS no disponible para %s: %s", username, exc)
        AuthEvent.record(
            username,
            client_ip,
            "ldap_unreachable",
            str(exc),
            db_path=runtime.get_db_path(),
        )
        flash(
            "Servidor LDAP no disponible. Inténtalo más tarde o pregunta "
            "al administrador.",
            "danger",
        )
        return

    if ldap_user is None:
        _register_login_failure(
            user, username, client_ip, "Credenciales LDAPS incorrectas"
        )
        return

    first_login = False
    if user is None:
        # Cuenta sombra corporativa: sin contraseña local, rol base y el
        # admin local decide el rol definitivo en /usuarios.
        try:
            user = User.create_or_update(
                username,
                "",
                db_path=runtime.get_db_path(),
                role=config.LDAP_DEFAULT_ROLE,
                auth_source="ldaps",
            )
        except ValueError:
            # Carrera con la creación local: el upsert se negó a
            # sobrescribir una cuenta local que apareció entre la consulta
            # y el INSERT. Mensaje genérico; sin sesión.
            AuthEvent.record(
                username,
                client_ip,
                "login_rejected_local_account",
                "Conflicto de origen al crear la sombra LDAPS: la cuenta ya "
                "es local",
                db_path=runtime.get_db_path(),
            )
            logger.warning(
                "Login LDAP rechazado por conflicto de origen para %r",
                username,
            )
            flash("Usuario o contraseña incorrectos.", "danger")
            return
        first_login = True
        AuthEvent.record(
            username,
            client_ip,
            "ldap_first_login",
            f"Cuenta creada con rol '{user.role}' (LDAPS); pendiente de "
            "promoción por el admin",
            db_path=runtime.get_db_path(),
        )
    return _finish_session_login(
        user,
        client_ip,
        "ldap_first_login" if first_login else "login_success",
        f"Autenticación LDAPS ({user.role})",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        client_ip = _client_ip()
        retry_after = _check_rate_limit(
            f"login:{client_ip}", LOGIN_RATE_LIMIT, LOGIN_RATE_WINDOW
        )
        if retry_after is not None:
            logger.warning(
                "Login bloqueado por rate limit para IP %s (%ss)",
                client_ip,
                retry_after,
            )
            return _rate_limited_response(
                retry_after,
                "Demasiados intentos de inicio de sesión. Espera antes de reintentar.",
            )

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.get_by_username(username, db_path=runtime.get_db_path())

        if user is not None and user.is_locked:
            AuthEvent.record(
                username,
                client_ip,
                "login_locked",
                "Intento con cuenta bloqueada",
                db_path=runtime.get_db_path(),
            )
            logger.warning(
                "Login bloqueado para usuario=%r (cuenta bloqueada)", username
            )
            flash(
                "La cuenta está bloqueada temporalmente por demasiados intentos "
                "fallidos. Inténtalo más tarde.",
                "danger",
            )
        elif user is not None and user.is_active and user.check_password(password):
            # Ruta local: el admin (y cualquier cuenta creada aquí) primero.
            return _finish_session_login(
                user, client_ip, "login_success", "Inicio de sesión correcto"
            )
        elif (
            username
            and password
            and load_ldap_access_setting()
            and ldap_auth.ldap_configured()
        ):
            # Ruta corporativa: la credencial se valida en el directorio.
            # Para nombres ya tomados por cuentas locales el helper no
            # consulta el directorio y explica la política al usuario.
            ldap_response = _try_ldap_login(username, password, user, client_ip)
            if ldap_response is not None:
                return ldap_response
        else:
            _register_login_failure(user, username, client_ip)

    no_admin = User.count(db_path=runtime.get_db_path()) == 0
    return render_template("auth/login.html", no_admin=no_admin)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))

def register(flask_app) -> None:
    app.apply(flask_app)

