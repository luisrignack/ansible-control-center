# Autenticación corporativa por LDAPS (bind de búsqueda + bind del usuario).
#
# Modelo equivalente al ejemplo Nuxeo: la app abre una conexión con el
# usuario de solo lectura del directorio, busca la entrada del uid solicitado
# y, con el DN devuelto, hace un rebind simple con la contraseña que el
# operador tipeó. La app nunca guarda esa contraseña: se valida en el
# servidor en cada login.
#
# Los valores de configuración se leen en tiempo de llamada (``config.*``)
# para que las pruebas los fijen con ``monkeypatch.setattr(config, ...)``.
# La conexión real se abre en :func:`_connect`, el punto de inyección que
# usan los tests con una conexión falsa (sin red).
from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

from config import (
    LDAP_CONNECT_TIMEOUT,
    LDAP_RETRIES,
    LDAP_SEARCH_SIZE_LIMIT,
    LDAP_SEARCH_TIMEOUT,
)

logger = logging.getLogger("acc")


class LdapError(Exception):
    """El servidor LDAP no está disponible o la operación falló."""


LDAP_BIND_RESULT_HINT = {
    "invalidCredentials": (
        " Credenciales del servicio de búsqueda (bind) rechazadas por el "
        "directorio: revisa ACC_LDAP_BIND_DN y su contraseña."
    ),
}


@dataclass(frozen=True)
class LdapUser:
    """Entrada de usuario encontrada en el directorio."""

    dn: str
    username: str
    email: str = ""
    display_name: str = ""


# ---------------------------------------------------------------------------
# Ayudantes de configuración (leídos en tiempo de llamada)
# ---------------------------------------------------------------------------
def ldap_enabled() -> bool:
    from config import LDAP_ENABLED, LDAP_URL

    return bool(LDAP_ENABLED) and bool(LDAP_URL.strip())


def ldap_configured() -> bool:
    """El directorio tiene URL y datos de bind: apto para activarse."""
    from config import LDAP_URL

    return bool(LDAP_URL.strip())


def _search_scope():
    from ldap3 import LEVEL, SUBTREE

    from config import LDAP_USER_SCOPE

    return SUBTREE if LDAP_USER_SCOPE == "subtree" else LEVEL


def _search_base() -> str:
    from config import LDAP_USER_BASE

    return LDAP_USER_BASE


def _search_attributes() -> list[str]:
    from config import LDAP_USERNAME_ATTR

    attrs = {LDAP_USERNAME_ATTR, "mail", "cn"}
    return sorted(attrs)


