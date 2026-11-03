#!/usr/bin/env python3
"""Hora Admin — configuración de hora/zona horaria en Ubuntu vía Ansible.

Incluye autenticación, validación de formularios, progreso SSE, historial en
SQLite, vault real con ansible-vault, ejecución de Ansible con inyección
segura de la contraseña candidata, CSRF, rate limiting, límites de red,
cabeceras de seguridad y ejecución en grupo con lotes, vista previa,
verificación e idempotencia (fases P0-P4).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import click
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

from config import (
    ADMIN_PASSWORD_MIN_LENGTH,
    ADMIN_USERNAME,
    ALLOWED_PASSWORD_EXTENSIONS,
    BATCH_TIME_BUDGET_SECONDS,
    COMMON_TIMEZONES,
    CSP_ENABLED,
    CSRF_ENABLED,
    CSRF_TIME_LIMIT_SECONDS,
    DEBUG,
    DEFAULT_NTP_SERVERS,
    DRY_RUN,
    EXEC_RATE_LIMIT,
    EXEC_RATE_WINDOW,
    HSTS_ENABLED,
    HSTS_MAX_AGE,
    JOB_RETENTION_HOURS,
    LOGIN_RATE_LIMIT,
    LOGIN_RATE_WINDOW,
    MAX_CONTENT_LENGTH,
    MAX_HOSTS,
    MAX_NTP_SERVERS,
    MAX_PASSWORDS,
    MAX_PASSWORD_ATTEMPTS,
    MAX_PASSWORD_LENGTH,
    NTP_POLICIES,
    PERMANENT_SESSION_LIFETIME_SECONDS,
    RATE_LIMIT_ENABLED,
    REMEMBER_COOKIE_HTTPONLY,
    REMEMBER_COOKIE_SAMESITE,
    REMEMBER_COOKIE_SECURE,
    REMEMBER_COOKIE_DURATION_SECONDS,
    SECURITY_HEADERS_ENABLED,
    SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
    TIME_VERIFY_TOLERANCE,
    TRUST_PROXY,
    VAULT_RATE_LIMIT,
    VAULT_RATE_WINDOW,
    ensure_directories,
    get_or_create_secret_key,
    resolve_db_path,
    resolve_vault_file,
    setup_logging,
)
from models.batch import ExecutionBatch
from models.db import get_connection, init_schema
from models.execution import ExecutionRecord
from models.password import PasswordRecord
from models.user import User
from services.ansible_runner import apply_host, inspect_host
from services.csrf import csrf_protect, generate_csrf_token, rotate_csrf_token
from services.host_resolver import resolve_target, validate_target
from services.password_tester import test_ansible_ping
from services.rate_limiter import LIMITER
from services.secure_temp import secure_temp_file
from services.vault_manager import VaultManager

logger = logging.getLogger("hora_admin")

# ---------------------------------------------------------------------------
# Estado en memoria de las tareas (progreso SSE)
# ---------------------------------------------------------------------------
_tasks: Dict[str, Dict[str, Any]] = {}
_tasks_lock = threading.Lock()
_db_path_cache: Optional[str] = None

# Idempotencia de envío: una operación solo se procesa una vez. El formulario
# incluye un UUID oculto y aquí se recuerda el task_id asociado durante una
# ventana corta. Es un estado por proceso (la app usa un worker por defecto).
_OPERATION_IDS: Dict[str, Dict[str, Any]] = {}
_OPERATION_IDS_LOCK = threading.Lock()
_OPERATION_ID_TTL = 3600

# Estados que cierran el stream SSE.
TERMINAL_STATES = {"completed", "failed", "error", "found", "failure", "timeout"}

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para continuar."
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"

# La app se crea aquí para que los decoradores @app.route de este módulo
# encuentren el objeto. La configuración se aplica al final (configure_app).
app = Flask(__name__)


def get_db_path() -> str:
    """Ruta de SQLite resuelta una sola vez por proceso."""
    global _db_path_cache
    if _db_path_cache is None:
        _db_path_cache = str(resolve_db_path())
    return _db_path_cache


def _wants_json() -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def _is_safe_redirect(target: Optional[str]) -> bool:
    """Solo permite redirecciones internas (evita open redirect)."""
    return bool(target) and target.startswith("/") and not target.startswith("//")


def _client_ip() -> str:
    """IP del cliente, respetando X-Forwarded-For solo si hay proxy de confianza."""
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
    return request.remote_addr or "unknown"


def _check_rate_limit(bucket: str, limit: int, window: int) -> Optional[int]:
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
            render_template(
                "429.html", message=message, retry_after=retry_after
            ),
            429,
        )
    response.headers["Retry-After"] = str(retry_after)
    return response


def init_db() -> None:
    """Crea o migra el esquema completo de SQLite (idempotente)."""
    init_schema(get_db_path())


def get_vault_file() -> str:
    """Ruta efectiva del vault (producción o fallback de desarrollo)."""
    return str(resolve_vault_file())


def read_temporal_passwords(uploaded) -> tuple[list[str], Optional[str]]:
    """Lee un .txt subido y devuelve contraseñas normalizadas o un error.

    El archivo se procesa en memoria: no se guarda en disco.
    """
    if uploaded is None or not uploaded.filename:
        return [], "Selecciona un archivo .txt con las contraseñas candidatas."

    extension = os.path.splitext(uploaded.filename)[1].lower()
    if extension != ".txt":
        return [], "El modo temporal requiere un archivo con extensión .txt."

    raw = uploaded.read(MAX_CONTENT_LENGTH + 1)
    if len(raw) > MAX_CONTENT_LENGTH:
        return [], "El archivo de contraseñas es demasiado grande."

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], "El archivo debe estar codificado en UTF-8."

    passwords: list[str] = []
    seen = set()
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if len(candidate) > MAX_PASSWORD_LENGTH:
            return [], (
                f"Una contraseña supera el máximo de {MAX_PASSWORD_LENGTH} caracteres."
            )
        if candidate not in seen:
            seen.add(candidate)
            passwords.append(candidate)
            if len(passwords) > MAX_PASSWORDS:
                return [], f"Máximo {MAX_PASSWORDS} contraseñas por archivo."

    if not passwords:
        return [], "El archivo no contiene contraseñas válidas."

    return passwords, None


_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.?$"
)


def _form_bool(name: str) -> bool:
    """Interpreta un checkbox/valor booleano del formulario."""
    return request.form.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "si",
        "sí",
    }


def _parse_ntp_servers(raw: str) -> tuple[List[str], Optional[str]]:
    """Valida y normaliza una lista de servidores NTP.

    Admite IPs (v4/v6) y nombres DNS separados por comas, espacios o saltos
    de línea. Devuelve ``(servidores, error)``.
    """
    servers: List[str] = []
    for token in re.split(r"[\s,;]+", raw or ""):
        token = token.strip()
        if not token:
            continue
        if len(token) > 253:
            return [], "Un servidor NTP es demasiado largo."
        try:
            ipaddress.ip_address(token)
        except ValueError:
            if not _HOSTNAME_RE.fullmatch(token):
                return [], f"Servidor NTP inválido: {token}"
        if token not in servers:
            servers.append(token)
        if len(servers) > MAX_NTP_SERVERS:
            return [], f"Máximo {MAX_NTP_SERVERS} servidores NTP."
    return servers, None


def _parse_operation_form(
    default_mode: str = "execute",
) -> tuple[Optional[Dict[str, Any]], List[str]]:
    """Valida el formulario completo de vista previa/ejecución (P4).

    Devuelve un diccionario normalizado o una lista de errores. La vista previa
    y la ejecución comparten exactamente la misma validación para que lo que se
    previsualiza sea lo que después se ejecuta.
    """
    target = request.form.get("target_ip", "").strip()
    admin_user = request.form.get("admin_user", "").strip()
    origin = request.form.get("password_origin", "stored").strip()
    master_password = request.form.get("master_password", "").strip()
    action = request.form.get("action", "TZ").strip().upper() or "TZ"
    value = request.form.get("value", "").strip()
    mode = (
        request.form.get("mode", default_mode).strip().lower() or default_mode
    )
    time_mode = request.form.get("time_mode", "utc").strip().lower() or "utc"
    ntp_policy = request.form.get("ntp_policy", "keep").strip().lower() or "keep"
    ntp_servers_raw = request.form.get("ntp_servers", "")
    sync_rtc = _form_bool("sync_rtc")
    force = _form_bool("force")

    errors: List[str] = []

    if mode not in {"preview", "execute"}:
        errors.append("Modo de operación no válido.")
    target_error = validate_target(target)
    if target_error:
        errors.append(target_error)
    if not admin_user:
        errors.append("El usuario administrador es obligatorio.")
    if action not in {"TZ", "TIME"}:
        errors.append("Acción no válida.")
    if ntp_policy not in NTP_POLICIES:
        errors.append("Política NTP no válida.")

    if action == "TIME":
        if time_mode not in {"utc", "local"}:
            errors.append("Semántica de hora no válida (utc o local).")
        if not value:
            errors.append("La hora exacta es obligatoria.")
        else:
            try:
                parsed = datetime.strptime(
                    value.replace(" ", "T"), "%Y-%m-%dT%H:%M"
                )
                value = parsed.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                errors.append("Formato de hora inválido; use AAAA-MM-DDTHH:MM.")
    else:
        # La semántica UTC/local solo aplica a TIME.
        time_mode = "utc"
        if not value:
            errors.append("La zona horaria es obligatoria.")
        elif not _TIMEZONE_RE.fullmatch(value):
            errors.append("Formato de zona horaria inválido.")

    ntp_servers: List[str] = []
    if ntp_policy == "configure":
        ntp_servers, ntp_error = _parse_ntp_servers(ntp_servers_raw)
        if ntp_error:
            errors.append(ntp_error)
        if not ntp_servers:
            ntp_servers = list(DEFAULT_NTP_SERVERS)
            if not ntp_servers:
                errors.append(
                    "La política NTP 'configure' requiere al menos un servidor NTP."
                )

    temporal_passwords: list[str] = []
    vault_file: Optional[str] = None
    if origin not in {"stored", "temporal"}:
        errors.append("Origen de contraseñas no válido.")
    elif origin == "stored":
        try:
            vault_file = get_vault_file()
        except RuntimeError as exc:
            errors.append(str(exc))

        if vault_file and not os.path.exists(vault_file):
            errors.append("No existe el archivo del vault (passwords_vault.yml).")
        if not master_password:
            errors.append("La contraseña maestra del Vault es obligatoria.")
        elif vault_file and os.path.exists(vault_file):
            valid, message = VaultManager.validate_master_password(
                vault_file=vault_file,
                test_password=master_password,
            )
            if not valid:
                errors.append(message or "Contraseña maestra incorrecta.")
    else:
        temporal_passwords, file_error = read_temporal_passwords(
            request.files.get("password_file")
        )
        if file_error:
            errors.append(file_error)

    if errors:
        return None, errors

    return (
        {
            "target": target,
            "admin_user": admin_user,
            "origin": origin,
            "master_password": master_password,
            "action": action,
            "value": value,
            "mode": mode,
            "time_mode": time_mode,
            "ntp_policy": ntp_policy,
            "ntp_servers": ntp_servers,
            "sync_rtc": sync_rtc,
            "force": force,
            "temporal_passwords": temporal_passwords,
        },
        [],
    )


def _state_bool(value: Any) -> bool:
    """Interpreta un booleano del playbook aunque llegue serializado.

    Ansible con Jinja nativa conserva ``bool``; con versiones anteriores un
    ``set_fact`` entrecomillado puede producir ``"True"``/``"False"``. Un
    ``bool("False")`` de Python sería verdadero, así que se normaliza aquí.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}
    return bool(value)


