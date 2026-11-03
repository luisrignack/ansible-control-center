# tests/test_ansible_runner.py
# Pruebas de inyección segura de credenciales en Ansible.
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from services import ansible_runner, password_tester


def _capture_vars_file(captured, cmd, **kwargs):
    index = cmd.index("--extra-vars")
    argument = cmd[index + 1]
    assert argument.startswith("@")
    vars_path = argument[1:]
    with open(vars_path, "r", encoding="utf-8") as handle:
        captured["vars"] = json.load(handle)
    captured["vars_path"] = vars_path
    captured["cmd"] = cmd
    captured["kwargs"] = kwargs
    return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")


def test_dry_run_does_not_invoke_ansible(monkeypatch):
    monkeypatch.setattr(ansible_runner, "DRY_RUN", True)

    def fail(*_args, **_kwargs):
        raise AssertionError("No debe ejecutarse Ansible en DRY_RUN")

    monkeypatch.setattr(ansible_runner.subprocess, "run", fail)
    result = ansible_runner.run_ansible_playbook(
        host="10.0.0.1",
        user="root",
        action="TZ",
        value="UTC",
        password="clave",
    )
    assert result["success"] is True
    assert result["dry_run"] is True


def test_password_goes_to_secure_extra_vars_file(monkeypatch):
    monkeypatch.setattr(ansible_runner, "DRY_RUN", False)
    captured = {}

    def fake_run(cmd, **kwargs):
        return _capture_vars_file(captured, cmd, **kwargs)

    monkeypatch.setattr(ansible_runner.subprocess, "run", fake_run)
    result = ansible_runner.run_ansible_playbook(
        host="10.0.0.1",
        user="root",
        action="TZ",
        value="Europe/Madrid",
        password="clave-super-secreta",
    )

    assert result["success"] is True
    assert captured["vars"]["ansible_password"] == "clave-super-secreta"
    assert captured["vars"]["ansible_become_password"] == "clave-super-secreta"
    assert captured["vars"]["accion"] == "TZ"
    assert captured["vars"]["valor"] == "Europe/Madrid"
    assert "clave-super-secreta" not in " ".join(captured["cmd"])
    assert not os.path.exists(captured["vars_path"])
    assert captured["kwargs"]["env"]["ANSIBLE_NOCOLOR"] == "1"


def test_missing_password_fails_without_invoking_ansible(monkeypatch):
    monkeypatch.setattr(ansible_runner, "DRY_RUN", False)

    def fail(*_args, **_kwargs):
        raise AssertionError("No debe ejecutarse Ansible sin contraseña")

    monkeypatch.setattr(ansible_runner.subprocess, "run", fail)
    result = ansible_runner.run_ansible_playbook(
        host="10.0.0.1",
        user="root",
        action="TZ",
        value="UTC",
    )
    assert result["success"] is False
    assert "contraseña" in result["error"].lower()


def test_ping_uses_extra_vars_and_cleans_up(monkeypatch):
    monkeypatch.setattr(password_tester, "DRY_RUN", False)
    captured = {}

    def fake_run(cmd, **kwargs):
        return _capture_vars_file(captured, cmd, **kwargs)

    monkeypatch.setattr(password_tester.subprocess, "run", fake_run)
    result = password_tester.test_ansible_ping(
        host="10.0.0.2", user="ubuntu", password="clave-ping"
    )

    assert result["success"] is True
    assert captured["vars"]["ansible_password"] == "clave-ping"
    assert "clave-ping" not in " ".join(captured["cmd"])
    assert not os.path.exists(captured["vars_path"])


def test_ping_nonzero_exit_is_failure(monkeypatch):
    monkeypatch.setattr(password_tester, "DRY_RUN", False)

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 255, stdout="", stderr="UNREACHABLE"
        )

    monkeypatch.setattr(password_tester.subprocess, "run", fake_run)
    result = password_tester.test_ansible_ping(
        host="10.0.0.3", user="ubuntu", password="clave"
    )
    assert result["success"] is False
    assert "UNREACHABLE" in result["error"]


def test_streaming_callback_receives_ansible_output(tmp_path, monkeypatch):
    script = tmp_path / "fake_playbook.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            print("PLAY [hora]")
            print("TASK [leer estado]")
            print("ok: [10.0.0.1]")
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ansible_runner, "BIN_ANSIBLE_PLAYBOOK", sys.executable)
    lines = []

    process = ansible_runner._invoke_playbook(
        host="10.0.0.1",
        user="root",
        password="clave-secreta",
        playbook_path=str(script),
        extra_vars={},
        timeout=10,
        line_callback=lines.append,
    )

    assert process.returncode == 0
    assert "PLAY [hora]" in process.stdout
    assert "TASK [leer estado]" in process.stdout
    assert lines == ["PLAY [hora]", "TASK [leer estado]", "ok: [10.0.0.1]"]


def test_streaming_timeout_kills_process(tmp_path, monkeypatch):
    script = tmp_path / "slow_playbook.py"
    script.write_text(
        "import time\nprint('inicio', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ansible_runner, "BIN_ANSIBLE_PLAYBOOK", sys.executable)
    lines = []

    with pytest.raises(subprocess.TimeoutExpired):
        ansible_runner._invoke_playbook(
            host="10.0.0.1",
            user="root",
            password="clave",
            playbook_path=str(script),
            extra_vars={},
            timeout=0.2,
            line_callback=lines.append,
        )

    assert lines == ["inicio"]
