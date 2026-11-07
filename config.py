"""Configuración central de Ansible Control Center.

Los valores por defecto cumplen la ubicación exigida en producción:

* Secretos cifrados: ``/etc/ansible-control-center/secrets`` (modo 0700).
* Base de datos SQLite: ``/var/lib/ansible-control-center/acc.db``.
* Logs: ``/var/log/ansible-control-center``.

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

# Compatibilidad con el nombre anterior del producto. Si un despliegue aún
# define HORA_ADMIN_*, se copia a su equivalente ACC_* sin pisar lo explícito.
for _legacy_name, _legacy_value in list(os.environ.items()):
    if _legacy_name.startswith("HORA_ADMIN_"):
        os.environ.setdefault(
            "ACC_" + _legacy_name[len("HORA_ADMIN_") :], _legacy_value
        )


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


def _env_secret(name: str) -> str:
    """Lee un secreto de ``<NAME>`` o, si no existe, de ``<NAME>_FILE``.

    El soporte de fichero permite usar Docker secrets o systemd
    ``LoadCredential`` sin exponer el valor en ``docker inspect`` ni en el
    entorno del proceso.
    """
    value = os.environ.get(name, "")
    if value:
        return value
    file_path = os.environ.get(f"{name}_FILE", "")
    if not file_path:
        return ""
    try:
        return Path(file_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Rutas principales
# ---------------------------------------------------------------------------
ENVIRONMENT = os.environ.get("ACC_ENV", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"

# En producción las rutas configuradas son obligatorias: si no son
# escribibles, la aplicación no debe arrancar con una base o un vault
# "de rebote" en ``instance/``. El fallback es solo para desarrollo.
ALLOW_DEV_FALLBACK = _env_bool(
    "ACC_ALLOW_DEV_FALLBACK", not IS_PRODUCTION
)

# Rol del proceso. Permite separar el servidor web del programador de tareas:
#   * ``all``    -> web + scheduler + mantenimiento (comportamiento histórico).
#   * ``web``    -> solo web; el scheduler se ejecuta en otro proceso.
#   * ``worker`` -> scheduler + mantenimiento (proceso dedicado).
#   * ``cli``    -> ni scheduler ni mantenimiento (comandos de administración).
PROCESS_ROLE = os.environ.get("ACC_ROLE", "all").strip().lower()
if PROCESS_ROLE not in {"all", "web", "worker", "cli"}:
    PROCESS_ROLE = "all"

SCHEDULER_ENABLED = _env_bool("ACC_SCHEDULER_ENABLED", True)

# Una tarea o un lote sin actualizaciones durante este tiempo se considera
# huérfano. Evita que un worker recién arrancado cierre operaciones que otro
# worker está ejecutando legítimamente.
RECOVERY_STALE_MINUTES = _env_int(
    "ACC_RECOVERY_STALE_MINUTES", 15, minimum=1, maximum=1440
)

SECRETS_DIR = _env_path("ACC_SECRETS_DIR", "/etc/ansible-control-center/secrets")
DATA_DIR = _env_path("ACC_DATA_DIR", "/var/lib/ansible-control-center")
LOG_DIR = _env_path("ACC_LOG_DIR", "/var/log/ansible-control-center")

# Archivo cifrado con ansible-vault. Es el único lugar donde viven las
# contraseñas candidatas, siempre cifradas con la contraseña maestra.
VAULT_FILENAME = "passwords_vault.yml"
VAULT_FILE = SECRETS_DIR / VAULT_FILENAME

DB_PATH = DATA_DIR / "acc.db"
LOG_FILE = LOG_DIR / "ansible-control-center.log"

# Directorio efímero (0700) para ficheros de contraseña del Vault que exige
# ``ansible-vault --vault-password-file``. Se borra tras cada operación.
TEMP_DIR = SECRETS_DIR / ".tmp"

ANSIBLE_DIR = BASE_DIR / "ansible"
PLAYBOOK_FILE = ANSIBLE_DIR / "cambiar_hora.yml"
INSPECT_PLAYBOOK_FILE = ANSIBLE_DIR / "estado_hora.yml"
# Playbooks equivalentes para hosts Windows (conexión winrm, módulos
# builtin sobre tzutil; funcionan sin colecciones externas). Se auto-disponen
# cuando el grupo de inventario es Windows.
WINDOWS_APPLY_PLAYBOOK_FILE = ANSIBLE_DIR / "win_cambiar_zona.yml"
WINDOWS_INSPECT_PLAYBOOK_FILE = ANSIBLE_DIR / "win_estado_zona.yml"
ANSIBLE_CFG = ANSIBLE_DIR / "ansible.cfg"

# Copias de seguridad del archivo /etc/timezone. La carpeta vive en el
# volumen de datos de la máquina donde corre el servicio (la PC del usuario
# operador): el playbook trae el .bak de cada estación con ``fetch`` justo
# antes de cambiar la zona. En Windows no existe ese archivo (el estado
# previo queda guardado en la base de datos, columna before_state).
TIMEZONE_BACKUP_DIR = DATA_DIR / "timezone_backups"

# ---------------------------------------------------------------------------
# Windows vía WinRM
# ---------------------------------------------------------------------------
# El transporte por defecto es ``ntlm`` (estaciones en dominio o con usuario
# local; sin TLS explícito). ``basic`` requiere el mismo usuario local pero
# suele necesitar LocalAccountTokenFilterPolicy en workgroup.
WINRM_PORT = _env_int("ACC_WINRM_PORT", 5985, minimum=1, maximum=65535)
WINRM_TRANSPORT = (
    os.environ.get("ACC_WINRM_TRANSPORT", "ntlm").strip().lower() or "ntlm"
)
if WINRM_TRANSPORT not in {"basic", "ntlm", "credssp"}:
    WINRM_TRANSPORT = "ntlm"

# Validación del certificado presentado por WinRM. ``validate`` es el valor
# seguro por defecto; ``ignore`` solo debe activarse de forma explícita para
# laboratorios con certificados autofirmados. Con WinRM sobre HTTP (5985)
# Ansible no usa el certificado, pero el valor se mantiene coherente.
WINRM_SERVER_CERT_VALIDATION = (
    os.environ.get("ACC_WINRM_SERVER_CERT_VALIDATION", "validate")
    .strip()
    .lower()
    or "validate"
)
if WINRM_SERVER_CERT_VALIDATION not in {"validate", "ignore"}:
    WINRM_SERVER_CERT_VALIDATION = "validate"

BIN_ANSIBLE = _find_binary("ansible", "ACC_ANSIBLE_BIN")
BIN_ANSIBLE_PLAYBOOK = _find_binary("ansible-playbook", "ACC_ANSIBLE_PLAYBOOK_BIN")
BIN_ANSIBLE_VAULT = _find_binary("ansible-vault", "ACC_ANSIBLE_VAULT_BIN")
BIN_ANSIBLE_GALAXY = _find_binary("ansible-galaxy", "ACC_ANSIBLE_GALAXY_BIN")

# ---------------------------------------------------------------------------
# Flask / sesiones / autenticación
# ---------------------------------------------------------------------------
# La clave se resuelve con ``get_or_create_secret_key()`` al arrancar la app.
SECRET_KEY = _env_secret("ACC_SECRET_KEY")
SECRET_KEY_FILE = DATA_DIR / "flask_secret.key"

# El depurador de Flask NUNCA debe quedar activo por defecto: expone una
# consola interactiva con ejecución de código en la red.
DEBUG = _env_bool("ACC_DEBUG", False)

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Detrás de un proxy inverso con HTTPS poner ACC_COOKIE_SECURE=1.
# Cookie de sesión solo por HTTPS. En producción es 1 por defecto a menos que
# se fije ACC_COOKIE_SECURE explícitamente (desarrollo sigue en 0: sin HTTPS
# la cookie Secure no se enviaría y nadie podría iniciar sesión).
SESSION_COOKIE_SECURE = _env_bool(
    "ACC_COOKIE_SECURE", default=IS_PRODUCTION
)
REMEMBER_COOKIE_HTTPONLY = True
REMEMBER_COOKIE_SAMESITE = "Lax"
REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
REMEMBER_COOKIE_DURATION_SECONDS = _env_int(
    "ACC_REMEMBER_DAYS", 7, minimum=1, maximum=30
) * 86400
PERMANENT_SESSION_LIFETIME_SECONDS = _env_int(
    "ACC_SESSION_HOURS", 8, minimum=1, maximum=72
) * 3600

# Pepper para los hashes de contraseñas candidatas guardados en SQLite. Si no
# se define, se genera un secreto local 0600 en DATA_DIR.
PASSWORD_PEPPER = _env_secret("ACC_PASSWORD_PEPPER")
PASSWORD_PEPPER_FILE = DATA_DIR / "password_pepper.key"

MAX_CONTENT_LENGTH = _env_int(
    "ACC_MAX_UPLOAD_BYTES", 2 * 1024 * 1024, minimum=1024
)

# ---------------------------------------------------------------------------
# Protección CSRF
# ---------------------------------------------------------------------------
# Todos los POST/PUT/PATCH/DELETE exigen un token ligado a la sesión. El token
# caduca para limitar la ventana de reutilización de una página abierta.
CSRF_ENABLED = _env_bool("ACC_CSRF_ENABLED", True)
CSRF_TIME_LIMIT_SECONDS = _env_int(
    "ACC_CSRF_TIME_LIMIT", 8 * 3600, minimum=300, maximum=86400
)

# ---------------------------------------------------------------------------
# Rate limiting (en memoria, por proceso)
# ---------------------------------------------------------------------------
RATE_LIMIT_ENABLED = _env_bool("ACC_RATE_LIMIT_ENABLED", True)

# Intentos de login por IP dentro de la ventana.
LOGIN_RATE_LIMIT = _env_int(
    "ACC_LOGIN_RATE_LIMIT", 10, minimum=1, maximum=1000
)
LOGIN_RATE_WINDOW = _env_int(
    "ACC_LOGIN_RATE_WINDOW", 300, minimum=10, maximum=86400
)

# Ejecuciones de Ansible por usuario/IP.
EXEC_RATE_LIMIT = _env_int(
    "ACC_EXEC_RATE_LIMIT", 10, minimum=1, maximum=1000
)
EXEC_RATE_WINDOW = _env_int(
    "ACC_EXEC_RATE_WINDOW", 60, minimum=10, maximum=86400
)

# Operaciones sobre el vault (cifrar, importar, borrar).
VAULT_RATE_LIMIT = _env_int(
    "ACC_VAULT_RATE_LIMIT", 20, minimum=1, maximum=1000
)
VAULT_RATE_WINDOW = _env_int(
    "ACC_VAULT_RATE_WINDOW", 60, minimum=10, maximum=86400
)

# ---------------------------------------------------------------------------
# Proxy inverso y cabeceras de seguridad
# ---------------------------------------------------------------------------
# Activar solo si la app está detrás de un proxy de confianza: habilita
# ProxyFix para leer X-Forwarded-For/Proto. Sin proxy, dejar en 0.
TRUST_PROXY = _env_bool("ACC_TRUST_PROXY", False)

SECURITY_HEADERS_ENABLED = _env_bool("ACC_SECURITY_HEADERS", True)
CSP_ENABLED = _env_bool("ACC_CSP_ENABLED", True)
HSTS_ENABLED = _env_bool("ACC_HSTS", SESSION_COOKIE_SECURE)
HSTS_MAX_AGE = _env_int(
    "ACC_HSTS_MAX_AGE", 31536000, minimum=0, maximum=63072000
)

# ---------------------------------------------------------------------------
# LDAPS (autenticación corporativa)
# ---------------------------------------------------------------------------
# Mapea el modelo clásico de directorio (bind de un usuario de solo lectura,
# búsqueda del uid y bind simple con la contraseña del usuario). Las cuentas
# LDAP no guardan contraseña local: en su primer login se crea una cuenta
# sombra con rol ``viewer`` que el admin local promueve en /usuarios.
LDAP_ENABLED = _env_bool("ACC_LDAP_ENABLED", False)
LDAP_URL = os.environ.get("ACC_LDAP_URL", "").strip() or "ldaps://lds.etecsa.cu:636"
# Usuario de solo lectura del directorio (como el ``rouser`` de operaciones).
LDAP_BIND_DN = os.environ.get(
    "ACC_LDAP_BIND_DN", "uid=rouser,ou=Special Users,dc=etecsa,dc=cu"
)
# Se admite en el entorno, o como Docker secret vía ACC_LDAP_BIND_PASSWORD_FILE.
LDAP_BIND_PASSWORD = _env_secret("ACC_LDAP_BIND_PASSWORD")
# Base de búsqueda de usuarios y filtro (mismo modelo que el ejemplo Nuxeo).
LDAP_USER_BASE = os.environ.get(
    "ACC_LDAP_USER_BASE", "ou=etecsa.cu,ou=people,dc=etecsa,dc=cu"
)
LDAP_USER_FILTER = os.environ.get("ACC_LDAP_USER_FILTER", "(objectClass=person)")
LDAP_USER_SCOPE = (
    os.environ.get("ACC_LDAP_USER_SCOPE", "onelevel").strip().lower()
)
if LDAP_USER_SCOPE not in ("onelevel", "subtree"):
    LDAP_USER_SCOPE = "onelevel"
# Atributo que contiene el nombre de usuario (uid en este directorio).
LDAP_USERNAME_ATTR = os.environ.get("ACC_LDAP_USERNAME_ATTR", "uid").strip()
# Límites de la búsqueda (como ``query.sizeLimit``/``timeLimit`` del ejemplo).
LDAP_SEARCH_SIZE_LIMIT = _env_int("ACC_LDAP_SEARCH_SIZE_LIMIT", 200, minimum=1)
LDAP_SEARCH_TIMEOUT = _env_int("ACC_LDAP_SEARCH_TIMEOUT", 10, minimum=1, maximum=60)
# Conexión y reintentos (como ``ldap.retries`` del ejemplo).
LDAP_CONNECT_TIMEOUT = _env_int("ACC_LDAP_CONNECT_TIMEOUT", 5, minimum=1, maximum=60)
LDAP_RETRIES = _env_int("ACC_LDAP_RETRIES", 2, minimum=1, maximum=5)
# TLS: con ``ACC_LDAP_TLS_CA_FILE`` se valida el certificado contra la CA
# del archivo; sin él se usa la CA del sistema. Baja la verificación solo
# con ACC_LDAP_TLS_VERIFY=false (certificado autofirmado sin CA a mano).
LDAP_TLS_VERIFY = _env_bool("ACC_LDAP_TLS_VERIFY", True)
LDAP_TLS_CA_FILE = os.environ.get("ACC_LDAP_TLS_CA_FILE", "").strip()
# ``ldap://`` envía la contraseña del usuario sin cifrar. Solo se admite si el
# operador lo autoriza explícitamente; en producción la configuración segura
# (LDAPS) es obligatoria.
LDAP_ALLOW_INSECURE = _env_bool("ACC_LDAP_ALLOW_INSECURE", False)
# Rol con el que se crea la cuenta sombra en el primer login LDAP. Se admite
# solo ``viewer`` (la promoción la hace el admin local en /usuarios).
LDAP_DEFAULT_ROLE = (
    os.environ.get("ACC_LDAP_DEFAULT_ROLE", "viewer").strip().lower()
)
if LDAP_DEFAULT_ROLE not in ("viewer", "operator"):
    LDAP_DEFAULT_ROLE = "viewer"

ADMIN_USERNAME = os.environ.get("ACC_USERNAME", "admin")
ADMIN_PASSWORD_MIN_LENGTH = _env_int(
    "ACC_ADMIN_PASSWORD_MIN_LENGTH", 12, minimum=8, maximum=128
)
# Usuario con el que se opera en los equipos destino. Es el valor que aparece
# por defecto en el campo "Usuario Admin" del formulario del panel; no es la
# cuenta local de la aplicación (esa es ``ADMIN_USERNAME``).
TARGET_ADMIN_USERNAME = "ladmin"

# Contacto para solicitar una tarea nueva en el catálogo (mailto en la
# portada). El asunto también es configurable para pruebas.
TASK_REQUEST_EMAIL = os.environ.get("ACC_TASK_REQUEST_EMAIL", "luis.rignack@etecsa.cu")
TASK_REQUEST_SUBJECT = os.environ.get(
    "ACC_TASK_REQUEST_SUBJECT", "solicitud de nueva tarea Ansible"
)

# ---------------------------------------------------------------------------
# Notificaciones de lotes por correo (SMTP opcional)
# ---------------------------------------------------------------------------
# ACC_NOTIFY_LEVEL: "off" (por defecto, sin correos), "failures" (solo lotes
# con fallos/errores/omisiones) o "all" (todos los lotes). Sin SMTP configurado
# es un no-op silencioso: nunca tumba la ejecución de un lote.
SMTP_HOST = os.environ.get("ACC_SMTP_HOST", "")
SMTP_PORT = _env_int("ACC_SMTP_PORT", 25, minimum=1, maximum=65535)
SMTP_USER = os.environ.get("ACC_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("ACC_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("ACC_SMTP_FROM", "")
SMTP_STARTTLS = _env_bool("ACC_SMTP_STARTTLS", SMTP_PORT == 587)
NOTIFY_LEVEL = os.environ.get("ACC_NOTIFY_LEVEL", "off").strip().lower()
NOTIFY_TO = [
    addr.strip()
    for addr in os.environ.get("ACC_NOTIFY_TO", "").split(",")
    if addr.strip()
]

# ---------------------------------------------------------------------------
# Doble aprobación (two-person rule) de tareas destructivas
# ---------------------------------------------------------------------------
# Una solicitud aprobada por un admin distinto del ejecutor habilita una
# ejecución de tarea destructiva durante esta ventana, y es de un solo uso.
APPROVAL_TTL_MINUTES = _env_int("ACC_APPROVAL_TTL_MINUTES", 30, minimum=1, maximum=1440)

# ---------------------------------------------------------------------------
# Parámetros de ejecución
# ---------------------------------------------------------------------------
# DRY RUN activado por defecto: al arrancar no se toca ningún equipo real.
DRY_RUN = _env_bool("ACC_DRY_RUN", True)

# Verificación estricta de la clave SSH del host. Si los equipos objetivo no
# están en known_hosts, el primer acceso fallará. Se puede desactivar de forma
# consciente con ACC_HOST_KEY_CHECKING=0 (menos seguro).
HOST_KEY_CHECKING = _env_bool("ACC_HOST_KEY_CHECKING", True)

# Provisionamiento automático de known_hosts con ssh-keyscan. Desactivado por
# defecto: aceptar automáticamente la huella de un host en el primer contacto
# debilita el modelo TOFU (un atacante en la red podría presentar su clave).
# Actívalo solo en redes de gestión de confianza.
SSH_HOST_KEYS_AUTO = _env_bool("ACC_SSH_HOST_KEYS_AUTO", False)
# Ruta explícita del known_hosts (opcional). Si está vacía se usa
# ``$HOME/.ssh/known_hosts``, que es el mismo fichero que ve Ansible.
KNOWN_HOSTS_FILE = os.environ.get("ACC_KNOWN_HOSTS_FILE", "").strip()

# Límites de seguridad
MAX_HOSTS = _env_int("ACC_MAX_HOSTS", 256, minimum=1, maximum=4096)
MAX_PASSWORDS = _env_int("ACC_MAX_PASSWORDS", 200, minimum=1, maximum=1000)
MAX_PASSWORD_LENGTH = _env_int(
    "ACC_MAX_PASSWORD_LENGTH", 512, minimum=8, maximum=4096
)

# Timeouts de Ansible (segundos)
PING_TIMEOUT = _env_int("ACC_PING_TIMEOUT", 30, minimum=5, maximum=300)
PLAYBOOK_TIMEOUT = _env_int("ACC_PLAYBOOK_TIMEOUT", 300, minimum=30, maximum=3600)
CONNECT_TIMEOUT = _env_int("ACC_CONNECT_TIMEOUT", 10, minimum=3, maximum=120)
# Líneas de salida de Ansible que se conservan en memoria por ejecución. El
# callback de consola sigue recibiendo todas las líneas; solo se acota la
# copia que se devuelve como ``stdout`` para evitar crecimientos sin límite.
MAX_OUTPUT_BUFFER_LINES = _env_int(
    "ACC_MAX_OUTPUT_BUFFER_LINES", 2000, minimum=100, maximum=100000
)

# Retención del historial y del progreso en memoria
HISTORY_RETENTION_DAYS = _env_int("ACC_HISTORY_DAYS", 180, minimum=1)
JOB_RETENTION_HOURS = _env_int("ACC_JOB_RETENTION_HOURS", 24, minimum=1)
AUTH_EVENT_RETENTION_DAYS = _env_int(
    "ACC_AUTH_EVENT_DAYS", 365, minimum=1
)
# Cada cuántas horas el scheduler vuelve a aplicar la retención mientras el
# proceso sigue vivo (al arrancar ya se aplica una vez). Evita que un proceso
# de larga vida acumule historial aunque no se reinicie.
RETENTION_INTERVAL_HOURS = _env_int(
    "ACC_RETENTION_INTERVAL_HOURS", 24, minimum=1, maximum=720
)

# Límites de memoria y de la tabla de instantáneas de tareas. Evitan que un
# proceso de larga vida acumule estados de todas las operaciones históricas.
MAX_MEMORY_TASKS = _env_int("ACC_MAX_MEMORY_TASKS", 200, minimum=10)
MAX_TASK_STATE_ROWS = _env_int(
    "ACC_MAX_TASK_STATE_ROWS", 500, minimum=10
)
# Intervalo mínimo entre escrituras de consola/log en SQLite. Los cambios de
# estado se guardan siempre; el resto se agrupa para no escribir en cada línea
# de Ansible. 0 desactiva el agrupado.
TASK_PERSIST_INTERVAL_SECONDS = _env_int(
    "ACC_TASK_PERSIST_INTERVAL", 2, minimum=0, maximum=60
)

# Métricas de rendimiento en proceso (sin dependencias externas). Se guardan
# las últimas N muestras por ruta para calcular p50/p95; no se persisten.
REQUEST_METRICS_ENABLED = _env_bool("ACC_REQUEST_METRICS", True)
REQUEST_METRICS_WINDOW = _env_int(
    "ACC_REQUEST_METRICS_WINDOW", 500, minimum=50, maximum=10000
)
# Umbral para registrar una petición lenta en el log (además de la métrica).
SLOW_REQUEST_MS = _env_int("ACC_SLOW_REQUEST_MS", 1000, minimum=100, maximum=60000)

# Cola de trabajos: concurrencia acotada y tamaño máximo de la cola.
JOB_MAX_WORKERS = _env_int("ACC_JOB_WORKERS", 2, minimum=1, maximum=32)
JOB_QUEUE_SIZE = _env_int("ACC_JOB_QUEUE_SIZE", 50, minimum=1)
JOB_SHUTDOWN_GRACE_SECONDS = _env_int(
    "ACC_JOB_SHUTDOWN_GRACE", 10, minimum=0, maximum=300
)

# Bloqueo progresivo de cuentas tras intentos fallidos de login.
LOGIN_LOCKOUT_THRESHOLD = _env_int(
    "ACC_LOGIN_LOCKOUT_THRESHOLD", 5, minimum=1, maximum=100
)
LOGIN_LOCKOUT_MINUTES = _env_int(
    "ACC_LOGIN_LOCKOUT_MINUTES", 15, minimum=1, maximum=1440
)

# Roles disponibles. El rol se guarda en la tabla ``users``.
ROLES: tuple[str, ...] = ("viewer", "operator", "admin")

# Extensión máxima de un rango CIDR aceptado (p. ej. /24 -> 254 hosts).
MIN_CIDR_PREFIX = _env_int("ACC_MIN_CIDR_PREFIX", 22, minimum=8, maximum=32)


def _parse_allowed_networks(raw: str) -> tuple:
    """Convierte ``ACC_ALLOWED_NETWORKS`` en redes validadas.

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
                f"ACC_ALLOWED_NETWORKS contiene una red inválida: "
                f"{chunk!r}"
            ) from exc
    return tuple(networks)