def _would_change(
    state: Optional[Dict[str, Any]],
    action: str,
    value: str,
    time_mode: str,
    ntp_policy: str,
    force: bool,
    ntp_servers: Optional[List[str]] = None,
) -> Optional[bool]:
    """Estima si hace falta cambiar algo según el estado leído (vista previa).

    Devuelve ``None`` si el estado no permite decidirlo (por ejemplo, un host
    inaccesible o una salida incompleta del playbook de inspección).

    Para ``configure`` se devuelve ``True`` salvo que el estado incluya la
    lista de servidores NTP y coincida exactamente: el playbook reescribe la
    configuración NTP, así que no se puede prometer “sin cambios” sin esos
    datos.
    """
    if force:
        return True
    if not isinstance(state, dict):
        return None

    changed = False
    if action == "TZ":
        changed = (state.get("timezone") or "") != value
    elif action == "TIME":
        if time_mode == "utc":
            try:
                target_epoch = int(
                    datetime.strptime(value, "%Y-%m-%dT%H:%M")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
                current_epoch = int(state.get("epoch") or 0)
                changed = current_epoch == 0 or (
                    abs(current_epoch - target_epoch) > TIME_VERIFY_TOLERANCE
                )
            except (TypeError, ValueError):
                return None
        else:
            local = str(state.get("local") or "")
            changed = not local.startswith(value)

    if ntp_policy == "off":
        changed = changed or _state_bool(state.get("ntp"))
    elif ntp_policy == "configure":
        current_servers = state.get("ntp_servers")
        servers_match = isinstance(current_servers, list) and {
            str(server) for server in current_servers
        } == {str(server) for server in (ntp_servers or [])}
        changed = (
            changed
            or not _state_bool(state.get("ntp"))
            or not servers_match
        )
    # ``restore`` devuelve NTP a su estado previo, así que no añade cambios
    # netos por sí mismo.

    return changed


def _batch_message(batch: Any) -> str:
    """Resumen legible del estado de un lote."""
    if batch is None:
        return "Lote sin resultado"
    if batch.status == "preview":
        return (
            f"Vista previa: {batch.preview_count} host(s) inspeccionado(s), "
            f"{batch.error_count} con error, {batch.skipped_count} omitido(s)"
        )
    prefix = ""
    if getattr(batch, "dry_run", False):
        prefix = "SIMULACIÓN (sin cambios reales): "
    return (
        prefix
        + f"Lote {batch.status}: {batch.success_count} éxito(s), "
        f"{batch.failure_count} fallo(s), {batch.skipped_count} omitido(s), "
        f"{batch.error_count} error(es)"
    )


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def sse_event(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def sse_retry(retry_ms: int) -> str:
    return f"retry: {retry_ms}\n\n"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_task_persist_warned = False


def _persist_task(tid: str, task: Dict[str, Any]) -> None:
    """Guarda una instantánea de la tarea en SQLite.

    El estado sigue viviendo en memoria para el SSE; SQLite es una copia de
    respaldo para que la página de progreso sobreviva a un reinicio. Un fallo
    de escritura no debe tumbar la ejecución.
    """
    global _task_persist_warned
    try:
        payload = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
        with get_connection(get_db_path()) as conn:
            conn.execute(
                """
                INSERT INTO task_state (task_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (tid, payload, _utc_now_iso()),
            )
    except Exception:
        if not _task_persist_warned:
            _task_persist_warned = True
            logger.exception("No se pudo persistir el estado de la tarea")


def _load_persisted_task(tid: str) -> Optional[Dict[str, Any]]:
    """Recupera una tarea de SQLite y la vuelve a cachear en memoria."""
    try:
        with get_connection(get_db_path()) as conn:
            row = conn.execute(
                "SELECT payload FROM task_state WHERE task_id = ?", (tid,)
            ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        task = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(task, dict):
        return None

    with _tasks_lock:
        _tasks.setdefault(tid, task)
        return _tasks[tid]


def _get_task(tid: str) -> Optional[Dict[str, Any]]:
    with _tasks_lock:
        task = _tasks.get(tid)
    if task is not None:
        return task
    return _load_persisted_task(tid)


def _delete_persisted_task(tid: str) -> None:
    try:
        with get_connection(get_db_path()) as conn:
            conn.execute("DELETE FROM task_state WHERE task_id = ?", (tid,))
    except Exception:
        logger.debug("No se pudo borrar la tarea persistida %s", tid)


def _recover_task_state() -> None:
    """Purga tareas antiguas y cierra las que quedaron en curso.

    Al arrancar no puede haber ninguna tarea legítimamente en ejecución en este
    proceso, así que las instantáneas incompletas se marcan como error.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=JOB_RETENTION_HOURS)
    ).isoformat(timespec="seconds")

    with get_connection(get_db_path()) as conn:
        conn.execute("DELETE FROM task_state WHERE updated_at < ?", (cutoff,))
        rows = conn.execute(
            "SELECT task_id, payload FROM task_state"
        ).fetchall()

    for row in rows:
        try:
            task = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(task, dict) or task.get("completed"):
            continue
        task.update(
            {
                "status": "error",
                "message": "El proceso se reinició durante la ejecución.",
                "overall_result": "error",
                "completed": True,
                "updated_at": _utc_now_iso(),
            }
        )
        _persist_task(row["task_id"], task)


def create_task() -> str:
    task_id = str(uuid.uuid4())
    task = {
        "status": "pending",
        "progress": 0,
        "message": "Proceso iniciado",
        "log": [],
        "console": [],
        "host": "",
        "password_hash": "",
        "overall_result": "unknown",
        "completed": False,
        "dry_run": DRY_RUN,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": _utc_now_iso(),
    }
    with _tasks_lock:
        _tasks[task_id] = task
    _persist_task(task_id, task)
    return task_id


def update_task(tid: str, updates: Dict[str, Any]) -> None:
    with _tasks_lock:
        task = _tasks.get(tid)
        if task is None:
            return
        task.update(updates)
        task["updated_at"] = _utc_now_iso()
        snapshot = dict(task)
    _persist_task(tid, snapshot)


def remove_task(tid: str) -> None:
    with _tasks_lock:
        _tasks.pop(tid, None)
    _delete_persisted_task(tid)


def _claim_operation_id(operation_id: str) -> Optional[str]:
    """Reserva un envío idempotente.

    Devuelve ``None`` si la operación es nueva. Si ya existía, devuelve el
    ``task_id`` asociado o ``""`` mientras el primer envío aún se está creando.
    """
    if not operation_id:
        return None

    now = time.monotonic()
    with _OPERATION_IDS_LOCK:
        expired = [
            key
            for key, entry in _OPERATION_IDS.items()
            if now - entry["created"] > _OPERATION_ID_TTL
        ]
        for key in expired:
            _OPERATION_IDS.pop(key, None)

        existing = _OPERATION_IDS.get(operation_id)
        if existing is not None:
            return existing.get("task_id", "")

        _OPERATION_IDS[operation_id] = {"created": now, "task_id": ""}
        if len(_OPERATION_IDS) > 5000:
            oldest = min(
                _OPERATION_IDS, key=lambda key: _OPERATION_IDS[key]["created"]
            )
            _OPERATION_IDS.pop(oldest, None)
        return None


def _complete_operation_id(operation_id: str, tid: str) -> None:
    """Asocia el task_id definitivo a un envío ya reservado."""
    if not operation_id:
        return
    with _OPERATION_IDS_LOCK:
        entry = _OPERATION_IDS.get(operation_id)
        if entry is not None:
            entry["task_id"] = tid


def generate_progress_events(task_id: str):
    yield sse_retry(1500)
    start = time.time()
    max_wait = 300
    last_payload: Optional[str] = None

    while True:
        task = _get_task(task_id)

        if task is None:
            yield sse_event(
                "completed",
                json.dumps({"status": "not_found", "completed": True}),
            )
            return

        payload = json.dumps(task, ensure_ascii=False)
        if payload != last_payload:
            yield sse_event("progress", payload)
            last_payload = payload

        if task.get("status") in TERMINAL_STATES or task.get("completed"):
            yield sse_event("completed", payload)
            return

        if time.time() - start >= max_wait:
            yield sse_event(
                "timeout",
                json.dumps({**task, "timeout": True}, ensure_ascii=False),
            )
            return

        time.sleep(1)


# ---------------------------------------------------------------------------
# Protección CSRF, rate limiting y cabeceras de seguridad
# ---------------------------------------------------------------------------
@app.before_request
def _security_before_request():
    g.csp_nonce = secrets.token_urlsafe(16)
    return csrf_protect()


@app.context_processor
def _security_context():
    return {
        "csrf_token": generate_csrf_token,
        "csp_nonce": getattr(g, "csp_nonce", ""),
        "dry_run": DRY_RUN,
    }


@app.after_request
def _security_headers(response):
    if not SECURITY_HEADERS_ENABLED:
        return response

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )

    if CSP_ENABLED:
        nonce = getattr(g, "csp_nonce", "")
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
                "https://fonts.googleapis.com; "
                "font-src 'self' https://cdn.jsdelivr.net "
                "https://fonts.gstatic.com; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'; "
                "frame-ancestors 'none'"
            ),
        )

    if HSTS_ENABLED:
        response.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={HSTS_MAX_AGE}; includeSubDomains",
        )

    if response.mimetype == "text/html":
        response.headers.setdefault("Cache-Control", "no-store")

    return response


