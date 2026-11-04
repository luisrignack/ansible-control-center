# services/vault_manager.py
# Gestión real del vault con la CLI de ansible-vault.
#
# Formato interno: el vault es un fichero cifrado con una contraseña por línea
# (las líneas vacías y las que empiezan por '#' se ignoran). Se cifra/descifra
# siempre con ``ansible-vault``; la contraseña maestra nunca se guarda en disco
# ni se registra en logs.
from __future__ import annotations

import fcntl
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from config import (
    BIN_ANSIBLE_VAULT,
    MAX_PASSWORDS,
    MAX_PASSWORD_LENGTH,
    resolve_vault_file,
)
from models.password import PasswordRecord
from services.secure_temp import secure_temp_file, temporary_directory

VAULT_HEADER = "$ANSIBLE_VAULT"
VAULT_TIMEOUT = 60


@contextmanager
def _vault_lock(vault_file: str) -> Iterator[None]:
    """Exclusión mutua entre procesos para las escrituras del vault.

    ``ansible-vault`` reescribe el fichero completo, así que dos operaciones
    concurrentes (dos peticiones, un CLI y la interfaz) podrían perder
    contraseñas. El bloqueo se toma sobre un fichero ``.lock`` contiguo para no
    bloquear las lecturas y sobrevivir a los reemplazos atómicos del vault.
    """
    path = Path(vault_file)
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _normalize_passwords(passwords: List[str]) -> List[str]:
    """Limpia, valida y deduplica contraseñas conservando el orden."""
    normalized: List[str] = []
    seen = set()
    for raw in passwords:
        password = (raw or "").strip()
        if not password or password.startswith("#"):
            continue
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError(
                f"Una contraseña supera el máximo de {MAX_PASSWORD_LENGTH} caracteres."
            )
        if password not in seen:
            seen.add(password)
            normalized.append(password)
            if len(normalized) > MAX_PASSWORDS:
                raise ValueError(
                    f"El vault no puede superar {MAX_PASSWORDS} contraseñas."
                )
    return normalized


def _parse_plaintext(content: str) -> List[Dict[str, Any]]:
    """Convierte el contenido descifrado en entradas con hashes."""
    entries: List[Dict[str, Any]] = []
    for line in content.splitlines():
        password = line.strip()
        if not password or password.startswith("#"):
            continue
        hash_full = PasswordRecord.hash_full(password)
        entries.append(
            {
                "index": len(entries) + 1,
                "password": password,
                "original": password,  # compatibilidad con versiones anteriores
                "hash_short": hash_full[:8],
                "hash_full": hash_full,
            }
        )
    return entries


def _vault_error(result: subprocess.CompletedProcess) -> str:
    """Mensaje de error legible sin exponer secretos."""
    detail = (result.stderr or result.stdout or "").strip()
    lowered = detail.lower()
    if "decrypt" in lowered or "invalid vault password" in lowered or "vault password" in lowered:
        return "Contraseña maestra incorrecta o vault corrupto."
    if "not found" in lowered or "no such file" in lowered:
        return "ansible-vault no encontrado. Instala ansible-core."
    if len(detail) > 300:
        detail = detail[:300] + "…"
    return detail or "ansible-vault terminó con error."


