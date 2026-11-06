# tests/test_ssh_host_keys.py
# Pruebas del provisionamiento opcional de known_hosts (sin red).
from __future__ import annotations

import concurrent.futures
import subprocess

import config as config_module
import services.ssh_host_keys as host_keys


def _completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["ssh-keyscan"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_known_hosts_path_honors_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-known-hosts"
    monkeypatch.setattr(config_module, "KNOWN_HOSTS_FILE", str(override))
    assert host_keys.known_hosts_path() == override


def test_known_hosts_path_defaults_to_home_ssh(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "KNOWN_HOSTS_FILE", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert host_keys.known_hosts_path() == tmp_path / ".ssh" / "known_hosts"


def test_scan_host_keys_builds_a_valid_ssh_keyscan_command(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return _completed("10.0.0.5 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIfake\n")

    monkeypatch.setattr(host_keys.subprocess, "run", fake_run)
    keys = host_keys.scan_host_keys("10.0.0.5", timeout=5, max_retries=1)

    assert seen["cmd"] == [
        "ssh-keyscan",
        "-p",
        "22",
        "-T",
        "5",
        "-t",
        "rsa,ed25519,ecdsa",
        "10.0.0.5",
    ]
    assert keys == [
        ("10.0.0.5", "ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIfake")
    ]


def test_scan_host_keys_rejects_option_injection(monkeypatch):
    called = []

    def fake_run(cmd, **kwargs):  # pragma: no cover - no debe llamarse
        called.append(cmd)
        return _completed()

    monkeypatch.setattr(host_keys.subprocess, "run", fake_run)
    assert host_keys.scan_host_keys("-oProxyCommand=evil") == []
    assert called == []


def test_update_known_hosts_is_idempotent_and_0600(tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text("10.0.0.1 ssh-ed25519 OLD\n", encoding="utf-8")

    key = ("10.0.0.5", "ssh-ed25519", "NEW")
    host_keys.update_known_hosts([key], path=path)
    host_keys.update_known_hosts([key], path=path)

    text = path.read_text(encoding="utf-8")
    assert "10.0.0.1 ssh-ed25519 OLD" in text
    assert text.count("10.0.0.5 ssh-ed25519 NEW") == 1
    assert (path.stat().st_mode & 0o777) == 0o600


def test_ensure_host_keys_skips_already_registered(monkeypatch, tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text("10.0.0.5 ssh-ed25519 PRESENT\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "KNOWN_HOSTS_FILE", str(path))
    monkeypatch.setattr(
        host_keys,
        "scan_host_keys",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe escanear")),
    )

    assert host_keys.ensure_host_keys(["10.0.0.5"]) == {"10.0.0.5": True}


def test_ensure_host_keys_scans_and_registers(monkeypatch, tmp_path):
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(config_module, "KNOWN_HOSTS_FILE", str(path))
    monkeypatch.setattr(
        host_keys,
        "scan_host_keys",
        lambda host, *a, **k: [(host, "ssh-ed25519", "NEWKEY")],
    )

    result = host_keys.ensure_host_keys(["10.0.0.5", "10.0.0.6"])

    assert result == {"10.0.0.5": True, "10.0.0.6": True}
    text = path.read_text(encoding="utf-8")
    assert "10.0.0.5 ssh-ed25519 NEWKEY" in text
    assert "10.0.0.6 ssh-ed25519 NEWKEY" in text


def test_ensure_host_keys_reports_unreachable(monkeypatch, tmp_path):
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(config_module, "KNOWN_HOSTS_FILE", str(path))
    monkeypatch.setattr(host_keys, "scan_host_keys", lambda host, *a, **k: [])

    assert host_keys.ensure_host_keys(["10.0.0.9"]) == {"10.0.0.9": False}
    assert not path.exists()


def test_update_known_hosts_takes_interprocess_lock(monkeypatch, tmp_path):
    path = tmp_path / "known_hosts"
    calls = []
    real_flock = host_keys.fcntl.flock

    def fake_flock(fd, operation):
        calls.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(host_keys.fcntl, "flock", fake_flock)
    host_keys.update_known_hosts(
        [("10.0.0.5", "ssh-ed25519", "NEW")], path=path
    )

    assert host_keys.fcntl.LOCK_EX in calls
    assert path.with_name(path.name + ".lock").exists()


def test_concurrent_known_hosts_updates_keep_all_keys(tmp_path):
    path = tmp_path / "known_hosts"
    keys = [
        (f"10.0.1.{index}", "ssh-ed25519", f"KEY{index}")
        for index in range(1, 21)
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda key: host_keys.update_known_hosts([key], path=path), keys))

    text = path.read_text(encoding="utf-8")
    for host, _key_type, key in keys:
        assert f"{host} ssh-ed25519 {key}" in text