# ---------------------------------------------------------------------------
# Autenticación
# ---------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id: str):
    try:
        return User.get_by_id(int(user_id), db_path=get_db_path())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    """Comprobación de salud para Docker/balanceadores (sin autenticación).

    Solo confirma que el proceso responde y que SQLite es accesible; no expone
    configuración ni datos.
    """
    try:
        with get_connection(get_db_path()) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        logger.exception("Healthcheck: base de datos no disponible")
        response = jsonify({"status": "error", "database": "unavailable"})
        response.status_code = 503
    else:
        response = jsonify({"status": "ok", "database": "ok"})
    response.headers["Cache-Control"] = "no-store"
    return response


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
        user = User.get_by_username(username, db_path=get_db_path())

        if user is not None and user.is_active and user.check_password(password):
            LIMITER.reset(f"login:{client_ip}")
            rotate_csrf_token()
            session.permanent = True
            login_user(user, remember=bool(request.form.get("remember")))
            next_url = request.args.get("next") or request.form.get("next")
            return redirect(next_url if _is_safe_redirect(next_url) else url_for("index"))

        logger.warning("Intento de login fallido para usuario=%r", username)
        flash("Usuario o contraseña incorrectos.", "danger")

    no_admin = User.count(db_path=get_db_path()) == 0
    return render_template("auth/login.html", no_admin=no_admin)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        return handle_execution()

    prefill = {
        key: request.args.get(key, "")
        for key in (
            "target_ip",
            "admin_user",
            "action",
            "value",
            "time_mode",
            "ntp_policy",
            "ntp_servers",
        )
    }
    prefill["operation_id"] = uuid.uuid4().hex
    return render_template(
        "index.html",
        common_timezones=COMMON_TIMEZONES,
        ntp_policies=NTP_POLICIES,
        default_ntp_servers=", ".join(DEFAULT_NTP_SERVERS),
        prefill=prefill,
    )


