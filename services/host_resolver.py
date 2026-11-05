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

# Rango IPv4 inclusivo con la forma ``inicio-fin``.
_IPV4_RANGE_RE = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$"
)


def _parse_ipv4_range(
    value: str,
) -> Optional[Tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]]:
    """Devuelve los extremos de un rango IPv4 ``inicio-fin``.

    No exige que ``inicio <= fin`` para que el llamador pueda distinguir un
    rango invertido de un formato inválido. Devuelve ``None`` si la forma no
    es un rango IPv4 válido.
    """
    match = _IPV4_RANGE_RE.fullmatch(value or "")
    if not match:
        return None
    try:
        start = ipaddress.IPv4Address(match.group(1))
        end = ipaddress.IPv4Address(match.group(2))
    except ValueError:
        return None
    return start, end


def _looks_like_ipv4_range(value: str) -> bool:
    """Indica si el texto pretende ser un rango IPv4, aunque sea inválido."""
    return bool(_IPV4_RANGE_RE.fullmatch(value or ""))


def _split_target_list(value: str) -> List[str]:
    """Divide un objetivo con varias entradas separadas por comas.

    Se ignoran los espacios alrededor de cada entrada y las comas sobrantes,
    de modo que ``10.0.0.1, 10.0.0.2`` y ``10.0.0.1,,10.0.0.2`` son válidos.
    """
    return [piece.strip() for piece in (value or "").split(",") if piece.strip()]


def _pinned_host_count(pinned: str) -> int:
    """Cuenta los hosts que expande un objetivo ya fijado por ``pin_target``."""
    pair = _parse_ipv4_range(pinned or "")
    if pair is not None:
        return int(pair[1]) - int(pair[0]) + 1
    if "/" in (pinned or ""):
        try:
            network = ipaddress.ip_network(pinned, strict=False)
        except ValueError:
            return 1
        return max(network.num_addresses - 2, 1)
    return 1


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


def _blocked_networks() -> tuple:
    from config import BLOCKED_NETWORKS

    return BLOCKED_NETWORKS


def _loopback_allowed() -> bool:
    from config import ALLOW_LOOPBACK

    return ALLOW_LOOPBACK


