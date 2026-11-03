"""Configuración central de Hora Admin.

Los valores por defecto cumplen la ubicación exigida en producción:

* Secretos cifrados: ``/etc/hora-admin/secrets`` (modo 0700).
* Base de datos SQLite: ``/var/lib/hora-admin/hora_admin.db``.
* Logs: ``/var/log/hora-admin``.

Todas las rutas y parámetros sensibles pueden sobrescribirse con variables de
entorno (o con un archivo ``.env``) para desarrollo y pruebas. Nunca se guarda
la contraseña maestra del Vault en este módulo ni en disco.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import shutil
import sys
import time
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"

# Carga opcional de .env. ``override=False`` respeta el entorno real (systemd,
# gunicorn, tests) y evita que un .env de desarrollo pise la configuración.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependencia opcional en tests aislados
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env", override=False)


# ---------------------------------------------------------------------------
# Utilidades internas para leer el entorno con valores por defecto seguros
# ---------------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    """Convierte una variable de entorno en booleano de forma tolerante."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


def _env_int(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Lee un entero del entorno, validando límites opcionales."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un número entero, se recibió {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} no puede ser menor que {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} no puede ser mayor que {maximum}")
    return value


def _env_path(name: str, default: str | Path) -> Path:
    """Lee una ruta del entorno y la normaliza a ruta absoluta."""
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


def _find_binary(name: str, env_var: str) -> str:
    """Localiza un binario de Ansible priorizando el venv en ejecución.

    Así funciona tanto con el venv activado como con gunicorn arrancado por
    systemd, donde ``PATH`` puede no incluir ``.venv/bin``.
    """
    override = os.environ.get(env_var)
    if override:
        return override

    venv_candidate = Path(sys.executable).with_name(name)
    if venv_candidate.exists():
        return str(venv_candidate)

    return shutil.which(name) or name


# ---------------------------------------------------------------------------
# Rutas principales
# ---------------------------------------------------------------------------
SECRETS_DIR = _env_path("HORA_ADMIN_SECRETS_DIR", "/etc/hora-admin/secrets")
DATA_DIR = _env_path("HORA_ADMIN_DATA_DIR", "/var/lib/hora-admin")
LOG_DIR = _env_path("HORA_ADMIN_LOG_DIR", "/var/log/hora-admin")

# Archivo cifrado con ansible-vault. Es el único lugar donde viven las
# contraseñas candidatas, siempre cifradas con la contraseña maestra.
VAULT_FILENAME = "passwords_vault.yml"
VAULT_FILE = SECRETS_DIR / VAULT_FILENAME

DB_PATH = DATA_DIR / "hora_admin.db"
LOG_FILE = LOG_DIR / "hora-admin.log"

# Directorio efímero (0700) para ficheros de contraseña del Vault que exige
# ``ansible-vault --vault-password-file``. Se borra tras cada operación.
TEMP_DIR = SECRETS_DIR / ".tmp"

ANSIBLE_DIR = BASE_DIR / "ansible"
PLAYBOOK_FILE = ANSIBLE_DIR / "cambiar_hora.yml"
INSPECT_PLAYBOOK_FILE = ANSIBLE_DIR / "estado_hora.yml"
ANSIBLE_CFG = ANSIBLE_DIR / "ansible.cfg"

BIN_ANSIBLE = _find_binary("ansible", "HORA_ADMIN_ANSIBLE_BIN")
BIN_ANSIBLE_PLAYBOOK = _find_binary("ansible-playbook", "HORA_ADMIN_ANSIBLE_PLAYBOOK_BIN")
BIN_ANSIBLE_VAULT = _find_binary("ansible-vault", "HORA_ADMIN_ANSIBLE_VAULT_BIN")
BIN_ANSIBLE_GALAXY = _find_binary("ansible-galaxy", "HORA_ADMIN_ANSIBLE_GALAXY_BIN")

# ---------------------------------------------------------------------------
# Flask / sesiones / autenticación
# ---------------------------------------------------------------------------
# La clave se resuelve con ``get_or_create_secret_key()`` al arrancar la app.
SECRET_KEY = os.environ.get("HORA_ADMIN_SECRET_KEY", "")
SECRET_KEY_FILE = DATA_DIR / "flask_secret.key"