@app.route("/preview", methods=["POST"])
@login_required
def preview_execution():
    """Alias explícito para lanzar una vista previa (no cambia hosts)."""
    return handle_execution(default_mode="preview")


@app.route("/execute", methods=["POST"])
@login_required
def execute_execution():
    """Alias explícito para lanzar una ejecución sobre todos los hosts."""
    return handle_execution(default_mode="execute")


def handle_execution(default_mode: str = "execute"):
    retry_after = _check_rate_limit(
        f"exec:{current_user.get_id()}:{_client_ip()}",
        EXEC_RATE_LIMIT,
        EXEC_RATE_WINDOW,
    )
    if retry_after is not None:
        logger.warning(
            "Ejecución bloqueada por rate limit para usuario=%s",
            current_user.get_id(),
        )
        return _rate_limited_response(
            retry_after,
            "Demasiadas ejecuciones seguidas. Espera antes de reintentar.",
        )

    form, errors = _parse_operation_form(default_mode=default_mode)
    if errors:
        for error in errors:
            flash(error, "danger")
        if _wants_json():
            return jsonify({"status": "error", "errors": errors}), 400
        return redirect(url_for("index"))

    assert form is not None  # ``errors`` vacío implica formulario válido.
    operator = getattr(current_user, "username", "") or str(current_user.get_id())

    operation_id = request.form.get("operation_id", "").strip()
    duplicate = _claim_operation_id(operation_id)
    if duplicate is not None:
        if duplicate:
            if _wants_json():
                return (
                    jsonify(
                        {
                            "status": "duplicate",
                            "task_id": duplicate,
                            "message": "Operación ya enviada",
                        }
                    ),
                    409,
                )
            return redirect(url_for("progress_page", task_id=duplicate))
        message = "La operación ya se está procesando; espera unos segundos."
        if _wants_json():
            return jsonify({"status": "duplicate", "message": message}), 409
        flash(message, "warning")
        return redirect(url_for("index"))

    tid = create_task()
    _complete_operation_id(operation_id, tid)
    update_task(
        tid,
        {
            "status": "running",
            "progress": 0,
            "message": "Iniciando…",
            "mode": form["mode"],
            "completed": False,
        },
    )
    thread = threading.Thread(
        target=execute_with_progress,
        args=(
            tid,
            form["target"],
            form["admin_user"],
            form["origin"],
            form["master_password"],
            form["action"],
            form["value"],
            form["temporal_passwords"],
        ),
        kwargs={
            "mode": form["mode"],
            "time_mode": form["time_mode"],
            "ntp_policy": form["ntp_policy"],
            "ntp_servers": form["ntp_servers"],
            "sync_rtc": form["sync_rtc"],
            "force": form["force"],
            "operator": operator,
        },
        daemon=True,
        name=f"hora-admin-{tid[:8]}",
    )
    thread.start()

    if _wants_json():
        return jsonify(
            {
                "status": "started",
                "task_id": tid,
                "mode": form["mode"],
                "message": f"Ver /progress/{tid} para el progreso en tiempo real",
            }
        )
    return redirect(url_for("progress_page", task_id=tid))


def _append_log(lines: List[str], message: str, limit: int = 200) -> None:
    """Añade una línea al log de progreso manteniendo un tamaño acotado."""
    lines.append(message)
    if len(lines) > limit:
        del lines[: len(lines) - limit]


# Límites de la consola en vivo. La salida de Ansible puede ser muy verbosa;
# se acota para que el payload SSE no crezca sin control.
_CONSOLE_MAX_LINES = 300
_CONSOLE_MAX_LINE_LENGTH = 500
_HORA_B64_RE = re.compile(r"HORA_[A-Z_]+_B64=")
_HORA_JSON_STATE_RE = re.compile(r"HORA_(?:STATE|BEFORE)=\{")


def _sanitize_console_line(line: str, secrets: List[str]) -> str:
    """Normaliza una línea de Ansible y oculta secretos conocidos.

    También compacta los marcadores base64 de estado, que son muy largos y no
    aportan nada a la consola: el estado ya se muestra en el detalle del lote.
    """
    text = line.replace("\r", "").replace("\n", "")
    text = "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32)
    for secret in secrets:
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    if _HORA_B64_RE.search(text) or _HORA_JSON_STATE_RE.search(text):
        return "[marcador de estado del host recibido]"
    text = text.rstrip()
    if len(text) > _CONSOLE_MAX_LINE_LENGTH:
        text = text[:_CONSOLE_MAX_LINE_LENGTH] + "…"
    return text


def _trim_console(console: List[str]) -> None:
    if len(console) > _CONSOLE_MAX_LINES:
        del console[: len(console) - _CONSOLE_MAX_LINES]


def _append_console(
    console: List[str], tid: str, message: str, host: str = ""
) -> None:
    """Añade una línea de resumen a la consola y actualiza la tarea."""
    text = f"[{host}] {message}" if host else message
    console.append(text)
    _trim_console(console)
    update_task(tid, {"console": list(console)})


def _make_console_callback(
    tid: str, host: str, console: List[str], secrets: List[str]
):
    """Crea el callback que recibe la salida de Ansible en tiempo real."""
    last_update = [0.0]

    def callback(raw_line: str) -> None:
        text = _sanitize_console_line(raw_line, secrets)
        if not text:
            return
        console.append(f"[{host}] {text}")
        _trim_console(console)
        # No actualizar en cada línea: el SSE reenvía el estado completo.
        now = time.monotonic()
        if now - last_update[0] >= 0.25:
            last_update[0] = now
            update_task(tid, {"console": list(console)})

    return callback


def _progress_for(index: int, total: int, start: int = 12, span: int = 85) -> int:
    """Progreso proporcional al número de hosts ya procesados."""
    if total <= 0:
        return start
    return min(start + int(index / total * span), start + span)


def _skip_remaining_hosts(
    tid: str,
    batch_id: str,
    hosts: List[str],
    db_path: str,
    log_lines: List[str],
    console_lines: List[str],
    reason: str = "Presupuesto de tiempo del lote agotado.",
) -> None:
    """Registra como omitidos los hosts que no se llegaron a procesar.

    Así el lote cierra con todos sus hosts contabilizados en vez de quedar
    abierto indefinidamente o perder los pendientes.
    """
    for host in hosts:
        ExecutionBatch.add_host_result(
            batch_id=batch_id,
            host=host,
            result="skipped",
            error=reason,
            db_path=db_path,
        )
        _append_log(log_lines, f"OMITIDO {host}: {reason}")
        _append_console(console_lines, tid, f"OMITIDO: {reason}", host=host)
    update_task(tid, {"log": list(log_lines), "console": list(console_lines)})


