# services/ansible_runner.py
# Wrapper para ejecutar playbooks de Ansible de forma segura.
#
# La contraseña candidata nunca se pasa por la línea de comandos (visible en
# ``ps``): se escribe en un fichero temporal 0600 y se pasa con
# ``--extra-vars @fichero``. El fichero se borra siempre al terminar.
#
# La fase P4 añade dos operaciones:
#   * ``inspect_host``: lee hora, zona horaria, NTP y RTC sin cambiar nada.
#   * ``apply_host``: aplica el cambio, verifica el resultado y devuelve el
#     estado previo y posterior.
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from config import (
    ANSIBLE_CFG,
    BIN_ANSIBLE_PLAYBOOK,
    CONNECT_TIMEOUT,
    DRY_RUN,
    HOST_KEY_CHECKING,
    INSPECT_PLAYBOOK_FILE,
    MAX_OUTPUT_BUFFER_LINES,
    PLAYBOOK_FILE,
    PLAYBOOK_TIMEOUT,
    TIME_VERIFY_TOLERANCE,
    WINRM_PORT,
    WINRM_SERVER_CERT_VALIDATION,
    WINRM_TRANSPORT,
    WINDOWS_APPLY_PLAYBOOK_FILE,
    WINDOWS_INSPECT_PLAYBOOK_FILE,
)
from services.job_queue import JobCancelled
from services.secure_temp import secure_temp_file

logger = logging.getLogger(__name__)

_STATE_B64_RE = re.compile(r"HORA_STATE_B64=([A-Za-z0-9+/=]+)")
_BEFORE_B64_RE = re.compile(r"HORA_BEFORE_B64=([A-Za-z0-9+/=]+)")
_STATE_JSON_RE = re.compile(r"HORA_STATE=(\{.*\})")
_BEFORE_JSON_RE = re.compile(r"HORA_BEFORE=(\{.*\})")


def _result(host: str, success: bool, **extra: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "success": success,
        "host": host,
        "output": "",
        "error": "",
        "ok_count": 0,
        "changed_count": 0,
        "failed_count": 0,
        "unreachable_count": 0,
    }
    result.update(extra)
    return result


def _emit_line(
    callback: Optional[Callable[[str], None]], message: str
) -> None:
    """Entrega una línea a la consola sin dejar que un fallo tumbe el flujo."""
    if callback is None:
        return
    try:
        callback(message)
    except Exception:  # pragma: no cover - salvaguarda
        logger.exception("Error en el callback de salida de Ansible")


def ansible_environment() -> Dict[str, str]:
    """Entorno mínimo para las llamadas a Ansible, sin secretos.

    No se hereda ``os.environ`` completo: variables como la clave de sesión,
    la contraseña maestra o credenciales de nube no deben llegar a un proceso
    hijo que ejecuta código remoto. Solo se conservan las variables necesarias
    para localizar binarios, idioma y configuración explícita de Ansible.
    """
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
        "LOGNAME",
        "SHELL",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "SSH_AUTH_SOCK",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in allowed
        or key.startswith("ANSIBLE_")
        or key.startswith("LC_")
        or key in {"http_proxy", "https_proxy", "no_proxy"}
    }
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_CFG)
    env["ANSIBLE_NOCOLOR"] = "1"
    # Evita que Ansible intente pedir contraseñas interactivamente.
    env["ANSIBLE_HOST_KEY_CHECKING"] = "True" if HOST_KEY_CHECKING else "False"
    return env