# El depurador de Flask NUNCA debe quedar activo por defecto: expone una
# consola interactiva con ejecución de código en la red.
DEBUG = _env_bool("HORA_ADMIN_DEBUG", False)

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Detrás de un proxy inverso con HTTPS poner HORA_ADMIN_COOKIE_SECURE=1.
SESSION_COOKIE_SECURE = _env_bool("HORA_ADMIN_COOKIE_SECURE", False)
REMEMBER_COOKIE_HTTPONLY = True
REMEMBER_COOKIE_SAMESITE = "Lax"
REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
REMEMBER_COOKIE_DURATION_SECONDS = _env_int(
    "HORA_ADMIN_REMEMBER_DAYS", 7, minimum=1, maximum=30
) * 86400
PERMANENT_SESSION_LIFETIME_SECONDS = _env_int(
    "HORA_ADMIN_SESSION_HOURS", 8, minimum=1, maximum=72
) * 3600

# Pepper para los hashes de contraseñas candidatas guardados en SQLite. Si no
# se define, se genera un secreto local 0600 en DATA_DIR.
PASSWORD_PEPPER = os.environ.get("HORA_ADMIN_PASSWORD_PEPPER", "")
PASSWORD_PEPPER_FILE = DATA_DIR / "password_pepper.key"

MAX_CONTENT_LENGTH = _env_int(
    "HORA_ADMIN_MAX_UPLOAD_BYTES", 2 * 1024 * 1024, minimum=1024
)

# ---------------------------------------------------------------------------
# Protección CSRF
# ---------------------------------------------------------------------------
# Todos los POST/PUT/PATCH/DELETE exigen un token ligado a la sesión. El token
# caduca para limitar la ventana de reutilización de una página abierta.
CSRF_ENABLED = _env_bool("HORA_ADMIN_CSRF_ENABLED", True)
CSRF_TIME_LIMIT_SECONDS = _env_int(
    "HORA_ADMIN_CSRF_TIME_LIMIT", 8 * 3600, minimum=300, maximum=86400
)

# ---------------------------------------------------------------------------
# Rate limiting (en memoria, por proceso)
# ---------------------------------------------------------------------------
RATE_LIMIT_ENABLED = _env_bool("HORA_ADMIN_RATE_LIMIT_ENABLED", True)

# Intentos de login por IP dentro de la ventana.
LOGIN_RATE_LIMIT = _env_int(
    "HORA_ADMIN_LOGIN_RATE_LIMIT", 10, minimum=1, maximum=1000
)
LOGIN_RATE_WINDOW = _env_int(
    "HORA_ADMIN_LOGIN_RATE_WINDOW", 300, minimum=10, maximum=86400
)

# Ejecuciones de Ansible por usuario/IP.
EXEC_RATE_LIMIT = _env_int(
    "HORA_ADMIN_EXEC_RATE_LIMIT", 10, minimum=1, maximum=1000
)
EXEC_RATE_WINDOW = _env_int(
    "HORA_ADMIN_EXEC_RATE_WINDOW", 60, minimum=10, maximum=86400
)

# Operaciones sobre el vault (cifrar, importar, borrar).
VAULT_RATE_LIMIT = _env_int(
    "HORA_ADMIN_VAULT_RATE_LIMIT", 20, minimum=1, maximum=1000
)
VAULT_RATE_WINDOW = _env_int(
    "HORA_ADMIN_VAULT_RATE_WINDOW", 60, minimum=10, maximum=86400
)

# ---------------------------------------------------------------------------
# Proxy inverso y cabeceras de seguridad
# ---------------------------------------------------------------------------
# Activar solo si la app está detrás de un proxy de confianza: habilita
# ProxyFix para leer X-Forwarded-For/Proto. Sin proxy, dejar en 0.
TRUST_PROXY = _env_bool("HORA_ADMIN_TRUST_PROXY", False)

