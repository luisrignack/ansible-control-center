"""Ejecucion de lotes: vista previa, aplicacion y progreso.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user

from acc import runtime
from acc.approvals import (
    _approval_params,
    _approval_payload,
)
from acc.console import (
    _append_console,
    _append_log,
    _batch_console_lock,
    _make_console_callback,
    _ntp_keep_warning,
    _progress_for,
    _skip_remaining_hosts,
)
from acc.operations import (
    _batch_message,
    _parse_operation_form,
    _would_change,
)
from acc.runtime import (
    _get_host_semaphore,
    get_vault_file,
    is_dry_run,
)
from acc.safety import _safety_gate
from acc.security import (
    _check_rate_limit,
    _client_ip,
    _rate_limited_response,
    _wants_json,
)
from acc.task_state import (
    _claim_operation_id,
    _complete_operation_id,
    _release_operation_id,
    create_task,
    remove_task,
    update_task,
)
from config import (
    APPROVAL_REQUEST_TTL_HOURS,
    APPROVAL_TTL_MINUTES,
    APT_MIN_FREE_GB,
    BATCH_HOST_CONCURRENCY,
    BATCH_TIME_BUDGET_SECONDS,
    EXEC_RATE_LIMIT,
    EXEC_RATE_WINDOW,
    HOST_KEY_CHECKING,
    MAX_HOSTS,
    MAX_PASSWORD_ATTEMPTS,
    NTP_SETTLE_SECONDS,
    OS_PASSWORD_DEFAULT_USER,
    SSH_HOST_KEYS_AUTO,
    TIME_REQUIRE_NTP_OFF,
    TIMEZONE_BACKUP_DIR,
)
from models.batch import ExecutionBatch
from models.execution import ExecutionRecord
from models.ops import (
    APPROVAL_PENDING,
    ApprovalRequest,
    HostGroup,
    params_hash,
)
from models.password import PasswordRecord
from services import notifier, task_catalog
from services.ansible_runner import (
    apply_host,
    inspect_apt,
    inspect_firewall,
    inspect_host,
    inspect_password,
    inspect_wallpaper,
)
from services.host_locks import (
    acquire_host_lock,
    refresh_host_lock,
    release_batch_locks,
    release_host_lock,
)
from services.host_resolver import (
    pin_target,
    resolve_target,
)
from services.job_queue import JobQueueFull
from services.password_tester import test_ansible_ping
from services.ssh_host_keys import ensure_host_keys
from services.vault_manager import VaultManager
from services.wallpaper import remove_wallpaper

logger = logging.getLogger("acc")


def handle_execution(default_mode: str = "execute", form_data=None, files=None):
    """Valida y lanza una ejecución desde el formulario o desde una aprobación.

    ``form_data`` permite reenviar los parámetros guardados en una solicitud de
    doble aprobación ya concedida, de modo que el solicitante no tenga que
    volver a rellenar el formulario.
    """
    submitted = request.form if form_data is None else form_data
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

    form, errors = _parse_operation_form(
        default_mode=default_mode, form_data=form_data, files=files
    )
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
    task = task_catalog.get_task(form["task_id"])
    if (
        task is not None
        and task.get("min_role") == "admin"
        and not current_user.has_role("admin")
    ):
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
    operation_id = submitted.get("operation_id", "").strip()
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

    approval_request_id: int | None = None
    if task is not None and task.get("destructive") and form["mode"] == "execute":
        # Reserva sin consumir: si el encolado falla (cola llena, excepción),
        # la aprobación vuelve a ``approved`` y no se pierde.
        approval_request_id, approval_error = ApprovalRequest.reserve_if_approved(
            form["task_id"],
            _approval_params(form),
            executor=operator,
            approves_by_differs=True,
            ttl_minutes=APPROVAL_TTL_MINUTES,
            ttl_hours=APPROVAL_REQUEST_TTL_HOURS,
            db_path=runtime.get_db_path(),
        )
        if approval_request_id is None:
            _release_operation_id(operation_id)
            discard_wallpaper()
            message = approval_error or "Se requiere doble aprobación para esta tarea."
            if (
                "pendiente" not in message
                and "ha expirado" not in message
                and "ya se usó" not in message
            ):
                approval_params = _approval_params(form)
                pending = [
                    req
                    for req in ApprovalRequest.list_status(
                        APPROVAL_PENDING, db_path=runtime.get_db_path()
                    )
                    if req.task_id == form["task_id"]
                    and req.params_hash == params_hash(approval_params)
                    and req.requested_by == operator
                ]
                if not pending:
                    created = ApprovalRequest.create(
                        form["task_id"],
                        approval_params,
                        requested_by=operator,
                        payload=_approval_payload(submitted),
                        db_path=runtime.get_db_path(),
                    )
                    message = (
                        "Tarea destructiva: solicitud #"
                        f"{created.id} registrada como pendiente de "
                        "aprobación. Un administrador distinto de ti debe "
                        "aprobarla en las próximas "
                        f"{APPROVAL_REQUEST_TTL_HOURS} h; una vez aprobada "
                        "podrás ejecutarla desde Mis solicitudes en los "
                        f"{APPROVAL_TTL_MINUTES // 60} h siguientes."
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
            "status": "pending",
            "progress": 0,
            "message": "En cola…",
            "mode": form["mode"],
            "action": form["action"],
            "completed": False,
        },
    )
    try:
        runtime.get_job_queue().submit(
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
            ApprovalRequest.release_reserved(approval_request_id, db_path=runtime.get_db_path())
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
            ApprovalRequest.release_reserved(approval_request_id, db_path=runtime.get_db_path())
        raise
    else:
        if approval_request_id is not None:
            ApprovalRequest.confirm_reserved(approval_request_id, db_path=runtime.get_db_path())

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


def _run_preview_batch(
    tid: str,
    batch_id: str,
    hosts: list[str],
    admin_user: str,
    candidates: list[str],
    action: str,
    value: str,
    password_origin: str,
    time_mode: str,
    ntp_policy: str,
    ntp_servers: list[str],
    force: bool,
    deadline: float,
    db_path: str,
    log_lines: list[str],
    target_epoch: int | None = None,
    cancel_event: threading.Event | None = None,
    private_key: str = "",
    connection: str = "ssh",
    wallpaper_uri: str = "",
    min_free_gb: float | None = None,
) -> str:
    """Inspecciona todos los hosts sin cambiar nada y registra el lote.

    Devuelve ``completed``, ``incomplete`` (presupuesto agotado) o
    ``cancelled`` (cancelación solicitada por el usuario).
    """
    total = len(hosts)
    console_lines: list[str] = []
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
        state: dict[str, Any] | None = None
        used_hash = ""
        last_error = ""
        inspected = False
        _append_console(console_lines, tid, f"=== {host} ({index}/{total}) ===")
        line_callback = _make_console_callback(tid, host, console_lines, candidates)

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
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - salvaguarda
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
            attention = bool(isinstance(state, dict) and state.get("attention"))
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
    hosts: list[str],
    admin_user: str,
    candidates: list[str],
    action: str,
    value: str,
    password_origin: str,
    time_mode: str,
    ntp_policy: str,
    ntp_servers: list[str],
    sync_rtc: bool,
    force: bool,
    deadline: float,
    db_path: str,
    log_lines: list[str],
    target_epoch: int | None = None,
    local_fold: int | None = None,
    time_zone: str = "",
    ntp_settle: int = 0,
    requiere_ntp_off: bool = True,
    cancel_event: threading.Event | None = None,
    private_key: str = "",
    connection: str = "ssh",
    wallpaper_path: str = "",
    wallpaper_uri: str = "",
    min_free_gb: float | None = None,
    new_password: str = "",
    dry_run: bool | None = None,
) -> str:
    """Aplica el cambio en todos los hosts (best-effort) y registra cada uno.

    Si una contraseña autentica pero el playbook falla, se prueban las
    siguientes candidatas hasta ``ACC_MAX_PASSWORD_ATTEMPTS``. Un fallo
    de un host nunca detiene el lote. Al agotarse el presupuesto de tiempo del
    lote, los hosts pendientes quedan como ``skipped``.

    Devuelve ``completed``, ``incomplete`` (presupuesto agotado) o
    ``cancelled`` (cancelación solicitada por el usuario).
    """
    total = len(hosts)
    console_lines: list[str] = []
    outcome = "completed"
    state_lock = threading.Lock()
    completed_hosts = 0
    cancelled = False
    incomplete = False
    semaphore = _get_host_semaphore()
    budget_reason = "Presupuesto de tiempo del lote agotado."

    def _completed_snapshot() -> tuple[int, int]:
        with state_lock:
            done = completed_hosts
        return done, _progress_for(done, total)

    def _finish_host() -> None:
        nonlocal completed_hosts
        with state_lock:
            completed_hosts += 1
            done = completed_hosts
        with _batch_console_lock:
            log_snapshot = list(log_lines)
            console_snapshot = list(console_lines)
        update_task(
            tid,
            {
                "progress": _progress_for(done, total),
                "message": f"Procesados {done}/{total} hosts",
                "log": log_snapshot,
                "console": console_snapshot,
            },
        )

    def _skip(host: str, reason: str, position: int) -> None:
        ExecutionBatch.add_host_result(
            batch_id=batch_id,
            host=host,
            result="skipped",
            error=reason,
            position=position,
            db_path=db_path,
        )
        _append_log(log_lines, f"OMITIDO {host}: {reason}")
        _append_console(console_lines, tid, f"OMITIDO: {reason}", host=host)

    def process_host(index: int, host: str) -> None:
        """Procesa un host; el lote nunca se detiene por un host con error."""
        nonlocal cancelled, incomplete
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _skip(host, "Cancelado por el usuario.", index)
            _finish_host()
            return
        if time.monotonic() >= deadline:
            incomplete = True
            _skip(host, budget_reason, index)
            _finish_host()
            return

        # Cupo global: espera acotada para atender cancelación y presupuesto
        # mientras otros lotes ocupan los playbooks simultáneos.
        while True:
            if semaphore.acquire(timeout=0.2):
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _skip(host, "Cancelado por el usuario.", index)
                _finish_host()
                return
            if time.monotonic() >= deadline:
                incomplete = True
                _skip(host, budget_reason, index)
                _finish_host()
                return

        try:
            # Reserva del host: dos lotes distintos pueden apuntar a la misma
            # IP. El segundo se omite en vez de competir por su estado final.
            if not acquire_host_lock(host, batch_id, db_path=db_path):
                _skip(host, "Otra operación en curso sobre el host.", index)
                return
            try:
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
                _append_console(console_lines, tid, f"=== {host} ({index}/{total}) ===")
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
                    # Un host con varias candidatas puede tardar más que el TTL
                    # inicial; se renueva antes de cada intento.
                    refresh_host_lock(host, batch_id, db_path=db_path)
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
                    except Exception as exc:  # noqa: BLE001  # salvaguarda
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
                            "progress": _completed_snapshot()[1],
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
                            dry_run=dry_run,
                        )
                    except Exception as exc:  # noqa: BLE001  # salvaguarda
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
                        warnings = str(result.get("warnings"))
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
                    position=index,
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
            except Exception as exc:  # pragma: no cover - salvaguarda
                logger.exception("Error inesperado procesando %s", host)
                ExecutionBatch.add_host_result(
                    batch_id=batch_id,
                    host=host,
                    result="failure",
                    error=f"Error interno: {str(exc)[:160]}",
                    position=index,
                    db_path=db_path,
                )
                _append_log(log_lines, f"FALLO {host}: error interno")
                _append_console(console_lines, tid, "FALLO: error interno", host=host)
            finally:
                release_host_lock(host, batch_id, db_path=db_path)
        finally:
            semaphore.release()
            _finish_host()

    workers = max(1, min(BATCH_HOST_CONCURRENCY, total))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=f"acc-hosts-{batch_id}"
    ) as pool:
        futures = [
            pool.submit(process_host, index, host)
            for index, host in enumerate(hosts, start=1)
        ]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:  # pragma: no cover - salvaguarda
                logger.exception("Fallo inesperado en un host del lote %s", batch_id)

    if cancelled:
        outcome = "cancelled"
    elif incomplete:
        outcome = "incomplete"
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
    temporal_passwords: list[str] | None = None,
    *,
    mode: str = "execute",
    time_mode: str = "utc",
    time_zone: str = "",
    time_fold: int | None = None,
    target_epoch: int | None = None,
    ntp_policy: str = "keep",
    ntp_servers: list[str] | None = None,
    sync_rtc: bool = False,
    force: bool = False,
    operator: str = "",
    task_id: str = "",
    cancel_event: threading.Event | None = None,
    target_hosts: list[str] | None = None,
    wallpaper_path: str = "",
    wallpaper_uri: str = "",
    wallpaper_name: str = "",
    min_free_gb: float | None = None,
    new_password: str = "",
) -> None:
    """Ejecuta una vista previa o un lote completo sobre todos los hosts.

    El flujo es best-effort: cada host se procesa de forma independiente y se
    guarda su resultado. Un host no detiene el resto del lote. La cancelación
    es cooperativa: se atiende entre hosts y dentro de cada playbook.
    """
    if cancel_event is None:
        cancel_event = runtime.get_job_queue().cancel_event(tid)
    batch_id = ""
    db_path = runtime.get_db_path()
    # El modo se captura al arrancar el lote: aunque el interruptor global
    # cambie, esta ejecución mantiene un único comportamiento de principio a
    # fin y no mezcla hosts simulados con hosts reales.
    batch_dry_run = is_dry_run()
    try:
        update_task(
            tid,
            {
                "status": "running",
                "queue_position": 0,
                "progress": 5,
                "message": "Resolviendo hosts…",
                "log": ["Iniciando"],
                "mode": mode,
                "action": ac,
                "completed": False,
                "dry_run": batch_dry_run,
            },
        )
        # Objetivo por grupo de inventario: ``inventory:<group_id>``. Los
        # hosts se leen del inventario en caliente (el grupo puede cambiar
        # entre el envío y el disparo) y la clave SSH del grupo, si está
        # configurada, autentica todas las conexiones del lote.
        private_key = ""
        target_display = ti
        inv_errors: list[str] = []
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
                    for inv_host in HostGroup.hosts_of(inv_group.id, db_path=db_path)
                ]
            for address in candidates:
                pinned_inv, inv_error = pin_target(address)
                if inv_error or not pinned_inv:
                    inv_errors.append(
                        f"Descartado {address}: " + (inv_error or "dirección inválida")
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
                missing = sorted(host for host, ok in key_status.items() if not ok)
                if missing:
                    preview = ", ".join(missing[:5])
                    extra = f" y {len(missing) - 5} más" if len(missing) > 5 else ""
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
                "log": [f"{target_display} -> {len(hosts)} host(s)"] + inv_errors,
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
            dry_run=batch_dry_run and mode != "preview",
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
                "dry_run": batch_dry_run,
            },
        )

        log_lines: list[str] = [f"Lote {batch_id}"]
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
                dry_run=batch_dry_run,
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
        # Salvaguarda: si una excepción escapó del bucle, no dejar reservas
        # de host hasta que caduquen.
        if batch_id:
            release_batch_locks(batch_id, db_path=db_path)
        # La imagen solo vive lo que dura el lote. Se borra incluso si la
        # ejecución falla antes de crear el lote o durante la aplicación.
        if ac == "WALLPAPER" and wallpaper_path:
            remove_wallpaper(wallpaper_path)
