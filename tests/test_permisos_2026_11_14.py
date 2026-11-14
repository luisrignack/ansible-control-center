"""Permisos de los directorios con clave privada y CA (2026-11-14).

`./certs` lo escribe Nginx (clave privada TLS) y `./certs-ca` solo lo lee la
aplicación (CA de LDAPS). Mezclarlos obliga a elegir entre proteger la clave
o permitir que LDAPS valide el certificado.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import yaml

from tools.check_permissions import check

ROOT = Path(__file__).resolve().parent.parent


def _mkdir(path: Path, mode: int) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path


def test_accepts_recommended_modes(tmp_path):
    _mkdir(tmp_path / "certs", 0o750)
    _mkdir(tmp_path / "certs-ca", 0o750)
    (tmp_path / "certs-ca" / "ca-etecsa.pem").write_text("CA", encoding="utf-8")
    os.chmod(tmp_path / "certs-ca" / "ca-etecsa.pem", 0o640)

    assert check(tmp_path) == []


def test_flags_group_writable_certs(tmp_path):
    _mkdir(tmp_path / "certs", 0o775)
    problems = check(tmp_path)
    assert any("certs" in problem and "0775" in problem for problem in problems)


def test_flags_private_key_readable_by_others(tmp_path):
    _mkdir(tmp_path / "certs", 0o750)
    key = tmp_path / "certs" / "privkey.pem"
    key.write_text("clave", encoding="utf-8")
    os.chmod(key, 0o644)

    problems = check(tmp_path)
    assert any("privkey.pem" in problem for problem in problems)


def test_flags_secrets_not_0600(tmp_path):
    secrets = _mkdir(tmp_path / "secrets.d", 0o700)
    secret = secrets / "acc_secret_key"
    secret.write_text("valor", encoding="utf-8")
    os.chmod(secret, 0o644)

    problems = check(tmp_path)
    assert any("acc_secret_key" in problem for problem in problems)


def test_missing_directories_are_not_an_error(tmp_path):
    assert check(tmp_path) == []


def test_compose_splits_tls_certificates_from_ldaps_ca():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    app_volumes = [str(volume) for volume in services["app"]["volumes"]]
    assert "./certs-ca:/certs:ro" in app_volumes
    assert not any(volume.startswith("./certs:") for volume in app_volumes)

    nginx_volumes = [str(volume) for volume in services["nginx"]["volumes"]]
    assert "./certs:/etc/nginx/certs" in nginx_volumes

    assert (ROOT / "certs-ca" / ".gitkeep").exists()
    assert "certs-ca/*.pem" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "certs-ca" in (ROOT / ".dockerignore").read_text(encoding="utf-8")


def test_tool_is_executable():
    mode = stat.S_IMODE((ROOT / "tools" / "check_permissions.py").stat().st_mode)
    assert mode & stat.S_IXUSR
