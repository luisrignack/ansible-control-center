"""Configuración común de pytest.

Fija rutas temporales ANTES de importar ``config`` para que las pruebas no
toquen ``/etc``, ``/var/lib`` ni ``/var/log``.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="ansible-control-center-tests-"))

os.environ.setdefault("ACC_SECRETS_DIR", str(_TMP / "secrets"))
os.environ.setdefault("ACC_DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("ACC_LOG_DIR", str(_TMP / "logs"))
os.environ.setdefault("ACC_SECRET_KEY", "test-secret-key-no-produccion")
os.environ.setdefault("ACC_DRY_RUN", "1")
os.environ.setdefault("ACC_DEBUG", "0")
# El proceso de pruebas no debe arrancar el scheduler ni ejecutar
# mantenimiento de arranque: cada prueba controla su propia base de datos y
# un hilo de fondo podría reclamar programaciones de forma no determinista.
os.environ.setdefault("ACC_ROLE", "cli")
os.environ.setdefault("ACC_SCHEDULER_ENABLED", "0")


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Evita que los límites en memoria se acumulen entre pruebas."""
    from services.rate_limiter import LIMITER

    LIMITER.reset()
    yield
    LIMITER.reset()