# Allowlist obligatoria de redes de destino. En producción, un valor vacío
# impide el arranque: la aplicación prueba credenciales y ejecuta ``become``
# contra lo que se le indique, así que nunca debe quedar sin restricción.
ALLOWED_NETWORKS = _parse_allowed_networks(
    os.environ.get("ACC_ALLOWED_NETWORKS", "")
)

# Loopback solo se permite explícitamente en desarrollo (tests y laboratorio).
# En producción siempre se bloquea, incluso si la allowlist lo incluyera.
ALLOW_LOOPBACK = _env_bool(
    "ACC_ALLOW_LOOPBACK", not IS_PRODUCTION
)

# Direcciones que nunca deben ser objetivo de una operación: loopback,
# link-local (incluye 169.254.169.254 de metadatos cloud), multicast,
# reservadas y no especificadas. Se comprueban siempre, además de la allowlist.
BLOCKED_NETWORKS: tuple = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fe80::/10",
        "ff00::/8",
        "::/128",
    )
)

# ---------------------------------------------------------------------------
# P4: ejecución en grupo, verificación e idempotencia
# ---------------------------------------------------------------------------
# Diferencia máxima aceptada entre la hora objetivo y la hora final del host.
# Para una herramienta de fijación de hora, 2 s es un margen razonable; 120 s
# permitía reportar como éxito un reloj claramente desviado.
TIME_VERIFY_TOLERANCE = _env_int(
    "ACC_TIME_TOLERANCE", 2, minimum=0, maximum=3600
)