def _ntp_keep_warning(
    action: str, ntp_policy: str, state: Any
) -> str:
    """Aviso cuando ``TIME`` se aplica con NTP activo y política ``keep``."""
    if (
        action == "TIME"
        and ntp_policy == "keep"
        and isinstance(state, dict)
        and _state_bool(state.get("ntp"))
    ):
        return (
            "TIME con NTP activo y política keep: el reloj puede volver a "
            "sincronizarse. Usa off, restore o configure."
        )
    return ""


def _run_preview_batch(
    tid: str,
    batch_id: str,
    hosts: List[str],
    admin_user: str,
    candidates: List[str],
    action: str,
    value: str,
    password_origin: str,
    time_mode: str,
    ntp_policy: str,
    ntp_servers: List[str],
    force: bool,
    deadline: float,
    db_path: str,
    log_lines: List[str],
) -> None:
    """Inspecciona todos los hosts sin cambiar nada y registra el lote."""
    total = len(hosts)
    console_lines: List[str] = []
    for index, host in enumerate(hosts, start=1):
        if time.monotonic() >= deadline:
            _skip_remaining_hosts(
                tid=tid,
                batch_id=batch_id,
                hosts=hosts[index - 1 :],
                db_path=db_path,
                log_lines=log_lines,
                console_lines=console_lines,
            )
            break

        started = time.monotonic()
        state: Optional[Dict[str, Any]] = None
        used_hash = ""
        last_error = ""
        inspected = False
        _append_console(
            console_lines, tid, f"=== {host} ({index}/{total}) ==="
        )
        line_callback = _make_console_callback(
            tid, host, console_lines, candidates
        )

        attempts = 0
        for candidate in candidates:
            if attempts >= MAX_PASSWORD_ATTEMPTS:
                _append_log(
                    log_lines,
                    f"Límite de {MAX_PASSWORD_ATTEMPTS} intento(s) alcanzado "
                    f"en {host} (vista previa)",
                )
                break
            attempts += 1
            try:
                result = inspect_host(
                    host=host,
                    user=admin_user,
                    password=candidate,
                    line_callback=line_callback,
                    force_real=True,
                )
            except Exception as exc:  # pragma: no cover - salvaguarda
                last_error = str(exc)
                continue

            if result.get("success"):
                inspected = True
                state = result.get("state")
                used_hash = PasswordRecord.hash_short(candidate)
                break
            last_error = result.get("error") or "No se pudo inspeccionar el host."

        duration_ms = int((time.monotonic() - started) * 1000)
        progress = _progress_for(index, total)

        if inspected:
            plan = {
                "planned": True,
                "would_change": _would_change(
                    state,
                    action,
                    value,
                    time_mode,
                    ntp_policy,
                    force,
                    ntp_servers=ntp_servers,
                ),
                "action": action,
                "value": value,
                "time_mode": time_mode,
                "ntp_policy": ntp_policy,
            }
            ExecutionBatch.add_host_result(
                batch_id=batch_id,
                host=host,
                result="preview",
                duration_ms=duration_ms,
                before_state=state,
                after_state=plan,
                password_hash=used_hash,
                password_origin=password_origin,
                db_path=db_path,
            )
            _append_log(
                log_lines,
                f"Vista previa OK en {host} (hash {used_hash or 'n/d'})",
            )
            _append_console(
                console_lines,
                tid,
                f"Vista previa OK (hash {used_hash or 'n/d'})",
                host=host,
            )
            warning = _ntp_keep_warning(action, ntp_policy, state)
            if warning:
                _append_log(log_lines, f"Advertencia en {host}: {warning}")
                _append_console(
                    console_lines,
                    tid,
                    f"Advertencia: {warning}",
                    host=host,
                )
        else:
            ExecutionBatch.add_host_result(
                batch_id=batch_id,
                host=host,
                result="error",
                error=last_error or "No se pudo autenticar en el host.",
                duration_ms=duration_ms,
                password_hash=used_hash,
                db_path=db_path,
            )
            _append_log(
                log_lines,
                f"Vista previa falló en {host}: {str(last_error)[:160]}",
            )
            _append_console(
                console_lines,
                tid,
                f"Vista previa falló: {str(last_error)[:160]}",
                host=host,
            )

        update_task(
            tid,
            {
                "progress": progress,
                "message": f"Vista previa {index}/{total}: {host}",
                "log": list(log_lines),
                "console": list(console_lines),
            },
        )

    update_task(tid, {"console": list(console_lines)})


