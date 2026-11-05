# services/ssh_host_keys.py
# ---------------------------------------------------------------------------
# Provisionamiento opcional de ``known_hosts`` para hosts objetivo.
#
# Ansible, con ``ANSIBLE_HOST_KEY_CHECKING=True`` (valor por defecto), falla el
# primer acceso a un host cuyo fingerprint no está presente en el ``known_hosts``
# del usuario que ejecuta el playbook. Esta utilidad obtiene esas huellas con
# ``ssh-keyscan`` y las añade al fichero existente, de modo que el operador no
# tenga que ejecutar ``ssh-keyscan`` manualmente en cada equipo.
#
# Importante: aceptar automáticamente la huella de un host en el primer
# contacto debilita el modelo TOFU (un atacante en la red podría presentar su
# propia clave). Por eso el flujo de ejecución solo lo usa cuando
# ``ACC_SSH_HOST_KEYS_AUTO=1``; el valor por defecto es desactivado.
#
# Diseño:
#   * El ``known_hosts`` es el del usuario de servicio (``$HOME/.ssh/known_hosts``)
#     o el que indique ``ACC_KNOWN_HOSTS_FILE``. Es el mismo fichero que ve
#     Ansible porque ``ansible_environment()`` propaga ``HOME``.
#   * Las huellas se añaden sin borrar las existentes.
#   * El fichero se escribe de forma atómica y con permisos ``0600``.
#   * El proceso es idempotente: solo escanea los hosts cuya huella no está
#     ya registrada. Si el escaneo falla (host caído, firewalled) se registra
#     un warning y Ansible reportará su propio error más tarde.
# ---------------------------------------------------------------------------

from __future__ import annotations

import concurrent.futures
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

HostKey = Tuple[str, str, str]  # (host, type, key)

_KEY_TYPES = frozenset(
    {
        "ssh-rsa",
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-dss",
    }
)
_SCAN_TYPES = ("rsa", "ed25519", "ecdsa")


