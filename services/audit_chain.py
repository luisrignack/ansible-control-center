"""Cadena criptográfica de eventos de auditoría.

Cada evento se serializa de forma determinista (JSON canónico: claves
ordenadas, sin espacios y UTF-8) y se encadena con el hash del evento
anterior::

    event_hash = SHA256(prev_hash + canonical_event)

El hash no sustituye a la base de datos ni proporciona inmutabilidad absoluta
frente a quien controla el fichero y el código: sirve para detectar
modificaciones, reordenaciones o borrados accidentales o posteriores, y junto
a los checkpoints firmados (``audit_checkpoints``) permite anclar el estado
ante un tercero.

Este módulo solo contiene funciones puras (sin SQLite ni Flask) para que lo
puedan usar tanto ``models.db`` (migración/backfill) como ``models.user``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

# Hash previo del primer evento encadenado.
GENESIS_HASH = ""

# Campos que forman parte del evento canónico. El orden de esta tupla es
# documental: la serialización usa ``sort_keys``.
CANONICAL_FIELDS = (
    "event_id",
    "timestamp",
    "username",
    "ip",
    "event",
    "detail",
)


def canonical_event(
    *,
    event_id: str,
    timestamp: str,
    username: str,
    ip: str,
    event: str,
    detail: str = "",
) -> str:
    """Serializa un evento de forma estable y sin ambigüedades.

    ``json.dumps`` con ``sort_keys`` y separadores fijos evita que dos
    representaciones distintas del mismo evento produzcan hashes distintos
    (por ejemplo, por el orden de inserción de claves o por espacios).
    ``ensure_ascii=False`` conserva los acentos tal cual, pero la cadena se
    codifica siempre en UTF-8 antes de hashear.
    """
    payload: dict[str, Any] = {
        "event_id": event_id or "",
        "timestamp": timestamp or "",
        "username": username or "",
        "ip": ip or "",
        "event": event or "",
        "detail": detail or "",
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def compute_hash(prev_hash: str, canonical: str) -> str:
    """Devuelve ``SHA256(prev_hash + canonical)`` en hexadecimal."""
    data = f"{prev_hash or ''}{canonical}".encode()
    return hashlib.sha256(data).hexdigest()


def chain_event(prev_hash: str, canonical: str) -> tuple[str, str]:
    """Atajo que devuelve ``(prev_hash, event_hash)`` normalizados."""
    previous = prev_hash or GENESIS_HASH
    return previous, compute_hash(previous, canonical)


def checkpoint_message(upto_event_id: int, last_hash: str, created_at: str) -> str:
    """Mensaje estable que se firma al crear un checkpoint."""
    return f"acc-audit-v1|{int(upto_event_id)}|{last_hash or ''}|{created_at or ''}"


def sign_checkpoint(
    secret: str, upto_event_id: int, last_hash: str, created_at: str
) -> str:
    """Firma HMAC-SHA256 de un checkpoint con la clave de la instancia.

    La firma impide que alguien con acceso de escritura a SQLite fabrique un
    checkpoint nuevo sin conocer el secreto. No protege frente a quien también
    controla la clave (por ejemplo, el administrador del servidor).
    """
    message = checkpoint_message(upto_event_id, last_hash, created_at).encode("utf-8")
    return hmac.new(
        (secret or "").encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


def verify_checkpoint(
    secret: str,
    upto_event_id: int,
    last_hash: str,
    created_at: str,
    signature: str,
) -> bool:
    """Comprueba en tiempo constante la firma de un checkpoint."""
    expected = sign_checkpoint(secret, upto_event_id, last_hash, created_at)
    return hmac.compare_digest(expected, signature or "")
