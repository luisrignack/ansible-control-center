"""Configuración común de pytest.

Fija rutas temporales ANTES de importar ``config`` para que las pruebas no
toquen ``/etc``, ``/var/lib`` ni ``/var/log``.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="hora-admin-tests-"))

os.environ.setdefault("HORA_ADMIN_SECRETS_DIR", str(_TMP / "secrets"))
os.environ.setdefault("HORA_ADMIN_DATA_DIR", str(_TMP / "data"))
os.environ.setdefault("HORA_ADMIN_LOG_DIR", str(_TMP / "logs"))
os.environ.setdefault("HORA_ADMIN_SECRET_KEY", "test-secret-key-no-produccion")
os.environ.setdefault("HORA_ADMIN_DRY_RUN", "1")
os.environ.setdefault("HORA_ADMIN_DEBUG", "0")
