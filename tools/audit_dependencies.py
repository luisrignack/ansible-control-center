#!/usr/bin/env python3
"""Audita dependencias con pip-audit usando Nexus y fallback a PyPI directo.

Modos:

* Por defecto audita el entorno instalado (``--local``): es rápido, incluye
  dependencias transitivas y no necesita resolver nada contra un índice.
* ``--resolve`` resuelve ``requirements.txt`` contra el primer índice
  alcanzable. El orden es: índice configurado en pip (por ejemplo Nexus) y,
  si no responde, PyPI directo. Si el índice configurado responde pero la
  auditoría falla por un problema de infraestructura, se reintenta con PyPI.

Códigos de salida: los de pip-audit (0 = sin vulnerabilidades, 1 = hallazgos).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRECT_INDEX = "https://pypi.org/simple"


def parse_index_list(output: str) -> str:
    """Extrae ``index-url`` de la salida de ``pip config list``."""
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in {"global.index-url", "index-url"}:
            return value.strip().strip("'\"")
    return ""


def configured_index() -> str:
    """Índice configurado en pip, o PyPI directo si no hay configuración."""
    env = os.environ.get("PIP_INDEX_URL", "").strip()
    if env:
        return env
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "config", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return DIRECT_INDEX
    # ``pip config get`` no reconoce la clave en algunas versiones (pip 26),
    # así que se parsea ``pip config list``, que sí la muestra.
    return parse_index_list(result.stdout) or DIRECT_INDEX


def candidate_indexes(configured: str | None = None) -> list[str]:
    """Índices únicos en orden: configurado primero, PyPI directo después."""
    configured = (configured or configured_index()).strip()
    candidates: list[str] = []
    for url in (configured, DIRECT_INDEX):
        if url and url not in candidates:
            candidates.append(url)
    return candidates


def index_reachable(url: str, timeout: float = 5.0) -> bool:
    """True si el índice responde HTTP, aunque sea con 401/403/404.

    Un error HTTP demuestra que la ruta de red funciona; solo los timeouts y
    los fallos de conexión se consideran inaccesibles.
    """
    probe = url.rstrip("/") + "/"
    request = urllib.request.Request(
        probe,
        headers={"User-Agent": "ansible-control-center-pip-audit/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def choose_index(indexes: list[str], timeout: float = 5.0) -> str | None:
    """Primer índice alcanzable; ``None`` si ninguno responde."""
    for url in indexes:
        if index_reachable(url, timeout=timeout):
            return url
    return None


def audit_command(
    index_url: str | None,
    requirements: Path | None,
    local: bool,
) -> list[str]:
    command = [sys.executable, "-m", "pip_audit", "--progress-spinner", "off"]
    if local:
        command.append("--local")
    if requirements is not None:
        command += ["-r", str(requirements)]
    if index_url:
        command += ["--index-url", index_url]
    return command


def _run(command: list[str], index_url: str | None = None) -> int:
    """Ejecuta pip-audit propagando el índice también al pip anidado.

    pip-audit crea un venv temporal y ejecuta ``pip install --upgrade pip``
    sin ``--index-url``; sin esta variable volvería a leer ``/etc/pip.conf``
    (Nexus) y el fallback a PyPI no serviría.
    """
    env = os.environ.copy()
    if index_url:
        env["PIP_INDEX_URL"] = index_url
    return subprocess.run(command, check=False, env=env).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audita dependencias con pip-audit y fallback de índice."
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="resolver requirements contra el índice (con fallback a PyPI)",
    )
    parser.add_argument(
        "--requirements",
        default="requirements.txt",
        help="fichero de requisitos para --resolve (por defecto: requirements.txt)",
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=5.0,
        help="segundos de espera al comprobar cada índice",
    )
    args = parser.parse_args(argv)

    if importlib.util.find_spec("pip_audit") is None:
        print(
            "[pip-audit] No está instalado. Instálalo con: "
            "python -m pip install pip-audit",
            file=sys.stderr,
        )
        return 2

    if not args.resolve:
        print(
            "[pip-audit] Auditando el entorno instalado (--local).",
            file=sys.stderr,
        )
        return _run(audit_command(None, None, local=True))

    requirements = Path(args.requirements)
    if not requirements.is_absolute():
        requirements = ROOT / requirements
    if not requirements.exists():
        print(f"[pip-audit] No existe {requirements}", file=sys.stderr)
        return 2

    chosen = choose_index(candidate_indexes(), timeout=args.probe_timeout)
    if chosen is None:
        print(
            "[pip-audit] Ningún índice respondió; se audita el entorno local.",
            file=sys.stderr,
        )
        return _run(audit_command(None, None, local=True))

    print(f"[pip-audit] Índice seleccionado: {chosen}", file=sys.stderr)
    code = _run(audit_command(chosen, requirements, local=False), index_url=chosen)
    if code not in (0, 1) and chosen != DIRECT_INDEX:
        print(
            "[pip-audit] Fallo de infraestructura con el índice configurado; "
            f"reintentando con {DIRECT_INDEX}.",
            file=sys.stderr,
        )
        code = _run(
            audit_command(DIRECT_INDEX, requirements, local=False),
            index_url=DIRECT_INDEX,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