def _is_blocked(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    """Indica si una red toca un rango prohibido (loopback, link-local, etc.)."""
    from config import IS_PRODUCTION

    for blocked in _blocked_networks():
        if network.version != blocked.version:
            continue
        if not network.overlaps(blocked):
            continue
        # Loopback solo se permite en desarrollo y con opt-in explícito.
        if blocked.is_loopback:
            if _loopback_allowed() and not IS_PRODUCTION:
                continue
        return True
    return False


def _ip_allowed(ip: str) -> bool:
    """Indica si una IP es segura y cae dentro de la allowlist configurada."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False

    if _is_blocked(ipaddress.ip_network(f"{address}/{address.max_prefixlen}")):
        return False

    networks = _allowed_networks()
    if not networks:
        return True
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def _network_allowed(
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    """Indica si todo un CIDR es seguro y está contenido en la allowlist."""
    if _is_blocked(network):
        return False

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
        addresses = resolve_hostname(ip_str)
        return addresses[0] if addresses else None


def resolve_hostname(hostname: str) -> List[str]:
    """Resuelve un hostname a todas sus direcciones IPv4 conocidas.

    Se resuelve una sola vez y se devuelven direcciones literales para que
    Ansible no vuelva a consultar DNS (evita el TOCTOU de DNS rebinding).
    """
    if not _HOSTNAME_RE.fullmatch(hostname or ""):
        return []
    try:
        _name, _aliases, addresses = socket.gethostbyname_ex(hostname)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    unique: List[str] = []
    for address in addresses:
        try:
            normalized = str(ipaddress.ip_address(address))
        except ValueError:
            continue
        if normalized not in unique:
            unique.append(normalized)
    return unique


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
            "Rango rechazado %s: fuera de ACC_ALLOWED_NETWORKS",
            cidr_str,
        )
        return []

    hosts: List[str] = []
    for host in network.hosts():
        hosts.append(str(host))
        if len(hosts) >= max_hosts:
            break

    return hosts


def resolve_ipv4_range(
    range_str: str,
    max_hosts: Optional[int] = None,
    min_prefix: Optional[int] = None,
) -> List[str]:
    """Expande un rango IPv4 inclusivo ``inicio-fin``.

    El rango se rechaza si está invertido, si supera ``max_hosts`` o si alguna
    de sus direcciones queda fuera de la allowlist o toca un rango prohibido.
    """
    max_hosts, _ = _effective_limits(max_hosts, min_prefix)
    pair = _parse_ipv4_range(range_str)
    if pair is None:
        logger.warning("Rango IPv4 inválido: %r", range_str)
        return []

    start, end = pair
    if int(start) > int(end):
        logger.warning("Rango IPv4 invertido: %r", range_str)
        return []

    count = int(end) - int(start) + 1
    if count > max_hosts:
        logger.warning(
            "Rango rechazado %s: %d direcciones supera el máximo de %d hosts",
            range_str,
            count,
            max_hosts,
        )
        return []

    hosts: List[str] = []
    for offset in range(count):
        ip = str(ipaddress.IPv4Address(int(start) + offset))
        if not _ip_allowed(ip):
            logger.warning(
                "Rango rechazado %s: la IP %s está fuera de las redes permitidas",
                range_str,
                ip,
            )
            return []
        hosts.append(ip)
    return hosts


def resolve_target(
    target: str,
    max_hosts: Optional[int] = None,
    min_prefix: Optional[int] = None,
) -> List[str]:
    """Resolver un objetivo que puede ser IP única, hostname, CIDR o rango.

    Args:
        target: IP única (192.168.1.10), rango CIDR (192.168.1.0/24),
            rango inclusivo (172.17.128.118-172.17.128.235), hostname o
            varias de esas entradas separadas por comas.

    Returns:
        Lista de direcciones IP individuales.
    """
    target = (target or "").strip()
    if not target:
        return []

    # Varias entradas separadas por comas: se resuelven una a una y se
    # deduplican, respetando el máximo de hosts del conjunto.
    if "," in target:
        pieces = _split_target_list(target)
        if len(pieces) > 1:
            return _resolve_target_list(pieces, max_hosts, min_prefix)
        if pieces:
            target = pieces[0]
        else:
            return []

    # Rango inclusivo inicio-fin (solo IPv4).
    if _parse_ipv4_range(target) is not None:
        return resolve_ipv4_range(target, max_hosts, min_prefix)
    if _looks_like_ipv4_range(target):
        return []

    # Verificar si contiene "/" para CIDR
    if "/" in target:
        return resolve_cidr_range(target, max_hosts, min_prefix)

    # IP única o hostname. Para un hostname se resuelve una sola vez y se fija
    # la primera IP; todas las direcciones deben ser seguras y estar dentro de
    # la allowlist. Así Ansible recibe una IP literal y no vuelve a consultar
    # DNS (evita el TOCTOU de DNS rebinding).
    addresses = resolve_hostname(target) if not validate_ip_format(target) else []
    if addresses:
        if any(not _ip_allowed(address) for address in addresses):
            logger.warning(
                "Host rechazado %s: alguna dirección resuelta está fuera de "
                "las redes permitidas",
                target,
            )
            return []
        return [addresses[0]]

    ip = resolve_single_ip(target)
    if ip:
        if not _ip_allowed(ip):
            logger.warning(
                "Host rechazado %s: fuera de ACC_ALLOWED_NETWORKS", ip
            )
            return []
        return [ip]
    return []


def _resolve_target_list(
    pieces: List[str],
    max_hosts: Optional[int],
    min_prefix: Optional[int],
) -> List[str]:
    """Resuelve un objetivo con varias entradas separadas por comas.

    Cada entrada se valida con las mismas restricciones que un objetivo
    individual y el resultado se deduplica conservando el orden. Si la suma
    supera ``max_hosts`` se rechaza el objetivo completo en lugar de truncarlo.
    """
    max_hosts, _ = _effective_limits(max_hosts, min_prefix)
    hosts: List[str] = []
    for piece in pieces:
        for host in resolve_target(piece, max_hosts, min_prefix):
            if host not in hosts:
                hosts.append(host)
    if len(hosts) > max_hosts:
        logger.warning(
            "Objetivo rechazado (%s): %d hosts supera el máximo de %d",
            ",".join(pieces),
            len(hosts),
            max_hosts,
        )
        return []
    return hosts


def validate_ip_format(ip_str: str) -> bool:
    """Validar que el string sea una IP, un CIDR o un rango IPv4 real.

    Args:
        ip_str: Cadena a validar.

    Returns:
        True si ``ipaddress`` la acepta como dirección/red o si es un rango
        IPv4 inclusivo con ``inicio <= fin``.
    """
    value = (ip_str or "").strip()
    if not value:
        return False

    range_pair = _parse_ipv4_range(value)
    if range_pair is not None:
        start, end = range_pair
        return int(start) <= int(end)

    try:
        if "/" in value:
            ipaddress.ip_network(value, strict=False)
        else:
            ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def pin_target(
    target: str,
    max_hosts: Optional[int] = None,
    min_prefix: Optional[int] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Valida un objetivo y devuelve una forma ya resuelta y fijada.

    Para un hostname se resuelve **una sola vez** y se devuelve la primera IP
    literal. La aplicación guarda ese valor y Ansible nunca vuelve a consultar
    DNS, lo que elimina la ventana de DNS rebinding entre la validación del
    formulario y la ejecución del playbook.

    Admite varias entradas separadas por comas: cada una se fija igual que un
    objetivo individual y el total de hosts no puede superar el máximo.

    Returns:
        ``(objetivo_fijado, error)``. Si hay error, el primer elemento es
        ``None``.
    """
    value = (target or "").strip()
    if not value:
        return None, "La IP o rango es obligatorio."

    # Varias entradas separadas por comas: se fija cada una con el presupuesto
    # de hosts que queda, de forma que la suma no supere el máximo.
    if "," in value:
        pieces = _split_target_list(value)
        if not pieces:
            return None, "La IP o rango es obligatorio."
        if len(pieces) > 1:
            return _pin_target_list(pieces, max_hosts, min_prefix)
        value = pieces[0]

    # Rango IPv4 inclusivo. Se valida cada dirección para que la allowlist y
    # los rangos prohibidos se apliquen también a los extremos intermedios.
    range_pair = _parse_ipv4_range(value)
    if range_pair is not None:
        start, end = range_pair
        if int(start) > int(end):
            return (
                None,
                "El inicio del rango debe ser menor o igual que el fin.",
            )
        max_hosts, _ = _effective_limits(max_hosts, min_prefix)
        host_count = int(end) - int(start) + 1
        if host_count > max_hosts:
            return (
                None,
                f"El rango incluye {host_count} hosts; el máximo permitido "
                f"es {max_hosts}.",
            )
        for offset in range(host_count):
            ip = str(ipaddress.IPv4Address(int(start) + offset))
            if not _ip_allowed(ip):
                return (
                    None,
                    f"El rango contiene la IP {ip}, fuera de las redes "
                    "permitidas.",
                )
        return f"{start}-{end}", None
    if _looks_like_ipv4_range(value):
        return (
            None,
            "Rango de IP inválido. Usa inicio-fin, por ejemplo "
            "172.17.128.118-172.17.128.235.",
        )

    if not validate_ip_format(value):
        # La interfaz admite también un hostname resoluble. Se resuelve una
        # sola vez y se comprueban *todas* las direcciones contra la allowlist
        # y los rangos prohibidos, para no eludir las restricciones con un
        # nombre que apunte a loopback o a metadatos cloud.
        if "/" in value or not _HOSTNAME_RE.fullmatch(value):
            return None, "IP o rango CIDR inválido."
        addresses = resolve_hostname(value)
        if not addresses:
            return None, "IP o rango CIDR inválido (hostname no resoluble)."
        # Se exige que *todas* las direcciones sean seguras: Ansible podría
        # elegir cualquiera de ellas al conectar. Aceptar una sola permitiría
        # que un DNS malicioso colara una IP de loopback o de metadatos.
        blocked = [address for address in addresses if not _ip_allowed(address)]
        if blocked:
            logger.warning(
                "Hostname %s rechazado: direcciones no permitidas %s",
                value,
                ", ".join(blocked),
            )
            return None, (
                "El hostname resuelve a direcciones fuera de las redes "
                "permitidas: " + ", ".join(blocked)
            )
        return addresses[0], None

    if "/" not in value:
        if not _ip_allowed(value):
            return None, "La IP está fuera de las redes permitidas."
        return str(ipaddress.ip_address(value)), None

    max_hosts, min_prefix = _effective_limits(max_hosts, min_prefix)
    network = ipaddress.ip_network(value, strict=False)

    if not _network_allowed(network):
        return None, "El rango está fuera de las redes permitidas."

    if network.version == 4 and network.prefixlen < min_prefix:
        return (
            None,
            f"Rango demasiado grande: el prefijo mínimo permitido es "
            f"/{min_prefix}.",
        )

    host_count = max(network.num_addresses - 2, 1)
    if host_count > max_hosts:
        return (
            None,
            f"El rango incluye {host_count} hosts; el máximo permitido es "
            f"{max_hosts}.",
        )
    return str(network), None


def _pin_target_list(
    pieces: List[str],
    max_hosts: Optional[int],
    min_prefix: Optional[int],
) -> Tuple[Optional[str], Optional[str]]:
    """Fija un objetivo con varias entradas separadas por comas.

    Devuelve las entradas normalizadas unidas por comas. El máximo de hosts se
    comparte entre todas las entradas y las repetidas se descartan.
    """
    max_hosts, min_prefix = _effective_limits(max_hosts, min_prefix)
    remaining = max_hosts
    pinned_parts: List[str] = []
    for piece in pieces:
        if remaining <= 0:
            return None, (
                "Las entradas suman más hosts que el máximo permitido "
                f"({max_hosts})."
            )
        pinned_piece, error = pin_target(
            piece, max_hosts=remaining, min_prefix=min_prefix
        )
        if error or not pinned_piece:
            return None, f"{piece}: {error or 'entrada no válida.'}"
        if pinned_piece in pinned_parts:
            continue
        remaining -= _pinned_host_count(pinned_piece)
        pinned_parts.append(pinned_piece)
    return ",".join(pinned_parts), None


def validate_target(
    target: str,
    max_hosts: Optional[int] = None,
    min_prefix: Optional[int] = None,
) -> Optional[str]:
    """Valida un objetivo y devuelve un mensaje de error o ``None``.

    Se usa antes de lanzar la ejecución para rechazar rangos excesivos sin
    llegar a enumerarlos en el hilo de trabajo. Para obtener además el valor
    fijado (IP literal) usa :func:`pin_target`.
    """
    _pinned, error = pin_target(target, max_hosts, min_prefix)
    return error
