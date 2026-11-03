# services/host_resolver.py
# Resolución de rangos CIDR a IPs individuales
import ipaddress
import logging
import socket
from typing import List, Optional

logger = logging.getLogger(__name__)


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
        except socket.gaierror:
            return None


def resolve_cidr_range(cidr_str: str) -> List[str]:
    """Resolver un rango CIDR a lista de direcciones IP individuales.

    Args:
        cidr_str: Cadena de texto con notación CIDR (ej: 192.168.1.0/24)

    Returns:
        Lista de direcciones IP como strings
    """
    hosts = []

    try:
        network = ipaddress.ip_network(cidr_str, strict=False)
        for host in network.hosts():
            hosts.append(str(host))
    except ValueError as e:
        logger.error(f"Error parsing CIDR {cidr_str}: {e}")

    return hosts


def resolve_target(target: str) -> List[str]:
    """Resolver un objetivo que puede ser IP única, IP o rango CIDR.

    Args:
        target: Cadena de texto - IP única (192.168.1.10) o rango CIDR (192.168.1.0/24)

    Returns:
        Lista de direcciones IP individuales
    """
    # Verificar si contiene "/" para CIDR
    if "/" in target:
        return resolve_cidr_range(target)
    else:
        # IP única o hostname
        ip = resolve_single_ip(target)
        if ip:
            return [ip]
        return []


def validate_ip_format(ip_str: str) -> bool:
    """Validar que el string tenga formato de IP o CIDR.

    Args:
        ip_str: Cadena a validar

    Returns:
        True si el formato es válido
    """
    import re

    # Patrón para IP simple o CIDR
    ip_pattern = r"^(\d{1,3}\.){3}\d{1,3}(\/(\d|[1-2]\d|3[0-2]))?$"
    return bool(re.match(ip_pattern, ip_str))