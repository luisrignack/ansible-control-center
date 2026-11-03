# services/host_resolver.py
# Resolución de IPs/rangos CIDR con límites de seguridad.
import ipaddress
import logging
import socket
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


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
        return "IP o rango CIDR inválido."
    if "/" not in value:
        return None

    max_hosts, min_prefix = _effective_limits(max_hosts, min_prefix)
    network = ipaddress.ip_network(value, strict=False)

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
