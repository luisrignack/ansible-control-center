# tests/test_ssh_container.py
# Prueba de integración opcional contra un contenedor SSH efímero.
#
# No se ejecuta por defecto: requiere Docker y la variable
# ACC_RUN_SSH_TESTS=1, porque descarga/arranca una imagen y tarda
# varios segundos. En CI sin Docker simplemente se omite.
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest

from services import ansible_runner, password_tester

RUN_SSH_TESTS = os.environ.get("ACC_RUN_SSH_TESTS") == "1"
HAS_DOCKER = shutil.which("docker") is not None

pytestmark = pytest.mark.skipif(
    not (RUN_SSH_TESTS and HAS_DOCKER),
    reason=(
        "Prueba de integración SSH desactivada "
        "(define ACC_RUN_SSH_TESTS=1 y ten Docker disponible)"
    ),
)


def _docker(*args, check=True):
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=check,
    )


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise AssertionError(f"El contenedor SSH no abrió {host}:{port}")


@pytest.fixture()
def ssh_container(monkeypatch):
    image = os.environ.get(
        "ACC_SSH_TEST_IMAGE", "linuxserver/openssh-server:latest"
    )
    password = "ansible-control-center-test-password"
    name = f"ansible-control-center-ssh-{uuid.uuid4().hex[:8]}"

    started = _docker(
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        "PASSWORD_ACCESS=true",
        "-e",
        "USER_NAME=hora",
        "-e",
        f"USER_PASSWORD={password}",
        "-p",
        "127.0.0.1::2222",
        image,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(
            "No se pudo arrancar el contenedor SSH "
            f"({started.stderr.strip()[:200]})"
        )

    try:
        mapping = _docker("port", name, "2222/tcp").stdout.strip()
        host, port_text = mapping.rsplit(":", 1)
        host = host.strip()
        port = int(port_text.strip())
        _wait_for_port(host, port)

        # El comando de ping no acepta un puerto explícito; se pasa a OpenSSH
        # mediante ANSIBLE_SSH_ARGS (permitido por el entorno mínimo).
        monkeypatch.setenv(
            "ANSIBLE_SSH_ARGS",
            (
                f"-o Port={port} -o StrictHostKeyChecking=no "
                "-o UserKnownHostsFile=/dev/null"
            ),
        )
        monkeypatch.setattr(ansible_runner, "HOST_KEY_CHECKING", False)
        monkeypatch.setattr(password_tester, "DRY_RUN", False)

        yield {"host": host, "port": port, "user": "hora", "password": password}
    finally:
        _docker("rm", "-f", name, check=False)


def test_ansible_ping_against_real_ssh_container(ssh_container):
    result = password_tester.test_ansible_ping(
        host=ssh_container["host"],
        user=ssh_container["user"],
        password=ssh_container["password"],
        timeout=60,
    )
    assert result["success"] is True, result
    assert "ping" in result.get("output", "").lower()
