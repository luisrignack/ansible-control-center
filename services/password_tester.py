# services/password_tester.py
# Prueba de contraseñas candidatas contra hosts mediante Ansible.
from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

from config import (
    BIN_ANSIBLE,
    CONNECT_TIMEOUT,
    DRY_RUN,
    PING_TIMEOUT,
)
from models.password import PasswordRecord
from services.ansible_runner import (
    ansible_environment,
    run_ansible_playbook as _run_playbook,
)
from services.host_resolver import resolve_target
from services.secure_temp import secure_temp_file
from services.vault_manager import VaultManager

logger = logging.getLogger(__name__)


def test_passwords_single(
    target_ip: str,
    admin_user: str,
    password_origin: str,
    master_password: str,
    action: str,
    value: str,
    temporal_passwords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Prueba contraseñas candidatas contra uno o varios hosts.

    Se conserva como API de servicio; la interfaz usa su propio flujo con
    progreso SSE. Nunca se registran contraseñas en claro.
    """
    result: Dict[str, Any] = {
        "success": False,
        "password_hash": "",
        "overall_result": "unknown",
        "hosts_tested": [],
        "details": [],
    }

    hosts = resolve_target(target_ip)
    if not hosts:
        result["overall_result"] = "error"
        result["details"].append(f"No se pudieron resolver hosts desde {target_ip}")
        return result

    if password_origin == "stored":
        ok, message, entries = VaultManager.decrypt_vault_file(
            master_password=master_password
        )
        if not ok:
            result["overall_result"] = "error"
            result["details"].append(f"Error al descifrar vault: {message}")
            return result
        candidates = [entry["password"] for entry in entries or []]
    elif password_origin == "temporal":
        candidates = temporal_passwords or []
    else:
        result["overall_result"] = "error"
        result["details"].append("Origen de contraseñas no válido.")
        return result

    if not candidates:
        result["overall_result"] = "error"
        result["details"].append("No hay contraseñas candidatas.")
        return result

    for host in hosts:
        result["hosts_tested"].append(host)
        for password in candidates:
            short_hash = PasswordRecord.hash_short(password)
            ping = test_ansible_ping(host, admin_user, password)
            if not ping.get("success"):
                result["details"].append(
                    f"Host {host}: ping falló con hash {short_hash}"
                )
                continue

            playbook_result = run_ansible_playbook(
                host=host,
                user=admin_user,
                password=password,
                action=action,
                value=value,
            )
            result["playbook_result"] = playbook_result
            if playbook_result.get("success"):
                result["success"] = True
                result["password_hash"] = short_hash
                result["overall_result"] = "success"
                result["details"].append(
                    f"Contraseña válida para {host} (hash: {short_hash})"
                )
                return result

            result["overall_result"] = "failure"
            result["details"].append(
                f"El playbook falló en {host} con hash {short_hash}"
            )
            return result

    result["overall_result"] = "failure"
    result["details"].append("Ninguna contraseña funcionó para los hosts objetivo.")
    return result


def test_ansible_ping(
    host: str,
    user: str,
    password: str,
    timeout: int = None,
) -> Dict[str, Any]:
    """Ejecuta ``ansible -m ping`` con la contraseña candidata.

    La contraseña se pasa en un fichero temporal 0600 mediante
    ``--extra-vars @fichero``, no como argumento de línea de comandos.
    """
    result: Dict[str, Any] = {"success": False, "host": host, "error": ""}

    if DRY_RUN:
        return {
            "success": True,
            "host": host,
            "output": "[DRY RUN] ping simulado",
            "error": "",
            "dry_run": True,
        }

    if not password:
        result["error"] = "No se proporcionó contraseña."
        return result

    try:
        with secure_temp_file(
            json.dumps({"ansible_password": password}),
            prefix=".ansible-ping-",
            suffix=".json",
        ) as vars_file:
            cmd = [
                BIN_ANSIBLE,
                host,
                "-m",
                "ping",
                "-u",
                user,
                "--connect-timeout",
                str(CONNECT_TIMEOUT),
                "-o",
                "--extra-vars",
                f"@{vars_file}",
            ]
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or PING_TIMEOUT,
                env=ansible_environment(),
            )
    except subprocess.TimeoutExpired:
        result["error"] = f"Timeout after {timeout or PING_TIMEOUT}s"
        return result
    except FileNotFoundError:
        result["error"] = (
            "Comando ansible no encontrado. Verifique que Ansible esté instalado."
        )
        return result
    except Exception as exc:  # pragma: no cover - salvaguarda
        result["error"] = str(exc)
        return result

    if process.returncode == 0:
        result["success"] = True
        result["output"] = process.stdout
    else:
        # Un código de salida distinto de cero NUNCA es un éxito.
        result["error"] = (
            process.stderr.strip() or process.stdout.strip() or "Sin salida"
        )
    return result


def run_ansible_playbook(
    host: str,
    user: str,
    password: str,
    action: str,
    value: str,
) -> Dict[str, Any]:
    """Ejecuta el playbook de configuración de hora con una contraseña."""
    return _run_playbook(
        host=host,
        user=user,
        password=password,
        action=action,
        value=value,
    )


def resolve_hosts(target_ip: str) -> List[str]:
    """Resolver una IP o rango CIDR a lista de hosts."""
    return resolve_target(target_ip)
