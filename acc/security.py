"""Autenticacion, propiedad de filas, roles y limites de peticiones.
"""

from __future__ import annotations

from functools import wraps
from typing import Any
from urllib.parse import urlsplit

from flask import abort, jsonify, make_response, render_template, request
from flask_login import current_user, login_required

from acc.extensions import login_manager
from config import RATE_LIMIT_ENABLED
from services.rate_limiter import LIMITER


def roles_required(*roles: str):
    """Restringe una ruta a los roles indicados (``admin`` siempre pasa)."""

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user
            if not getattr(user, "is_authenticated", False):
                return login_manager.unauthorized()
            if not user.has_role("admin", *roles):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _authenticated() -> bool:
    return bool(getattr(current_user, "is_authenticated", False))


def _current_owner_id() -> int:
    """Identificador estable del usuario actual para la propiedad de filas."""
    if not _authenticated():
        return 0
    raw = str(current_user.get_id() or "")
    return int(raw) if raw.isdigit() else 0


def _view_all_requested() -> bool:
    """Vista global explícita: solo administradores y solo con ``?all=1``."""
    return bool(
        _authenticated()
        and current_user.has_role("admin")
        and request.args.get("all") == "1"
    )


def _owner_scope() -> int | None:
    """Filtro de propiedad para listados; ``None`` significa «ver todo»."""
    return None if _view_all_requested() else _current_owner_id()


def _can_manage_owner(owner_user_id: int | str | None) -> bool:
    """Admin puede gestionar todo; el resto, solo sus propias filas."""
    if _authenticated() and current_user.has_role("admin"):
        return True
    return int(owner_user_id or 0) == _current_owner_id()


def _can_use_group(group) -> bool:
    """Indica si el usuario puede elegir un grupo como objetivo.

    Los grupos compartidos (``owner_user_id = 0``) siguen disponibles para
    todos. Un administrador puede operar con cualquier grupo; un operador o
    viewer solo con los suyos y los compartidos.
    """
    if int(getattr(group, "owner_user_id", 0) or 0) == 0:
        return True
    if _authenticated() and current_user.has_role("admin"):
        return True
    return int(getattr(group, "owner_user_id", 0) or 0) == _current_owner_id()


def _wants_json() -> bool:
    return request.headers.get(
        "X-Requested-With"
    ) == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")


def _is_safe_redirect(target: str | None) -> bool:
    """Solo permite redirecciones internas (evita open redirect).

    Reconoce las dos variantes que los navegadores normalizan como
    protocolo relativo: ``//host`` y ``/\\host`` (la barra invertida se
    acepta como ``/`` por varios). Se descarta además cualquier objetivo
    con netloc tras el parseo.
    """
    if not target or not target.startswith("/"):
        return False
    if target.startswith(("//", "/\\")):
        return False
    return urlsplit(target).netloc in ("", None)


def _client_ip() -> str:
    """IP del cliente tal y como la resuelve el servidor.

    Con ``ACC_TRUST_PROXY=1`` Werkzeug aplica ``ProxyFix`` y reescribe
    ``request.remote_addr`` con la IP añadida por el proxy de confianza. Leer
    ``X-Forwarded-For`` a mano permitiría a un cliente falsificar el primer
    valor de la cabecera y eludir los límites por IP, así que no se hace.
    """
    return request.remote_addr or "unknown"


def _check_rate_limit(bucket: str, limit: int, window: int) -> int | None:
    """Devuelve los segundos de espera si se superó el límite, o ``None``."""
    if not RATE_LIMIT_ENABLED:
        return None
    allowed, retry_after = LIMITER.check(bucket, limit, window)
    return None if allowed else retry_after


def _rate_limited_response(retry_after: int, message: str):
    """Respuesta 429 coherente para HTML y para peticiones AJAX/JSON."""
    if _wants_json():
        response = jsonify(
            {"success": False, "message": message, "retry_after": retry_after}
        )
        response.status_code = 429
    else:
        response = make_response(
            render_template("429.html", message=message, retry_after=retry_after),
            429,
        )
    response.headers["Retry-After"] = str(retry_after)
    return response


def _task_viewer_actor() -> str:
    """Identidad del usuario actual para compararla con el dueño de la tarea."""
    return getattr(current_user, "username", "") or str(current_user.get_id())


def _can_view_task(task: dict[str, Any]) -> bool:
    """Indica si el usuario actual puede ver la tarea completa.

    La tarea incluye consola, log y metadatos de ejecución, así que solo la
    ven su dueño y los administradores. Las tareas sin dueño (programaciones
    desatendidas) quedan reservadas a los administradores, igual que su
    cancelación. Para no revelar la existencia de tareas ajenas, quien no
    tiene permiso recibe el mismo 404 que una tarea inexistente.
    """
    if current_user.has_role("admin"):
        return True
    return str(task.get("owner") or "") == _task_viewer_actor()
