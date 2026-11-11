#!/usr/bin/env python3
"""Ansible Control Center — configuración de hora/zona horaria en Ubuntu vía Ansible.

Incluye autenticación, validación de formularios, progreso SSE, historial en
SQLite, vault real con ansible-vault, ejecución de Ansible con inyección
segura de la contraseña candidata, CSRF, rate limiting, límites de red,
cabeceras de seguridad y ejecución en grupo con lotes, vista previa,
verificación e idempotencia (fases P0-P4).
"""
from __future__ import annotations

import atexit
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

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
    APPROVAL_TTL_MINUTES,
    APT_MIN_FREE_GB,
    APT_MIN_FREE_GB_CEILING,
    APT_MIN_FREE_GB_FLOOR,
    AUTH_EVENT_RETENTION_DAYS,
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
    HISTORY_RETENTION_DAYS,
    HOST_KEY_CHECKING,
    HSTS_ENABLED,
    HSTS_MAX_AGE,
    OS_PASSWORD_DEFAULT_USER,
    OS_PASSWORD_MAX_LENGTH,
    OS_PASSWORD_MIN_LENGTH,
    SSH_HOST_KEYS_AUTO,
    TARGET_ADMIN_USERNAME,
    TIMEZONE_BACKUP_DIR,
    IS_PRODUCTION,
    JOB_MAX_WORKERS,
    JOB_QUEUE_SIZE,
    JOB_RETENTION_HOURS,
    JOB_SHUTDOWN_GRACE_SECONDS,
    LOGIN_LOCKOUT_MINUTES,
    LOGIN_LOCKOUT_THRESHOLD,
    LOGIN_RATE_LIMIT,
    LOGIN_RATE_WINDOW,
    MAX_CONTENT_LENGTH,
    MAX_HOSTS,
    MAX_MEMORY_TASKS,
    MAX_NTP_SERVERS,
    MAX_PASSWORDS,
    MAX_PASSWORD_ATTEMPTS,
    MAX_PASSWORD_LENGTH,
    MAX_TASK_STATE_ROWS,
    NTP_POLICIES,
    NTP_SETTLE_SECONDS,
    PASSWORD_ORIGINS,
    PASSWORD_ORIGIN_VALUES,
    PERMANENT_SESSION_LIFETIME_SECONDS,
    PROCESS_ROLE,
    RATE_LIMIT_ENABLED,
    REQUEST_METRICS_ENABLED,
    REQUEST_METRICS_WINDOW,
    RECOVERY_STALE_MINUTES,
    REMEMBER_COOKIE_HTTPONLY,
    REMEMBER_COOKIE_SAMESITE,
    REMEMBER_COOKIE_SECURE,
    REMEMBER_COOKIE_DURATION_SECONDS,
    RETENTION_INTERVAL_HOURS,
    ROLES,
    SECURITY_HEADERS_ENABLED,
    SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
    SECRETS_DIR,
    SLOW_REQUEST_MS,
    TASK_PERSIST_INTERVAL_SECONDS,
    TASK_REQUEST_EMAIL,
    TASK_REQUEST_SUBJECT,
    TIME_REQUIRE_NTP_OFF,
    TIME_VERIFY_TOLERANCE,
    TRUST_PROXY,
    UI_CONFIG_PANEL,
    VAULT_RATE_LIMIT,
    VAULT_RATE_WINDOW,
    ensure_directories,
    get_or_create_secret_key,
    resolve_db_path,
    resolve_vault_file,
    setup_logging,
    validate_security_config,
)
from models.batch import (
    HOST_RESULT_LABELS,
    STATUS_LABELS,
    ExecutionBatch,
)
from models.db import get_connection, get_setting, init_schema, set_setting
from models.execution import ExecutionRecord
from models.ops import (
    ApprovalRequest,
    APPROVAL_PENDING,
    HostGroup,
    InventoryHost,
    Schedule,
    SCHEDULE_RECURRENCES,
    params_hash,
)
from models.password import PasswordRecord
from models.user import AuthEvent, User
import config
import services.ansible_runner as ansible_runner_module
import services.ldap_auth as ldap_auth
import services.password_tester as password_tester_module
from services.ansible_runner import (
    apply_host,
    inspect_apt,
    inspect_firewall,
    inspect_host,
    inspect_password,
    inspect_wallpaper,
)
from services.csrf import csrf_protect, generate_csrf_token, rotate_csrf_token
from services.host_resolver import pin_target, resolve_target
from services.job_queue import JobQueue, JobQueueFull
from services.metrics import RequestMetrics, summarize
from services.password_tester import test_ansible_ping
from services.pdf_report import (
    build_execution_report,
    build_migration_report,
    format_spanish_datetime,
)
from services.rate_limiter import LIMITER
from services.safety import (
    CRITICALITY_LABELS,
    CRITICALITY_LEVELS,
    evaluate_safety,
)
import services.notifier as notifier
import services.scheduler as scheduler
from services import modules_panel
from services.secure_temp import secure_temp_file
from services.ssh_host_keys import ensure_host_keys
from services.task_catalog import (
    TASK_CATALOG,
    STATUS_ACTIVE,
    get_task,
)
from services.time_utils import (
    LOCAL_TIME_AMBIGUOUS,
    LOCAL_TIME_MISSING,
    local_time_epoch,
    local_time_status,
    timezone_exists,
    utc_time_epoch,
)
from services.vault_manager import VaultManager
from services.wallpaper import (
    remove_wallpaper,
    remote_wallpaper_uri,
    save_wallpaper,
)

logger = logging.getLogger("acc")

# ---------------------------------------------------------------------------
# Estado en memoria de las tareas (progreso SSE)
# ---------------------------------------------------------------------------
_tasks: Dict[str, Dict[str, Any]] = {}
_tasks_lock = threading.Lock()
# Evento por tarea para despertar el stream SSE en cuanto cambia su estado,
# sin sondeo con ``time.sleep``. El evento se descarta al borrar la tarea.
_task_events: Dict[str, threading.Event] = {}
# Última escritura en SQLite por tarea (tiempo monotónico) para agrupar las
# actualizaciones frecuentes de consola/log.
_task_persist_times: Dict[str, float] = {}
_task_persist_lock = threading.Lock()
_db_path_cache: Optional[str] = None

# Métricas de rendimiento por ruta (ventana acotada en memoria, sin
# persistencia). Se consultan desde ``/metricas`` y ``/api/metricas``.
request_metrics = RequestMetrics(window=REQUEST_METRICS_WINDOW)


def _active_task_count() -> int:
    """Número de tareas pendientes o en ejecución en este proceso."""
    with _tasks_lock:
        return sum(
            1
            for task in _tasks.values()
            if not task.get("completed")
            and task.get("status") in {"pending", "running"}
        )


# Idempotencia de envío: una operación solo se procesa una vez. El formulario
# incluye un UUID oculto y la reserva se guarda en SQLite, de modo que también
# sobrevive a reinicios y es compartida por varios workers.
_OPERATION_ID_TTL = 3600
_MAX_OPERATION_IDS = 5000

# Estados que cierran el stream SSE.
TERMINAL_STATES = {
    "completed",
    "failed",
    "error",
    "found",
    "failure",
    "timeout",
    "cancelled",
    "incomplete",
}

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para continuar."
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"

# La app se crea aquí para que los decoradores @app.route de este módulo
# encuentren el objeto. La configuración se aplica al final (configure_app).
app = Flask(__name__)

# Cola acotada de trabajos. Se crea de forma perezosa para no arrancar hilos al
# importar el módulo (tests, CLI) y se apaga ordenadamente en ``configure_app``.
_job_queue: Optional[JobQueue] = None


def get_job_queue() -> JobQueue:
    global _job_queue
    if _job_queue is None:
        _job_queue = JobQueue(
            max_workers=JOB_MAX_WORKERS,
            max_queue=JOB_QUEUE_SIZE,
            shutdown_grace=JOB_SHUTDOWN_GRACE_SECONDS,
        )
    return _job_queue


def roles_required(*roles: str):
    """Restringe una ruta a los roles indicados (``admin`` siempre pasa)."""
    from functools import wraps

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


def get_db_path() -> str:
    """Ruta de SQLite resuelta una sola vez por proceso."""
    global _db_path_cache
    if _db_path_cache is None:
        _db_path_cache = str(resolve_db_path())
    return _db_path_cache


# ---------------------------------------------------------------------------
# Modo simulación (DRY RUN) editable desde la interfaz
# ---------------------------------------------------------------------------
DRY_RUN_SETTING_KEY = "dry_run"
# Interruptor de acceso LDAPS, cambiado desde /usuarios por el admin local.
# Antes del primer cambio decide ``ACC_LDAP_ENABLED`` (entorno).
LDAP_ACCESS_SETTING_KEY = "ldap_access_enabled"
_dry_run_lock = threading.Lock()


def is_dry_run() -> bool:
    """Estado actual del modo simulación en este proceso."""
    return bool(DRY_RUN)


def apply_dry_run(enabled: bool) -> None:
    """Propaga el modo simulación a todos los módulos que lo consultan.

    ``app``, ``services.ansible_runner`` y ``services.password_tester``
    importan la constante en su propio espacio de nombres; hay que actualizar
    las tres copias. Gunicorn se ejecuta con un único worker y varios hilos,
    así que el estado compartido en proceso es consistente.
    """
    global DRY_RUN
    value = bool(enabled)
    with _dry_run_lock:
        DRY_RUN = value
        ansible_runner_module.DRY_RUN = value
        password_tester_module.DRY_RUN = value
        app.config["DRY_RUN"] = value


def load_dry_run_setting() -> bool:
    """Recupera el modo simulación persistido o mantiene el valor de entorno."""
    stored = get_setting(DRY_RUN_SETTING_KEY, db_path=get_db_path())
    if stored is None:
        return DRY_RUN
    return str(stored).strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def persist_dry_run(enabled: bool) -> None:
    set_setting(
        DRY_RUN_SETTING_KEY,
        "1" if enabled else "0",
        db_path=get_db_path(),
    )


def load_ldap_access_setting() -> bool:
    """Estado del interruptor de acceso LDAPS (persistido en la BD).

    Antes del primer cambio desde la interfaz manda ``ACC_LDAP_ENABLED``
    (el valor de entorno); después de cualquier conmutación decide la BD,
    sin reinicios del contenedor.
    """
    stored = get_setting(LDAP_ACCESS_SETTING_KEY, db_path=get_db_path())
    if stored is None:
        return bool(config.LDAP_ENABLED)
    return str(stored).strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def persist_ldap_access(enabled: bool) -> None:
    set_setting(
        LDAP_ACCESS_SETTING_KEY,
        "1" if enabled else "0",
        db_path=get_db_path(),
    )


def _wants_json() -> bool:
    return (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )


def _is_safe_redirect(target: Optional[str]) -> bool:
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
_OS_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.?$"
)

# Acciones que el motor de ejecución sabe aplicar. ``WALLPAPER`` usa su
# propio playbook y transporte de imagen; ``APT`` usa el de actualización
# segura; ``PASSWORD`` cambia la contraseña de un usuario del sistema;
# ``FIREWALL`` activa, desactiva o permite un origen en el firewall.
VALID_ACTIONS = {"TZ", "TIME", "WALLPAPER", "APT", "PASSWORD", "FIREWALL"}


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


def _fold_value(raw: str) -> Optional[int]:
    """Normaliza la desambiguación DST: ``auto``, ``0`` o ``1``."""
    value = (raw or "auto").strip().lower()
    if value in {"0", "1"}:
        return int(value)
    return None


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


def _parse_min_free_gb(raw: str) -> tuple[Optional[float], Optional[str]]:
    """Valida el umbral de espacio libre (GiB) de la tarjeta APT.

    Un valor vacío usa el umbral del servidor (``ACC_APT_MIN_FREE_GB``). Los
    límites evitan tanto umbrales absurdos como valores negativos.
    """
    text = (raw or "").strip()
    if not text:
        return float(APT_MIN_FREE_GB), None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None, "El umbral de espacio libre debe ser un número en GB."
    if value < APT_MIN_FREE_GB_FLOOR or value > APT_MIN_FREE_GB_CEILING:
        return None, (
            "El umbral de espacio libre debe estar entre "
            f"{APT_MIN_FREE_GB_FLOOR:g} y {APT_MIN_FREE_GB_CEILING:g} GB."
        )
    return value, None


def _parse_target_user(raw: str) -> tuple[str, Optional[str]]:
    """Normaliza el usuario del sistema al que se cambiará la contraseña."""
    user = (raw or "").strip() or OS_PASSWORD_DEFAULT_USER
    if not _OS_USERNAME_RE.fullmatch(user):
        return "", (
            "El usuario del sistema no es válido: usa minúsculas, números, "
            "guion, punto o guion bajo (máximo 32 caracteres)."
        )
    return user, None


_FIREWALL_STATES = {"enable", "disable"}


def _parse_firewall_value(raw: str) -> tuple[str, Optional[str]]:
    """Normaliza la operación de firewall a ``enable``, ``disable`` o
    ``allow:<ip>``.

    Se acepta una IP suelta o una red CIDR (IPv4 o IPv6). Se rechaza la red
    universal (``0.0.0.0/0`` y ``::/0``): abrir el firewall a cualquier origen
    no es una regla de confianza y anularía el propósito de la tarjeta.
    """
    text = (raw or "").strip()
    lowered = text.lower()
    if lowered in _FIREWALL_STATES:
        return lowered, None
    if lowered.startswith("allow:"):
        candidate = text.split(":", 1)[1].strip()
        if not candidate:
            return "", "Indica la IP o red que se quiere permitir."
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            return "", f"IP o red no válida: {candidate}."
        if network.prefixlen == 0:
            return "", (
                "No se permite abrir el firewall a cualquier origen "
                "(0.0.0.0/0 o ::/0). Indica una IP o red concreta."
            )
        if network.prefixlen == network.max_prefixlen:
            source = str(network.network_address)
        else:
            source = str(network)
        return f"allow:{source}", None
    if not text:
        return "", "Selecciona una operación de firewall."
    return "", "Operación de firewall no válida."