def _run_apply_batch(
    tid: str,
    batch_id: str,
    hosts: List[str],
    admin_user: str,
    candidates: List[str],
    action: str,
    value: str,
    password_origin: str,
    time_mode: str,
    ntp_policy: str,
    ntp_servers: List[str],
    sync_rtc: bool,
    force: bool,
    deadline: float,
    db_path: str,
    log_lines: List[str],
) -> None:
    """Aplica el cambio en todos los hosts (best-effort) y registra cada uno.

    Si una contraseña autentica pero el playbook falla, se prueban las
    siguientes candidatas hasta ``HORA_ADMIN_MAX_PASSWORD_ATTEMPTS``. Un fallo
    de un host nunca detiene el lote. Al agotarse el presupuesto de tiempo del
    lote, los hosts pendientes quedan como ``skipped``.
    """
    total = len(hosts)
    console_lines: List[str] = []
    for index, host in enumerate(hosts, start=1):
        if time.monotonic() >= deadline:
            _skip_remaining_hosts(
                tid=tid,
                batch_id=batch_id,
                hosts=hosts[index - 1 :],
                db_path=db_path,
                log_lines=log_lines,
                console_lines=console_lines,
            )
            break

        started = time.monotonic()
        success = False
        before_state: Any = None
        after_state: Any = None
        used_hash = ""
        warnings = ""
        last_auth_error = ""
        last_apply_error = ""
        _append_console(
            console_lines, tid, f"=== {host} ({index}/{total}) ==="
        )
        line_callback = _make_console_callback(
            tid, host, console_lines, candidates
        )

        attempts = 0
        for candidate in candidates:
            if attempts >= MAX_PASSWORD_ATTEMPTS:
                _append_log(
                    log_lines,
                    f"Límite de {MAX_PASSWORD_ATTEMPTS} intento(s) alcanzado "
                    f"en {host}",
                )
                _append_console(
                    console_lines,
                    tid,
                    f"Límite de {MAX_PASSWORD_ATTEMPTS} intento(s) alcanzado",
                    host=host,
                )
                break
            attempts += 1
            candidate_hash = PasswordRecord.hash_short(candidate)
            try:
                ping = test_ansible_ping(
                    host=host, user=admin_user, password=candidate
                )
            except Exception as exc:  # pragma: no cover - salvaguarda
                last_auth_error = str(exc)
                _append_console(
                    console_lines,
                    tid,
                    f"Error probando credencial {candidate_hash}: {str(exc)[:120]}",
                    host=host,
                )
                continue

            if not ping.get("success"):
                last_auth_error = ping.get("error") or "sin conexión"
                _append_log(
                    log_lines,
                    f"Sin acceso a {host}: {str(last_auth_error)[:120]}",
                )
                _append_console(
                    console_lines,
                    tid,
                    f"Sin acceso (hash {candidate_hash}): "
                    f"{str(last_auth_error)[:120]}",
                    host=host,
                )
                continue

            short_hash = candidate_hash
            update_task(
                tid,
                {
                    "progress": _progress_for(index - 1, total),
                    "message": f"Credencial válida en {host}; aplicando cambio…",
                    "log": list(log_lines),
                },
            )
            _append_console(
                console_lines,
                tid,
                f"Credencial válida (hash {short_hash}); ejecutando playbook…",
                host=host,
            )

            try:
                result = apply_host(
                    host=host,
                    user=admin_user,
                    action=action,
                    value=value,
                    password=candidate,
                    time_mode=time_mode,
                    ntp_policy=ntp_policy,
                    ntp_servers=ntp_servers,
                    sync_rtc=sync_rtc,
                    force=force,
                    line_callback=line_callback,
                )
            except Exception as exc:  # pragma: no cover - salvaguarda
                last_apply_error = str(exc)
                _append_log(
                    log_lines,
                    f"Error inesperado en {host} (hash {short_hash}): {str(exc)[:120]}",
                )
                _append_console(
                    console_lines,
                    tid,
                    f"Error inesperado: {str(exc)[:160]}",
                    host=host,
                )
                continue

            # El estado previo debe ser el del primer intento que llegó a
            # conectar; los reintentos con otras contraseñas no lo reemplazan.
            if before_state is None and result.get("before_state") is not None:
                before_state = result.get("before_state")
            if result.get("after_state") is not None:
                after_state = result.get("after_state")
            if result.get("warnings"):
                warnings = result.get("warnings")

            if result.get("success"):
                success = True
                used_hash = short_hash
                last_apply_error = ""
                _append_log(
                    log_lines,
                    f"Playbook OK en {host} (hash {short_hash})",
                )
                _append_console(
                    console_lines,
                    tid,
                    "Playbook y verificación OK",
                    host=host,
                )
                break

            # La contraseña es válida pero el cambio/verificación falló:
            # se sigue probando el resto de candidatas para este host.
            used_hash = short_hash
            last_apply_error = result.get("error") or "El playbook falló"
            _append_log(
                log_lines,
                f"Playbook falló en {host} con hash {short_hash}: "
                f"{str(last_apply_error)[:140]}",
            )
            _append_console(
                console_lines,
                tid,
                f"Playbook/verificación falló: {str(last_apply_error)[:160]}",
                host=host,
            )
            if time.monotonic() >= deadline:
                last_apply_error = "Presupuesto de tiempo del lote agotado."
                _append_log(
                    log_lines,
                    f"Presupuesto de tiempo agotado durante {host}",
                )
                break

        duration_ms = int((time.monotonic() - started) * 1000)
        error = ""
        if not success:
            error = (
                last_apply_error
                or last_auth_error
                or "No se pudo autenticar en el host."
            )

        ExecutionBatch.add_host_result(
            batch_id=batch_id,
            host=host,
            result="success" if success else "failure",
            error=error,
            duration_ms=duration_ms,
            before_state=before_state,
            after_state=after_state,
            password_hash=used_hash,
            password_origin=password_origin,
            db_path=db_path,
        )

        # Se conserva el registro por host heredado para auditoría/consultas
        # antiguas; el lote es la fuente principal del historial desde P4.
        try:
            ExecutionRecord.record_execution(
                action=action,
                value=value,
                host=host,
                result="success" if success else "failure",
                password_hash=used_hash,
                password_origin=password_origin,
                db_path=db_path,
            )
        except Exception:  # pragma: no cover - no debe tumbar el lote
            logger.exception("No se pudo registrar el historial de %s", host)

        if warnings:
            _append_log(
                log_lines, f"Advertencias en {host}: {str(warnings)[:160]}"
            )
            _append_console(
                console_lines,
                tid,
                f"Advertencias: {str(warnings)[:160]}",
                host=host,
            )
        _append_log(
            log_lines,
            f"{'OK' if success else 'FALLO'} {host} ({duration_ms} ms)"
            + (f": {str(error)[:160]}" if error else ""),
        )
        _append_console(
            console_lines,
            tid,
            f"{'OK' if success else 'FALLO'} ({duration_ms} ms)"
            + (f": {str(error)[:160]}" if error else ""),
            host=host,
        )
        update_task(
            tid,
            {
                "progress": _progress_for(index, total),
                "message": f"Procesados {index}/{total} hosts",
                "log": list(log_lines),
                "console": list(console_lines),
            },
        )

    update_task(tid, {"console": list(console_lines)})