def known_hosts_path() -> Path:
    """Ruta del ``known_hosts`` usada por la app y por Ansible.

    ``ACC_KNOWN_HOSTS_FILE`` tiene prioridad; si está vacío se usa
    ``$HOME/.ssh/known_hosts``. No se crea el fichero aquí: lo hace
    :func:`update_known_hosts` cuando hay algo que añadir.
    """
    from config import KNOWN_HOSTS_FILE

    override = (KNOWN_HOSTS_FILE or "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        home = Path(os.environ.get("HOME") or Path.home()).expanduser()
        path = home / ".ssh" / "known_hosts"
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:  # pragma: no cover - el error real aparece al escribir
        pass
    return path


def _normalize_host_key(line: str) -> Optional[HostKey]:
    """Convierte una línea de ``known_hosts`` en ``(host, tipo, clave)``.

    Ignora comentarios, líneas vacías y marcadores de certificado/negación.
    Las entradas con host hasheado (``|1|...``) se conservan tal cual: no se
    puede comprobar su correspondencia sin resolver el hash, pero tampoco se
    deben corromper.
    """
    parts = line.split()
    if len(parts) < 3:
        return None
    host_or_marker, key_type, key = parts[0], parts[1], parts[2]
    if host_or_marker.startswith(("@", "!")):
        return None
    if key_type not in _KEY_TYPES:
        return None
    return (host_or_marker, key_type, key)


def parse_known_hosts(text: str) -> List[HostKey]:
    """Devuelve todas las claves válidas de un texto ``known_hosts``."""
    keys: List[HostKey] = []
    for line in (text or "").splitlines():
        parsed = _normalize_host_key(line)
        if parsed is not None:
            keys.append(parsed)
    return keys


def _read_known_hosts(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _normalize_scan_types(types: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Filtra los tipos de clave para que no puedan inyectar opciones."""
    if not types:
        return _SCAN_TYPES
    allowed = {t.lower() for t in _SCAN_TYPES}
    selected = tuple(t for t in (str(x).strip().lower() for x in types) if t in allowed)
    return selected or _SCAN_TYPES


def scan_host_keys(
    host: str,
    port: int = 22,
    timeout: float = 5.0,
    types: Optional[Iterable[str]] = None,
    max_retries: int = 1,
) -> List[HostKey]:
    """Obtiene las huellas SSH de ``host`` con ``ssh-keyscan``.

    Devuelve todas las claves que se hayan podido leer (una por tipo) o una
    lista vacía si el host no responde. El resultado no se escribe en disco.
    """
    host = (host or "").strip()
    # Evita inyección de opciones y hosts con espacios.
    if not host or host.startswith("-") or any(ch.isspace() for ch in host):
        logger.debug("keyscan saltado para host no válido: %r", host)
        return []

    selected = _normalize_scan_types(types)
    cmd = [
        "ssh-keyscan",
        "-p",
        str(int(port)),
        "-T",
        str(max(1, int(timeout))),
        "-t",
        ",".join(selected),
        host,
    ]
    attempts = max(1, int(max_retries))
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=float(timeout) + 2.0,
            )
        except FileNotFoundError:
            logger.warning("keyscan: ssh-keyscan no está instalado")
            return []
        except subprocess.TimeoutExpired:
            logger.warning(
                "keyscan: timeout al leer %s (intento %d/%d)",
                host,
                attempt,
                attempts,
            )
            continue
        except OSError as exc:
            logger.warning("keyscan: no se pudo ejecutar ssh-keyscan: %s", exc)
            return []

        if result.returncode != 0:
            logger.warning(
                "keyscan: ssh-keyscan falló para %s (rc=%d): %s",
                host,
                result.returncode,
                (result.stderr or "").strip()[:200],
            )
            continue

        expected_forms = {host}
        if int(port) != 22:
            expected_forms.add(f"[{host}]:{int(port)}")
        keys = [
            key
            for key in parse_known_hosts(result.stdout or "")
            # ssh-keyscan puede devolver comentarios con el nombre resuelto;
            # solo nos interesan las líneas del host solicitado.
            if key[0].split(",")[0] in expected_forms
        ]
        if keys:
            logger.info("keyscan: %d huella(s) para %s", len(keys), host)
            return keys
        logger.warning(
            "keyscan: sin huellas para %s (intento %d/%d)",
            host,
            attempt,
            attempts,
        )
    return []


def update_known_hosts(
    keys: Iterable[HostKey],
    path: Optional[Path] = None,
) -> Path:
    """Añade ``keys`` al ``known_hosts`` de forma atómica y con permisos 0600.

    Las líneas duplicadas (mismo host+tipo+clave) se omiten y el contenido
    existente se conserva.
    """
    target = path or known_hosts_path()
    existing = _read_known_hosts(target)
    existing_fingerprints = {
        f"{host}|{key_type}|{key}"
        for host, key_type, key in parse_known_hosts(existing)
    }

    lines_to_add: List[str] = []
    for host, key_type, key in keys:
        fingerprint = f"{host}|{key_type}|{key}"
        if fingerprint in existing_fingerprints:
            continue
        existing_fingerprints.add(fingerprint)
        lines_to_add.append(f"{host} {key_type} {key}")

    if not lines_to_add:
        logger.debug("update_known_hosts: sin líneas nuevas para %s", target)
        return target

    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        logger.warning("update_known_hosts: no se pudo crear %s: %s", target.parent, exc)
        return target

    new_text = existing.rstrip("\n")
    if new_text:
        new_text += "\n"
    new_text += "\n".join(lines_to_add) + "\n"

    # Escritura atómica: un lector (Ansible) nunca ve un fichero a medias.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".known_hosts-", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except OSError as exc:  # pragma: no cover - permisos/FS exóticos
        logger.warning("update_known_hosts: no se pudo escribir %s: %s", target, exc)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return target

    logger.info(
        "update_known_hosts: añadidas %d línea(s) a %s",
        len(lines_to_add),
        target,
    )
    return target


def ensure_host_keys(
    hosts: Iterable[str],
    port: int = 22,
    timeout: float = 5.0,
    types: Optional[Iterable[str]] = None,
    max_retries: int = 1,
    workers: int = 8,
) -> Dict[str, bool]:
    """Asegura que ``hosts`` tiene su huella en el ``known_hosts``.

    Devuelve ``{host: ok}``. ``ok`` es ``True`` cuando la huella ya estaba
    registrada o pudo obtenerse. Es ``False`` cuando el host no respondió y
    no había huella previa: Ansible fallará después por host key checking.

    Los hosts que faltan se escanean en paralelo (con un límite) para que un
    lote grande no convierta el primer acceso en una espera serial.
    """
    def _registered(known_hosts: set, host: str) -> bool:
        if host in known_hosts:
            return True
        return int(port) != 22 and f"[{host}]:{int(port)}" in known_hosts

    host_list = [str(host).strip() for host in hosts if str(host).strip()]
    path = known_hosts_path()
    known = {
        host for host, _key_type, _key in parse_known_hosts(_read_known_hosts(path))
    }

    missing = [host for host in host_list if not _registered(known, host)]
    if missing:
        found: List[HostKey] = []
        max_workers = max(1, min(int(workers), len(missing)))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers
        ) as pool:
            futures = {
                pool.submit(
                    scan_host_keys,
                    host,
                    port,
                    timeout,
                    types,
                    max_retries,
                ): host
                for host in missing
            }
            for future in concurrent.futures.as_completed(futures):
                host = futures[future]
                try:
                    keys = future.result()
                except Exception:  # pragma: no cover - salvaguarda
                    logger.exception("keyscan: fallo inesperado para %s", host)
                    keys = []
                if keys:
                    found.extend(keys)
                else:
                    logger.warning(
                        "known_hosts: no se pudo obtener la huella de %s; "
                        "Ansible fallará si no está ya registrada.",
                        host,
                    )

        if found:
            update_known_hosts(found, path=path)
            known = {
                host
                for host, _key_type, _key in parse_known_hosts(
                    _read_known_hosts(path)
                )
            }

    return {host: _registered(known, host) for host in host_list}