class VaultManager:
    """Cifra y descifra el vault usando la CLI de ansible-vault."""

    @staticmethod
    def _get_master_password() -> str:
        """Solicita la contraseña maestra por terminal (solo uso CLI)."""
        import getpass

        return getpass.getpass("Ingrese la contraseña maestra del Vault: ")

    @staticmethod
    def vault_path() -> str:
        """Ruta efectiva del vault (producción o ``instance/secrets``)."""
        return str(resolve_vault_file())

    @staticmethod
    def encrypt_passwords(
        passwords: List[str],
        master_password: str,
        output_file: str = None,
        origin: str = "manual",
        append: bool = True,
        db_path: str = None,
    ) -> Tuple[bool, str]:
        """Cifra contraseñas y actualiza el vault de forma atómica y segura.

        La lectura-modificación-escritura se hace bajo un bloqueo de fichero
        para que dos operaciones concurrentes no se pisen.
        """
        if output_file is None:
            try:
                output_file = VaultManager.vault_path()
            except RuntimeError as exc:
                return False, str(exc)
        try:
            with _vault_lock(output_file):
                return VaultManager._encrypt_passwords_unlocked(
                    passwords=passwords,
                    master_password=master_password,
                    output_file=output_file,
                    origin=origin,
                    append=append,
                    db_path=db_path,
                )
        except OSError as exc:
            return False, f"No se pudo bloquear el vault: {exc}"

    @staticmethod
    def _encrypt_passwords_unlocked(
        passwords: List[str],
        master_password: str,
        output_file: str = None,
        origin: str = "manual",
        append: bool = True,
        db_path: str = None,
    ) -> Tuple[bool, str]:
        """Cifra contraseñas y actualiza el vault de forma atómica.

        Si ``append`` es ``True`` y el vault ya existe, se descifra con la
        misma contraseña maestra, se combinan las entradas sin duplicados y se
        vuelve a cifrar todo. El fichero final se escribe con permisos ``0600``.

        Returns:
            ``(éxito, mensaje)``. El mensaje nunca contiene contraseñas.
        """
        if not master_password:
            return False, "La contraseña maestra del Vault es obligatoria."

        try:
            new_passwords = _normalize_passwords(passwords)
        except ValueError as exc:
            return False, str(exc)

        if not new_passwords:
            return False, "No hay contraseñas válidas (solo vacíos y comentarios)."

        if output_file is None:
            try:
                output_file = VaultManager.vault_path()
            except RuntimeError as exc:
                return False, str(exc)

        output_path = Path(output_file)
        try:
            parent_existed = output_path.parent.exists()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if not parent_existed:
                os.chmod(output_path.parent, 0o700)
        except OSError as exc:
            return False, f"No se puede escribir el vault en {output_path.parent}: {exc}"

        existing: List[str] = []
        if append and output_path.exists() and output_path.stat().st_size > 0:
            ok, message, entries = VaultManager.decrypt_vault_file(
                vault_file=str(output_path),
                master_password=master_password,
            )
            if not ok:
                return False, f"No se pudo abrir el vault existente: {message}"
            existing = [entry["password"] for entry in entries or []]

        combined = _normalize_passwords(existing + new_passwords)
        if not combined:
            return False, "No hay contraseñas para guardar."

        ok, message = VaultManager._write_encrypted(
            combined, master_password, output_path
        )
        if not ok:
            return False, message

        # Registrar en SQLite todos los hashes del vault que falten. Así la
        # tabla de la interfaz refleja siempre el contenido real del vault.
        stored = 0
        try:
            for password in combined:
                hash_full = PasswordRecord.hash_full(password)
                if not PasswordRecord.exists_by_hash_full(
                    hash_full, db_path=db_path
                ):
                    PasswordRecord.store_password(
                        password, origin=origin, db_path=db_path
                    )
                    stored += 1
        except Exception:  # pragma: no cover - no impedir el vault por la BD
            # El vault ya está guardado; la UI puede reintentar el registro.
            return True, (
                f"Vault guardado con {len(combined)} contraseña(s), pero no se "
                "pudieron registrar todas en la base de datos."
            )

        return True, (
            f"Vault actualizado: {len(combined)} contraseña(s) en total "
            f"({stored} nueva(s))."
        )

    @staticmethod
    def _write_encrypted(
        passwords: List[str],
        master_password: str,
        output_path: Path,
    ) -> Tuple[bool, str]:
        """Cifra la lista completa y reemplaza el vault de forma atómica."""
        plaintext = "\n".join(passwords) + "\n"

        try:
            # El fichero de salida se crea en un directorio temporal del mismo
            # sistema de ficheros y se mueve con os.replace (atómico).
            with temporary_directory(
                prefix=".vault-out-", directory=str(output_path.parent)
            ) as tmp_dir:
                encrypted_tmp = os.path.join(tmp_dir, "vault.yml")

                with secure_temp_file(
                    plaintext, prefix=".vault-plain-", suffix=".txt"
                ) as plain_file, secure_temp_file(
                    master_password + "\n", prefix=".vault-pass-", suffix=".txt"
                ) as pass_file:
                    result = subprocess.run(
                        [
                            BIN_ANSIBLE_VAULT,
                            "encrypt",
                            "--vault-password-file",
                            pass_file,
                            "--output",
                            encrypted_tmp,
                            plain_file,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=VAULT_TIMEOUT,
                    )

                if result.returncode != 0:
                    return False, f"Error al cifrar el vault: {_vault_error(result)}"

                if not os.path.exists(encrypted_tmp):
                    return False, "ansible-vault no generó el fichero cifrado."

                os.chmod(encrypted_tmp, 0o600)
                os.replace(encrypted_tmp, output_path)
                os.chmod(output_path, 0o600)

        except FileNotFoundError:
            return False, "ansible-vault no encontrado. Instala ansible-core."
        except subprocess.TimeoutExpired:
            return False, f"Timeout al cifrar el vault ({VAULT_TIMEOUT}s)."
        except OSError as exc:
            return False, f"Error de fichero al guardar el vault: {exc}"
        except Exception as exc:  # pragma: no cover - salvaguarda
            return False, f"Error inesperado al cifrar el vault: {exc}"

        return True, ""

    @staticmethod
    def remove_password(
        password_hash_full: str,
        master_password: str,
        vault_file: str = None,
    ) -> Tuple[bool, str]:
        """Elimina del vault la contraseña con ese hash completo.

        La operación se serializa con el resto de escrituras del vault.
        """
        if not master_password:
            return False, "La contraseña maestra del Vault es obligatoria."

        if vault_file is None:
            try:
                vault_file = VaultManager.vault_path()
            except RuntimeError as exc:
                return False, str(exc)

        try:
            with _vault_lock(vault_file):
                return VaultManager._remove_password_unlocked(
                    password_hash_full, master_password, vault_file
                )
        except OSError as exc:
            return False, f"No se pudo bloquear el vault: {exc}"

    @staticmethod
    def _remove_password_unlocked(
        password_hash_full: str,
        master_password: str,
        vault_file: str,
    ) -> Tuple[bool, str]:
        """Implementación de ``remove_password`` ya bajo el bloqueo."""
        ok, message, entries = VaultManager.decrypt_vault_file(
            vault_file=vault_file,
            master_password=master_password,
        )
        if not ok:
            return False, message

        entries = entries or []
        remaining = [
            entry
            for entry in entries
            if not PasswordRecord.matches_hash(
                entry.get("password", ""), password_hash_full
            )
        ]
        if len(remaining) == len(entries):
            return False, "No se encontró la contraseña en el vault."

        vault_path = Path(vault_file)
        if not remaining:
            try:
                vault_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                return False, f"No se pudo eliminar el vault vacío: {exc}"
            return True, "Se eliminó la última contraseña; el vault quedó vacío."

        ok, message = VaultManager._write_encrypted(
            [entry["password"] for entry in remaining],
            master_password,
            vault_path,
        )
        if not ok:
            return False, message

        return True, (
            f"Contraseña eliminada del vault ({len(remaining)} restante(s))."
        )

    @staticmethod
    def decrypt_vault_file(
        vault_file: str = None,
        master_password: str = None,
    ) -> Tuple[bool, str, Optional[List[Dict[str, Any]]]]:
        """Descifra el vault en memoria y devuelve las contraseñas.

        Returns:
            ``(éxito, mensaje, entradas)``. Cada entrada contiene ``password``,
            ``original``, ``hash_short`` y ``hash_full``. El texto plano solo
            vive en memoria y nunca se registra.
        """
        if vault_file is None:
            try:
                vault_file = VaultManager.vault_path()
            except RuntimeError as exc:
                return False, str(exc), None

        if master_password is None:
            master_password = VaultManager._get_master_password()

        if not master_password:
            return False, "La contraseña maestra del Vault es obligatoria.", None

        if not os.path.exists(vault_file):
            return False, "El archivo vault no existe.", None

        try:
            with secure_temp_file(
                master_password + "\n", prefix=".vault-pass-", suffix=".txt"
            ) as pass_file:
                result = subprocess.run(
                    [
                        BIN_ANSIBLE_VAULT,
                        "view",
                        "--vault-password-file",
                        pass_file,
                        vault_file,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=VAULT_TIMEOUT,
                )
        except FileNotFoundError:
            return False, "ansible-vault no encontrado. Instala ansible-core.", None
        except subprocess.TimeoutExpired:
            return False, f"Timeout al descifrar el vault ({VAULT_TIMEOUT}s).", None
        except Exception as exc:  # pragma: no cover - salvaguarda
            return False, f"Error inesperado al descifrar: {exc}", None

        if result.returncode != 0:
            return False, _vault_error(result), None

        entries = _parse_plaintext(result.stdout)
        if not entries:
            return False, "El vault no contiene contraseñas.", None

        return True, f"Vault descifrado: {len(entries)} contraseña(s).", entries

    @staticmethod
    def validate_master_password(
        vault_file: str = None,
        test_password: str = None,
    ) -> Tuple[bool, str]:
        """Comprueba que la contraseña maestra puede abrir el vault."""
        if test_password is None:
            test_password = VaultManager._get_master_password()

        success, message, _entries = VaultManager.decrypt_vault_file(
            vault_file=vault_file,
            master_password=test_password,
        )
        return success, message
