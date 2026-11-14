"""Secretos por fichero en Docker Compose (2026-11-14).

El compose base mantiene las variables de entorno para no romper el arranque
en desarrollo ni las instalaciones existentes. El override
``docker-compose.secrets.yml`` es la vía recomendada en producción: monta los
secretos en ``/run/secrets`` y la aplicación los lee con ``*_FILE``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE = ROOT / "docker-compose.secrets.yml"
GENERATOR = ROOT / "docker" / "generate-secrets.sh"

SECRETS = ("acc_secret_key", "acc_admin_password", "acc_ldap_bind_password")


@pytest.fixture(scope="module")
def override() -> dict:
    return yaml.safe_load(OVERRIDE.read_text(encoding="utf-8"))


def test_override_defines_every_secret(override):
    assert set(override["secrets"]) == set(SECRETS)
    for name, spec in override["secrets"].items():
        assert spec["file"].endswith(f"/{name}")
        assert "secrets.d" in spec["file"]


def test_app_mounts_secrets_and_uses_file_variables(override):
    app_service = override["services"]["app"]
    assert set(app_service["secrets"]) == set(SECRETS)

    environment = app_service["environment"]
    for name in SECRETS:
        variable = name.upper()
        assert environment[f"{variable}_FILE"] == f"/run/secrets/{name}"
        # El valor directo debe quedar vacío: si no, tendría prioridad y
        # volvería a ser visible en `docker inspect`.
        assert environment[variable] == ""


def test_scheduler_receives_the_same_secrets(override):
    scheduler = override["services"]["scheduler"]
    assert set(scheduler["secrets"]) == set(SECRETS)
    assert scheduler["environment"]["ACC_SECRET_KEY_FILE"] == (
        "/run/secrets/acc_secret_key"
    )


def test_generator_is_executable_and_idempotent(tmp_path):
    assert GENERATOR.stat().st_mode & stat.S_IXUSR

    first = subprocess.run(
        [str(GENERATOR), str(tmp_path)], capture_output=True, text=True, check=True
    )
    assert "Creado acc_secret_key" in first.stdout

    for name in SECRETS:
        target = tmp_path / name
        assert target.exists()
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        # La clave y la contraseña deben tener contenido; el marcador de LDAP
        # se crea vacío a propósito.
        assert bool(target.read_text()) == (name != "acc_ldap_bind_password")

    key_before = (tmp_path / "acc_secret_key").read_text()
    subprocess.run(
        [str(GENERATOR), str(tmp_path)], capture_output=True, text=True, check=True
    )
    assert (tmp_path / "acc_secret_key").read_text() == key_before


def test_secrets_directory_is_not_versioned_nor_built():
    assert "secrets.d/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "secrets.d" in dockerignore


def test_env_secret_reads_file_when_value_is_absent(tmp_path, monkeypatch):
    """``config._env_secret`` debe preferir la variable y caer al fichero."""
    secret_file = tmp_path / "clave"
    secret_file.write_text("desde-fichero\n", encoding="utf-8")

    monkeypatch.delenv("ACC_SECRET_KEY", raising=False)
    monkeypatch.setenv("ACC_SECRET_KEY_FILE", str(secret_file))
    import config

    assert config._env_secret("ACC_SECRET_KEY") == "desde-fichero"

    monkeypatch.setenv("ACC_SECRET_KEY", "desde-entorno")
    assert config._env_secret("ACC_SECRET_KEY") == "desde-entorno"

    monkeypatch.delenv("ACC_SECRET_KEY")
    monkeypatch.setenv("ACC_SECRET_KEY_FILE", str(tmp_path / "no-existe"))
    assert config._env_secret("ACC_SECRET_KEY") == ""
    monkeypatch.delenv("ACC_SECRET_KEY_FILE", raising=False)
    assert os.environ.get("ACC_SECRET_KEY_FILE") is None