SECURITY_HEADERS_ENABLED = _env_bool("HORA_ADMIN_SECURITY_HEADERS", True)
CSP_ENABLED = _env_bool("HORA_ADMIN_CSP_ENABLED", True)
HSTS_ENABLED = _env_bool("HORA_ADMIN_HSTS", SESSION_COOKIE_SECURE)
HSTS_MAX_AGE = _env_int(
    "HORA_ADMIN_HSTS_MAX_AGE", 31536000, minimum=0, maximum=63072000
)

ADMIN_USERNAME = os.environ.get("HORA_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_MIN_LENGTH = _env_int(
    "HORA_ADMIN_ADMIN_PASSWORD_MIN_LENGTH", 12, minimum=8, maximum=128
)

# ---------------------------------------------------------------------------
# Parámetros de ejecución
# ---------------------------------------------------------------------------
# DRY RUN activado por defecto: al arrancar no se toca ningún equipo real.
DRY_RUN = _env_bool("HORA_ADMIN_DRY_RUN", True)

# Verificación estricta de la clave SSH del host. Si los equipos objetivo no
# están en known_hosts, el primer acceso fallará. Se puede desactivar de forma
# consciente con HORA_ADMIN_HOST_KEY_CHECKING=0 (menos seguro).
HOST_KEY_CHECKING = _env_bool("HORA_ADMIN_HOST_KEY_CHECKING", True)

# Límites de seguridad
MAX_HOSTS = _env_int("HORA_ADMIN_MAX_HOSTS", 256, minimum=1, maximum=4096)
MAX_PASSWORDS = _env_int("HORA_ADMIN_MAX_PASSWORDS", 200, minimum=1, maximum=1000)
MAX_PASSWORD_LENGTH = _env_int(
    "HORA_ADMIN_MAX_PASSWORD_LENGTH", 512, minimum=8, maximum=4096
)

# Timeouts de Ansible (segundos)
PING_TIMEOUT = _env_int("HORA_ADMIN_PING_TIMEOUT", 30, minimum=5, maximum=300)
PLAYBOOK_TIMEOUT = _env_int("HORA_ADMIN_PLAYBOOK_TIMEOUT", 300, minimum=30, maximum=3600)
CONNECT_TIMEOUT = _env_int("HORA_ADMIN_CONNECT_TIMEOUT", 10, minimum=3, maximum=120)

# Retención del historial y del progreso en memoria
HISTORY_RETENTION_DAYS = _env_int("HORA_ADMIN_HISTORY_DAYS", 180, minimum=1)
JOB_RETENTION_HOURS = _env_int("HORA_ADMIN_JOB_RETENTION_HOURS", 24, minimum=1)

# Extensión máxima de un rango CIDR aceptado (p. ej. /24 -> 254 hosts).
MIN_CIDR_PREFIX = _env_int("HORA_ADMIN_MIN_CIDR_PREFIX", 22, minimum=8, maximum=32)


def _parse_allowed_networks(raw: str) -> tuple:
    """Convierte ``HORA_ADMIN_ALLOWED_NETWORKS`` en redes validadas.

    Vacío significa sin restricción (comportamiento histórico). Una entrada
    inválida es un error de configuración: es mejor no arrancar que aplicar una
    allowlist incompleta sin avisar.
    """
    networks = []
    for chunk in re.split(r"[,\s]+", (raw or "").strip()):
        if not chunk:
            continue
        try:
            networks.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"HORA_ADMIN_ALLOWED_NETWORKS contiene una red inválida: "
                f"{chunk!r}"
            ) from exc
    return tuple(networks)


# Allowlist opcional de redes de destino. Si está vacía, no se restringe.
ALLOWED_NETWORKS = _parse_allowed_networks(
    os.environ.get("HORA_ADMIN_ALLOWED_NETWORKS", "")
)

# ---------------------------------------------------------------------------
# P4: ejecución en grupo, verificación e idempotencia
# ---------------------------------------------------------------------------
# Diferencia máxima aceptada entre la hora objetivo y la hora final del host.
TIME_VERIFY_TOLERANCE = _env_int(
    "HORA_ADMIN_TIME_TOLERANCE", 120, minimum=0, maximum=3600
)