def _validate_new_password(
    password: str,
    confirm: str,
    *,
    required: bool,
) -> Optional[str]:
    """Valida la contraseña nueva y su repetición sin recortar el secreto.

    La repetición es obligatoria para evitar errores de escritura. La
    contraseña no se registra ni se devuelve en el mensaje de error.
    """
    password = password or ""
    confirm = confirm or ""
    if not password and not confirm:
        if required:
            return "La nueva contraseña es obligatoria."
        return None
    if password != confirm:
        return "La nueva contraseña y su repetición no coinciden."
    if len(password) < OS_PASSWORD_MIN_LENGTH:
        return (
            "La nueva contraseña debe tener al menos "
            f"{OS_PASSWORD_MIN_LENGTH} caracteres."
        )
    if len(password) > OS_PASSWORD_MAX_LENGTH:
        return (
            "La nueva contraseña supera el máximo de "
            f"{OS_PASSWORD_MAX_LENGTH} caracteres."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        return "La nueva contraseña no puede contener caracteres de control."
    return None


def _validate_operation_core(
    *,
    task: Optional[Dict[str, Any]],
    task_id: str,
    admin_user: str,
    action: str,
    value: str,
    ntp_policy: str,
    ntp_servers_raw: str,
    platform: str = "",
    group_name: str = "",
    reject_destructive: bool = False,
    enforce_time_ntp_off: bool = True,
    for_schedule: bool = False,
    min_free_gb_raw: str = "",
    target_user_raw: str = "",
    new_password: str = "",
    new_password_confirm: str = "",
    require_password: bool = False,
) -> tuple[Optional[Dict[str, Any]], List[str]]:
    """Reglas comunes a la ejecución manual y las programaciones.

    Centralizar esta validación evita que una programación acepte algo que el
    panel rechaza (o al revés): tarea activa, acción admitida, valor fijo,
    plataforma del grupo, zona/hora válida, política NTP y usuario.
    """
    errors: List[str] = []
    admin_user = (admin_user or "").strip()
    action = (action or "").strip().upper()
    value = (value or "").strip()
    ntp_policy = (ntp_policy or "keep").strip().lower() or "keep"

    if task is None or task.get("status") != "active":
        errors.append("Selecciona una tarea activa del catálogo.")
    else:
        if action not in (task.get("actions") or ["TZ", "TIME"]):
            errors.append(
                f"La tarea '{task.get('name', task_id)}' no admite la acción "
                f"{action}."
            )
        if task.get("fixed_value") and action == "TZ":
            value = str(task["fixed_value"])
        if reject_destructive and task.get("destructive"):
            errors.append(
                "Las tareas destructivas requieren aprobación manual; no se "
                "pueden programar."
            )
        if for_schedule and not task.get("schedulable", True):
            errors.append(
                f"La tarea '{task.get('name', task_id)}' no se puede "
                "programar: ejecútala manualmente desde su tarjeta."
            )
        platforms = task.get("platforms") or []
        if platform and platforms and platform not in platforms:
            platform_label = "Windows" if platform == "windows" else "Linux"
            objective = (
                f"el grupo '{group_name}'" if group_name else "el objetivo"
            )
            errors.append(
                f"La tarea '{task.get('name', task_id)}' no está disponible "
                f"para {objective} ({platform_label})."
            )

    if not admin_user:
        errors.append("El usuario administrador es obligatorio.")
    if action not in VALID_ACTIONS:
        errors.append("Acción no válida.")

    ntp_servers: List[str] = []
    min_free_gb = float(APT_MIN_FREE_GB)
    if action in {"TZ", "TIME"}:
        if ntp_policy not in NTP_POLICIES:
            errors.append("Política NTP no válida.")
        if ntp_policy == "configure":
            ntp_servers, ntp_error = _parse_ntp_servers(ntp_servers_raw)
            if ntp_error:
                errors.append(ntp_error)
            if not ntp_servers:
                ntp_servers = list(DEFAULT_NTP_SERVERS)
                if not ntp_servers:
                    errors.append(
                        "La política NTP 'configure' requiere al menos un "
                        "servidor NTP."
                    )
    else:
        # El tapiz y la actualización no tocan el reloj ni NTP: se normaliza
        # la política para no arrastrar opciones sin sentido al playbook.
        ntp_policy = "keep"

    if action == "APT":
        # ``safe`` equivale a apt-get upgrade: nunca se elimina ni se instala
        # un paquete nuevo. No se acepta otro modo desde el formulario.
        value = "safe"
        min_free_gb, min_error = _parse_min_free_gb(min_free_gb_raw)
        if min_error:
            errors.append(min_error)

    if action == "PASSWORD":
        # El valor visible del lote es el usuario del sistema, nunca el
        # secreto. La contraseña se valida aquí y se envía al playbook por el
        # fichero temporal de variables, fuera de la línea de comandos.
        value, user_error = _parse_target_user(target_user_raw)
        if user_error:
            errors.append(user_error)
        password_error = _validate_new_password(
            new_password,
            new_password_confirm,
            required=require_password,
        )
        if password_error:
            errors.append(password_error)

    if action == "FIREWALL":
        # ``value`` transporta la operación completa para que la validación,
        # la aprobación y el historial compartan un único campo:
        # ``enable``, ``disable`` o ``allow:<ip|red>``.
        value, firewall_error = _parse_firewall_value(value)
        if firewall_error:
            errors.append(firewall_error)

    if action == "TZ":
        if not value:
            errors.append("La zona horaria es obligatoria.")
        elif not _TIMEZONE_RE.fullmatch(value):
            errors.append("Formato de zona horaria inválido.")
        elif not timezone_exists(value):
            errors.append(f"Zona horaria desconocida: {value}.")
    elif action == "TIME":
        if not value:
            errors.append("La hora exacta es obligatoria.")
        else:
            try:
                parsed = datetime.strptime(
                    value.replace(" ", "T"), "%Y-%m-%dT%H:%M"
                )
                value = parsed.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                errors.append(
                    "Formato de hora inválido; use AAAA-MM-DDTHH:MM."
                )
        if (
            enforce_time_ntp_off
            and ntp_policy == "keep"
            and TIME_REQUIRE_NTP_OFF
        ):
            errors.append(
                "TIME con NTP en política keep no está permitido al "
                "ejecutar: el reloj podría revertirse. Usa off, restore o "
                "configure."
            )
    # ``WALLPAPER`` no valida un valor de texto: la imagen la valida y guarda
    # ``_parse_operation_form`` antes de llegar aquí.

    if errors:
        return None, errors
    return (
        {
            "admin_user": admin_user,
            "action": action,
            "value": value,
            "ntp_policy": ntp_policy,
            "ntp_servers": ntp_servers,
            "min_free_gb": min_free_gb,
        },
        [],
    )


def _validate_scheduled_params(
    task: Optional[Dict[str, Any]],
    admin_user: str,
    action: str,
    value: str,
    ntp_policy: str,
    ntp_servers_raw: str,
    platform: str = "",
    group_name: str = "",
    min_free_gb_raw: str = "",
) -> tuple[Optional[Dict[str, Any]], List[str]]:
    """Valida una programación con las mismas reglas que la ejecución manual."""
    return _validate_operation_core(
        task=task,
        task_id=(task or {}).get("id", ""),
        admin_user=admin_user,
        action=action,
        value=value,
        ntp_policy=ntp_policy,
        ntp_servers_raw=ntp_servers_raw,
        platform=platform,
        group_name=group_name,
        reject_destructive=True,
        enforce_time_ntp_off=True,
        for_schedule=True,
        min_free_gb_raw=min_free_gb_raw,
    )


def _parse_operation_form(
    default_mode: str = "execute",
) -> tuple[Optional[Dict[str, Any]], List[str]]:
    """Valida el formulario completo de vista previa/ejecución (P4).

    Devuelve un diccionario normalizado o una lista de errores. La vista previa
    y la ejecución comparten exactamente la misma validación para que lo que se
    previsualiza sea lo que después se ejecuta.
    """
    target_mode = (
        request.form.get("target_mode", "manual").strip().lower() or "manual"
    )
    target_group_raw = request.form.get("target_group", "").strip()
    # Tarea del catálogo: determina permisos y si es destructiva. Para no
    # romper envíos anteriores (que no enviaban el campo oculto), si falta se
    # asume la tarea operativa por defecto del catálogo.
    task_id = request.form.get("task", "").strip()
    target = request.form.get("target_ip", "").strip()
    admin_user = request.form.get("admin_user", "").strip()
    origin = request.form.get("password_origin", "stored").strip()
    master_password = request.form.get("master_password", "").strip()
    action = request.form.get("action", "").strip().upper()
    value = request.form.get("value", "").strip()
    mode = (
        request.form.get("mode", default_mode).strip().lower() or default_mode
    )
    time_mode = request.form.get("time_mode", "utc").strip().lower() or "utc"
    time_zone = request.form.get("time_zone", "").strip()
    time_fold_raw = request.form.get("time_fold", "auto").strip().lower()
    ntp_policy = request.form.get("ntp_policy", "keep").strip().lower() or "keep"
    ntp_servers_raw = request.form.get("ntp_servers", "")
    min_free_gb_raw = request.form.get("min_free_gb", "")
    target_user_raw = request.form.get("target_user", "")
    new_password = request.form.get("new_password", "")
    new_password_confirm = request.form.get("new_password_confirm", "")
    sync_rtc = _form_bool("sync_rtc")
    force = _form_bool("force")

    errors: List[str] = []

    # Tarea del catálogo: determina permisos y si es destructiva. Para no
    # romper envíos anteriores (que no enviaban el campo oculto), si falta se
    # asume la tarea operativa por defecto del catálogo.
    if not task_id:
        for candidate in TASK_CATALOG:
            if candidate.get("status") == "active" and not candidate.get(
                "destructive"
            ):
                task_id = candidate["id"]
                break
    task = get_task(task_id) if task_id else None

    # Si el envío no trae ``action`` (clientes antiguos o un formulario de una
    # sola acción), se usa la única acción de la tarea; en su defecto, TZ.
    if task is not None and not action:
        task_actions = task.get("actions") or ["TZ", "TIME"]
        action = (
            str(task_actions[0]).strip().upper()
            if len(task_actions) == 1
            else "TZ"
        )
    elif not action:
        action = "TZ"

    # Firewall: el formulario separa «operación» e «IP» por claridad, pero el
    # motor, el historial y la validación comparten un único campo ``value``
    # (``enable``, ``disable`` o ``allow:<ip|red>``).
    if action == "FIREWALL":
        firewall_op = request.form.get("firewall_op", "").strip().lower()
        firewall_ip = request.form.get("firewall_ip", "").strip()
        value = f"allow:{firewall_ip}" if firewall_op == "allow" else firewall_op

    # Imagen del tapiz: se valida por firma binaria y se guarda fuera de la
    # raíz web. El archivo se borra al terminar el lote (o si el envío falla).
    wallpaper_path = ""
    wallpaper_uri = ""
    wallpaper_name = ""
    if (
        action == "WALLPAPER"
        and task is not None
        and task.get("status") == "active"
    ):
        try:
            saved_wallpaper = save_wallpaper(
                request.files.get("wallpaper_file")
            )
        except ValueError as exc:
            errors.append(str(exc))
        else:
            wallpaper_path = saved_wallpaper["path"]
            wallpaper_name = saved_wallpaper["name"]
            wallpaper_uri = remote_wallpaper_uri(saved_wallpaper["ext"])
            # El nombre del archivo es el valor visible en el lote y el PDF.
            value = wallpaper_name

    if mode not in {"preview", "execute"}:
        errors.append("Modo de operación no válido.")

    # Objetivo: manual (IP literal) o grupo del inventario persistente.
    target_group_id: Optional[int] = None
    group_name = ""
    group_target_hosts: List[str] = []
    group_os_flavor = ""
    group_connection = ""
    if target_mode not in {"manual", "group"}:
        errors.append("Modo de objetivo no válido.")
        target_mode = "manual"
    if target_mode == "group":
        try:
            target_group_id = int(target_group_raw)
        except ValueError:
            target_group_id = None
        group = (
            HostGroup.get(target_group_id, db_path=get_db_path())
            if target_group_id is not None
            else None
        )
        if group is None:
            errors.append("Selecciona un grupo del inventario válido.")
            target_group_id = None
        else:
            group_name = group.name
            group_os_flavor = group.os_flavor
            group_connection = group.connection
            # Instantánea ordenada de hosts para que una aprobación quede
            # ligada al conjunto concreto que el admin autorizó. Si el grupo
            # cambia después, el hash de la aprobación deja de coincidir.
            group_target_hosts = sorted(
                {
                    host.address
                    for host in HostGroup.hosts_of(
                        group.id, db_path=get_db_path()
                    )
                }
            )
            if not group.host_count:
                errors.append(
                    f"El grupo '{group.name}' no tiene hosts; añádelos en el inventario."
                )
            elif group.host_count > MAX_HOSTS:
                errors.append(
                    f"El grupo '{group.name}' tiene {group.host_count} hosts "
                    f"y el máximo es {MAX_HOSTS}."
                )
        # El objetivo real se resuelve en caliente al ejecutar (los hosts del
        # grupo pueden cambiar entre el envío y el disparo del lote).
        target = f"inventory:{target_group_id}" if target_group_id is not None else ""
    else:
        target_error = None
        pinned_target, target_error = pin_target(target)
        if target_error:
            errors.append(target_error)
        else:
            # Guardar la IP literal evita que Ansible vuelva a resolver DNS.
            target = pinned_target
    # Mismas reglas que una programación: tarea, acción, valor fijo,
    # plataforma del grupo, zona/hora, NTP y usuario.
    validated, core_errors = _validate_operation_core(
        task=task,
        task_id=task_id,
        admin_user=admin_user,
        action=action,
        value=value,
        ntp_policy=ntp_policy,
        ntp_servers_raw=ntp_servers_raw,
        platform=group_os_flavor,
        group_name=group_name,
        reject_destructive=False,
        enforce_time_ntp_off=mode == "execute",
        min_free_gb_raw=min_free_gb_raw,
        target_user_raw=target_user_raw,
        new_password=new_password,
        new_password_confirm=new_password_confirm,
        require_password=action == "PASSWORD" and mode == "execute",
    )
    errors.extend(core_errors)
    if validated is not None:
        admin_user = validated["admin_user"]
        action = validated["action"]
        value = validated["value"]
        ntp_policy = validated["ntp_policy"]
        ntp_servers = list(validated["ntp_servers"])
        min_free_gb = float(validated["min_free_gb"])
    else:
        ntp_servers = []
        min_free_gb = float(APT_MIN_FREE_GB)

    if action == "TIME":
        if time_mode not in {"utc", "local"}:
            errors.append("Semántica de hora no válida (utc o local).")
        # El formato de ``value`` ya lo normalizó el validador común.

        if time_mode == "local":
            # La hora de pared necesita una zona real y resolver el hueco o el
            # solapamiento de DST. Así la vista previa y el playbook calculan
            # exactamente el mismo epoch.
            if not time_zone:
                errors.append(
                    "La zona horaria es obligatoria para la hora local (DST)."
                )
            elif not timezone_exists(time_zone):
                errors.append(f"Zona horaria desconocida: {time_zone}.")
            else:
                status, message = local_time_status(
                    value,
                    time_zone,
                    fold=_fold_value(time_fold_raw),
                )
                if status == LOCAL_TIME_MISSING:
                    errors.append(message)
                elif status == LOCAL_TIME_AMBIGUOUS:
                    errors.append(message)
                elif status not in {"ok"}:
                    errors.append(message)
        else:
            # En UTC no se usa la zona del host; se calcula el instante en
            # Python para que la comprobación sea determinista.
            time_zone = ""
            time_fold_raw = "auto"

        if time_mode in {"utc", "local"} and not errors:
            fold = _fold_value(time_fold_raw)
            if time_mode == "local":
                # ``auto`` solo puede resolverse a 0 cuando la hora no es
                # ambigua (las ambiguas se rechazan arriba). Fijamos el valor
                # para que vista previa y ejecución compartan el mismo fold.
                fold = 0 if fold is None else fold
                time_fold_raw = str(fold)
            target_epoch = (
                utc_time_epoch(value)
                if time_mode == "utc"
                else local_time_epoch(value, time_zone, fold or 0)
            )
            if target_epoch is None:
                errors.append("No se pudo calcular el instante objetivo.")
        else:
            target_epoch = None
    else:
        # La semántica UTC/local solo aplica a TIME.
        time_mode = "utc"
        time_zone = ""
        time_fold_raw = "auto"
        target_epoch = None

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
        # Nunca dejar imágenes huérfanas si el formulario no llega a ejecutarse.
        if wallpaper_path:
            remove_wallpaper(wallpaper_path)
        return None, errors

    return (
        {
            "target": target,
            "target_mode": target_mode,
            "target_group_id": target_group_id,
            "group_name": group_name,
            "group_os_flavor": group_os_flavor,
            "group_connection": group_connection,
            "target_hosts": group_target_hosts,
            "task_id": task_id,
            "task_name": task.get("name", task_id) if task else task_id,
            "admin_user": admin_user,
            "origin": origin,
            "master_password": master_password,
            "action": action,
            "value": value,
            "target_user": value if action == "PASSWORD" else "",
            "new_password": new_password if action == "PASSWORD" else "",
            "mode": mode,
            "time_mode": time_mode,
            "time_zone": time_zone,
            "time_fold": _fold_value(time_fold_raw),
            "target_epoch": target_epoch,
            "ntp_policy": ntp_policy,
            "ntp_servers": ntp_servers,
            "sync_rtc": sync_rtc,
            "force": force,
            "min_free_gb": min_free_gb,
            "temporal_passwords": temporal_passwords,
            "wallpaper_path": wallpaper_path,
            "wallpaper_uri": wallpaper_uri,
            "wallpaper_name": wallpaper_name,
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
    target_epoch: Optional[int] = None,
    wallpaper_uri: str = "",
) -> Optional[bool]:
    """Estima si hace falta cambiar algo según el estado leído (vista previa).

    Devuelve ``None`` si el estado no permite decidirlo (por ejemplo, un host
    inaccesible o una salida incompleta del playbook de inspección).

    Para ``TIME`` se compara el epoch calculado en Python (``target_epoch``),
    no la cadena local del host: así la vista previa y el playbook usan el
    mismo instante incluso en cambios de horario de verano.
    """
    if force:
        return True
    if not isinstance(state, dict):
        return None

    if action == "WALLPAPER":
        current_uri = str(state.get("wallpaper_uri") or "").strip()
        return current_uri != (wallpaper_uri or "").strip()

    if action == "APT":
        # Una estación marcada para migración o sin espacio no se tocará.
        if state.get("attention"):
            return False
        try:
            pending = int(state.get("pending") or 0)
        except (TypeError, ValueError):
            return None
        return pending > 0

    if action == "PASSWORD":
        # El cambio de contraseña siempre se aplica si el usuario existe; la
        # inspección ya falla cuando no existe.
        return bool(state.get("exists"))

    if action == "FIREWALL":
        mode, _, source = str(value or "").partition(":")
        active = _state_bool(state.get("active"))
        if mode == "enable":
            return not active
        if mode == "disable":
            return active
        if mode == "allow":
            sources = state.get("allowed_sources") or []
            if not isinstance(sources, list):
                return None
            return source not in {str(item) for item in sources}
        return None

    changed = False
    if action == "TZ":
        changed = (state.get("timezone") or "") != value
    elif action == "TIME":
        if target_epoch is None:
            return None
        try:
            current_epoch = int(state.get("epoch") or 0)
        except (TypeError, ValueError):
            return None
        changed = current_epoch == 0 or (
            abs(current_epoch - int(target_epoch)) > TIME_VERIFY_TOLERANCE
        )

    if ntp_policy == "off":
        changed = changed or _state_bool(state.get("ntp"))
    elif ntp_policy == "configure":
        current_servers = state.get("ntp_servers")
        desired_servers = {str(server) for server in (ntp_servers or [])}
        # El playbook escribe los servidores deseados en su propio fichero,
        # pero no elimina los que ya existan en la configuración del sistema.
        # Exigir igualdad exacta hacía que la vista previa siguiera indicando
        # "cambiará" después de aplicar. Basta con que estén todos los deseados.
        servers_match = isinstance(current_servers, list) and desired_servers.issubset(
            {str(server) for server in current_servers}
        )
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
        f"{batch.error_count} error(es), "
        f"{batch.attention_count} requieren atención"
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


def _refresh_task_from_db(
    tid: str, current: Dict[str, Any]
) -> Dict[str, Any]:
    """Adopta la instantánea de SQLite si otra instancia la actualizó.

    La copia en memoria del proceso que ejecuta la tarea puede ser más nueva
    que la persistida (la consola se agrupa cada ``ACC_TASK_PERSIST_INTERVAL``
    segundos). Por eso solo se reemplaza cuando la marca ``updated_at`` de
    SQLite es estrictamente mayor.
    """
    try:
        with get_connection(get_db_path()) as conn:
            row = conn.execute(
                "SELECT payload FROM task_state WHERE task_id = ?", (tid,)
            ).fetchone()
    except Exception:
        return current
    if row is None:
        return current

    try:
        persisted = json.loads(row["payload"])
    except (TypeError, ValueError):
        return current
    if not isinstance(persisted, dict):
        return current

    persisted_updated = str(persisted.get("updated_at") or "")
    current_updated = str(current.get("updated_at") or "")
    if persisted_updated <= current_updated:
        return current

    with _tasks_lock:
        latest = _tasks.get(tid)
        if latest is not None and str(
            latest.get("updated_at") or ""
        ) > persisted_updated:
            return latest
        _tasks[tid] = persisted
        return persisted


def _task_viewer_actor() -> str:
    """Identidad del usuario actual para compararla con el dueño de la tarea."""
    return getattr(current_user, "username", "") or str(current_user.get_id())


def _can_view_task(task: Dict[str, Any]) -> bool:
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


def _get_task(
    tid: str, refresh: bool = False
) -> Optional[Dict[str, Any]]:
    """Devuelve una tarea, leyendo SQLite cuando no está en este proceso.

    ``refresh=True`` consulta la instantánea persistida para que las lecturas
    (estado, progreso y cancelación) sean correctas aunque la petición caiga
    en un worker distinto del que ejecuta la tarea.
    """
    with _tasks_lock:
        task = _tasks.get(tid)
    if task is None:
        return _load_persisted_task(tid)
    if refresh:
        return _refresh_task_from_db(tid, task)
    return task


def _delete_persisted_task(tid: str) -> None:
    try:
        with get_connection(get_db_path()) as conn:
            conn.execute("DELETE FROM task_state WHERE task_id = ?", (tid,))
    except Exception:
        logger.debug("No se pudo borrar la tarea persistida %s", tid)


def _trim_task_state_rows() -> None:
    """Acota la tabla ``task_state`` a las instantáneas más recientes.

    Sin este límite, un proceso de larga vida acumularía el estado completo de
    todas las operaciones históricas (incluida la consola) en SQLite.
    """
    try:
        with get_connection(get_db_path()) as conn:
            conn.execute(
                """
                DELETE FROM task_state
                WHERE task_id NOT IN (
                    SELECT task_id FROM task_state
                    ORDER BY updated_at DESC, task_id DESC
                    LIMIT ?
                )
                """,
                (MAX_TASK_STATE_ROWS,),
            )
    except Exception:
        logger.debug("No se pudo recortar task_state", exc_info=True)


def _purge_retention() -> None:
    """Aplica las políticas de retención al arrancar.

    Se ejecuta de forma perezosa y tolerante a fallos: una tabla ausente o una
    base de datos bloqueada no debe impedir que la aplicación arranque.
    """
    db_path = get_db_path()
    try:
        tasks = ExecutionBatch.purge_old(HISTORY_RETENTION_DAYS, db_path=db_path)
    except Exception:
        tasks = 0
        logger.exception("No se pudieron purgar lotes antiguos")
    try:
        records = ExecutionRecord.purge_old(
            HISTORY_RETENTION_DAYS, db_path=db_path
        )
    except Exception:
        records = 0
        logger.exception("No se pudo purgar el historial heredado")
    try:
        events = AuthEvent.purge_old(
            AUTH_EVENT_RETENTION_DAYS, db_path=db_path
        )
    except Exception:
        events = 0
        logger.exception("No se pudieron purgar eventos de autenticación")
    _trim_task_state_rows()
    if tasks or records or events:
        logger.info(
            "Retención aplicada: %s lote(s), %s registro(s) y %s evento(s) "
            "eliminados",
            tasks,
            records,
            events,
        )


def _recover_task_state(stale_minutes: Optional[int] = None) -> None:
    """Purga tareas antiguas y cierra las que quedaron en curso.

    Al arrancar no puede haber ninguna tarea legítimamente en ejecución en este
    proceso, así que las instantáneas incompletas se marcan como error. Con
    ``stale_minutes`` solo se tocan las que llevan ese tiempo sin actualizarse:
    así un worker que arranca no cierra operaciones que otro worker sigue
    ejecutando.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=JOB_RETENTION_HOURS)
    ).isoformat(timespec="seconds")
    stale_cutoff = None
    if stale_minutes is not None:
        stale_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(minutes=int(stale_minutes))
        ).isoformat(timespec="seconds")

    with get_connection(get_db_path()) as conn:
        conn.execute("DELETE FROM task_state WHERE updated_at < ?", (cutoff,))
        rows = conn.execute(
            "SELECT task_id, payload, updated_at FROM task_state"
        ).fetchall()

    for row in rows:
        if stale_cutoff is not None and (row["updated_at"] or "") >= stale_cutoff:
            # Actualizada recientemente: puede ser otra instancia viva.
            continue
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


def create_task(owner: str = "") -> str:
    """Crea una tarea en memoria y en SQLite.

    ``owner`` es el usuario que la lanzó. Se usa para que un operador solo
    pueda cancelar sus propias operaciones; un administrador puede cancelar
    cualquiera. Las ejecuciones programadas no tienen dueño humano y quedan
    reservadas a los administradores.
    """
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
        "dry_run": is_dry_run(),
        "owner": owner or "",
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
    }
    with _tasks_lock:
        _tasks[task_id] = task
        _task_events[task_id] = threading.Event()
        # Acota el estado en memoria: las tareas cerradas más antiguas se
        # descartan primero. La instantánea SQLite sigue disponible para el
        # historial reciente.
        if len(_tasks) > MAX_MEMORY_TASKS:
            removable = [
                key
                for key, value in _tasks.items()
                if key != task_id and value.get("completed")
            ]
            for key in removable[: max(0, len(_tasks) - MAX_MEMORY_TASKS)]:
                _tasks.pop(key, None)
                _task_events.pop(key, None)
                with _task_persist_lock:
                    _task_persist_times.pop(key, None)
    _persist_task(task_id, task)
    _trim_task_state_rows()
    return task_id


def update_task(tid: str, updates: Dict[str, Any]) -> None:
    with _tasks_lock:
        task = _tasks.get(tid)
        if task is None:
            return
        task.update(updates)
        task["updated_at"] = _utc_now_iso()
        snapshot = dict(task)
        event = _task_events.get(tid)

    # Los cambios de estado y la finalización se guardan siempre. La consola
    # puede generar decenas de líneas por segundo: se agrupan para no escribir
    # en SQLite en cada una. Un fallo de escritura no interrumpe la operación.
    force_persist = bool(
        updates.get("completed")
        or "status" in updates
        or "cancellation_requested" in updates
    )
    now = time.monotonic()
    should_persist = force_persist
    with _task_persist_lock:
        last = _task_persist_times.get(tid, 0.0)
        if TASK_PERSIST_INTERVAL_SECONDS <= 0 or now - last >= (
            TASK_PERSIST_INTERVAL_SECONDS
        ):
            should_persist = True
        if should_persist:
            _task_persist_times[tid] = now
    if should_persist:
        _persist_task(tid, snapshot)
    if event is not None:
        event.set()


def remove_task(tid: str) -> None:
    with _tasks_lock:
        _tasks.pop(tid, None)
        _task_events.pop(tid, None)
    with _task_persist_lock:
        _task_persist_times.pop(tid, None)
    _delete_persisted_task(tid)


def _claim_operation_id(operation_id: str) -> Optional[str]:
    """Reserva un envío idempotente en SQLite.

    Devuelve ``None`` si la operación es nueva. Si ya existía, devuelve el
    ``task_id`` asociado o ``""`` mientras el primer envío aún se está creando.
    ``INSERT OR IGNORE`` hace la reserva atómica incluso con varios workers.
    """
    if not operation_id:
        return None

    now = datetime.now(timezone.utc)
    cutoff = (
        now - timedelta(seconds=_OPERATION_ID_TTL)
    ).isoformat(timespec="seconds")
    with get_connection(get_db_path()) as conn:
        conn.execute(
            "DELETE FROM operation_ids WHERE created_at < ?", (cutoff,)
        )
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO operation_ids
                (operation_id, created_at, task_id)
            VALUES (?, ?, '')
            """,
            (operation_id, now.isoformat(timespec="seconds")),
        )
        if cursor.rowcount == 1:
            # Acota la tabla sin tocar las reservas recientes.
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM operation_ids"
                ).fetchone()[0]
            )
            if total > _MAX_OPERATION_IDS:
                conn.execute(
                    """
                    DELETE FROM operation_ids WHERE operation_id IN (
                        SELECT operation_id FROM operation_ids
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                    """,
                    (total - _MAX_OPERATION_IDS,),
                )
            return None
        row = conn.execute(
            "SELECT task_id FROM operation_ids WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
    return row["task_id"] if row is not None else ""


def _complete_operation_id(operation_id: str, tid: str) -> None:
    """Asocia el task_id definitivo a un envío ya reservado."""
    if not operation_id:
        return
    with get_connection(get_db_path()) as conn:
        conn.execute(
            "UPDATE operation_ids SET task_id = ? WHERE operation_id = ?",
            (tid, operation_id),
        )


def _release_operation_id(operation_id: str) -> None:
    """Libera una reserva que no llegó a crear ninguna tarea.

    Se usa cuando la aprobación falta o la cola está llena, para que el
    operador pueda reintentar con el mismo ``operation_id`` sin esperar al TTL.
    """
    if not operation_id:
        return
    with get_connection(get_db_path()) as conn:
        conn.execute(
            "DELETE FROM operation_ids WHERE operation_id = ?",
            (operation_id,),
        )


def _task_version(task: Dict[str, Any]) -> tuple:
    """Huella barata para evitar serializar el estado sin cambios.

    Se comparan campos que cambian en cada actualización relevante; así una
    conexión SSE ociosa no reenvía la tarea completa cada segundo.
    """
    console = task.get("console") or []
    log = task.get("log") or []
    return (
        task.get("updated_at"),
        task.get("status"),
        task.get("completed"),
        task.get("progress"),
        task.get("message"),
        len(console),
        len(log),
        console[-1] if console else "",
        log[-1] if log else "",
    )


def generate_progress_events(task_id: str):
    yield sse_retry(1500)
    start = time.time()
    max_wait = 90
    last_version: Optional[tuple] = None

    while True:
        # Si la tarea la ejecuta otro proceso no existe evento local: se
        # refresca desde SQLite para que el SSE funcione con varios workers.
        with _tasks_lock:
            event = _task_events.get(task_id)
        task = _get_task(task_id, refresh=event is None)

        if task is None:
            yield sse_event(
                "completed",
                json.dumps({"status": "not_found", "completed": True}),
            )
            return

        version = _task_version(task)
        if version != last_version:
            yield sse_event(
                "progress", json.dumps(task, ensure_ascii=False)
            )
            last_version = version

        if task.get("status") in TERMINAL_STATES or task.get("completed"):
            yield sse_event(
                "completed", json.dumps(task, ensure_ascii=False)
            )
            return

        if time.time() - start >= max_wait:
            yield sse_event(
                "timeout",
                json.dumps({**task, "timeout": True}, ensure_ascii=False),
            )
            return

        # Espera a que update_task señale el evento; si no hay evento local,
        # sondea SQLite una vez por segundo (coste acotado por conexión).
        if event is not None:
            event.wait(1.0)
            event.clear()
        else:
            time.sleep(1)


# ---------------------------------------------------------------------------
# Métricas de rendimiento (instrumentación ligera, sin dependencias)
# ---------------------------------------------------------------------------
_METRICS_SKIP_ENDPOINTS = {"static", "healthz", "progress_sse"}


@app.before_request
def _metrics_before_request():
    if REQUEST_METRICS_ENABLED:
        g._acc_request_started = time.perf_counter()
    return None


@app.after_request
def _metrics_after_request(response):
    started = getattr(g, "_acc_request_started", None)
    if started is None:
        return response
    endpoint = request.endpoint or "desconocido"
    if endpoint in _METRICS_SKIP_ENDPOINTS:
        return response
    duration_ms = (time.perf_counter() - started) * 1000.0
    request_metrics.record(endpoint, duration_ms, response.status_code)
    if duration_ms >= SLOW_REQUEST_MS:
        logger.warning(
            "Petición lenta: endpoint=%s status=%s duracion_ms=%.1f",
            endpoint,
            response.status_code,
            duration_ms,
        )
    return response


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
        "dry_run": is_dry_run(),
        "is_production": IS_PRODUCTION,
        "max_hosts": MAX_HOSTS,
        "status_labels": STATUS_LABELS,
        "host_result_labels": HOST_RESULT_LABELS,
        "ui_config_panel": UI_CONFIG_PANEL,
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
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self'; "
                "font-src 'self'; "
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
        user = User.get_by_id(int(user_id), db_path=get_db_path())
    except (TypeError, ValueError):
        return None
    # Una cuenta desactivada pierde el acceso de inmediato, aunque conserve la
    # cookie de sesión o la cookie "remember" de Flask-Login.
    if user is None or not user.is_active:
        return None
    return user


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


def _finish_session_login(user: "User", client_ip: str, event: str, detail: str):
    """Completa el flujo común tras validar credenciales (local o LDAPS)."""
    user.register_success(db_path=get_db_path())
    AuthEvent.record(
        user.username,
        client_ip,
        event,
        detail,
        db_path=get_db_path(),
    )
    LIMITER.reset(f"login:{client_ip}")
    # Evita fijación de sesión: se descarta cualquier estado previo antes de
    # emitir la cookie autenticada.
    session.clear()
    rotate_csrf_token()
    session.permanent = True
    login_user(user, remember=bool(request.form.get("remember")))
    next_url = request.args.get("next") or request.form.get("next")
    return redirect(next_url if _is_safe_redirect(next_url) else url_for("index"))


def _register_login_failure(
    user: Optional["User"], username: str, client_ip: str, detail: str = ""
) -> None:
    """Registra un intento fallido (bloqueo, evento y flash)."""
    locked = False
    if user is not None:
        try:
            locked = user.register_failure(
                LOGIN_LOCKOUT_THRESHOLD,
                LOGIN_LOCKOUT_MINUTES,
                db_path=get_db_path(),
            )
        except Exception:  # pragma: no cover - no tumbar el login
            logger.exception("No se pudo registrar el fallo de login")
    AuthEvent.record(
        username,
        client_ip,
        "account_locked" if locked else "login_failure",
        ("Cuenta bloqueada por intentos fallidos" if locked else detail)
        or "Credenciales incorrectas",
        db_path=get_db_path(),
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
    username: str, password: str, user: Optional["User"], client_ip: str
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
            db_path=get_db_path(),
        )
        logger.info(
            "Login LDAP rechazado: %r es una cuenta local", username
        )
        flash("Usuario o contraseña incorrectos.", "danger")
        return

    if user is not None and not user.is_active:
        AuthEvent.record(
            username,
            client_ip,
            "login_disabled",
            "Cuenta LDAP desactivada por el admin",
            db_path=get_db_path(),
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
            db_path=get_db_path(),
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
                db_path=get_db_path(),
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
                db_path=get_db_path(),
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
            db_path=get_db_path(),
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
        user = User.get_by_username(username, db_path=get_db_path())

        if user is not None and user.is_locked:
            AuthEvent.record(
                username,
                client_ip,
                "login_locked",
                "Intento con cuenta bloqueada",
                db_path=get_db_path(),
            )
            logger.warning("Login bloqueado para usuario=%r (cuenta bloqueada)", username)
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

    # La portada es el catálogo: solo tarjetas. Cada tarjeta enlaza a
    # /tarea/<task_id> para la vista de detalle de la tarea.
    # Contacto para pedir tareas nuevas (mailto: abre el cliente de correo
    # del sistema, p. ej. Thunderbird).
    contact_task_mailto = "mailto:{}?subject={}".format(
        TASK_REQUEST_EMAIL,
        quote(TASK_REQUEST_SUBJECT, safe=""),
    )
    hidden_ids = (
        modules_panel.hidden_task_ids(
            current_user.get_id(), db_path=get_db_path()
        )
        if UI_CONFIG_PANEL
        else set()
    )
    task_catalog, parked_catalog = modules_panel.split_catalog(
        TASK_CATALOG, hidden_ids
    )
    return render_template(
        "tasks.html",
        task_catalog=task_catalog,
        parked_count=len(parked_catalog),
        contact_task_mailto=contact_task_mailto,
    )


@app.route("/configuracion")
@login_required
def configuracion():
    """Entrada del grupo Configuración: abre la primera pestaña disponible."""
    if not UI_CONFIG_PANEL:
        abort(404)
    return redirect(url_for("inventario"))


@app.route("/modulos")
@login_required
def modulos():
    """Vista de módulos: tarjetas que el usuario quitó del panel principal."""
    if not UI_CONFIG_PANEL:
        abort(404)
    hidden_ids = modules_panel.hidden_task_ids(
        current_user.get_id(), db_path=get_db_path()
    )
    _, parked_catalog = modules_panel.split_catalog(TASK_CATALOG, hidden_ids)
    return render_template(
        "modules.html",
        hidden_tasks=parked_catalog,
        total_tasks=len(TASK_CATALOG),
    )


@app.route("/modulos/<task_id>/ocultar", methods=["POST"])
@login_required
def modulo_ocultar(task_id: str):
    if not UI_CONFIG_PANEL:
        abort(404)
    task = get_task(task_id)
    if task is None:
        abort(404)
    modules_panel.set_task_hidden(
        current_user.get_id(), task_id, True, db_path=get_db_path()
    )
    flash(f"«{task['name']}» se movió a Módulos.", "info")
    return redirect(url_for("index"))


@app.route("/modulos/<task_id>/mostrar", methods=["POST"])
@login_required
def modulo_mostrar(task_id: str):
    if not UI_CONFIG_PANEL:
        abort(404)
    task = get_task(task_id)
    if task is None:
        abort(404)
    modules_panel.set_task_hidden(
        current_user.get_id(), task_id, False, db_path=get_db_path()
    )
    flash(f"«{task['name']}» vuelve al panel principal.", "success")
    return redirect(url_for("modulos"))


@app.route("/modulos/restaurar", methods=["POST"])
@login_required
def modulos_restaurar():
    if not UI_CONFIG_PANEL:
        abort(404)
    modules_panel.restore_all(current_user.get_id(), db_path=get_db_path())
    flash("Todas las tareas vuelven al panel principal.", "success")
    return redirect(url_for("modulos"))


@app.route("/tarea/<task_id>", methods=["GET"])
@login_required
def task_view(task_id):
    task = get_task(task_id)
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
    for group in HostGroup.all(db_path=get_db_path()):
        data = group.to_dict()
        data["safety"] = _safety_for_group(group)
        inventory_groups.append(data)
    return render_template(
        "index.html",
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
    task = get_task(task_id)
    if task is None:
        abort(404)

    db_path = get_db_path()
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
            report_task_name = (
                f"{task['name']} (última corrida general)"
            )
    if batch is None:
        flash(
            "Esta tarea aún no tiene ejecuciones registradas: lanza una "
            "corrida para poder generar el informe.",
            "info",
        )
        return redirect(url_for("task_view", task_id=task_id))

    host_results = ExecutionBatch.get_host_results(
        batch.batch_id, db_path=db_path
    )
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
    task = get_task(task_id)
    if task is None:
        abort(404)
    db_path = get_db_path()
    batch = ExecutionBatch.get_latest_by_task(task_id, db_path=db_path)
    if batch is None:
        flash(
            "Esta tarea aún no tiene ejecuciones registradas: lanza una "
            "corrida para poder generar el informe de migración.",
            "info",
        )
        return redirect(url_for("task_view", task_id=task_id))
    host_results = ExecutionBatch.get_host_results(
        batch.batch_id, db_path=db_path
    )
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
    batch = ExecutionBatch.get_batch(batch_id, db_path=get_db_path())
    if batch is None:
        abort(404)
    host_results = ExecutionBatch.get_host_results(
        batch_id, db_path=get_db_path()
    )
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


# ---------------------------------------------------------------------------
# Inventario, programaciones y aprobaciones
# ---------------------------------------------------------------------------
INVENTORY_KEYS_DIR = SECRETS_DIR / "inventory_keys"


def _inventario_group_and_hosts():
    groups = [g.to_dict() for g in HostGroup.all(db_path=get_db_path())]
    for group in groups:
        group["hosts"] = [
            h.to_dict() for h in HostGroup.hosts_of(group["id"], db_path=get_db_path())
        ]
    return groups


@app.route("/inventario", methods=["GET", "POST"])
@login_required
def inventario():
    if request.method == "POST":
        if not current_user.has_role("admin"):
            abort(403)
        action = request.form.get("action", "").strip()
        db_path = get_db_path()
        if action == "create_group":
            try:
                HostGroup.create(
                    request.form.get("name", ""),
                    description=request.form.get("description", ""),
                    os_flavor=request.form.get("os_flavor", "linux"),
                    db_path=db_path,
                    criticality=request.form.get("criticality", "medium"),
                    window_days=request.form.get("window_days", ""),
                    window_start=request.form.get("window_start", ""),
                    window_end=request.form.get("window_end", ""),
                    window_note=request.form.get("window_note", ""),
                )
                flash("Grupo creado.", "success")
            except ValueError as exc:
                flash(str(exc), "danger")
        elif action == "update_group_safety":
            gid = request.form.get("group_id", "").strip()
            group = HostGroup.get(gid, db_path=db_path) if gid.isdigit() else None
            if group is None:
                flash("El grupo no existe.", "warning")
            else:
                try:
                    HostGroup.set_safety(
                        group.id,
                        request.form.get("criticality", "medium"),
                        window_days=request.form.get("window_days", ""),
                        window_start=request.form.get("window_start", ""),
                        window_end=request.form.get("window_end", ""),
                        window_note=request.form.get("window_note", ""),
                        db_path=db_path,
                    )
                    flash(
                        f"Política de seguridad de '{group.name}' actualizada.",
                        "success",
                    )
                except ValueError as exc:
                    flash(str(exc), "danger")
        elif action == "delete_group":
            group = HostGroup.get(
                request.form.get("group_id", "0"), db_path=db_path
            ) if request.form.get("group_id", "").isdigit() else None
            if group is None:
                flash("El grupo no existe.", "warning")
            else:
                group.delete(db_path=db_path)
                flash(f"Grupo '{group.name}' eliminado.", "success")
        elif action == "add_host":
            group = HostGroup.get(
                request.form.get("group_id", "0"), db_path=db_path
            ) if request.form.get("group_id", "").isdigit() else None
            if group is None:
                flash("El grupo no existe.", "warning")
            else:
                host, host_error = InventoryHost.add(
                    group.id,
                    request.form.get("address", ""),
                    label=request.form.get("label", ""),
                    db_path=db_path,
                    criticality=request.form.get("criticality", ""),
                )
                if host_error:
                    flash(host_error, "danger")
                else:
                    flash("Host añadido al grupo.", "success")
        elif action == "update_host_criticality":
            host_id = request.form.get("host_id", "").strip()
            if not host_id.isdigit():
                flash("Host no válido.", "warning")
            else:
                try:
                    InventoryHost.set_criticality(
                        int(host_id),
                        request.form.get("criticality", ""),
                        db_path=db_path,
                    )
                    flash("Criticidad del host actualizada.", "success")
                except ValueError as exc:
                    flash(str(exc), "danger")
        elif action == "delete_host":
            host_id = request.form.get("host_id", "").strip()
            if not host_id.isdigit():
                flash("Host no válido.", "warning")
            else:
                with get_connection(db_path) as conn:
                    row = conn.execute(
                        "SELECT * FROM inventory_hosts WHERE id = ?",
                        (int(host_id),),
                    ).fetchone()
                if row is None:
                    flash("El host no existe.", "warning")
                else:
                    InventoryHost.from_row(row).delete(db_path=db_path)
                    flash("Host eliminado del grupo.", "success")
        elif action == "upload_key":
            gid = request.form.get("group_id", "").strip()
            group = HostGroup.get(gid, db_path=db_path) if gid.isdigit() else None
            uploaded = request.files.get("ssh_key")
            if group is None:
                flash("El grupo no existe.", "warning")
            elif group.os_flavor == "windows":
                flash(
                    "Los grupos Windows se autentican por usuario/contraseña "
                    "winrm, no admiten clave SSH.",
                    "warning",
                )
            elif uploaded is None or not uploaded.filename:
                flash("Selecciona el fichero de clave privada (.pem).", "warning")
            else:
                raw = uploaded.read(MAX_CONTENT_LENGTH + 1)
                if len(raw) > MAX_CONTENT_LENGTH:
                    flash("La clave es demasiado grande.", "danger")
                elif b"OPENSSH PRIVATE KEY" not in raw and b"PRIVATE KEY" not in raw:
                    flash(
                        "El fichero no parece una clave privada OpenSSH/PEM.",
                        "danger",
                    )
                else:
                    try:
                        INVENTORY_KEYS_DIR.mkdir(parents=True, exist_ok=True)
                        os.chmod(INVENTORY_KEYS_DIR, 0o700)
                    except OSError:  # pragma: no cover - permisos locales
                        INVENTORY_KEYS_DIR.mkdir(parents=True, exist_ok=True)
                    slug = "".join(
                        ch if ch.isalnum() or ch in "-_" else "_"
                        for ch in group.name.lower()
                    )[:40] or f"group-{group.id}"
                    key_path = INVENTORY_KEYS_DIR / f"{slug}.pem"
                    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as key_file:
                        key_file.write(raw)
                    HostGroup.set_ssh_key(
                        group.id, str(key_path), db_path=db_path
                    )
                    flash(
                        "Clave SSH guardada y asociada al grupo (0600).",
                        "success",
                    )
        else:
            flash("Acción de inventario no válida.", "warning")
        return redirect(url_for("inventario"))

    return render_template(
        "inventory.html",
        groups=_inventario_group_and_hosts(),
        criticality_levels=CRITICALITY_LEVELS,
        criticality_labels=CRITICALITY_LABELS,
    )


@app.route("/programaciones", methods=["GET", "POST"])
@login_required
def programaciones():
    db_path = get_db_path()
    tasks = {
        task["id"]: task
        for task in TASK_CATALOG
        if task.get("status") == "active"
        and task.get("schedulable", True)
    }
    if request.method == "POST":
        if not current_user.has_role("admin"):
            abort(403)
        # ``op`` identifica la operación del formulario (crear/activar/
        # eliminar); ``action`` es la acción operativa de la programación
        # (TZ/TIME). Antes ambos usaban ``action`` y el formulario de alta
        # enviaba dos valores: la programación quedaba guardada con
        # ``action="create"`` y fallaba al dispararse.
        action = request.form.get("op", "").strip()
        if action == "toggle":
            schedule = Schedule.get(
                request.form.get("schedule_id", "0"), db_path=db_path
            ) if request.form.get("schedule_id", "").isdigit() else None
            if schedule is None:
                flash("La programación no existe.", "warning")
            else:
                Schedule.set_enabled(
                    schedule.id, not schedule.enabled, db_path=db_path
                )
                flash(
                    f"Programación '{schedule.name}' "
                    f"{'activada' if not schedule.enabled else 'desactivada'}.",
                    "success",
                )
        elif action == "delete":
            schedule = Schedule.get(
                request.form.get("schedule_id", "0"), db_path=db_path
            ) if request.form.get("schedule_id", "").isdigit() else None
            if schedule is None:
                flash("La programación no existe.", "warning")
            else:
                Schedule.delete(schedule.id, db_path=db_path)
                flash(f"Programación '{schedule.name}' eliminada.", "success")
        elif action == "create":
            name = request.form.get("name", "").strip()
            task_id = request.form.get("task_id", "").strip()
            run_at_raw = request.form.get("run_at", "").strip()
            recurrence = request.form.get("recurrence", "once").strip().lower()
            if task_id not in tasks:
                flash("Selecciona una tarea activa del catálogo.", "danger")
                return redirect(url_for("programaciones"))
            try:
                run_at = datetime.strptime(run_at_raw, "%Y-%m-%dT%H:%M").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                flash(
                    "Fecha/hora inválida; usa el selector o el formato "
                    "AAAA-MM-DDTHH:MM (hora UTC).",
                    "danger",
                )
                return redirect(url_for("programaciones"))
            master_password = request.form.get("master_password", "")
            if not master_password:
                flash(
                    "La contraseña maestra del vault es obligatoria para "
                    "operar desatendidamente.",
                    "danger",
                )
                return redirect(url_for("programaciones"))
            target_mode = (
                request.form.get("target_mode", "manual").strip().lower()
                or "manual"
            )
            target_group_id = None
            schedule_group = None
            pinned_ip = None
            if target_mode == "group":
                gid = request.form.get("target_group", "").strip()
                if gid.isdigit():
                    schedule_group = HostGroup.get(gid, db_path=db_path)
                if schedule_group is None:
                    flash("Selecciona un grupo del inventario válido.", "danger")
                    return redirect(url_for("programaciones"))
                target_group_id = int(gid)
            else:
                pinned_ip, ip_error = pin_target(
                    request.form.get("target_ip", "").strip()
                )
                if ip_error or not pinned_ip:
                    flash(ip_error or "IP objetivo no válida.", "danger")
                    return redirect(url_for("programaciones"))
            # Mismas reglas que la ejecución manual: una programación no
            # puede saltarse la política de hora/NTP, usar una acción que la
            # tarea no admite ni apuntar a un grupo con otra plataforma.
            validated, schedule_errors = _validate_scheduled_params(
                tasks[task_id],
                request.form.get("admin_user", ""),
                request.form.get("action", "TZ"),
                request.form.get("value", ""),
                request.form.get("ntp_policy", "keep"),
                request.form.get("ntp_servers", ""),
                platform=schedule_group.os_flavor if schedule_group else "",
                group_name=schedule_group.name if schedule_group else "",
                min_free_gb_raw=request.form.get("min_free_gb", ""),
            )
            if schedule_errors or validated is None:
                for error in schedule_errors:
                    flash(error, "danger")
                return redirect(url_for("programaciones"))
            # Las programaciones no admiten excepción: si el horario elegido
            # cae fuera de la ventana del grupo, se rechaza en vez de ejecutar
            # hosts críticos sin supervisión.
            if schedule_group is not None:
                schedule_safety = _safety_for_group(
                    schedule_group, now=run_at
                )
            else:
                schedule_safety = _safety_for_target(
                    None, pinned_ip, now=run_at
                )
            if schedule_safety["blocked"]:
                flash(
                    "No se puede programar fuera de la ventana de "
                    "mantenimiento: " + schedule_safety["reasons"][0],
                    "danger",
                )
                return redirect(url_for("programaciones"))
            for warning in schedule_safety["warnings"]:
                flash(warning, "warning")
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
                    "task_name": tasks[task_id]["name"],
                    "admin_user": validated["admin_user"],
                    "origin": "stored",
                    "action": validated["action"],
                    "value": validated["value"],
                    "time_mode": "utc",
                    "time_zone": "",
                    "time_fold": None,
                    "ntp_policy": validated["ntp_policy"],
                    "ntp_servers": validated["ntp_servers"],
                    "sync_rtc": _form_bool("sync_rtc"),
                    "force": _form_bool("force"),
                    "min_free_gb": validated["min_free_gb"],
                }
            )
            try:
                Schedule.create(
                    name,
                    task_id,
                    run_at,
                    {
                        **schedule_params,
                        "master_password_enc": scheduler.encrypt_secret(
                            master_password
                        ),
                    },
                    recurrence=recurrence,
                    created_by=getattr(current_user, "username", "") or "",
                    db_path=db_path,
                )
                flash(
                    f"Programación '{name}' creada. Se ejecutará sola (UTC) "
                    "mientras esté activa.",
                    "success",
                )
            except ValueError as exc:
                flash(str(exc), "danger")
        else:
            flash("Acción de programación no válida.", "warning")
        return redirect(url_for("programaciones"))

    schedules = [s.to_dict() for s in Schedule.all(db_path=db_path)]
    groups = [g.to_dict() for g in HostGroup.all(db_path=db_path)]
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
        item["group_criticality"] = (
            group.get("criticality_label", "") if group else ""
        )
        item["group_window"] = group.get("window_label", "") if group else ""
    return render_template(
        "schedules.html",
        schedules=schedules,
        tasks=[
            {"id": key, "name": value["name"]} for key, value in tasks.items()
        ],
        inventory_groups=groups,
        recurrence_labels={
            "once": "Una vez",
            "daily": "Diaria",
            "weekly": "Semanal",
        },
        default_min_free_gb=APT_MIN_FREE_GB,
        min_free_gb_floor=APT_MIN_FREE_GB_FLOOR,
        min_free_gb_ceiling=APT_MIN_FREE_GB_CEILING,
    )


@app.route("/aprobaciones", methods=["GET", "POST"])
@roles_required("admin")
def aprobaciones():
    db_path = get_db_path()
    if request.method == "POST":
        action = request.form.get("action", "").strip()
        request_id = request.form.get("request_id", "").strip()
        if not request_id.isdigit():
            flash("Solicitud no válida.", "warning")
            return redirect(url_for("aprobaciones"))
        req = ApprovalRequest.get(int(request_id), db_path=db_path)
        if action == "approve":
            decided, decide_error = ApprovalRequest.decide(
                int(request_id),
                approver=getattr(current_user, "username", ""),
                approve=True,
                note=request.form.get("note", ""),
                db_path=db_path,
            )
            if decide_error or decided is None:
                flash(decide_error or "No se pudo aprobar.", "danger")
            else:
                flash(
                    f"Solicitud #{decided.id} aprobada: el ejecutor dispone "
                    f"de {APPROVAL_TTL_MINUTES} minutos para lanzar la tarea.",
                    "success",
                )
        elif action == "reject":
            decided, decide_error = ApprovalRequest.decide(
                int(request_id),
                approver=getattr(current_user, "username", ""),
                approve=False,
                note=request.form.get("note", ""),
                db_path=db_path,
            )
            if decide_error or decided is None:
                flash(decide_error or "No se pudo rechazar.", "danger")
            else:
                flash(f"Solicitud #{decided.id} rechazada.", "info")
        else:
            flash("Acción de aprobación no válida.", "warning")
        return redirect(url_for("aprobaciones"))

    def _pretty(req: ApprovalRequest) -> Dict[str, Any]:
        data = req.to_dict()
        stored = req.params
        data["detail"] = ", ".join(
            f"{key}={str(value)[:60]}"
            for key, value in stored.items()
            if key
            in {
                "target",
                "group_name",
                "admin_user",
                "action",
                "value",
                "mode",
            }
        )
        return data

    return render_template(
        "approvals.html",
        approval_ttl_minutes=APPROVAL_TTL_MINUTES,
        pending=[
            _pretty(req)
            for req in ApprovalRequest.list_status(
                APPROVAL_PENDING, db_path=db_path
            )
        ],
        decided=[
            _pretty(req)
            for req in ApprovalRequest.list_status("approved", db_path=db_path)
            + ApprovalRequest.list_status("rejected", db_path=db_path)
        ],
    )


def launch_scheduled(schedule, params: Dict[str, Any]) -> bool:
    """Dispara una programación vencida purando la cola de trabajos normal.

    ``params`` lleva los parámetros guardados (sin secretos en claro) y la
    contraseña maestra cifrada. Registra el disparo y calcula la siguiente
    ejecución según la recurrencia (una vez se desactiva tras el disparo).
    """
    db_path = get_db_path()
    master_password = scheduler.decrypt_secret(
        params.get("master_password_enc", "")
    )
    if not master_password:
        Schedule.mark_run(
            schedule["id"],
            error="Contraseña maestra no descifrable; la clave de cifrado "
            "cambió o falta.",
            disable=True,
            db_path=db_path,
        )
        return False

    # Defensa en profundidad: una fila antigua o editada a mano no debe
    # saltarse las reglas que la ejecución manual aplica. La plataforma se
    # resuelve con el grupo actual: si cambió, el disparo se rechaza.
    target_group = None
    if params.get("target_mode") == "group" and params.get(
        "target_group_id"
    ) is not None:
        target_group = HostGroup.get(
            params["target_group_id"], db_path=db_path
        )
    validated, errors = _validate_scheduled_params(
        get_task(schedule["task_id"]),
        params.get("admin_user", ""),
        params.get("action", "TZ"),
        params.get("value", ""),
        params.get("ntp_policy", "keep"),
        ",".join(str(server) for server in (params.get("ntp_servers") or [])),
        platform=target_group.os_flavor if target_group else "",
        group_name=target_group.name if target_group else "",
        min_free_gb_raw=str(params.get("min_free_gb", "")),
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
                delta = datetime.fromisoformat(next_open) - datetime.now(
                    timezone.utc
                )
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

    tid = create_task()
    update_task(
        tid,
        {
            "status": "running",
            "progress": 0,
            "message": "Lanzada por programación…",
            "mode": "execute",
            "completed": False,
        },
    )
    try:
        get_job_queue().submit(
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
        )
    except JobQueueFull as exc:
        remove_task(tid)
        # Fallo transitorio de capacidad: no se desactiva la programación; se
        # pospone el siguiente intento para no martillear la cola.
        Schedule.mark_run(
            schedule["id"],
            error=f"Cola llena: {str(exc)[:120]}",
            retry_delay_seconds=scheduler.SCHEDULE_RETRY_DELAY_SECONDS,
            db_path=db_path,
        )
        return False

    schedule_row = Schedule.get(schedule["id"], db_path=db_path)
    next_run = (
        schedule_row.next_run_from() if schedule_row else None
    )
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


def _safety_for_group(group, now=None, hosts=None):
    """Evalúa la política de un grupo con su lista actual de hosts."""
    db_path = get_db_path()
    if hosts is None:
        hosts = [
            {"address": host.address, "criticality": host.criticality}
            for host in HostGroup.hosts_of(group.id, db_path=db_path)
        ]
    return evaluate_safety(
        group_name=group.name,
        group_criticality=group.criticality,
        hosts=hosts,
        window_days=group.window_days,
        window_start=group.window_start,
        window_end=group.window_end,
        window_note=group.window_note,
        now=now,
    )


def _safety_for_target(target_group_id, target, now=None):
    """Evalúa un objetivo por grupo o por IP manual clasificada."""
    db_path = get_db_path()
    if target_group_id is not None:
        group = HostGroup.get(target_group_id, db_path=db_path)
        if group is not None:
            return _safety_for_group(group, now=now)

    text = str(target or "").strip()
    if not text.startswith("inventory:"):
        addresses = [part.strip() for part in text.split(",") if part.strip()]
        if len(addresses) == 1:
            try:
                import ipaddress as _ip

                _ip.ip_address(addresses[0])
            except ValueError:
                pass
            else:
                host, group = InventoryHost.find_by_address(
                    addresses[0], db_path=db_path
                )
                if host is not None and group is not None:
                    return _safety_for_group(
                        group,
                        now=now,
                        hosts=[
                            {
                                "address": host.address,
                                "criticality": host.criticality,
                            }
                        ],
                    )

    result = evaluate_safety(now=now)
    result["warnings"].append(
        "El objetivo manual no está clasificado en el inventario; no se "
        "aplica criticidad ni ventana de mantenimiento."
    )
    result["level"] = "warn"
    result["summary"] = result["warnings"][0]
    return result


def _safety_gate(form: Dict[str, Any], operator: str, mode: str):
    """Aplica criticidad y ventana antes de encolar una ejecución real.

    La vista previa nunca se bloquea (no modifica nada). Una ejecución real
    sobre hosts ``critical`` fuera de ventana solo continúa con una excepción
    explícita de un administrador, con motivo y auditoría. Devuelve una
    respuesta Flask si hay que detener el envío; ``None`` si puede seguir.
    """
    safety = _safety_for_target(form.get("target_group_id"), form["target"])
    override_requested = _form_bool("safety_override")
    reason = request.form.get("safety_reason", "").strip()

    if safety["blocked"] and mode == "execute":
        if not override_requested:
            AuthEvent.record(
                operator,
                _client_ip(),
                "safety_blocked",
                f"{form['task_id']} sobre {form['target']}: "
                + " | ".join(safety["reasons"]),
                db_path=get_db_path(),
            )
            for text in safety["reasons"]:
                flash(text, "danger")
            flash(
                "La operación no se envió. Espera a la ventana de "
                "mantenimiento o solicita una excepción a un administrador.",
                "warning",
            )
            if _wants_json():
                return (
                    jsonify(
                        {
                            "status": "blocked",
                            "errors": safety["reasons"],
                            "next_open": safety.get("next_open", ""),
                        }
                    ),
                    403,
                )
            return redirect(url_for("task_view", task_id=form["task_id"]))
        if not current_user.has_role("admin"):
            flash(
                "Solo un administrador puede autorizar una excepción de "
                "ventana de mantenimiento.",
                "danger",
            )
            if _wants_json():
                return (
                    jsonify(
                        {
                            "status": "blocked",
                            "errors": [
                                "Se requiere rol admin para la excepción."
                            ],
                        }
                    ),
                    403,
                )
            return redirect(url_for("task_view", task_id=form["task_id"]))
        if len(reason) < 10:
            flash(
                "La excepción requiere un motivo de al menos 10 caracteres.",
                "danger",
            )
            if _wants_json():
                return (
                    jsonify(
                        {
                            "status": "blocked",
                            "errors": ["Motivo de excepción demasiado corto."],
                        }
                    ),
                    403,
                )
            return redirect(url_for("task_view", task_id=form["task_id"]))
        AuthEvent.record(
            operator,
            _client_ip(),
            "safety_override",
            f"{form['task_id']} sobre {form['target']}: {reason} | "
            + " | ".join(safety["reasons"]),
            db_path=get_db_path(),
        )
        logger.warning(
            "Excepción de ventana autorizada por %s sobre %s: %s",
            operator,
            form["target"],
            reason,
        )
        flash(
            "Excepción de ventana autorizada y auditada. La operación "
            "continuará fuera de la ventana de mantenimiento.",
            "warning",
        )
        return None

    if safety["blocked"] and mode != "execute":
        for text in safety["reasons"]:
            flash(f"Vista previa permitida: {text}", "warning")
    for text in safety["warnings"]:
        flash(text, "warning")
    return None


def _approval_params(form: Dict[str, Any]) -> Dict[str, Any]:
    """Parámetros estables de una ejecución, sin secretos, para aprobaciones.

    La nueva contraseña de la tarjeta ``PASSWORD`` tampoco se guarda: la
    aprobación solo identifica el usuario y el resto de parámetros.
    """
    return {
        key: value
        for key, value in form.items()
        if key
        not in {
            "master_password",
            "temporal_passwords",
            "new_password",
            "new_password_confirm",
        }
    }


def handle_execution(default_mode: str = "execute"):
    # Incluso un envío desde el panel exige rol de operador: el rol viewer es
    # de solo lectura y nunca debe poder lanzar Ansible.
    if not current_user.has_role("admin", "operator"):
        abort(403)
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

    if form is None:  # ``errors`` vacío implica formulario válido.
        logger.error("El formulario se validó sin errores pero sin resultado")
        abort(500)
    operator = getattr(current_user, "username", "") or str(current_user.get_id())

    # La imagen del tapiz se guardó al validar el formulario. A partir de aquí
    # el lote es su dueño; si el envío no llega a encolarse, se borra para no
    # dejar archivos huérfanos en el volumen de datos.
    wallpaper_path = form.get("wallpaper_path", "")

    def discard_wallpaper() -> None:
        if wallpaper_path:
            remove_wallpaper(wallpaper_path)

    # Gate por tarea: rol mínimo y doble aprobación para tareas destructivas.
    task = get_task(form["task_id"])
    if task is not None and task.get("min_role") == "admin":
        if not current_user.has_role("admin"):
            discard_wallpaper()
            abort(403)
    # Criticidad y ventana de mantenimiento: se aplican antes de consumir la
    # idempotencia o una aprobación de un solo uso.
    blocked_response = _safety_gate(form, operator, form["mode"])
    if blocked_response is not None:
        discard_wallpaper()
        return blocked_response
    # La idempotencia se comprueba antes de consumir una aprobación: un doble
    # clic o un reintento no debe gastar una autorización de un solo uso.
    operation_id = request.form.get("operation_id", "").strip()
    duplicate = _claim_operation_id(operation_id)
    if duplicate is not None:
        discard_wallpaper()
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

    approval_request_id: Optional[int] = None
    if task is not None and task.get("destructive") and form["mode"] == "execute":
        # Reserva sin consumir: si el encolado falla (cola llena, excepción),
        # la aprobación vuelve a ``approved`` y no se pierde.
        approval_request_id, approval_error = ApprovalRequest.reserve_if_approved(
            form["task_id"],
            _approval_params(form),
            executor=operator,
            approves_by_differs=True,
            ttl_minutes=APPROVAL_TTL_MINUTES,
            db_path=get_db_path(),
        )
        if approval_request_id is None:
            _release_operation_id(operation_id)
            discard_wallpaper()
            message = approval_error or "Se requiere doble aprobación para esta tarea."
            if "pendiente" not in message and "ha expirado" not in message and "ya se usó" not in message:
                approval_params = _approval_params(form)
                pending = [
                    req
                    for req in ApprovalRequest.list_status(
                        APPROVAL_PENDING, db_path=get_db_path()
                    )
                    if req.task_id == form["task_id"]
                    and req.params_hash == params_hash(approval_params)
                    and req.requested_by == operator
                ]
                if not pending:
                    ApprovalRequest.create(
                        form["task_id"],
                        approval_params,
                        requested_by=operator,
                        db_path=get_db_path(),
                    )
                    message = (
                        "Tarea destructiva: se registró una solicitud de "
                        "aprobación. Un administrador distinto de ti debe "
                        "aprobarla antes de ejecutar (tienes "
                        f"{APPROVAL_TTL_MINUTES} minutos una vez aprobada)."
                    )
            flash(message, "warning")
            if _wants_json():
                return (
                    jsonify({"status": "approval_required", "message": message}),
                    403,
                )
            return redirect(url_for("task_view", task_id=form["task_id"]))

    tid = create_task(owner=operator)
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
    try:
        get_job_queue().submit(
            tid,
            execute_with_progress,
            tid,
            form["target"],
            form["admin_user"],
            form["origin"],
            form["master_password"],
            form["action"],
            form["value"],
            form["temporal_passwords"],
            mode=form["mode"],
            time_mode=form["time_mode"],
            time_zone=form["time_zone"],
            time_fold=form["time_fold"],
            target_epoch=form["target_epoch"],
            ntp_policy=form["ntp_policy"],
            ntp_servers=form["ntp_servers"],
            sync_rtc=form["sync_rtc"],
            force=form["force"],
            operator=operator,
            task_id=form["task_id"],
            target_hosts=form.get("target_hosts") or None,
            wallpaper_path=form.get("wallpaper_path", ""),
            wallpaper_uri=form.get("wallpaper_uri", ""),
            wallpaper_name=form.get("wallpaper_name", ""),
            min_free_gb=form.get("min_free_gb"),
            new_password=form.get("new_password", ""),
        )
    except JobQueueFull as exc:
        remove_task(tid)
        _release_operation_id(operation_id)
        discard_wallpaper()
        if approval_request_id is not None:
            ApprovalRequest.release_reserved(
                approval_request_id, db_path=get_db_path()
            )
        message = (
            "El servicio está ocupado con demasiadas operaciones. "
            "Espera a que termine alguna e inténtalo de nuevo."
        )
        logger.warning("Cola de trabajos llena al enviar %s: %s", tid, exc)
        if _wants_json():
            return jsonify({"status": "error", "message": message}), 503
        flash(message, "warning")
        return redirect(url_for("index"))
    except Exception:
        # No dejar la operación a medias ni una aprobación reservada.
        remove_task(tid)
        _release_operation_id(operation_id)
        discard_wallpaper()
        if approval_request_id is not None:
            ApprovalRequest.release_reserved(
                approval_request_id, db_path=get_db_path()
            )
        raise
    else:
        if approval_request_id is not None:
            ApprovalRequest.confirm_reserved(
                approval_request_id, db_path=get_db_path()
            )

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
_STATE_B64_RE = re.compile(r"(?:HORA|TAPIZ|APT)_[A-Z_]+_B64=")
_STATE_JSON_STATE_RE = re.compile(r"(?:HORA|TAPIZ|APT)_(?:STATE|BEFORE)=\{")


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
    if _STATE_B64_RE.search(text) or _STATE_JSON_STATE_RE.search(text):
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
    target_epoch: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
    private_key: str = "",
    connection: str = "ssh",
    wallpaper_uri: str = "",
    min_free_gb: Optional[float] = None,
) -> None:
    """Inspecciona todos los hosts sin cambiar nada y registra el lote.

    Devuelve ``completed``, ``incomplete`` (presupuesto agotado) o
    ``cancelled`` (cancelación solicitada por el usuario).
    """
    total = len(hosts)
    console_lines: List[str] = []
    outcome = "completed"
    for index, host in enumerate(hosts, start=1):
        if cancel_event is not None and cancel_event.is_set():
            outcome = "cancelled"
            _skip_remaining_hosts(
                tid=tid,
                batch_id=batch_id,
                hosts=hosts[index - 1 :],
                db_path=db_path,
                log_lines=log_lines,
                console_lines=console_lines,
                reason="Cancelado por el usuario.",
            )
            break
        if time.monotonic() >= deadline:
            outcome = "incomplete"
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
            if cancel_event is not None and cancel_event.is_set():
                last_error = "Cancelado por el usuario."
                break
            if attempts >= MAX_PASSWORD_ATTEMPTS:
                _append_log(
                    log_lines,
                    f"Límite de {MAX_PASSWORD_ATTEMPTS} intento(s) alcanzado "
                    f"en {host} (vista previa)",
                )
                break
            attempts += 1
            try:
                if action == "WALLPAPER":
                    result = inspect_wallpaper(
                        host=host,
                        user=admin_user,
                        password=candidate,
                        line_callback=line_callback,
                        cancel_event=cancel_event,
                        private_key=private_key or None,
                    )
                elif action == "APT":
                    result = inspect_apt(
                        host=host,
                        user=admin_user,
                        password=candidate,
                        min_free_gb=min_free_gb,
                        line_callback=line_callback,
                        cancel_event=cancel_event,
                        private_key=private_key or None,
                    )
                elif action == "PASSWORD":
                    result = inspect_password(
                        host=host,
                        user=admin_user,
                        password=candidate,
                        target_user=value or OS_PASSWORD_DEFAULT_USER,
                        line_callback=line_callback,
                        cancel_event=cancel_event,
                        private_key=private_key or None,
                    )
                elif action == "FIREWALL":
                    result = inspect_firewall(
                        host=host,
                        user=admin_user,
                        password=candidate,
                        line_callback=line_callback,
                        cancel_event=cancel_event,
                        private_key=private_key or None,
                    )
                else:
                    result = inspect_host(
                        host=host,
                        user=admin_user,
                        password=candidate,
                        line_callback=line_callback,
                        force_real=True,
                        cancel_event=cancel_event,
                        private_key=private_key or None,
                        connection=connection or "ssh",
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
            attention = bool(
                isinstance(state, dict) and state.get("attention")
            )
            attention_kind = (
                str(state.get("attention_kind") or "")
                if isinstance(state, dict)
                else ""
            )
            attention_reason = (
                str(state.get("attention_reason") or "")
                if isinstance(state, dict)
                else ""
            )
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
                    target_epoch=target_epoch,
                    wallpaper_uri=wallpaper_uri,
                ),
                "action": action,
                "value": value,
                "time_mode": time_mode,
                "ntp_policy": ntp_policy,
                "attention": attention,
                "attention_kind": attention_kind,
                "attention_reason": attention_reason,
                "min_free_gb": min_free_gb if action == "APT" else None,
                "target_user": value if action == "PASSWORD" else None,
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
            if attention:
                label = (
                    "migración de distribución"
                    if attention_kind == "migration"
                    else "espacio insuficiente"
                )
                _append_log(
                    log_lines,
                    f"Atención en {host}: {label}. {attention_reason}",
                )
                _append_console(
                    console_lines,
                    tid,
                    f"Atención ({label}): {attention_reason[:180]}",
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
    return outcome


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
    target_epoch: Optional[int] = None,
    local_fold: Optional[int] = None,
    time_zone: str = "",
    ntp_settle: int = 0,
    requiere_ntp_off: bool = True,
    cancel_event: Optional[threading.Event] = None,
    private_key: str = "",
    connection: str = "ssh",
    wallpaper_path: str = "",
    wallpaper_uri: str = "",
    min_free_gb: Optional[float] = None,
    new_password: str = "",
) -> None:
    """Aplica el cambio en todos los hosts (best-effort) y registra cada uno.

    Si una contraseña autentica pero el playbook falla, se prueban las
    siguientes candidatas hasta ``ACC_MAX_PASSWORD_ATTEMPTS``. Un fallo
    de un host nunca detiene el lote. Al agotarse el presupuesto de tiempo del
    lote, los hosts pendientes quedan como ``skipped``.

    Devuelve ``completed``, ``incomplete`` (presupuesto agotado) o
    ``cancelled`` (cancelación solicitada por el usuario).
    """
    total = len(hosts)
    console_lines: List[str] = []
    outcome = "completed"
    for index, host in enumerate(hosts, start=1):
        if cancel_event is not None and cancel_event.is_set():
            outcome = "cancelled"
            _skip_remaining_hosts(
                tid=tid,
                batch_id=batch_id,
                hosts=hosts[index - 1 :],
                db_path=db_path,
                log_lines=log_lines,
                console_lines=console_lines,
                reason="Cancelado por el usuario.",
            )
            break
        if time.monotonic() >= deadline:
            outcome = "incomplete"
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
        attention = False
        attention_kind = ""
        attention_reason = ""
        _append_console(
            console_lines, tid, f"=== {host} ({index}/{total}) ==="
        )
        console_secrets = list(candidates)
        if new_password:
            console_secrets.append(new_password)
        line_callback = _make_console_callback(
            tid, host, console_lines, console_secrets
        )

        attempts = 0
        for candidate in candidates:
            if cancel_event is not None and cancel_event.is_set():
                last_apply_error = "Cancelado por el usuario."
                break
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
                    host=host,
                    user=admin_user,
                    password=candidate,
                    cancel_event=cancel_event,
                    private_key=private_key or None,
                    connection=connection or "ssh",
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
                    target_epoch=target_epoch,
                    local_fold=local_fold,
                    time_zone=time_zone,
                    ntp_settle=ntp_settle,
                    requiere_ntp_off=requiere_ntp_off,
                    line_callback=line_callback,
                    cancel_event=cancel_event,
                    private_key=private_key or None,
                    connection=connection or "ssh",
                    backup_dir=str(TIMEZONE_BACKUP_DIR / str(batch_id)),
                    wallpaper_path=wallpaper_path,
                    wallpaper_uri=wallpaper_uri,
                    min_free_gb=min_free_gb,
                    new_password=new_password,
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
            if result.get("attention"):
                attention = True
                attention_kind = str(result.get("attention_kind") or "")
                attention_reason = str(result.get("attention_reason") or "")

            if result.get("success"):
                success = True
                used_hash = short_hash
                last_apply_error = ""
                if attention:
                    label = (
                        "migración de distribución"
                        if attention_kind == "migration"
                        else "espacio insuficiente"
                    )
                    _append_log(
                        log_lines,
                        f"Requiere atención en {host} ({label}): "
                        f"{attention_reason}",
                    )
                    _append_console(
                        console_lines,
                        tid,
                        f"REQUIERE ATENCIÓN ({label}): "
                        f"{attention_reason[:180]}",
                        host=host,
                    )
                elif result.get("dry_run") or result.get("simulated"):
                    _append_log(
                        log_lines,
                        f"Simulación OK en {host} (hash {short_hash}): "
                        "no se aplicó ningún cambio",
                    )
                    _append_console(
                        console_lines,
                        tid,
                        "SIMULACIÓN: credencial válida; no se aplicó ningún cambio",
                        host=host,
                    )
                else:
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
                    if action == "APT" and isinstance(after_state, dict):
                        pending = after_state.get("pending")
                        if pending:
                            _append_console(
                                console_lines,
                                tid,
                                f"Aviso: quedan {pending} paquete(s) "
                                "pendientes en la estación",
                                host=host,
                            )
                        if after_state.get("reboot_required"):
                            _append_console(
                                console_lines,
                                tid,
                                "Aviso: la estación requiere reinicio",
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
        if attention and success:
            error = attention_reason or (
                "La estación requiere migración o espacio antes de actualizar."
            )
        elif not success:
            error = (
                last_apply_error
                or last_auth_error
                or "No se pudo autenticar en el host."
            )
        result_type = (
            "attention"
            if (attention and success)
            else ("success" if success else "failure")
        )

        ExecutionBatch.add_host_result(
            batch_id=batch_id,
            host=host,
            result=result_type,
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
                result=result_type,
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
        status_word = (
            "ATENCIÓN"
            if result_type == "attention"
            else ("OK" if success else "FALLO")
        )
        _append_log(
            log_lines,
            f"{status_word} {host} ({duration_ms} ms)"
            + (f": {str(error)[:160]}" if error else ""),
        )
        _append_console(
            console_lines,
            tid,
            f"{status_word} ({duration_ms} ms)"
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
    return outcome


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
    time_zone: str = "",
    time_fold: Optional[int] = None,
    target_epoch: Optional[int] = None,
    ntp_policy: str = "keep",
    ntp_servers: Optional[List[str]] = None,
    sync_rtc: bool = False,
    force: bool = False,
    operator: str = "",
    task_id: str = "",
    cancel_event: Optional[threading.Event] = None,
    target_hosts: Optional[List[str]] = None,
    wallpaper_path: str = "",
    wallpaper_uri: str = "",
    wallpaper_name: str = "",
    min_free_gb: Optional[float] = None,
    new_password: str = "",
) -> None:
    """Ejecuta una vista previa o un lote completo sobre todos los hosts.

    El flujo es best-effort: cada host se procesa de forma independiente y se
    guarda su resultado. Un host no detiene el resto del lote. La cancelación
    es cooperativa: se atiende entre hosts y dentro de cada playbook.
    """
    if cancel_event is None:
        cancel_event = get_job_queue().cancel_event(tid)
    batch_id = ""
    db_path = get_db_path()
    cancelled = False
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
                "dry_run": is_dry_run(),
            },
        )
        # Objetivo por grupo de inventario: ``inventory:<group_id>``. Los
        # hosts se leen del inventario en caliente (el grupo puede cambiar
        # entre el envío y el disparo) y la clave SSH del grupo, si está
        # configurada, autentica todas las conexiones del lote.
        private_key = ""
        target_display = ti
        inv_errors: List[str] = []
        connection = "ssh"
        if ti.startswith("inventory:"):
            try:
                inv_group_id = int(ti.split(":", 1)[1])
            except ValueError:
                inv_group_id = None
            inv_group = (
                HostGroup.get(inv_group_id, db_path=db_path)
                if inv_group_id is not None
                else None
            )
            if inv_group is None:
                update_task(
                    tid,
                    {
                        "status": "error",
                        "progress": 0,
                        "message": "El grupo del inventario no existe",
                        "log": [f"Objetivo inválido: {ti}"],
                        "overall_result": "error",
                        "completed": True,
                    },
                )
                return
            target_display = f"Inventario: {inv_group.name}"
            connection = inv_group.connection or "ssh"
            if connection == "winrm":
                private_key = ""  # Windows: autenticación por contraseña
            hosts = []
            # Si la ejecución viene de una aprobación, se usa la instantánea
            # de hosts autorizada, no la composición actual del grupo. Así un
            # cambio de membresía posterior no amplía el alcance aprobado.
            if target_hosts:
                candidates = sorted({str(address) for address in target_hosts})
            else:
                candidates = [
                    inv_host.address
                    for inv_host in HostGroup.hosts_of(
                        inv_group.id, db_path=db_path
                    )
                ]
            for address in candidates:
                pinned_inv, inv_error = pin_target(address)
                if inv_error or not pinned_inv:
                    inv_errors.append(
                        f"Descartado {address}: "
                        + (inv_error or "dirección inválida")
                    )
                    continue
                hosts.append(pinned_inv)
            if inv_group.ssh_key_path:
                if os.path.exists(inv_group.ssh_key_path):
                    private_key = inv_group.ssh_key_path
                    inv_errors.append(
                        f"Autenticación SSH por clave: {inv_group.ssh_key_path}"
                    )
                else:
                    inv_errors.append(
                        "Aviso: la clave SSH del grupo no existe en el "
                        "servidor; se usará solo contraseña."
                    )
        else:
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

        # Provisionamiento opcional de known_hosts. Está desactivado por
        # defecto porque aceptar automáticamente una huella en el primer
        # contacto debilita el modelo TOFU.
        if connection == "ssh" and HOST_KEY_CHECKING and SSH_HOST_KEYS_AUTO:
            try:
                key_status = ensure_host_keys(hosts)
            except Exception:  # pragma: no cover - salvaguarda
                logger.exception("No se pudo provisionar known_hosts")
                inv_errors.append(
                    "Aviso: no se pudo provisionar known_hosts; se usará el "
                    "fichero existente."
                )
            else:
                missing = sorted(
                    host for host, ok in key_status.items() if not ok
                )
                if missing:
                    preview = ", ".join(missing[:5])
                    extra = (
                        f" y {len(missing) - 5} más"
                        if len(missing) > 5
                        else ""
                    )
                    inv_errors.append(
                        f"Aviso: sin huella SSH para {preview}{extra}; Ansible "
                        "fallará si el host no está ya en known_hosts."
                    )
                else:
                    inv_errors.append(
                        f"Huellas SSH verificadas en {len(key_status)} host(s)."
                    )

        update_task(
            tid,
            {
                "progress": 10,
                "message": f"Objetivo: {', '.join(hosts[:3])}"
                + (f" +{len(hosts) - 3} más" if len(hosts) > 3 else ""),
                "log": [
                    f"{target_display} -> {len(hosts)} host(s)"
                ] + inv_errors,
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

        if ac == "WALLPAPER" and not vl:
            # El valor visible del lote es el nombre original de la imagen.
            vl = wallpaper_name or "tapiz"
        if ac == "APT":
            # Umbral efectivo del lote; el playbook lo contrasta con su propia
            # estimación de APT y nunca usa un valor menor.
            if min_free_gb is None:
                min_free_gb = float(APT_MIN_FREE_GB)
            vl = "safe"
        batch_id = str(uuid.uuid4())
        ExecutionBatch.create_batch(
            batch_id=batch_id,
            operator=operator or "desconocido",
            action=ac,
            value=vl,
            target=target_display,
            time_mode=time_mode,
            ntp_policy=ntp_policy,
            total_hosts=len(hosts),
            status="preview" if mode == "preview" else "running",
            # La vista previa nunca aplica cambios: no es una ejecución
            # simulada, sino una inspección real de solo lectura.
            dry_run=is_dry_run() and mode != "preview",
            admin_user=au,
            password_origin=po,
            ntp_servers=list(ntp_servers or []),
            sync_rtc=sync_rtc,
            force=force,
            local_fold=time_fold,
            time_zone=time_zone,
            task_id=task_id or "",
            min_free_gb=min_free_gb if ac == "APT" else None,
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
                "dry_run": is_dry_run(),
            },
        )

        log_lines: List[str] = [f"Lote {batch_id}"]
        deadline = time.monotonic() + BATCH_TIME_BUDGET_SECONDS
        if mode == "preview":
            outcome = _run_preview_batch(
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
                target_epoch=target_epoch,
                cancel_event=cancel_event,
                private_key=private_key,
                connection=connection,
                wallpaper_uri=wallpaper_uri,
                min_free_gb=min_free_gb,
            )
        else:
            outcome = _run_apply_batch(
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
                target_epoch=target_epoch,
                local_fold=time_fold,
                time_zone=time_zone,
                ntp_settle=NTP_SETTLE_SECONDS,
                requiere_ntp_off=TIME_REQUIRE_NTP_OFF,
                cancel_event=cancel_event,
                private_key=private_key,
                connection=connection,
                wallpaper_path=wallpaper_path,
                wallpaper_uri=wallpaper_uri,
                min_free_gb=min_free_gb,
                new_password=new_password,
            )

        if cancel_event is not None and cancel_event.is_set():
            outcome = "cancelled"
        if outcome in {"cancelled", "incomplete"}:
            explicit_status = outcome
        elif mode == "preview":
            # El lote nace como ``preview`` y debe conservar esa marca aunque
            # alguna inspección falle: así sus errores no se suman a las
            # estadísticas de ejecuciones reales.
            explicit_status = "preview"
        else:
            explicit_status = None
        batch = ExecutionBatch.finish_batch(
            batch_id=batch_id,
            total_hosts=len(hosts),
            status=explicit_status,
            db_path=db_path,
        )
        overall_result = (
            explicit_status
            if explicit_status
            else (batch.status if batch is not None else "error")
        )
        if overall_result not in {
            "success",
            "partial",
            "failure",
            "error",
            "preview",
            "incomplete",
            "cancelled",
        }:
            overall_result = "error"
        _append_log(log_lines, _batch_message(batch))
        # Notificación opcional por correo del cierre del lote (best-effort:
        # un fallo de SMTP no afecta el resultado ya persistido).
        try:
            host_results = (
                ExecutionBatch.get_host_results(batch_id, db_path=db_path)
                if batch is not None
                else []
            )
            notify_error = notifier.notify_batch_result(batch, host_results)
            if notify_error:
                log_lines.append(f"Aviso: correo no enviado ({notify_error[:120]})")
        except Exception:  # pragma: no cover - salvaguarda
            logger.exception("No se pudo notificar el resultado del lote")
        update_task(
            tid,
            {
                "status": (
                    overall_result
                    if overall_result in {"cancelled", "incomplete", "error"}
                    else "completed"
                ),
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
    finally:
        # La imagen solo vive lo que dura el lote. Se borra incluso si la
        # ejecución falla antes de crear el lote o durante la aplicación.
        if ac == "WALLPAPER" and wallpaper_path:
            remove_wallpaper(wallpaper_path)


@app.route("/progress/<task_id>")
@login_required
def progress_page(task_id: str):
    task = _get_task(task_id, refresh=True)
    if task is None or not _can_view_task(task):
        abort(404)
    actor = _task_viewer_actor()
    can_cancel = current_user.has_role("admin") or str(
        task.get("owner") or ""
    ) == actor
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
            db_path=get_db_path(),
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

    requested = get_job_queue().cancel(task_id)
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
        db_path=get_db_path(),
    )
    logger.info(
        "Cancelación solicitada para la tarea %s por %s", task_id, actor
    )
    if _wants_json():
        return jsonify(
            {
                "success": True,
                "status": "cancelling",
                "message": "Cancelación solicitada.",
            }
        ), 202
    flash("Cancelación solicitada.", "info")
    return redirect(url_for("progress_page", task_id=task_id))


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
    duration_stats = ExecutionBatch.get_duration_stats(
        batch_id, db_path=get_db_path()
    )
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
        if not current_user.has_role("admin", "operator"):
            abort(403)
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
            origin = request.form.get("origin", "manual").strip().lower() or "manual"
            if not mpw:
                return _password_response(
                    False, "Ingresa la contraseña maestra del Vault.", 400
                )
            if not pws:
                return _password_response(
                    False, "Ingresa al menos una contraseña candidata.", 400
                )
            if origin not in PASSWORD_ORIGIN_VALUES:
                return _password_response(False, "Origen de contraseña no válido.", 400)
            success, msg = VaultManager.encrypt_passwords(
                passwords=pws,
                master_password=mpw,
                origin=origin,
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
        "passwords.html",
        passwords=stored,
        vault_exists=vault_exists,
        password_origin_labels=dict(PASSWORD_ORIGINS),
    )


@app.route("/history")
@login_required
def history():
    batches = ExecutionBatch.get_batches(limit=50, db_path=get_db_path())
    stats = ExecutionBatch.get_stats(db_path=get_db_path())
    return render_template("history.html", batches=batches, stats=stats)


@app.route("/acerca-de", methods=["GET"])
@login_required
def acerca_de():
    """Página institucional con la fecha del host (hora local del servidor)."""
    host_date = format_spanish_datetime(
        datetime.now(timezone.utc), with_time=True
    )
    return render_template("about.html", host_date=host_date)


@app.route("/metricas")
@roles_required("admin")
def metricas():
    """Panel de latencia por ruta (ventana reciente, solo administradores)."""
    snapshot = request_metrics.snapshot()
    rows = sorted(
        snapshot.items(),
        key=lambda item: item[1].get("p95_ms", 0.0),
        reverse=True,
    )
    return render_template(
        "metrics.html",
        metrics=snapshot,
        rows=rows,
        summary=summarize(snapshot),
        window=request_metrics.window,
        slow_request_ms=SLOW_REQUEST_MS,
    )


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
            "endpoints": snapshot,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/audit")
@roles_required("admin")
def audit():
    """Auditoría de inicios de sesión y eventos de autenticación."""
    events = AuthEvent.recent(limit=200, db_path=get_db_path())
    return render_template(
        "audit.html", events=events, retention_days=AUTH_EVENT_RETENTION_DAYS
    )


# ---------------------------------------------------------------------------
# Administración de usuarios (roles y cuentas LDAPS)
# ---------------------------------------------------------------------------
@app.route("/usuarios")
@roles_required("admin")
def usuarios():
    """Lista de todas las cuentas: local y LDAPS (solo el admin entra)."""
    ldap_on = load_ldap_access_setting()
    users = User.all(db_path=get_db_path())
    last_events = AuthEvent.recent(limit=200, db_path=get_db_path())
    last_login: Dict[str, str] = {}
    for event in last_events:
        event_kind = (event["event"] or "")
        if event_kind in ("login_success", "ldap_first_login"):
            last_login.setdefault(event["username"], event["timestamp"])
    return render_template(
        "users.html",
        users=users,
        roles_in_ui=("viewer", "operator", "admin"),
        ldap_supported=ldap_on,
        ldap_configured=ldap_auth.ldap_configured(),
        last_login=last_login,
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
                db_path=get_db_path(),
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
        db_path=get_db_path(),
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

    target = User.get_by_username(username, db_path=get_db_path())
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
        and User.count_active_local_admins_excluding(
            username, db_path=get_db_path()
        )
        == 0
    ):
        flash(
            f"'{username}' es el último administrador local activo: no "
            "puedes retirarle el rol admin.",
            "danger",
        )
        return redirect(url_for("usuarios"))

    try:
        User.set_role(username, new_role, db_path=get_db_path())
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("usuarios"))

    AuthEvent.record(
        username,
        _client_ip(),
        "role_updated",
        f"El admin '{current_user.username}' fijó rol " f"'{new_role}'",
        db_path=get_db_path(),
    )
    flash(f"Rol de '{username}' actualizado.", "success")
    return redirect(
        url_for("usuarios")
        + (f"#{next_username}" if next_username else "")
    )


@app.route("/usuarios/estado", methods=["POST"])
@roles_required("admin")
def usuarios_estado():
    """Activa o desactiva una cuenta (bloquea el acceso, sin borrar datos)."""
    username = (request.form.get("username") or "").strip()
    is_active = (request.form.get("active") or "").strip().lower() in {
        "1", "true", "si", "sí", "yes", "on"
    }
    if not username:
        flash("Nombre de usuario no indicado.", "danger")
        return redirect(url_for("usuarios"))

    if username == current_user.username:
        flash("No puedes desactivar tu propia cuenta.", "danger")
        return redirect(url_for("usuarios"))

    target = User.get_by_username(username, db_path=get_db_path())
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
        and User.count_active_local_admins_excluding(
            username, db_path=get_db_path()
        )
        == 0
    ):
        flash(
            f"'{username}' es el último administrador local activo: no "
            "puedes desactivarlo.",
            "danger",
        )
        return redirect(url_for("usuarios"))

    User.set_active(username, is_active, db_path=get_db_path())
    AuthEvent.record(
        username,
        _client_ip(),
        "account_state_changed",
        f"El admin '{current_user.username}' "
        + ("activó" if is_active else "desactivó")
        + " la cuenta",
        db_path=get_db_path(),
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
            return redirect(url_for("index"))

    if _active_task_count():
        message = (
            "No se puede cambiar el modo mientras hay operaciones en curso. "
            "Espera a que terminen e inténtalo de nuevo."
        )
        if _wants_json():
            return jsonify({"success": False, "message": message}), 409
        flash(message, "warning")
        return redirect(url_for("index"))

    try:
        persist_dry_run(enabled)
        apply_dry_run(enabled)
    except Exception:
        logger.exception("No se pudo actualizar el modo simulación")
        message = "No se pudo guardar el cambio de modo."
        if _wants_json():
            return jsonify({"success": False, "message": message}), 500
        flash(message, "danger")
        return redirect(url_for("index"))

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
        db_path=get_db_path(),
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
    return redirect(target if _is_safe_redirect(target) else url_for("index"))


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


@app.errorhandler(403)
def forbidden(_error):
    if _wants_json():
        return jsonify({"success": False, "message": "Acceso denegado."}), 403
    return render_template("403.html"), 403


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
    @click.option(
        "--role",
        type=click.Choice(list(ROLES)),
        default="admin",
        show_default=True,
    )
    @click.password_option(confirmation_prompt=True)
    def init_admin_command(username: str, password: str, role: str):
        """Crea o actualiza el usuario administrador."""
        if len(password) < ADMIN_PASSWORD_MIN_LENGTH:
            raise click.ClickException(
                f"La contraseña debe tener al menos {ADMIN_PASSWORD_MIN_LENGTH} caracteres."
            )
        db_path = get_db_path()
        user = User.create_or_update(
            username, password, db_path=db_path, role=role
        )
        # ``create_or_update`` no cambia el rol de una cuenta existente para
        # no degradar roles de forma implícita (p. ej. en el primer login
        # LDAPS). El CLI sí es una acción explícita del operador: aplica el
        # rol pedido y lo informa.
        if user.role != role:
            User.set_role(username, role, db_path=db_path)
            click.echo(
                f"Usuario '{username}' actualizado: rol cambiado de "
                f"'{user.role}' a '{role}'."
            )
        else:
            click.echo(
                f"Usuario '{username}' creado/actualizado con rol '{role}'."
            )

    @flask_app.cli.command("scheduler")
    @click.option(
        "--once",
        is_flag=True,
        help="Ejecuta una sola revisión de programaciones y termina.",
    )
    def scheduler_command(once: bool):
        """Ejecuta el programador de tareas en primer plano.

        Útil para un proceso/servicio dedicado (``ACC_ROLE=worker``) cuando
        el servidor web corre con ``ACC_ROLE=web``. El lock de instancia
        impide que dos procesos programen a la vez.
        """
        db_path = str(get_db_path())
        if once:
            launched = scheduler.scan_once(db_path, launch_scheduled)
            click.echo(f"Programaciones lanzadas: {launched}")
            return
        thread = scheduler.start(
            launch_scheduled,
            lambda: db_path,
            maintenance=_purge_retention,
            maintenance_interval=RETENTION_INTERVAL_HOURS * 3600,
        )
        if thread is None:
            raise click.ClickException(
                "El scheduler no se inició: revisa ACC_SCHEDULER_ENABLED, "
                "ACC_ROLE o si otro proceso ya tiene el lock."
            )
        click.echo("Scheduler en ejecución (Ctrl+C para detener).")
        try:
            while thread.is_alive():
                thread.join(timeout=1)
        except KeyboardInterrupt:
            scheduler.stop()
            click.echo("Scheduler detenido.")

    @flask_app.cli.command("reconcile-batches")
    def reconcile_batches_command():
        """Marca como error los lotes que quedaron en curso."""
        count = ExecutionBatch.reconcile_orphans(db_path=get_db_path())
        click.echo(f"Lotes huérfanos reconciliados: {count}")

    @flask_app.cli.command("sync-known-hosts")
    def sync_known_hosts_command():
        """Precarga en known_hosts las huellas SSH de todo el inventario.

        Es opcional: con ``ACC_SSH_HOST_KEYS_AUTO=1`` cada ejecución ya
        aprovisiona las huellas que falten, incluidas IPs nuevas. Este comando
        sirve para dejar el despliegue listo de una vez.
        """
        db_path = get_db_path()
        hosts = []
        for group in HostGroup.all(db_path=db_path):
            if (group.connection or "ssh") != "ssh":
                continue
            for inv_host in HostGroup.hosts_of(
                group.id, db_path=db_path
            ):
                hosts.append(inv_host.address)
        hosts = sorted(set(hosts))
        if not hosts:
            click.echo("No hay hosts SSH en el inventario.")
            return

        status = ensure_host_keys(hosts)
        ok = sum(1 for value in status.values() if value)
        click.echo(f"Huellas disponibles: {ok}/{len(status)}")
        missing = sorted(host for host, value in status.items() if not value)
        if missing:
            click.echo("Sin huella (host caído o SSH no accesible):")
            for host in missing:
                click.echo(f"  - {host}")
            raise click.ClickException(
                "No se pudieron obtener todas las huellas SSH."
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def _shutdown_job_queue() -> None:
    """Espera brevemente a los trabajos en curso al terminar el proceso."""
    if _job_queue is not None:
        _job_queue.shutdown(wait=True)


def _is_cli_invocation() -> bool:
    """Detecta si el proceso es el CLI de Flask (no el servidor web)."""
    argv = [str(part).lower() for part in sys.argv]
    if not argv:
        return False
    programa = os.path.basename(argv[0])
    if programa in {"flask", "flask.exe"}:
        return True
    if len(argv) >= 3 and argv[1] == "-m" and argv[2] == "flask":
        return True
    return False


def configure_app(flask_app: Flask) -> Flask:
    # Falla pronto si la configuración de producción no es segura (allowlist,
    # clave de sesión y verificación de claves SSH).
    validate_security_config()
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
        if IS_PRODUCTION:
            # En producción no se degrada a instance/: la base y los secretos
            # deben estar en las rutas respaldadas.
            raise
        logger.warning(
            "No se pudieron crear los directorios de producción (%s); "
            "se usará el directorio local instance/ para desarrollo.",
            exc,
        )

    login_manager.init_app(flask_app)
    init_db()
    try:
        # El modo simulación elegido en la interfaz sobrevive a los reinicios.
        apply_dry_run(load_dry_run_setting())
    except Exception:  # pragma: no cover - no impedir el arranque
        logger.exception("No se pudo cargar el modo simulación persistido")

    cli_mode = _is_cli_invocation()
    if not cli_mode and PROCESS_ROLE != "cli":
        try:
            _purge_retention()
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudo aplicar la retención al arrancar")
        try:
            # Solo tareas estancadas: un worker no debe cerrar operaciones que
            # otro worker sigue actualizando.
            _recover_task_state(stale_minutes=RECOVERY_STALE_MINUTES)
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudieron recuperar las tareas persistidas")
        try:
            reconciled = ExecutionBatch.reconcile_orphans(
                db_path=get_db_path(),
                stale_minutes=RECOVERY_STALE_MINUTES,
            )
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudieron reconciliar lotes huérfanos")
        else:
            if reconciled:
                logger.warning(
                    "Se marcaron %s lote(s) huérfano(s) como error al arrancar",
                    reconciled,
                )
    register_cli(flask_app)

    # Programador de ejecuciones desatendidas. Un hilo daemon revisa la tabla
    # ``schedules`` y lanza los lotes por la misma cola que la operación
    # manual. El lock de instancia evita que varios workers programen a la vez
    # y ``ACC_ROLE=web`` permite delegarlo a un proceso dedicado.
    if not cli_mode:
        try:
            scheduler.start(
                launch_scheduled,
                lambda: str(get_db_path()),
                maintenance=_purge_retention,
                maintenance_interval=RETENTION_INTERVAL_HOURS * 3600,
            )
        except Exception:  # pragma: no cover - no impedir el arranque
            logger.exception("No se pudo iniciar el programador de ejecuciones")

    # Apagado ordenado. El ``atexit`` se registra una sola vez por proceso; si
    # la cola nunca se creó, el callback no hace nada.
    atexit.register(_shutdown_job_queue)
    return flask_app


configure_app(app)


if __name__ == "__main__":
    host = os.environ.get("ACC_HOST", "127.0.0.1")
    port = int(os.environ.get("ACC_PORT", "5000"))
    app.run(host=host, port=port, debug=DEBUG)