def _invoke_playbook(
    host: str,
    user: str,
    password: Optional[str],
    playbook_path: str,
    extra_vars: Dict[str, Any],
    timeout: Optional[int] = None,
    line_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    private_key: Optional[str] = None,
    connection: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Ejecuta ``ansible-playbook`` con las credenciales en un fichero 0600.

    Si se pasa ``line_callback`` o ``cancel_event``, la salida se lee línea a
    línea y el proceso se puede cancelar en caliente. Sin callback se mantiene
    la ruta clásica con ``subprocess.run``.
    """
    path = str(playbook_path)
    payload = dict(extra_vars)
    payload["ansible_password"] = password
    payload["ansible_become_password"] = password
    if private_key:
        # La clave se pasa por variables de conexión, no por línea de
        # comandos, para que no aparezca en ``ps`` ni en el historial.
        payload["ansible_ssh_private_key_file"] = private_key
    if connection == "winrm":
        # Windows: la conexión es WinRM (también por variables, nunca por
        # línea de comandos). El playbook define ``become: false`` porque el
        # escalamiento sudo no aplica a WinRM.
        payload["ansible_connection"] = "winrm"
        payload["ansible_port"] = str(WINRM_PORT)
        payload["ansible_winrm_transport"] = WINRM_TRANSPORT
        payload["ansible_winrm_server_cert_validation"] = (
            WINRM_SERVER_CERT_VALIDATION
        )

    with secure_temp_file(
        json.dumps(payload), prefix=".ansible-vars-", suffix=".json"
    ) as vars_file:
        cmd = [
            BIN_ANSIBLE_PLAYBOOK,
            path,
            "-i",
            f"{host},",
            "-u",
            user,
            "--extra-vars",
            f"@{vars_file}",
            "--timeout",
            str(CONNECT_TIMEOUT),
        ]
        env = ansible_environment()
        if line_callback is None and cancel_event is None:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or PLAYBOOK_TIMEOUT,
                env=env,
            )
        return _run_streaming(
            cmd, env, timeout, line_callback, cancel_event
        )


def _run_streaming(
    cmd: List[str],
    env: Dict[str, str],
    timeout: Optional[int],
    line_callback: Optional[Callable[[str], None]],
    cancel_event: Optional[threading.Event] = None,
) -> subprocess.CompletedProcess:
    """Ejecuta un proceso y entrega su salida línea a línea.

    Se usa un hilo lector para no bloquear el pipe si el proceso escribe mucho.
    El proceso se sondea cada 200 ms para poder atender una cancelación sin
    esperar al timeout completo. Al expirar o cancelarse se mata y se propaga
    la excepción correspondiente. La copia en memoria se acota a
    ``MAX_OUTPUT_BUFFER_LINES`` líneas: el callback sigue recibiendo todas.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    lines: deque = deque(maxlen=max(100, int(MAX_OUTPUT_BUFFER_LINES)))

    def _reader() -> None:
        stream = process.stdout
        if stream is None:  # pragma: no cover - Popen siempre lo crea
            return
        try:
            for line in iter(stream.readline, ""):
                lines.append(line)
                if line_callback is None:
                    continue
                try:
                    line_callback(line.rstrip("\r\n"))
                except Exception:  # pragma: no cover - salvaguarda
                    logger.exception("Error en el callback de salida de Ansible")
        finally:
            try:
                stream.close()
            except OSError:  # pragma: no cover - cierre defensivo
                pass

    reader = threading.Thread(
        target=_reader, name="ansible-output", daemon=True
    )
    reader.start()

    limit = timeout or PLAYBOOK_TIMEOUT
    deadline = time.monotonic() + limit
    returncode: Optional[int] = None
    while returncode is None:
        try:
            returncode = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                process.kill()
                process.wait()
                reader.join(timeout=2)
                raise JobCancelled("Operación cancelada por el usuario.")
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                reader.join(timeout=2)
                raise subprocess.TimeoutExpired(cmd, limit)

    reader.join(timeout=2)
    return subprocess.CompletedProcess(
        cmd, returncode, stdout="".join(lines), stderr=""
    )


def _decode_b64_marker(output: str, marker: str) -> Optional[Dict[str, Any]]:
    """Decodifica un marcador ``<marker>_B64=<base64-json>``."""
    match = re.search(rf"{re.escape(marker)}_B64=([A-Za-z0-9+/=]+)", output)
    if not match:
        return None
    try:
        raw = base64.b64decode(match.group(1)).decode("utf-8")
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _decode_json_marker(output: str, marker: str) -> Optional[Dict[str, Any]]:
    """Compatibilidad con salida no codificada: ``<marker>={json}``."""
    match = re.search(rf"{re.escape(marker)}=(\{{.*\}})", output)
    if not match:
        return None
    raw = match.group(1).replace('\\"', '"').replace("\\\\", "\\")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _decode_state(output: str, marker: str) -> Optional[Dict[str, Any]]:
    """Lee un estado publicado por el playbook (base64 o JSON plano)."""
    return _decode_b64_marker(output, marker) or _decode_json_marker(
        output, marker
    )


def _decode_text_marker(output: str, marker: str) -> str:
    """Lee un marcador de texto/base64; devuelve cadena vacía si no existe."""
    b64_match = re.search(rf"{re.escape(marker)}_B64=([A-Za-z0-9+/=]*)", output)
    if b64_match:
        token = b64_match.group(1)
        if not token:
            return ""
        try:
            return base64.b64decode(token).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    text_match = re.search(rf"{re.escape(marker)}=([^\r\n\"]+)", output)
    return text_match.group(1).strip() if text_match else ""


def _decode_bool_marker(output: str, marker: str) -> Optional[bool]:
    text = _decode_text_marker(output, marker).strip().lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return None


def _epoch_for(value: str, time_mode: str) -> int:
    """Convierte una hora ``AAAA-MM-DDTHH:MM`` a epoch si el modo es UTC."""
    if time_mode != "utc":
        return 0
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return 0
    return int(parsed.replace(tzinfo=timezone.utc).timestamp())


def _missing_playbook(host: str, path: str) -> Dict[str, Any]:
    logger.error("Playbook not found: %s", path)
    return _result(
        host,
        False,
        error=f"Playbook no encontrado: {path}",
        playbook_path=path,
    )


def _playbook_error(
    host: str, path: str, process: subprocess.CompletedProcess
) -> str:
    return (
        process.stderr.strip()
        or process.stdout.strip()
        or "El playbook falló."
    )


def run_ansible_playbook(
    host: str,
    user: str,
    action: str,
    value: str,
    password: Optional[str] = None,
    playbook_path: str = None,
    timeout: int = None,
    line_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    private_key: Optional[str] = None,
    connection: Optional[str] = None,
) -> Dict[str, Any]:
    """Ejecuta el playbook de configuración de hora contra un host.

    Se conserva por compatibilidad con el flujo anterior. En modo ``DRY_RUN``
    no se toca ningún equipo: se devuelve un resultado simulado.
    """
    path = str(playbook_path or PLAYBOOK_FILE)

    if not os.path.exists(path):
        return _missing_playbook(host, path)

    if DRY_RUN:
        logger.info("DRY RUN: se omite el playbook %s en %s", path, host)
        _emit_line(
            line_callback,
            f"[DRY RUN] Se habría ejecutado {path} en {host} "
            f"(acción={action}, valor={value}, usuario={user})",
        )
        return _result(
            host,
            True,
            output=(
                f"[DRY RUN] Se habría ejecutado {path} en {host} "
                f"(acción={action}, valor={value}, usuario={user})"
            ),
            ok_count=0,
            changed_count=0,
            dry_run=True,
            simulated=True,
            playbook_path=path,
        )

    if not password:
        return _result(
            host,
            False,
            error="No se proporcionó ninguna contraseña candidata.",
            playbook_path=path,
        )

    try:
        process = _invoke_playbook(
            host=host,
            user=user,
            password=password,
            playbook_path=path,
            extra_vars={
                "accion": action,
                "valor": value,
                "modo_hora": "utc",
                "valor_epoch": _epoch_for(value, "utc") if action == "TIME" else 0,
                "ntp_policy": "keep",
                "ntp_servers": [],
                "sync_rtc": False,
                "force": False,
                "tolerancia": TIME_VERIFY_TOLERANCE,
                "target_host": host,
                "requiere_ntp_off": True,
                "local_fold": "",
                "zona_horaria": "",
                "ntp_settle": 0,
            },
            timeout=timeout,
            line_callback=line_callback,
            cancel_event=cancel_event,
            private_key=private_key,
            connection=connection,
        )
    except FileNotFoundError:
        return _result(
            host,
            False,
            error="ansible-playbook no encontrado. Verifica la instalación de Ansible.",
            playbook_path=path,
        )
    except JobCancelled:
        return _result(
            host,
            False,
            error="Operación cancelada por el usuario.",
            playbook_path=path,
        )
    except subprocess.TimeoutExpired:
        return _result(
            host,
            False,
            error=f"Timeout al ejecutar el playbook ({timeout or PLAYBOOK_TIMEOUT}s).",
            playbook_path=path,
        )
    except Exception as exc:  # pragma: no cover - salvaguarda
        logger.exception("Error ejecutando playbook para %s", host)
        return _result(host, False, error=str(exc), playbook_path=path)

    # Compatibilidad con llamadas antiguas: ya no basta con que el proceso
    # termine bien. Igual que ``apply_host``, exige la verificación publicada
    # por el playbook para no reportar como éxito un cambio sin confirmar.
    verified = _decode_bool_marker(process.stdout, "HORA_VERIFIED")
    success = process.returncode == 0 and verified is True
    error = ""
    if process.returncode != 0:
        error = _playbook_error(host, path, process)
    elif verified is False:
        error = (
            _decode_text_marker(process.stdout, "HORA_ERRORS")
            or "La verificación posterior no coincide con el objetivo."
        )
    elif verified is None:
        error = (
            "El playbook no devolvió el resultado de verificación "
            "HORA_VERIFIED."
        )

    return _result(
        host,
        success,
        output=process.stdout.strip(),
        error=error,
        playbook_path=path,
    )


def inspect_host(
    host: str,
    user: str,
    password: Optional[str] = None,
    timeout: int = None,
    line_callback: Optional[Callable[[str], None]] = None,
    force_real: bool = False,
    cancel_event: Optional[threading.Event] = None,
    private_key: Optional[str] = None,
    connection: Optional[str] = None,
) -> Dict[str, Any]:
    """Lee el estado de hora/zona horaria/NTP de un host sin cambiar nada.

    Devuelve ``state`` con el diccionario publicado por el playbook o ``None``
    si no se pudo obtener.

    Con ``connection == "winrm"`` inspecciona un host Windows con su playbook
    equivalente (solo soporta la zona horaria de momento).

    La inspección es de solo lectura, así que la vista previa la ejecuta de
    verdad incluso en ``DRY_RUN`` (``force_real=True``). Simularla haría que
    hosts inalcanzables aparecieran como inspeccionados correctamente.
    """
    path = str(
        WINDOWS_INSPECT_PLAYBOOK_FILE
        if connection == "winrm"
        else INSPECT_PLAYBOOK_FILE
    )

    if not os.path.exists(path):
        return _missing_playbook(host, path)

    if DRY_RUN and not force_real:
        logger.info("DRY RUN: se omite la inspección de %s", host)
        _emit_line(
            line_callback, f"[DRY RUN] Se habría inspeccionado {host}"
        )
        return _result(
            host,
            True,
            output=f"[DRY RUN] Se habría inspeccionado {host}",
            dry_run=True,
            state=None,
            playbook_path=path,
        )

    if not password:
        return _result(
            host,
            False,
            error="No se proporcionó ninguna contraseña candidata.",
            playbook_path=path,
        )

    try:
        process = _invoke_playbook(
            host=host,
            user=user,
            password=password,
            playbook_path=path,
            extra_vars={},
            timeout=timeout,
            line_callback=line_callback,
            cancel_event=cancel_event,
            private_key=private_key,
            connection=connection,
        )
    except FileNotFoundError:
        return _result(
            host,
            False,
            error="ansible-playbook no encontrado. Verifica la instalación de Ansible.",
            playbook_path=path,
        )
    except JobCancelled:
        return _result(
            host,
            False,
            error="Operación cancelada por el usuario.",
            playbook_path=path,
        )
    except subprocess.TimeoutExpired:
        return _result(
            host,
            False,
            error=f"Timeout al inspeccionar el host ({timeout or PLAYBOOK_TIMEOUT}s).",
            playbook_path=path,
        )
    except Exception as exc:  # pragma: no cover - salvaguarda
        logger.exception("Error inspeccionando %s", host)
        return _result(host, False, error=str(exc), playbook_path=path)

    state = _decode_state(process.stdout, "HORA_STATE")
    success = process.returncode == 0 and state is not None
    error = ""
    if process.returncode != 0:
        error = _playbook_error(host, path, process)
    elif state is None:
        error = "El playbook de inspección no devolvió el estado HORA_STATE."

    return _result(
        host,
        success,
        output=process.stdout.strip(),
        error=error,
        state=state,
        playbook_path=path,
    )


def apply_host(
    host: str,
    user: str,
    action: str,
    value: str,
    password: Optional[str] = None,
    time_mode: str = "utc",
    private_key: Optional[str] = None,
    ntp_policy: str = "keep",
    ntp_servers: Optional[List[str]] = None,
    sync_rtc: bool = False,
    force: bool = False,
    tolerance: Optional[int] = None,
    timeout: int = None,
    line_callback: Optional[Callable[[str], None]] = None,
    local_fold: Optional[int] = None,
    time_zone: str = "",
    requiere_ntp_off: bool = True,
    ntp_settle: int = 0,
    target_epoch: Optional[int] = None,
    cancel_event: Optional[threading.Event] = None,
    connection: Optional[str] = None,
    backup_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Aplica el cambio en un host y verifica el resultado.

    Devuelve, además del éxito, ``before_state``, ``after_state``, ``changed``,
    ``verified``, ``warnings`` y ``deviation``. La verificación se considera
    parte del éxito: un playbook que termina pero no deja el estado esperado
    se reporta como fallo.

    Con ``connection == "winrm"`` aplica el playbook de Windows (equivalente).
    """
    path = str(
        WINDOWS_APPLY_PLAYBOOK_FILE if connection == "winrm" else PLAYBOOK_FILE
    )

    if not os.path.exists(path):
        return _missing_playbook(host, path)

    if DRY_RUN:
        logger.info("DRY RUN: se omite el cambio real en %s", host)
        _emit_line(
            line_callback,
            f"[DRY RUN] Se habría aplicado {action}={value} en {host} "
            f"(modo={time_mode}, ntp={ntp_policy})",
        )
        return _result(
            host,
            True,
            output=(
                f"[DRY RUN] Se habría aplicado {action}={value} en {host} "
                f"(modo={time_mode}, ntp={ntp_policy})"
            ),
            dry_run=True,
            simulated=True,
            # No se ha aplicado ni verificado nada en el host: no se debe
            # presentar como un cambio confirmado.
            changed=False,
            verified=False,
            before_state=None,
            after_state=None,
            warnings="",
            deviation=None,
            playbook_path=path,
        )

    if not password:
        return _result(
            host,
            False,
            error="No se proporcionó ninguna contraseña candidata.",
            playbook_path=path,
        )

    extra_vars = {
        "accion": action,
        "valor": value,
        "modo_hora": time_mode,
        "valor_epoch": int(target_epoch)
        if target_epoch is not None
        else _epoch_for(value, time_mode),
        "ntp_policy": ntp_policy,
        "ntp_servers": list(ntp_servers or []),
        "sync_rtc": bool(sync_rtc),
        "force": bool(force),
        "tolerancia": int(
            tolerance if tolerance is not None else TIME_VERIFY_TOLERANCE
        ),
        "target_host": host,
        "local_fold": "" if local_fold is None else int(local_fold),
        "zona_horaria": time_zone or "",
        "requiere_ntp_off": bool(requiere_ntp_off),
        "ntp_settle": max(0, int(ntp_settle or 0)),
    }
    if backup_dir:
        # Solo Linux: el playbook hace fetch de /etc/timezone.bak. En Windows
        # no existe ese archivo (el estado previo queda en la base de datos).
        if connection != "winrm":
            os.makedirs(backup_dir, exist_ok=True)
            extra_vars["acc_backup_dir"] = backup_dir

    try:
        process = _invoke_playbook(
            host=host,
            user=user,
            password=password,
            playbook_path=path,
            extra_vars=extra_vars,
            timeout=timeout,
            line_callback=line_callback,
            cancel_event=cancel_event,
            private_key=private_key,
            connection=connection,
        )
    except FileNotFoundError:
        return _result(
            host,
            False,
            error="ansible-playbook no encontrado. Verifica la instalación de Ansible.",
            playbook_path=path,
        )
    except JobCancelled:
        return _result(
            host,
            False,
            error="Operación cancelada por el usuario.",
            playbook_path=path,
        )
    except subprocess.TimeoutExpired:
        return _result(
            host,
            False,
            error=f"Timeout al ejecutar el playbook ({timeout or PLAYBOOK_TIMEOUT}s).",
            playbook_path=path,
        )
    except Exception as exc:  # pragma: no cover - salvaguarda
        logger.exception("Error aplicando el playbook en %s", host)
        return _result(host, False, error=str(exc), playbook_path=path)

    before_state = _decode_state(process.stdout, "HORA_BEFORE")
    after_state = _decode_state(process.stdout, "HORA_STATE")
    changed = _decode_bool_marker(process.stdout, "HORA_CHANGED")
    verified = _decode_bool_marker(process.stdout, "HORA_VERIFIED")
    errors = _decode_text_marker(process.stdout, "HORA_ERRORS")
    warnings = _decode_text_marker(process.stdout, "HORA_WARNINGS")
    deviation_text = _decode_text_marker(process.stdout, "HORA_DEVIATION")
    deviation = int(deviation_text) if deviation_text.isdigit() else None

    success = process.returncode == 0 and verified is True
    error = ""
    if process.returncode != 0:
        error = _playbook_error(host, path, process)
    elif verified is False:
        error = errors or "La verificación posterior no coincide con el objetivo."
    elif verified is None:
        error = "El playbook no devolvió el resultado de verificación HORA_VERIFIED."

    return _result(
        host,
        success,
        output=process.stdout.strip(),
        error=error,
        before_state=before_state,
        after_state=after_state,
        changed=bool(changed),
        verified=verified,
        warnings=warnings,
        deviation=deviation,
        playbook_path=path,
    )


def run_playbook_simple(
    host: str,
    user: str,
    vault_password: str,
    playbook_path: str = None,
) -> Dict[str, Any]:
    """Ejecución simplificada (mantenida por compatibilidad)."""
    return run_ansible_playbook(
        host=host,
        user=user,
        action="TZ",
        value="UTC",
        password=vault_password,
        playbook_path=playbook_path,
    )