# Políticas NTP:
#   keep      -> no tocar la configuración NTP existente.
#   off       -> desactivar NTP (queda desactivado).
#   restore   -> desactivar, fijar la hora y restaurar el estado NTP previo.
#   configure -> configurar servidores NTP y dejar NTP activo.
NTP_POLICIES: tuple[str, ...] = ("keep", "off", "restore", "configure")
MAX_NTP_SERVERS = _env_int(
    "HORA_ADMIN_MAX_NTP_SERVERS", 8, minimum=1, maximum=32
)
DEFAULT_NTP_SERVERS: tuple[str, ...] = tuple(
    server.strip()
    for server in os.environ.get(
        "HORA_ADMIN_DEFAULT_NTP_SERVERS", "pool.ntp.org"
    ).split(",")
    if server.strip()
)

# Extensiones permitidas para archivos de contraseñas
ALLOWED_PASSWORD_EXTENSIONS = {".txt", ".yml", ".yaml"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("HORA_ADMIN_LOG_LEVEL", "INFO").upper()
LOG_TO_FILE = _env_bool("HORA_ADMIN_LOG_TO_FILE", True)
LOG_MAX_BYTES = _env_int("HORA_ADMIN_LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=1024)
LOG_BACKUP_COUNT = _env_int("HORA_ADMIN_LOG_BACKUPS", 5, minimum=1, maximum=50)

# ---------------------------------------------------------------------------
# Zonas horarias sugeridas para el datalist del formulario
# ---------------------------------------------------------------------------
COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    "Europe/Madrid",
    "Europe/Lisbon",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Rome",
    "Europe/Amsterdam",
    "Europe/Brussels",
    "Europe/Zurich",
    "Europe/Vienna",
    "Europe/Warsaw",
    "Europe/Athens",
    "Europe/Istanbul",
    "Europe/Moscow",
    "Atlantic/Canary",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Mexico_City",
    "America/Bogota",
    "America/Lima",
    "America/Santiago",
    "America/Argentina/Buenos_Aires",
    "America/Sao_Paulo",
    "America/Caracas",
    "America/Panama",
    "America/Santo_Domingo",
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Singapore",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Seoul",
    "Australia/Sydney",
    "Pacific/Auckland",
    "Africa/Cairo",
    "Africa/Johannesburg",
    "Africa/Casablanca",
)


# ---------------------------------------------------------------------------
# Funciones de soporte
# ---------------------------------------------------------------------------
def ensure_directories() -> None:
    """Crea los directorios de trabajo con permisos restrictivos.

    Se invoca al arrancar la app. Si no hay permisos (por ejemplo, se ejecuta
    sin haber lanzado ``install.sh``), se lanza un error explicativo en lugar
    de continuar con una configuración insegura.
    """
    required = (
        (SECRETS_DIR, 0o700),
        (DATA_DIR, 0o750),
        (LOG_DIR, 0o750),
    )
    for path, mode in required:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
        except PermissionError as exc:
            raise RuntimeError(
                f"No hay permisos para crear {path}. Ejecuta ./install.sh o "
                "define HORA_ADMIN_SECRETS_DIR/HORA_ADMIN_DATA_DIR/HORA_ADMIN_LOG_DIR "
                "a rutas de desarrollo."
            ) from exc


def is_within_secrets_dir(path: str | os.PathLike[str]) -> bool:
    """Comprueba que una ruta resuelta está dentro de SECRETS_DIR.

    Se usa como defensa contra path traversal en cualquier endpoint que reciba
    nombres de archivo del usuario.
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
        secrets_root = SECRETS_DIR.resolve(strict=False)
    except OSError:
        return False
    return resolved == secrets_root or secrets_root in resolved.parents


def _is_writable_dir(path: Path) -> bool:
    """Comprueba de forma segura si se puede crear/escribir en un directorio."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".hora-admin-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_db_path() -> Path:
    """Devuelve una ruta de SQLite utilizable.

    En producción usa ``DATA_DIR`` (``/var/lib/hora-admin``). Si no hay
    permisos —por ejemplo al ejecutar ``python app.py`` como usuario normal
    sin haber lanzado ``install.sh``— cae a ``instance/`` para que el entorno
    de desarrollo arranque sin sudo.
    """
    if _is_writable_dir(DATA_DIR):
        return DB_PATH
    if _is_writable_dir(INSTANCE_DIR):
        return INSTANCE_DIR / "hora_admin.db"
    raise RuntimeError(
        f"No hay ningún directorio escribible para la base de datos "
        f"({DATA_DIR} ni {INSTANCE_DIR})."
    )


def resolve_vault_file() -> Path:
    """Devuelve una ruta de vault utilizable.

    En producción usa ``SECRETS_DIR``. En desarrollo sin permisos cae a
    ``instance/secrets/`` para que el flujo de vault funcione sin ``sudo``.
    """
    if _is_writable_dir(SECRETS_DIR):
        return VAULT_FILE
    fallback = INSTANCE_DIR / "secrets"
    if _is_writable_dir(fallback):
        return fallback / VAULT_FILENAME
    raise RuntimeError(
        f"No hay ningún directorio escribible para el vault "
        f"({SECRETS_DIR} ni {fallback})."
    )


def get_or_create_secret_key() -> str:
    """Devuelve la clave de sesión de Flask, creándola si no existe.

    Prioridad: variable de entorno > archivo local 0600 > clave nueva. Si
    ``DATA_DIR`` no es escribible se intenta en ``instance/`` (desarrollo).
    La clave no es la contraseña maestra del Vault ni se registra en logs.
    """
    if SECRET_KEY:
        return SECRET_KEY

    for key_file in (SECRET_KEY_FILE, INSTANCE_DIR / "flask_secret.key"):
        try:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            if key_file.exists():
                key = key_file.read_text(encoding="utf-8").strip()
                if key:
                    return key
            key = secrets.token_hex(32)
            key_file.write_text(key + "\n", encoding="utf-8")
            os.chmod(key_file, 0o600)
            return key
        except OSError:
            continue

    # Último recurso: clave efímera (las sesiones se invalidan al reiniciar).
    logging.getLogger("hora_admin").warning(
        "No se pudo persistir la clave de sesión; se usará una clave efímera."
    )
    return secrets.token_hex(32)


@lru_cache(maxsize=1)
def get_or_create_password_pepper() -> str:
    """Devuelve el pepper usado en los hashes de contraseñas candidatas.

    Prioridad: variable de entorno > archivo local 0600 > valor efímero. El
    archivo se crea con ``O_EXCL`` para tolerar varios workers arrancando a la
    vez: solo uno escribe y el resto lee el mismo secreto.
    """
    if PASSWORD_PEPPER:
        return PASSWORD_PEPPER

    candidates = (PASSWORD_PEPPER_FILE, INSTANCE_DIR / "password_pepper.key")
    for pepper_file in candidates:
        try:
            pepper_file.parent.mkdir(parents=True, exist_ok=True)
            if pepper_file.exists():
                pepper = pepper_file.read_text(encoding="utf-8").strip()
                if pepper:
                    return pepper
            pepper = secrets.token_hex(32)
            try:
                fd = os.open(
                    pepper_file,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                # Otro worker ganó la carrera; puede tardar un instante en
                # escribir el valor, así que se reintenta la lectura.
                for _ in range(5):
                    pepper = pepper_file.read_text(
                        encoding="utf-8"
                    ).strip()
                    if pepper:
                        return pepper
                    time.sleep(0.05)
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(pepper + "\n")
            except OSError:
                continue
            return pepper
        except OSError:
            continue

    logging.getLogger("hora_admin").warning(
        "No se pudo persistir el pepper de contraseñas; se usará uno efímero "
        "y los hashes cambiarán al reiniciar."
    )
    return secrets.token_hex(32)


def setup_logging() -> logging.Logger:
    """Configura el logger raíz de la aplicación.

    No hay ningún handler que reciba contraseñas: los servicios deben registrar
    únicamente hashes cortos e identificadores.
    """
    logger = logging.getLogger("hora_admin")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if LOG_TO_FILE:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                LOG_FILE,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            logger.warning("No se pudo abrir el log %s: %s", LOG_FILE, exc)

    return logger