# ``TIME`` con NTP activo y política ``keep`` puede revertirse en segundos.
# Por defecto se bloquea la ejecución; la vista previa sigue mostrando el
# aviso para que el operador pueda corregir la política.
TIME_REQUIRE_NTP_OFF = _env_bool("ACC_TIME_REQUIRE_NTP_OFF", True)

# Espera tras reactivar NTP antes de la verificación final (segundos). Da
# margen a que el demonio aplique su corrección y permite detectar reversiones.
NTP_SETTLE_SECONDS = _env_int(
    "ACC_NTP_SETTLE_SECONDS", 3, minimum=0, maximum=60
)

# Límite de contraseñas candidatas que se prueban contra un mismo host. Sin
# este tope, una contraseña válida con un playbook que falla por un motivo
# ajeno a la autenticación haría probar todas las candidatas (hasta 200 por
# defecto) con ping + playbook cada una.
MAX_PASSWORD_ATTEMPTS = _env_int(
    "ACC_MAX_PASSWORD_ATTEMPTS", 3, minimum=1, maximum=50
)

# Presupuesto total de tiempo para un lote (segundos). Al agotarse, los hosts
# pendientes se registran como ``skipped`` en lugar de dejar el lote abierto
# durante horas. Un host en curso puede terminar su playbook (timeout propio).
BATCH_TIME_BUDGET_SECONDS = _env_int(
    "ACC_BATCH_TIME_BUDGET", 3600, minimum=60, maximum=86400
)