def execute_with_progress(
    tid: str,
    ti: str,
    au: str,
    po: str,
    mpw: str,
    ac: str,
    vl: str,
    temporal_passwords: Optional[list[str]] = None,
    *,
    mode: str = "execute",
    time_mode: str = "utc",
    ntp_policy: str = "keep",
    ntp_servers: Optional[List[str]] = None,
    sync_rtc: bool = False,
    force: bool = False,
    operator: str = "",
) -> None:
    """Ejecuta una vista previa o un lote completo sobre todos los hosts.

    El flujo es best-effort: cada host se procesa de forma independiente y se
    guarda su resultado. Un host no detiene el resto del lote.
    """
    batch_id = ""
    db_path = get_db_path()
    try:
        update_task(
            tid,
            {
                "status": "running",
                "progress": 5,
                "message": "Resolviendo hosts…",
                "log": ["Iniciando"],
                "mode": mode,
                "completed": False,
                "dry_run": DRY_RUN,
            },
        )
        hosts = resolve_target(ti)
        if not hosts:
            update_task(
                tid,
                {
                    "status": "error",
                    "progress": 0,
                    "message": "No se resolvieron hosts",
                    "log": [f"No se resolvieron hosts de {ti}"],
                    "overall_result": "error",
                    "completed": True,
                },
            )
            return

        if len(hosts) > MAX_HOSTS:
            update_task(
                tid,
                {
                    "status": "error",
                    "progress": 0,
                    "message": f"Demasiados hosts ({len(hosts)})",
                    "log": [f"Máximo permitido: {MAX_HOSTS}"],
                    "overall_result": "error",
                    "completed": True,
                },
            )
            return

        update_task(
            tid,
            {
                "progress": 10,
                "message": f"Objetivo: {', '.join(hosts[:3])}"
                + (f" +{len(hosts) - 3} más" if len(hosts) > 3 else ""),
                "log": [f"{ti} -> {len(hosts)} host(s)"],
            },
        )

        if po == "stored":
            ok, message, entries = VaultManager.decrypt_vault_file(
                vault_file=get_vault_file(),
                master_password=mpw,
            )
            if not ok:
                update_task(
                    tid,
                    {
                        "status": "error",
                        "progress": 0,
                        "message": f"Error de vault: {message}",
                        "log": [f"Vault: {message}"],
                        "overall_result": "error",
                        "completed": True,
                    },
                )
                return
            candidates = [entry["password"] for entry in entries or []]
        else:
            candidates = list(temporal_passwords or [])

        # Deduplicar preservando el orden de prueba.
        candidates = list(dict.fromkeys(p for p in candidates if p))
        if not candidates:
            update_task(
                tid,
                {
                    "status": "error",
                    "progress": 0,
                    "message": "No hay contraseñas candidatas",
                    "log": ["El vault o el archivo no contenían contraseñas"],
                    "overall_result": "error",
                    "completed": True,
                },
            )
            return

        batch_id = str(uuid.uuid4())
        ExecutionBatch.create_batch(
            batch_id=batch_id,
            operator=operator or "desconocido",
            action=ac,
            value=vl,
            target=ti,
            time_mode=time_mode,
            ntp_policy=ntp_policy,
            total_hosts=len(hosts),
            status="preview" if mode == "preview" else "running",
            # La vista previa nunca aplica cambios: no es una ejecución
            # simulada, sino una inspección real de solo lectura.
            dry_run=DRY_RUN and mode != "preview",
            db_path=db_path,
        )
        update_task(
            tid,
            {
                "batch_id": batch_id,
                "progress": 12,
                "message": (
                    f"Vista previa de {len(hosts)} host(s)…"
                    if mode == "preview"
                    else f"Ejecutando en {len(hosts)} host(s)…"
                ),
                "log": [f"Lote {batch_id} creado con {len(hosts)} host(s)"],
                "total_hosts": len(hosts),
                "operator": operator,
                "time_mode": time_mode,
                "ntp_policy": ntp_policy,
                "dry_run": DRY_RUN,
            },
        )

        log_lines: List[str] = [f"Lote {batch_id}"]
        deadline = time.monotonic() + BATCH_TIME_BUDGET_SECONDS
        if mode == "preview":
            _run_preview_batch(
                tid=tid,
                batch_id=batch_id,
                hosts=hosts,
                admin_user=au,
                candidates=candidates,
                action=ac,
                value=vl,
                password_origin=po,
                time_mode=time_mode,
                ntp_policy=ntp_policy,
                ntp_servers=list(ntp_servers or []),
                force=force,
                deadline=deadline,
                db_path=db_path,
                log_lines=log_lines,
            )
        else:
            _run_apply_batch(
                tid=tid,
                batch_id=batch_id,
                hosts=hosts,
                admin_user=au,
                candidates=candidates,
                action=ac,
                value=vl,
                password_origin=po,
                time_mode=time_mode,
                ntp_policy=ntp_policy,
                ntp_servers=list(ntp_servers or []),
                sync_rtc=sync_rtc,
                force=force,
                deadline=deadline,
                db_path=db_path,
                log_lines=log_lines,
            )

        batch = ExecutionBatch.finish_batch(
            batch_id=batch_id,
            total_hosts=len(hosts),
            db_path=db_path,
        )
        overall_result = batch.status if batch is not None else "error"
        if overall_result not in {"success", "partial", "failure", "error", "preview"}:
            overall_result = "error"
        _append_log(log_lines, _batch_message(batch))
        update_task(
            tid,
            {
                "status": "completed" if overall_result != "error" else "error",
                "progress": 100,
                "message": _batch_message(batch),
                "log": list(log_lines),
                "overall_result": overall_result,
                "completed": True,
                "batch_id": batch_id,
            },
        )
    except Exception as exc:  # pragma: no cover - salvaguarda
        logger.exception("Error en la ejecución %s", tid)
        if batch_id:
            try:
                ExecutionBatch.finish_batch(
                    batch_id=batch_id, status="error", db_path=db_path
                )
            except Exception:  # pragma: no cover - salvaguarda
                logger.exception("No se pudo cerrar el lote %s", batch_id)
        update_task(
            tid,
            {
                "status": "error",
                "progress": 0,
                "message": f"Error: {str(exc)[:120]}",
                "log": [f"Error: {exc}"],
                "overall_result": "error",
                "completed": True,
                "batch_id": batch_id,
            },
        )


@app.route("/progress/<task_id>")
@login_required
def progress_page(task_id: str):
    task = _get_task(task_id)
    if task is None:
        abort(404)
    return render_template("progress.html", task_id=task_id, task=task)


@app.route("/progress/<task_id>/events")
@login_required
def progress_sse(task_id: str):
    if _get_task(task_id) is None:
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
    task = _get_task(task_id)
    if task is None:
        response = jsonify({"status": "not_found", "exists": False})
    else:
        response = jsonify({**task, "exists": True})
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Lotes y vista previa (P4)
# ---------------------------------------------------------------------------
@app.route("/batches/<batch_id>")
@login_required
def batch_detail(batch_id: str):
    batch = ExecutionBatch.get_batch(batch_id, db_path=get_db_path())
    if batch is None:
        abort(404)
    host_results = ExecutionBatch.get_host_results(
        batch_id, db_path=get_db_path()
    )
    return render_template(
        "batch.html", batch=batch, host_results=host_results
    )


@app.route("/preview/<batch_id>")
@login_required
def preview_detail(batch_id: str):
    """Alias de la vista de lote para enlaces de vista previa."""
    return redirect(url_for("batch_detail", batch_id=batch_id))


# ---------------------------------------------------------------------------
# Contraseñas
# ---------------------------------------------------------------------------
def _password_response(success: bool, message: str, status: int = 200):
    if _wants_json():
        return jsonify({"success": success, "message": message}), status
    flash(message, "success" if success else "danger")
    return redirect(url_for("passwords"))


