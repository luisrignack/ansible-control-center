#!/usr/bin/env python3
"""Hora Admin — configuración de hora/zona horaria en Ubuntu vía Ansible.

Incluye autenticación, validación de formularios, progreso SSE, historial en
SQLite, vault real con ansible-vault y ejecución de Ansible con inyección
segura de la contraseña candidata (fases P0-P2).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import click
from flask import (
    Flask,
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
    COMMON_TIMEZONES,
    DEBUG,
    MAX_CONTENT_LENGTH,
    MAX_HOSTS,
    MAX_PASSWORDS,
    MAX_PASSWORD_LENGTH,
    PERMANENT_SESSION_LIFETIME_SECONDS,
    REMEMBER_COOKIE_HTTPONLY,
    REMEMBER_COOKIE_SAMESITE,
    SESSION_COOKIE_HTTPONLY,
    SESSION_COOKIE_SAMESITE,
    SESSION_COOKIE_SECURE,
    ensure_directories,
    get_or_create_secret_key,
    resolve_db_path,
    resolve_vault_file,
    setup_logging,
)
from models.db import init_schema
from models.execution import ExecutionRecord
from models.password import PasswordRecord
from models.user import User
from services.ansible_runner import run_ansible_playbook
from services.host_resolver import resolve_target, validate_ip_format
from services.password_tester import test_ansible_ping
from services.secure_temp import secure_temp_file
from services.vault_manager import VaultManager

logger = logging.getLogger("hora_admin")

# ---------------------------------------------------------------------------
# Estado en memoria de las tareas (progreso SSE)
# ---------------------------------------------------------------------------
_tasks: Dict[str, Dict[str, Any]] = {}
_tasks_lock = threading.Lock()
_db_path_cache: Optional[str] = None

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


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def sse_event(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def sse_retry(retry_ms: int) -> str:
    return f"retry: {retry_ms}\n\n"


def create_task() -> str:
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = {
            "status": "pending",
            "progress": 0,
            "message": "Proceso iniciado",
            "log": [],
            "host": "",
            "password_hash": "",
            "overall_result": "unknown",
            "completed": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    return task_id


def update_task(tid: str, updates: Dict[str, Any]) -> None:
    with _tasks_lock:
        if tid in _tasks:
            _tasks[tid].update(updates)


def remove_task(tid: str) -> None:
    with _tasks_lock:
        _tasks.pop(tid, None)


def generate_progress_events(task_id: str):
    yield sse_retry(1500)
    start = time.time()
    max_wait = 300
    last_payload: Optional[str] = None

    while True:
        with _tasks_lock:
            task = _tasks.get(task_id)

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
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.get_by_username(username, db_path=get_db_path())

        if user is not None and user.is_active and user.check_password(password):
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
    return render_template("index.html", common_timezones=COMMON_TIMEZONES)


def handle_execution():
    ti = request.form.get("target_ip", "").strip()
    au = request.form.get("admin_user", "").strip()
    po = request.form.get("password_origin", "stored")
    mpw = request.form.get("master_password", "").strip()
    ac = request.form.get("action", "TZ")
    vl = request.form.get("value", "").strip()

    errors = []

    if not ti or not validate_ip_format(ti):
        errors.append("IP o rango CIDR inválido.")
    if not au:
        errors.append("El usuario administrador es obligatorio.")
    if ac not in {"TZ", "TIME"}:
        errors.append("Acción no válida.")

    if ac == "TZ":
        if not vl:
            errors.append("La zona horaria es obligatoria.")
        elif vl not in COMMON_TIMEZONES and not re.fullmatch(r"[A-Za-z0-9_+\-/]+", vl):
            errors.append("Formato de zona horaria inválido.")
    elif ac == "TIME":
        if not vl:
            errors.append("La hora exacta es obligatoria.")
        else:
            try:
                datetime.strptime(vl, "%Y-%m-%dT%H:%M")
            except ValueError:
                errors.append("Formato de hora inválido; use AAAA-MM-DDTHH:MM.")

    temporal_passwords: list[str] = []

    if po not in {"stored", "temporal"}:
        errors.append("Origen de contraseñas no válido.")
    elif po == "stored":
        try:
            vault_file = get_vault_file()
        except RuntimeError as exc:
            errors.append(str(exc))
            vault_file = None

        if vault_file and not os.path.exists(vault_file):
            errors.append("No existe el archivo del vault (passwords_vault.yml).")
        if not mpw:
            errors.append("La contraseña maestra del Vault es obligatoria.")
        elif vault_file and os.path.exists(vault_file):
            valid, message = VaultManager.validate_master_password(
                vault_file=vault_file,
                test_password=mpw,
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
        for error in errors:
            flash(error, "danger")
        if _wants_json():
            return jsonify({"status": "error", "errors": errors}), 400
        return redirect(url_for("index"))

    tid = create_task()
    update_task(
        tid,
        {"status": "running", "progress": 0, "message": "Iniciando…", "completed": False},
    )
    thread = threading.Thread(
        target=execute_with_progress,
        args=(tid, ti, au, po, mpw, ac, vl, temporal_passwords),
        daemon=True,
        name=f"hora-admin-{tid[:8]}",
    )
    thread.start()

    if _wants_json():
        return jsonify(
            {
                "status": "started",
                "task_id": tid,
                "message": f"Ver /progress/{tid} para el progreso en tiempo real",
            }
        )
    return redirect(url_for("progress_page", task_id=tid))


def execute_with_progress(
    tid: str,
    ti: str,
    au: str,
    po: str,
    mpw: str,
    ac: str,
    vl: str,
    temporal_passwords: Optional[list[str]] = None,
) -> None:
    """Ejecuta el flujo completo: hosts, credenciales y playbook."""
    try:
        update_task(
            tid,
            {
                "status": "running",
                "progress": 5,
                "message": "Resolviendo hosts…",
                "log": ["Iniciando"],
                "completed": False,
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

        total = max(len(hosts) * len(candidates), 1)
        tested = 0
        last_error = ""

        for host in hosts:
            for candidate in candidates:
                tested += 1
                progress = min(15 + int(tested / total * 55), 70)
                update_task(
                    tid,
                    {
                        "progress": progress,
                        "message": f"Probando credenciales en {host}…",
                        "log": [f"Intento {tested}/{total} en {host}"],
                    },
                )

                ping = test_ansible_ping(host=host, user=au, password=candidate)
                if not ping.get("success"):
                    last_error = ping.get("error", "sin conexión")
                    update_task(
                        tid,
                        {
                            "progress": progress,
                            "message": f"Sin acceso a {host}",
                            "log": [f"Ping falló en {host}: {str(last_error)[:160]}"],
                        },
                    )
                    continue

                short_hash = PasswordRecord.hash_short(candidate)
                update_task(
                    tid,
                    {
                        "progress": 75,
                        "message": f"Credencial válida en {host}; ejecutando playbook…",
                        "log": [f"Ping OK en {host} (hash {short_hash})"],
                    },
                )

                playbook_result = run_ansible_playbook(
                    host=host,
                    user=au,
                    password=candidate,
                    action=ac,
                    value=vl,
                )

                if playbook_result.get("success"):
                    ExecutionRecord.record_execution(
                        action=ac,
                        value=vl,
                        host=host,
                        result="success",
                        password_hash=short_hash,
                        password_origin=po,
                        db_path=get_db_path(),
                    )
                    update_task(
                        tid,
                        {
                            "status": "completed",
                            "progress": 100,
                            "message": "Configuración completada",
                            "log": [f"Playbook OK en {host}"],
                            "overall_result": "success",
                            "completed": True,
                        },
                    )
                    return

                last_error = playbook_result.get("error", "El playbook falló")
                ExecutionRecord.record_execution(
                    action=ac,
                    value=vl,
                    host=host,
                    result="failure",
                    password_hash=short_hash,
                    password_origin=po,
                    db_path=get_db_path(),
                )
                update_task(
                    tid,
                    {
                        "status": "failed",
                        "progress": 85,
                        "message": f"El playbook falló en {host}",
                        "log": [str(last_error)[:200]],
                        "overall_result": "failure",
                        "completed": True,
                    },
                )
                return

        # Ningún intento llegó a ejecutar el playbook: dejar constancia.
        ExecutionRecord.record_execution(
            action=ac,
            value=vl,
            host=ti,
            result="failure",
            password_hash="",
            password_origin=po,
            db_path=get_db_path(),
        )
        update_task(
            tid,
            {
                "status": "failure",
                "progress": 65,
                "message": "Ningún host respondió correctamente",
                "log": [f"{tested} intento(s) sin éxito", str(last_error)[:200]],
                "overall_result": "failure",
                "completed": True,
            },
        )
    except Exception as exc:  # pragma: no cover - salvaguarda
        logger.exception("Error en la ejecución %s", tid)
        update_task(
            tid,
            {
                "status": "error",
                "progress": 0,
                "message": f"Error: {str(exc)[:120]}",
                "log": [f"Error: {exc}"],
                "overall_result": "error",
                "completed": True,
            },
        )


@app.route("/progress/<task_id>")
@login_required
def progress_page(task_id: str):
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        abort(404)
    return render_template("progress.html", task_id=task_id, task=task)


@app.route("/progress/<task_id>/events")
@login_required
def progress_sse(task_id: str):
    with _tasks_lock:
        exists = task_id in _tasks
    if not exists:
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
    with _tasks_lock:
        task = _tasks.get(task_id)
    if task is None:
        return jsonify({"status": "not_found", "exists": False})
    return jsonify({**task, "exists": True})


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
    executions = ExecutionRecord.get_all_executions(
        limit=50, db_path=get_db_path()
    )
    return render_template("history.html", executions=executions)


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


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
        PERMANENT_SESSION_LIFETIME=timedelta(
            seconds=PERMANENT_SESSION_LIFETIME_SECONDS
        ),
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
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
    register_cli(flask_app)
    return flask_app


configure_app(app)


if __name__ == "__main__":
    host = os.environ.get("HORA_ADMIN_HOST", "127.0.0.1")
    port = int(os.environ.get("HORA_ADMIN_PORT", "5000"))
    app.run(host=host, port=port, debug=DEBUG)