# Políticas NTP:
#   keep      -> no tocar la configuración NTP existente.
#   off       -> desactivar NTP (queda desactivado).
#   restore   -> desactivar, fijar la hora y restaurar el estado NTP previo.
#   configure -> configurar servidores NTP y dejar NTP activo.
NTP_POLICIES: tuple[str, ...] = ("keep", "off", "restore", "configure")
MAX_NTP_SERVERS = _env_int(
    "ACC_MAX_NTP_SERVERS", 8, minimum=1, maximum=32
)
DEFAULT_NTP_SERVERS: tuple[str, ...] = tuple(
    server.strip()
    for server in os.environ.get(
        "ACC_DEFAULT_NTP_SERVERS", "pool.ntp.org"
    ).split(",")
    if server.strip()
)

# Extensiones permitidas para archivos de contraseñas
ALLOWED_PASSWORD_EXTENSIONS = {".txt", ".yml", ".yaml"}

# Orígenes permitidos para las contraseñas candidatas del vault. El valor se
# guarda en SQLite y se muestra en la tabla de credenciales.
PASSWORD_ORIGINS: tuple[tuple[str, str], ...] = (
    ("manual", "Manual"),
    ("file", "Archivo"),
    ("generated", "Generada"),
    ("shared", "Compartida"),
    ("other", "Otra"),
)
PASSWORD_ORIGIN_VALUES = frozenset(code for code, _label in PASSWORD_ORIGINS)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("ACC_LOG_LEVEL", "INFO").upper()
LOG_TO_FILE = _env_bool("ACC_LOG_TO_FILE", True)
LOG_MAX_BYTES = _env_int("ACC_LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=1024)
LOG_BACKUP_COUNT = _env_int("ACC_LOG_BACKUPS", 5, minimum=1, maximum=50)

# ---------------------------------------------------------------------------
# Zonas horarias sugeridas para el datalist del formulario
# ---------------------------------------------------------------------------
# Solo se sugieren zonas de América. El campo admite cualquier zona IANA
# válida, pero el datalist prioriza el ámbito operativo habitual.
COMMON_TIMEZONES: tuple[str, ...] = (
    "America/Halifax",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Mexico_City",
    "America/Panama",
    "America/Bogota",
    "America/Lima",
    "America/La_Paz",
    "America/Santiago",
    "America/Argentina/Buenos_Aires",
    "America/Sao_Paulo",
    "America/Caracas",
    "America/Santo_Domingo",
)


# ---------------------------------------------------------------------------
# Funciones de soporte
# ---------------------------------------------------------------------------
def validate_security_config() -> None:
    """Rechaza configuraciones inseguras antes de arrancar en producción.

    En producción la allowlist de destino es obligatoria. También se exige una
    clave de sesión explícita y se advierte si la comprobación de claves SSH
    está desactivada. Es preferible no arrancar que operar sin estas garantías.
    """
    if not IS_PRODUCTION:
        return

    problems = []
    if not ALLOWED_NETWORKS:
        problems.append(
            "ACC_ALLOWED_NETWORKS es obligatoria en producción "
            "(ejemplo: 10.0.0.0/8,192.168.1.0/24)."
        )
    if not SECRET_KEY:
        problems.append(
            "ACC_SECRET_KEY es obligatoria en producción; no se debe "
            "depender de una clave generada en el primer arranque."
        )
    if not HOST_KEY_CHECKING:
        problems.append(
            "ACC_HOST_KEY_CHECKING=0 desactiva la verificación de "
            "claves SSH y no está permitido en producción."
        )
    if not SESSION_COOKIE_SECURE:
        problems.append(
            "ACC_COOKIE_SECURE=0 permite enviar la cookie de sesión por HTTP. "
            "Actívalo (ACC_COOKIE_SECURE=1) en producción con HTTPS."
        )
    if (
        LDAP_ENABLED
        and not LDAP_URL.lower().startswith("ldaps://")
        and not LDAP_ALLOW_INSECURE
    ):
        problems.append(
            "ACC_LDAP_URL debe usar ldaps:// en producción: con ldap:// la "
            "contraseña del usuario viaja sin cifrar. Si es imprescindible, "
            "define ACC_LDAP_ALLOW_INSECURE=1 de forma consciente."
        )
    if problems:
        raise RuntimeError("Configuración insegura:\n- " + "\n- ".join(problems))


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
                "define ACC_SECRETS_DIR/ACC_DATA_DIR/ACC_LOG_DIR "
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
        probe = path / ".ansible-control-center-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _existing_db(candidate: Path, legacy_name: str = "hora_admin.db") -> Path:
    """Prefiere la base nueva y reutiliza la del nombre anterior si existe.

    Así un despliegue que se actualiza no arranca con una base vacía: sigue
    usando ``hora_admin.db`` hasta que se migre manualmente a ``acc.db``.
    """
    if candidate.exists():
        return candidate
    legacy = candidate.parent / legacy_name
    return legacy if legacy.exists() else candidate


def resolve_db_path() -> Path:
    """Devuelve una ruta de SQLite utilizable.

    En producción usa ``DATA_DIR`` (``/var/lib/ansible-control-center``) y
    falla si no es escribible: arrancar con una base de datos en ``instance/``
    dejaría el historial fuera de las copias de seguridad y con permisos de
    desarrollo. El fallback local solo se permite en desarrollo y con
    ``ACC_ALLOW_DEV_FALLBACK=1``.
    """
    if _is_writable_dir(DATA_DIR):
        return _existing_db(DB_PATH)
    if IS_PRODUCTION or not ALLOW_DEV_FALLBACK:
        raise RuntimeError(
            f"El directorio de datos configurado no es escribible: {DATA_DIR}. "
            "En producción revisa permisos y ACC_DATA_DIR; no se permite caer "
            "a instance/."
        )
    if _is_writable_dir(INSTANCE_DIR):
        return _existing_db(INSTANCE_DIR / "acc.db")
    raise RuntimeError(
        f"No hay ningún directorio escribible para la base de datos "
        f"({DATA_DIR} ni {INSTANCE_DIR})."
    )


def resolve_vault_file() -> Path:
    """Devuelve una ruta de vault utilizable.

    En producción usa ``SECRETS_DIR`` y falla si no es escribible. El
    fallback a ``instance/secrets/`` queda reservado al desarrollo.
    """
    if _is_writable_dir(SECRETS_DIR):
        return VAULT_FILE
    if IS_PRODUCTION or not ALLOW_DEV_FALLBACK:
        raise RuntimeError(
            f"El directorio de secretos configurado no es escribible: "
            f"{SECRETS_DIR}. En producción revisa permisos y ACC_SECRETS_DIR; "
            "no se permite caer a instance/."
        )
    fallback = INSTANCE_DIR / "secrets"
    if _is_writable_dir(fallback):
        return fallback / VAULT_FILENAME
    raise RuntimeError(
        f"No hay ningún directorio escribible para el vault "
        f"({SECRETS_DIR} ni {fallback})."
    )


def get_or_create_secret_key() -> str:
    """Devuelve la clave de sesión de Flask, creándola si no existe.

    Prioridad: variable de entorno > archivo local 0600 > clave nueva. En
    producción no se cae a ``instance/``: si no se puede persistir la clave,
    el arranque falla en lugar de usar una clave efímera que invalide las
    sesiones en cada reinicio.
    """
    if SECRET_KEY:
        return SECRET_KEY

    candidates = [SECRET_KEY_FILE]
    if not IS_PRODUCTION and ALLOW_DEV_FALLBACK:
        candidates.append(INSTANCE_DIR / "flask_secret.key")
    for key_file in candidates:
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

    if IS_PRODUCTION:
        raise RuntimeError(
            f"No se pudo leer ni escribir la clave de sesión en "
            f"{SECRET_KEY_FILE}. Define ACC_SECRET_KEY o revisa permisos de "
            "ACC_DATA_DIR."
        )

    # Último recurso en desarrollo: clave efímera (sesiones se invalidan al
    # reiniciar).
    logging.getLogger("acc").warning(
        "No se pudo persistir la clave de sesión; se usará una clave efímera."
    )
    return secrets.token_hex(32)


@lru_cache(maxsize=1)
def get_or_create_password_pepper() -> str:
    """Devuelve el pepper usado en los hashes de contraseñas candidatas.

    Prioridad: variable de entorno > archivo local 0600 > valor efímero. El
    archivo se crea con ``O_EXCL`` para tolerar varios workers arrancando a la
    vez: solo uno escribe y el resto lee el mismo secreto.

    En producción un pepper efímero invalidaría todos los hashes al
    reiniciar, así que la falta de persistencia es un error de arranque.
    """
    if PASSWORD_PEPPER:
        return PASSWORD_PEPPER

    candidates = [PASSWORD_PEPPER_FILE]
    if not IS_PRODUCTION and ALLOW_DEV_FALLBACK:
        candidates.append(INSTANCE_DIR / "password_pepper.key")
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

    if IS_PRODUCTION:
        raise RuntimeError(
            f"No se pudo leer ni escribir el pepper en "
            f"{PASSWORD_PEPPER_FILE}. Define ACC_PASSWORD_PEPPER o revisa "
            "los permisos de ACC_DATA_DIR."
        )

    logging.getLogger("acc").warning(
        "No se pudo persistir el pepper de contraseñas; se usará uno efímero "
        "y los hashes cambiarán al reiniciar."
    )
    return secrets.token_hex(32)


def setup_logging() -> logging.Logger:
    """Configura el logger raíz de la aplicación.

    No hay ningún handler que reciba contraseñas: los servicios deben registrar
    únicamente hashes cortos e identificadores.
    """
    logger = logging.getLogger("acc")
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
