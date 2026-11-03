# tests/test_vault_manager.py
# Pruebas del vault real con ansible-vault.
from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from config import BIN_ANSIBLE_VAULT
from models.db import init_schema
from models.password import PasswordRecord
from services.vault_manager import VaultManager

requires_vault = pytest.mark.skipif(
    shutil.which(BIN_ANSIBLE_VAULT) is None,
    reason="ansible-vault no está instalado",
)


@pytest.fixture()
def vault_env(tmp_path):
    db_path = str(tmp_path / "hora_admin.db")
    init_schema(db_path)
    return {
        "vault": str(tmp_path / "passwords_vault.yml"),
        "db": db_path,
    }


def test_encrypt_passwords_empty_list():
    ok, msg = VaultManager.encrypt_passwords([], "test")
    assert ok is False
    assert "No hay" in msg


def test_encrypt_requires_master_password(vault_env):
    ok, msg = VaultManager.encrypt_passwords(
        ["clave"], "", output_file=vault_env["vault"]
    )
    assert ok is False
    assert "maestra" in msg.lower()


@requires_vault
def test_encrypt_decrypt_roundtrip(vault_env):
    ok, msg = VaultManager.encrypt_passwords(
        ["clave-uno", "clave-dos"],
        "master-secreta",
        output_file=vault_env["vault"],
        db_path=vault_env["db"],
    )
    assert ok is True, msg
    assert os.path.exists(vault_env["vault"])
    assert stat.S_IMODE(os.stat(vault_env["vault"]).st_mode) == 0o600

    with open(vault_env["vault"], "r", encoding="utf-8") as handle:
        assert handle.readline().startswith("$ANSIBLE_VAULT")

    ok, msg, entries = VaultManager.decrypt_vault_file(
        vault_env["vault"], "master-secreta"
    )
    assert ok is True, msg
    assert [entry["password"] for entry in entries] == ["clave-uno", "clave-dos"]
    assert entries[0]["hash_short"] == PasswordRecord.hash_short("clave-uno")
    assert PasswordRecord.get_password_count(db_path=vault_env["db"]) == 2


@requires_vault
def test_append_deduplicates_and_preserves_order(vault_env):
    ok, _ = VaultManager.encrypt_passwords(
        ["uno", "dos"],
        "master-secreta",
        output_file=vault_env["vault"],
        db_path=vault_env["db"],
    )
    assert ok

    ok, msg = VaultManager.encrypt_passwords(
        ["dos", "tres"],
        "master-secreta",
        output_file=vault_env["vault"],
        db_path=vault_env["db"],
    )
    assert ok is True, msg

    ok, _, entries = VaultManager.decrypt_vault_file(
        vault_env["vault"], "master-secreta"
    )
    assert ok is True
    assert [entry["password"] for entry in entries] == ["uno", "dos", "tres"]
    # Solo se registran hashes nuevos en SQLite.
    assert PasswordRecord.get_password_count(db_path=vault_env["db"]) == 3


@requires_vault
def test_wrong_master_password_is_rejected(vault_env):
    ok, _ = VaultManager.encrypt_passwords(
        ["clave"], "correcta", output_file=vault_env["vault"]
    )
    assert ok

    valid, message = VaultManager.validate_master_password(
        vault_env["vault"], "incorrecta"
    )
    assert valid is False
    assert "Contraseña maestra incorrecta" in message


@requires_vault
def test_remove_password_from_vault(vault_env):
    ok, _ = VaultManager.encrypt_passwords(
        ["uno", "dos"],
        "master-secreta",
        output_file=vault_env["vault"],
        db_path=vault_env["db"],
    )
    assert ok

    ok, msg = VaultManager.remove_password(
        PasswordRecord.hash_full("uno"),
        "master-secreta",
        vault_file=vault_env["vault"],
    )
    assert ok is True, msg

    ok, _, entries = VaultManager.decrypt_vault_file(
        vault_env["vault"], "master-secreta"
    )
    assert ok is True
    assert [entry["password"] for entry in entries] == ["dos"]


@requires_vault
def test_remove_last_password_deletes_empty_vault(vault_env):
    ok, _ = VaultManager.encrypt_passwords(
        ["unica"], "master-secreta", output_file=vault_env["vault"]
    )
    assert ok

    ok, msg = VaultManager.remove_password(
        PasswordRecord.hash_full("unica"),
        "master-secreta",
        vault_file=vault_env["vault"],
    )
    assert ok is True, msg
    assert not os.path.exists(vault_env["vault"])


def test_remove_unknown_password_reports_error(vault_env, monkeypatch):
    monkeypatch.setattr(
        VaultManager,
        "decrypt_vault_file",
        staticmethod(lambda **_kwargs: (True, "ok", [])),
    )
    ok, msg = VaultManager.remove_password(
        "no-existe", "master", vault_file=vault_env["vault"]
    )
    assert ok is False
    assert "No se encontró" in msg


def test_missing_ansible_vault_is_reported(vault_env, monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, msg = VaultManager.encrypt_passwords(
        ["clave"], "master", output_file=vault_env["vault"]
    )
    assert ok is False
    assert "ansible-vault no encontrado" in msg
