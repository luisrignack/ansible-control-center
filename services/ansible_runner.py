# services/ansible_runner.py
# Wrapper para ejecutar playbooks de Ansible de forma segura.
#
# La contraseña candidata nunca se pasa por la línea de comandos (visible en
# ``ps``): se escribe en un fichero temporal 0600 y se pasa con
# ``--extra-vars @fichero``. El fichero se borra siempre al terminar.
from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, Optional

from config import (
    ANSIBLE_CFG,
    BIN_ANSIBLE_PLAYBOOK,
    CONNECT_TIMEOUT,
    DRY_RUN,
    HOST_KEY_CHECKING,
    PLAYBOOK_FILE,
    PLAYBOOK_TIMEOUT,
)
from services.secure_temp import secure_temp_file

logger = logging.getLogger(__name__)


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


def ansible_environment() -> Dict[str, str]:
    """Entorno para las llamadas a Ansible, sin secretos."""
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(ANSIBLE_CFG)
    env["ANSIBLE_NOCOLOR"] = "1"
    # Evita que Ansible intente pedir contraseñas interactivamente.
    env["ANSIBLE_HOST_KEY_CHECKING"] = "True" if HOST_KEY_CHECKING else "False"
    return env


def run_ansible_playbook(
    host: str,
    user: str,
    action: str,
    value: str,
    password: Optional[str] = None,
    playbook_path: str = None,
    timeout: int = None,
) -> Dict[str, Any]:
    """Ejecuta el playbook de configuración de hora contra un host.

    En modo ``DRY_RUN`` no se toca ningún equipo: se devuelve un resultado
    simulado para poder probar el flujo completo.
    """
    path = str(playbook_path or PLAYBOOK_FILE)

    if not os.path.exists(path):
        logger.error("Playbook not found: %s", path)
        return _result(
            host,
            False,
            error=f"Playbook no encontrado: {path}",
            playbook_path=path,
        )

    if DRY_RUN:
        logger.info("DRY RUN: se omite el playbook %s en %s", path, host)
        return _result(
            host,
            True,
            output=(
                f"[DRY RUN] Se habría ejecutado {path} en {host} "
                f"(acción={action}, valor={value}, usuario={user})"
            ),
            ok_count=1,
            changed_count=1,
            dry_run=True,
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
        "target_host": host,
        "ansible_password": password,
        "ansible_become_password": password,
    }

    try:
        with secure_temp_file(
            json.dumps(extra_vars), prefix=".ansible-vars-", suffix=".json"
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
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or PLAYBOOK_TIMEOUT,
                env=ansible_environment(),
            )
    except FileNotFoundError:
        return _result(
            host,
            False,
            error="ansible-playbook no encontrado. Verifica la instalación de Ansible.",
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

    success = process.returncode == 0
    return _result(
        host,
        success,
        output=process.stdout.strip(),
        error="" if success else (process.stderr.strip() or "El playbook falló."),
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