def _search_filter(username: str) -> str:
    from ldap3.utils.conv import escape_filter_chars

    from config import LDAP_USER_FILTER, LDAP_USERNAME_ATTR

    base = LDAP_USER_FILTER.strip()
    if not base.startswith("("):
        base = f"({base}"
    if not base.endswith(")"):
        base = f"{base})"
    return f"(&{base}({LDAP_USERNAME_ATTR}={escape_filter_chars(username)}))"


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def parse_ldap_url(url: str, allow_insecure: bool | None = None):
    """Extrae (host, puerto, use_ssl) desde ``ldaps://host:puerto``.

    ``ldap://`` envía la contraseña del usuario sin cifrar y se rechaza por
    defecto. Solo se admite si ``allow_insecure`` (o
    ``ACC_LDAP_ALLOW_INSECURE``) es verdadero, de forma consciente.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = parts.hostname or ""
    if not host:
        raise LdapError(f"URL LDAP no válida: {url!r}")
    if scheme not in {"ldap", "ldaps"}:
        raise LdapError(f"URL LDAP no válida: {url!r} (esquema soportado: ldaps://).")
    if allow_insecure is None:
        from config import LDAP_ALLOW_INSECURE

        allow_insecure = LDAP_ALLOW_INSECURE
    if scheme == "ldap" and not allow_insecure:
        raise LdapError(
            "ACC_LDAP_URL usa ldap://, que envía la contraseña sin cifrar. "
            "Usa ldaps:// o define ACC_LDAP_ALLOW_INSECURE=1 de forma "
            "consciente."
        )
    use_ssl = scheme == "ldaps"
    port = parts.port or (636 if use_ssl else 389)
    return host, port, use_ssl


def _tls_object():
    """Objeto TLS según la configuración de verificación del certificado."""
    from ldap3 import Tls

    from config import LDAP_TLS_CA_FILE, LDAP_TLS_VERIFY

    if not LDAP_TLS_VERIFY:
        logger.warning(
            "LDAP con ACC_LDAP_TLS_VERIFY=false: el certificado del servidor "
            "no se valida (desarrollo o CA autofirmada sin CRT)."
        )
        return Tls(validate=ssl.CERT_NONE)
    if LDAP_TLS_CA_FILE:
        try:
            from pathlib import Path

            Path(LDAP_TLS_CA_FILE).resolve(strict=True)
        except OSError as exc:
            raise LdapError(
                f"CA de LDAP no encontrada ({LDAP_TLS_CA_FILE}): {exc}"
            ) from exc
        return Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=str(LDAP_TLS_CA_FILE),
        )
    # Sin CA explícita: la CA del sistema (ca-certificates instaladas).
    return Tls(validate=ssl.CERT_REQUIRED)


def _connect(bind_as: str = "", bind_password: str = ""):
    """Abre una ``Connection`` de ldap3 contra el servidor configurado.

    Sin argumentos usa el bind del servicio de búsqueda. Punto de inyección
    de los tests: se monkeypatchea con una conexión falsa y no se toca la
    red.
    """
    from ldap3 import Connection, Server

    from config import LDAP_BIND_DN, LDAP_BIND_PASSWORD, LDAP_URL

    host, port, use_ssl = parse_ldap_url(LDAP_URL)
    server = Server(
        host,
        port=port,
        use_ssl=use_ssl,
        tls=_tls_object() if use_ssl else None,
        connect_timeout=LDAP_CONNECT_TIMEOUT,
    )
    user = bind_as or LDAP_BIND_DN
    password = bind_password or LDAP_BIND_PASSWORD
    connection = Connection(server, user=user, password=password, read_only=True)
    return connection


def _connection_error_hint(exc: Exception) -> str:
    """Traduce los fallos de conexión habituales a una indicación útil."""
    detail = str(exc)
    lowered = detail.lower()
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return (
            " El certificado del directorio no se pudo validar: instala la CA "
            "que lo emite en ACC_LDAP_TLS_CA_FILE (o define "
            "ACC_LDAP_TLS_VERIFY=0 de forma consciente)."
        )
    if "timed out" in lowered or "timeout" in lowered:
        return (
            " El servidor LDAP no respondió a tiempo: revisa la URL, el "
            "puerto y la conectividad de red."
        )
    return ""


def _bind_or_fail(connection, context: str) -> None:
    """Hace ``bind()`` y convierte el fallo en ``LdapError`` claro.

    ``ldap3`` lanza ``LDAPSocketOpenError``/``LDAPException`` cuando la
    conexión TCP/TLS no se puede abrir (CA no confiada, DNS, puerto
    cerrado). Sin esta conversión el fallo escaparía como excepción no
    controlada y la vista respondería 500.
    """
    try:
        bound = connection.bind()
    except Exception as exc:  # ldap3: LDAPSocketOpenError, LDAPException…
        detail = str(exc)[:300]
        raise LdapError(
            f"{context}: no se pudo conectar con el directorio ({detail})."
            f"{_connection_error_hint(exc)}"
        ) from exc
    if not bound:
        result = getattr(connection, "result", {}) or {}
        description = result.get("description", "desconocido")
        hint = LDAP_BIND_RESULT_HINT.get(description, "")
        raise LdapError(f"{context}: bind LDAP fallido ({description}).{hint}")


def test_search_bind() -> None:
    """Comprueba que el bind del servicio de búsqueda funciona.

    Lo usa el interruptor de ``/usuarios`` antes de activar el acceso:
    evita dejar ON un directorio al que no se puede conectar (URL, bind o
    certificado mal configurados). Lanza ``LdapError`` si el bind falla.
    """
    try:
        connection = _connect()
    except LdapError:
        raise
    except Exception as exc:  # TLS/DNS: se informa, nunca se propaga en crudo
        raise LdapError(
            "servicio de búsqueda: no se pudo abrir la conexión LDAP "
            f"({str(exc)[:300]}).{_connection_error_hint(exc)}"
        ) from exc
    try:
        _bind_or_fail(connection, "servicio de búsqueda")
    finally:
        if getattr(connection, "bound", False):
            try:
                connection.unbind()
            except Exception:  # noqa: BLE001, S110  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# Búsqueda y verificación
# ---------------------------------------------------------------------------
def search_user(username: str, connection=None):
    """Busca la entrada del uid y la devuelve como :class:`LdapUser`.

    Devuelve ``None`` si el uid no existe. Lanza ``LdapError`` si el
    servidor no responde o devuelve más de una entrada.
    """
    from config import LDAP_USERNAME_ATTR

    local_conn = connection or _connect()
    try:
        if connection is None:
            _bind_or_fail(local_conn, "servicio de búsqueda")
        local_conn.search(
            _search_base(),
            _search_filter(username),
            search_scope=_search_scope(),
            attributes=_search_attributes(),
            size_limit=LDAP_SEARCH_SIZE_LIMIT,
            time_limit=LDAP_SEARCH_TIMEOUT,
        )
        entries = [e for e in local_conn.response or [] if e.get("dn")]
        if not entries:
            return None
        if len(entries) > 1:
            raise LdapError(
                f"La búsqueda LDAP devolvió {len(entries)} entradas para el "
                f"uid {username!r}; revisa el filtro/base del directorio."
            )
        entry = entries[0]
        attrs = entry.get("attributes", {}) or {}

        def _first(value, default: str = "") -> str:
            if isinstance(value, (list, tuple)):
                return str(value[0]) if value else default
            return str(value) if value is not None else default

        return LdapUser(
            dn=str(entry["dn"]),
            username=_first(attrs.get(LDAP_USERNAME_ATTR), username),
            email=_first(attrs.get("mail")),
            display_name=_first(attrs.get("cn"), username),
        )
    finally:
        if connection is None and getattr(local_conn, "bound", False):
            try:
                local_conn.unbind()
            except Exception:  # noqa: BLE001, S110  # cierre best-effort
                pass


def authenticate(username: str, password: str):
    """Autentica el par ``username``/``password`` contra el directorio.

    Devuelve :class:`LdapUser` si el bind del usuario es correcto, ``None``
    si el uid no existe o la contraseña es inválida, y lanza ``LdapError``
    si el servidor no está alcanzado o el bind de búsqueda falla.
    """
    if not password:
        return None
    last_error: Exception | None = None
    for _attempt in range(max(1, LDAP_RETRIES)):
        try:
            return _authenticate_once(username, password)
        except LdapError:
            raise
        except Exception as exc:  # noqa: BLE001  # reintentos de red
            last_error = exc
            logger.warning("Intento LDAPS fallido (%s): %s", username, exc)
    if last_error is not None:
        raise LdapError(f"Servidor LDAP no disponible: {last_error}")
    return None


def _authenticate_once(username: str, password: str):
    """Un intento completo: conectar, buscar, rebindar como el usuario."""
    connection = _connect()
    try:
        _bind_or_fail(connection, "servicio de búsqueda")
        user = search_user(username, connection=connection)
        if user is None:
            logger.info("Login LDAP: uid %r no existe en el directorio", username)
            return None
        # Rebind como el usuario: es la verificación real de credenciales.
        if not connection.rebind(user=user.dn, password=password):
            result = getattr(connection, "result", {}) or {}
            description = result.get("description", "")
            if description == "invalidCredentials":
                return None
            raise LdapError(
                f"Bind del usuario {username!r} falló "
                f"({description or 'sin detalle'})."
            )
        return user
    finally:
        if getattr(connection, "bound", False):
            try:
                connection.unbind()
            except Exception:  # noqa: BLE001, S110  # pragma: no cover
                pass
