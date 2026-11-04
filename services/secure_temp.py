# services/secure_temp.py
# Utilidades para manejar secretos efímeros en disco de forma segura.
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from typing import Iterator, Optional

from config import TEMP_DIR


def _base_dir() -> Optional[str]:
    """Devuelve un directorio 0700 para secretos temporales.

    Se prefiere ``TEMP_DIR`` (dentro de ``SECRETS_DIR``). Si no existe o no se
    puede crear (desarrollo sin permisos), se usa el directorio temporal del
    sistema; el fichero seguirá creándose con permisos ``0600``.
    """
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(TEMP_DIR, 0o700)
        return str(TEMP_DIR)
    except OSError:
        return None


@contextmanager
def secure_temp_file(
    content: str,
    prefix: str = "ansible-control-center-",
    suffix: str = ".tmp",
    directory: Optional[str] = None,
) -> Iterator[str]:
    """Crea un fichero temporal 0600, lo entrega y lo borra siempre.

    El contenido nunca se registra en logs. Se usa para el fichero de
    contraseña de ``ansible-vault`` y para las variables de conexión de
    Ansible (``ansible_password``).
    """
    fd, path = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=directory if directory is not None else _base_dir(),
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


@contextmanager
def temporary_directory(
    prefix: str = "ansible-control-center-",
    directory: Optional[str] = None,
) -> Iterator[str]:
    """Crea un directorio temporal 0700 y lo elimina siempre."""
    path = tempfile.mkdtemp(
        prefix=prefix,
        dir=directory if directory is not None else _base_dir(),
    )
    try:
        os.chmod(path, 0o700)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
