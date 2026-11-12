# services/csrf.py
# Protección CSRF ligada a la sesión de Flask.
#
# El token es aleatorio y se guarda en la sesión firmada. Se exige en todas las
# peticiones mutables (POST/PUT/PATCH/DELETE) mediante el campo ``csrf_token`` o
# la cabecera ``X-CSRFToken``. Caduca para reducir la ventana de reutilización.
from __future__ import annotations

import hmac
import secrets
import time

from flask import abort, current_app, jsonify, request, session

CSRF_SESSION_KEY = "_csrf_token"
CSRF_SESSION_TS_KEY = "_csrf_ts"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRFToken"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _wants_json() -> bool:
    return request.headers.get(
        "X-Requested-With"
    ) == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")


def _time_limit() -> int:
    return int(current_app.config.get("CSRF_TIME_LIMIT", 8 * 3600))


def generate_csrf_token() -> str:
    """Devuelve el token de la sesión, creándolo o renovándolo si caducó."""
    token = session.get(CSRF_SESSION_KEY)
    issued = session.get(CSRF_SESSION_TS_KEY, 0)
    if (
        not token
        or not isinstance(issued, (int, float))
        or time.time() - issued > _time_limit()
    ):
        token = None

    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
        session[CSRF_SESSION_TS_KEY] = time.time()
    return token


def rotate_csrf_token() -> str:
    """Genera un token nuevo (se usa al iniciar sesión)."""
    session.pop(CSRF_SESSION_KEY, None)
    session.pop(CSRF_SESSION_TS_KEY, None)
    return generate_csrf_token()


def validate_csrf_token(token: str | None) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    issued = session.get(CSRF_SESSION_TS_KEY, 0)
    if not expected or not token:
        return False
    if not isinstance(issued, (int, float)):
        return False
    if time.time() - issued > _time_limit():
        return False
    return hmac.compare_digest(str(expected), str(token))


def _reject():
    message = "Token CSRF inválido o expirado. Recarga la página e inténtalo de nuevo."
    if _wants_json():
        return jsonify({"success": False, "message": message}), 400
    abort(400, description=message)


def csrf_protect():
    """Hook ``before_request``: valida el token en métodos mutables."""
    if not current_app.config.get("CSRF_ENABLED", True):
        return None
    if request.method in SAFE_METHODS:
        return None

    token = request.form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER)
    if not validate_csrf_token(token):
        current_app.logger.warning(
            "Petición rechazada por CSRF: %s %s desde %s",
            request.method,
            request.path,
            request.remote_addr,
        )
        return _reject()
    return None
