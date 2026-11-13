"""Versión de la aplicación y commit de compilación.

La fuente única de verdad es el archivo ``VERSION`` de la raíz del proyecto.
El paquete de despliegue, el ``Dockerfile`` y el script de exportación leen ese
mismo archivo para que la versión de la aplicación, el tag de Git y el tag de
la imagen Docker no puedan divergir.

``ACC_BUILD_COMMIT`` se inyecta en tiempo de construcción de la imagen. En
desarrollo queda vacío y la interfaz simplemente omite el dato.
"""

from __future__ import annotations

import os
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_version() -> str:
    """Lee ``VERSION``; si falta o está vacío, no impide arrancar."""
    try:
        raw = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return raw or "0.0.0"


__version__ = _read_version()
BUILD_COMMIT = os.environ.get("ACC_BUILD_COMMIT", "").strip()

__all__ = ["BUILD_COMMIT", "__version__"]
