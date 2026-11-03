# services/host_resolver.py
# Resolución de IPs/rangos CIDR con límites de seguridad.
import ipaddress
import logging
import re
import socket
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Hostname DNS razonable: etiquetas alfanuméricas separadas por puntos.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.?$"
)


def _effective_limits(
    max_hosts: Optional[int], min_prefix: Optional[int]
) -> Tuple[int, int]:
    """Completa los límites con los valores de configuración."""
    if max_hosts is None or min_prefix is None:
        from config import MAX_HOSTS, MIN_CIDR_PREFIX

        if max_hosts is None:
            max_hosts = MAX_HOSTS
        if min_prefix is None:
            min_prefix = MIN_CIDR_PREFIX
    return max_hosts, min_prefix


def _allowed_networks() -> tuple:
    """Redes permitidas; se lee en tiempo de ejecución para permitir tests."""
    from config import ALLOWED_NETWORKS

    return ALLOWED_NETWORKS


def _ip_allowed(ip: str) -> bool:
    """Indica si una IP cae dentro de la allowlist configurada."""
    networks = _allowed_networks()
    if not networks:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def _network_allowed(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    """Indica si todo un CIDR está contenido en alguna red permitida."""
    networks = _allowed_networks()
    if not networks:
        return True
    for allowed in networks:
        if network.version != allowed.version:
            continue
        if network.subnet_of(allowed):
            return True
    return False


def resolve_single_ip(ip_str: str) -> Optional[str]:
    """Validar y devolver una dirección IP única.

    Args:
        ip_str: Cadena de texto con la dirección IP

    Returns:
        IP formateada o None si inválida
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        return str(ip)
    except ValueError:
        # Intentar resolver hostname a IP
        try:
            addr = socket.gethostbyname(ip_str)
            return addr
        except (socket.gaierror, UnicodeError):
            return None


def resolve_cidr_range(
    cidr_str: str,
    max_hosts: Optional[int] = None,
    min_prefix: Optional[int] = None,
) -> List[str]:
    """Resolver un rango CIDR a una lista acotada de direcciones.

    El rango se rechaza antes de enumerarlo si supera el prefijo mínimo o el
    número máximo de hosts. Así una entrada como ``0.0.0.0/0`` no agota la
    memoria del proceso.

    Args:
        cidr_str: Cadena en notación CIDR (ej: 192.168.1.0/24).
        max_hosts: Máximo de hosts a devolver.
        min_prefix: Prefijo IPv4 mínimo aceptado (p. ej. 22 para /22).

    Returns:
        Lista de direcciones IP como strings (vacía si no es válido o excede).
    """
    max_hosts, min_prefix = _effective_limits(max_hosts, min_prefix)

    try:
        network = ipaddress.ip_network(cidr_str, strict=False)
    except ValueError as exc:
        logger.error("CIDR inválido %r: %s", cidr_str, exc)
        return []

    if network.version == 4 and network.prefixlen < min_prefix:
        logger.warning(
            "Rango rechazado %s: prefijo /%d menor que el mínimo /%d",
            cidr_str,
            network.prefixlen,
            min_prefix,
        )
        return []

    if network.num_addresses > max_hosts + 2:
        logger.warning(
            "Rango rechazado %s: %d direcciones supera el máximo de %d hosts",
            cidr_str,
            network.num_addresses,
            max_hosts,
        )
        return []

    if not _network_allowed(network):
        logger.warning(
            "Rango rechazado %s: fuera de HORA_ADMIN_ALLOWED_NETWORKS",
            cidr_str,
        )
        return []

    hosts: List[str] = []
    for host in network.hosts():
        hosts.append(str(host))
        if len(hosts) >= max_hosts:
            break

    return hosts


def resolve_target(
    target: str,
    max_hosts: Optional[int] = None,
    min_prefix: Optional[int] = None,
) -> List[str]:
    """Resolver un objetivo que puede ser IP única, hostname o rango CIDR.

    Args:
        target: IP única (192.168.1.10) o rango CIDR (192.168.1.0/24).

    Returns:
        Lista de direcciones IP individuales.
    """
    target = (target or "").strip()
    if not target:
        return []

    # Verificar si contiene "/" para CIDR
    if "/" in target:
        return resolve_cidr_range(target, max_hosts, min_prefix)

    # IP única o hostname
    ip = resolve_single_ip(target)
    if ip:
        if not _ip_allowed(ip):
            logger.warning(
                "Host rechazado %s: fuera de HORA_ADMIN_ALLOWED_NETWORKS", ip
            )
            return []
        return [ip]
    return []


def validate_ip_format(ip_str: str) -> bool:
    """Validar que el string sea una IP o un rango CIDR real.

    Args:
        ip_str: Cadena a validar.

    Returns:
        True si ``ipaddress`` la acepta como dirección o red.
    """
    value = (ip_str or "").strip()
    if not value:
        return False
    try:
        if "/" in value:
            ipaddress.ip_network(value, strict=False)
        else:
            ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def validate_target(
    target: str,
    max_hosts: Optional[int] = None,
    min_prefix: Optional[int] = None,
) -> Optional[str]:
    """Valida un objetivo y devuelve un mensaje de error o ``None``.

    Se usa antes de lanzar la ejecución para rechazar rangos excesivos sin
    llegar a enumerarlos en el hilo de trabajo.
    """
    value = (target or "").strip()
    if not value:
        return "La IP o rango es obligatorio."
    if not validate_ip_format(value):
        # La interfaz admite también un hostname resoluble. Se valida su
        # sintaxis y se comprueba la allowlist contra la IP resultante para no
        # eludir HORA_ADMIN_ALLOWED_NETWORKS resolviendo un nombre.
        if "/" in value or not _HOSTNAME_RE.fullmatch(value):
            return "IP o rango CIDR inválido."
        try:
            resolved = socket.gethostbyname(value)
        except (socket.gaierror, UnicodeError):
            return "IP o rango CIDR inválido (hostname no resoluble)."
        if not _ip_allowed(resolved):
            return "La IP está fuera de las redes permitidas."
        return None
    if "/" not in value:
        if not _ip_allowed(value):
            return "La IP está fuera de las redes permitidas."
        return None

    max_hosts, min_prefix = _effective_limits(max_hosts, min_prefix)
    network = ipaddress.ip_network(value, strict=False)

    if not _network_allowed(network):
        return "El rango está fuera de las redes permitidas."

    if network.version == 4 and network.prefixlen < min_prefix:
        return (
            f"Rango demasiado grande: el prefijo mínimo permitido es "
            f"/{min_prefix}."
        )

    host_count = max(network.num_addresses - 2, 1)
    if host_count > max_hosts:
        return (
            f"El rango incluye {host_count} hosts; el máximo permitido es "
            f"{max_hosts}."
        )
    return None