@app.route("/passwords", methods=["GET", "POST"])
@login_required
def passwords():
    if request.method == "POST":
        retry_after = _check_rate_limit(
            f"vault:{current_user.get_id()}:{_client_ip()}",
            VAULT_RATE_LIMIT,
            VAULT_RATE_WINDOW,
        )
        if retry_after is not None:
            logger.warning(
                "Operación de vault bloqueada por rate limit para usuario=%s",
                current_user.get_id(),
            )
            return _rate_limited_response(
                retry_after,
                "Demasiadas operaciones sobre el vault. Espera antes de reintentar.",
            )

        action = request.form.get("action", "")
        mpw = request.form.get("master_password", "")

        if action == "encrypt":
            pws = [
                line
                for line in request.form.get("passwords_textarea", "").splitlines()
                if line.strip()
            ]
            if not mpw:
                return _password_response(
                    False, "Ingresa la contraseña maestra del Vault.", 400
                )
            if not pws:
                return _password_response(
                    False, "Ingresa al menos una contraseña candidata.", 400
                )
            success, msg = VaultManager.encrypt_passwords(
                passwords=pws,
                master_password=mpw,
                origin="manual",
                db_path=get_db_path(),
            )
            return _password_response(success, msg, 200 if success else 400)

        if action == "delete":
            pid = request.form.get("password_id", "")
            try:
                password_id = int(pid)
            except (TypeError, ValueError):
                return _password_response(False, "ID de contraseña no válido.", 400)

            hash_full = PasswordRecord.get_hash_full(
                password_id, db_path=get_db_path()
            )
            if hash_full is None:
                return _password_response(False, "La contraseña no existe.", 400)

            try:
                vault_file = get_vault_file()
            except RuntimeError as exc:
                return _password_response(False, str(exc), 400)

            if os.path.exists(vault_file):
                if not mpw:
                    return _password_response(
                        False,
                        "La contraseña maestra es obligatoria para actualizar el vault.",
                        400,
                    )
                removed, message = VaultManager.remove_password(
                    hash_full, mpw, vault_file=vault_file
                )
                if not removed:
                    return _password_response(False, message, 400)

            deleted = PasswordRecord.delete_password(
                password_id, db_path=get_db_path()
            )
            return _password_response(
                deleted,
                "Contraseña eliminada del vault y de la base de datos."
                if deleted
                else "No se pudo eliminar de la base de datos.",
                200 if deleted else 400,
            )

        if action == "load_file":
            uploaded = request.files.get("password_file")
            if uploaded is None or not uploaded.filename:
                return _password_response(False, "No se seleccionó ningún archivo.", 400)

            extension = os.path.splitext(uploaded.filename)[1].lower()
            if extension not in ALLOWED_PASSWORD_EXTENSIONS:
                return _password_response(
                    False,
                    "Solo se permiten archivos .txt, .yml o .yaml.",
                    400,
                )

            if not mpw:
                return _password_response(
                    False,
                    "La contraseña maestra es obligatoria para importar contraseñas.",
                    400,
                )

            if extension == ".txt":
                pws, file_error = read_temporal_passwords(uploaded)
                if file_error:
                    return _password_response(False, file_error, 400)
                success, msg = VaultManager.encrypt_passwords(
                    passwords=pws,
                    master_password=mpw,
                    origin="file",
                    db_path=get_db_path(),
                )
                return _password_response(success, msg, 200 if success else 400)

            # .yml/.yaml: debe ser un vault ya cifrado con ansible-vault.
            raw = uploaded.read(MAX_CONTENT_LENGTH + 1)
            if len(raw) > MAX_CONTENT_LENGTH:
                return _password_response(
                    False, "El archivo del vault es demasiado grande.", 400
                )
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return _password_response(
                    False, "El archivo debe estar codificado en UTF-8.", 400
                )

            if not content.lstrip().startswith("$ANSIBLE_VAULT"):
                return _password_response(
                    False,
                    "El archivo .yml debe ser un vault cifrado con ansible-vault.",
                    400,
                )

            with secure_temp_file(
                content, prefix=".vault-upload-", suffix=".yml"
            ) as uploaded_path:
                ok, message, entries = VaultManager.decrypt_vault_file(
                    vault_file=uploaded_path,
                    master_password=mpw,
                )

            if not ok:
                return _password_response(False, message, 400)

            pws = [entry["password"] for entry in entries or []]
            if not pws:
                return _password_response(
                    False, "El vault no contiene contraseñas.", 400
                )

            success, msg = VaultManager.encrypt_passwords(
                passwords=pws,
                master_password=mpw,
                origin="file",
                db_path=get_db_path(),
            )
            if success:
                msg = f"Vault importado correctamente. {msg}"
            return _password_response(success, msg, 200 if success else 400)

        return _password_response(False, "Acción no reconocida.", 400)

    stored = PasswordRecord.get_all_passwords(limit=100, db_path=get_db_path())
    try:
        vault_exists = os.path.exists(get_vault_file())
    except RuntimeError:
        vault_exists = False
    return render_template(
        "passwords.html", passwords=stored, vault_exists=vault_exists
    )


@app.route("/history")
@login_required
def history():
    batches = ExecutionBatch.get_batches(limit=50, db_path=get_db_path())
    stats = ExecutionBatch.get_stats(db_path=get_db_path())
    return render_template("history.html", batches=batches, stats=stats)


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
@app.errorhandler(400)
def bad_request(error):
    message = getattr(error, "description", None) or "Petición no válida."
    if _wants_json():
        return jsonify({"success": False, "message": message}), 400
    return render_template("400.html", message=message), 400


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(429)
def too_many_requests(error):
    message = getattr(error, "description", None) or "Demasiadas peticiones."
    retry_after = request.headers.get("Retry-After", "60")
    if _wants_json():
        return jsonify({"success": False, "message": message}), 429
    response = make_response(
        render_template("429.html", message=message, retry_after=retry_after), 429
    )
    response.headers["Retry-After"] = str(retry_after)
    return response


@app.errorhandler(500)
def internal_error(error):
    logger.error("Error interno: %s", error)
    return render_template("500.html"), 500


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def register_cli(flask_app: Flask) -> None:
    @flask_app.cli.command("init-db")
    def init_db_command():
        """Crea las tablas de la base de datos."""
        init_db()
        click.echo(f"Base de datos inicializada en {get_db_path()}")

    @flask_app.cli.command("init-admin")
    @click.option("--username", default=ADMIN_USERNAME, show_default=True)
    @click.password_option(confirmation_prompt=True)
    def init_admin_command(username: str, password: str):
        """Crea o actualiza el usuario administrador."""
        if len(password) < ADMIN_PASSWORD_MIN_LENGTH:
            raise click.ClickException(
                f"La contraseña debe tener al menos {ADMIN_PASSWORD_MIN_LENGTH} caracteres."
            )
        User.create_or_update(username, password, db_path=get_db_path())
        click.echo(f"Usuario administrador '{username}' creado/actualizado.")

    @flask_app.cli.command("reconcile-batches")
    def reconcile_batches_command():
        """Marca como error los lotes que quedaron en curso."""
        count = ExecutionBatch.reconcile_orphans(db_path=get_db_path())
        click.echo(f"Lotes huérfanos reconciliados: {count}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def configure_app(flask_app: Flask) -> Flask:
    flask_app.config.update(
        SECRET_KEY=get_or_create_secret_key(),
        DEBUG=DEBUG,
        SESSION_COOKIE_HTTPONLY=SESSION_COOKIE_HTTPONLY,
        SESSION_COOKIE_SAMESITE=SESSION_COOKIE_SAMESITE,
        SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
        REMEMBER_COOKIE_HTTPONLY=REMEMBER_COOKIE_HTTPONLY,
        REMEMBER_COOKIE_SAMESITE=REMEMBER_COOKIE_SAMESITE,
        REMEMBER_COOKIE_SECURE=REMEMBER_COOKIE_SECURE,
        REMEMBER_COOKIE_DURATION=timedelta(
            seconds=REMEMBER_COOKIE_DURATION_SECONDS
        ),
        PERMANENT_SESSION_LIFETIME=timedelta(
            seconds=PERMANENT_SESSION_LIFETIME_SECONDS
        ),
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        CSRF_ENABLED=CSRF_ENABLED,
        CSRF_TIME_LIMIT=CSRF_TIME_LIMIT_SECONDS,
    )

    if TRUST_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix

        flask_app.wsgi_app = ProxyFix(
            flask_app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0
        )

    setup_logging()
    try:
        ensure_directories()
    except RuntimeError as exc:
        logger.warning(
            "No se pudieron crear los directorios de producción (%s); "
            "se usará el directorio local instance/ para desarrollo.",
            exc,
        )

    login_manager.init_app(flask_app)
    init_db()
    try:
        _recover_task_state()
    except Exception:  # pragma: no cover - no impedir el arranque
        logger.exception("No se pudieron recuperar las tareas persistidas")
    try:
        reconciled = ExecutionBatch.reconcile_orphans(db_path=get_db_path())
    except Exception:  # pragma: no cover - no impedir el arranque
        logger.exception("No se pudieron reconciliar lotes huérfanos")
    else:
        if reconciled:
            logger.warning(
                "Se marcaron %s lote(s) huérfano(s) como error al arrancar",
                reconciled,
            )
    register_cli(flask_app)
    return flask_app


configure_app(app)


if __name__ == "__main__":
    host = os.environ.get("HORA_ADMIN_HOST", "127.0.0.1")
    port = int(os.environ.get("HORA_ADMIN_PORT", "5000"))
    app.run(host=host, port=port, debug=DEBUG)
