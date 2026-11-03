# models/password.py
# Modelo SQLite para el almacenamiento de contraseñas candidatas (hash, fecha, origen).
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import resolve_db_path
from models.db import get_connection, init_schema


def _default_db_path() -> str:
    """Ruta de SQLite resuelta en tiempo de ejecución (producción o instance/)."""
    return str(resolve_db_path())


class PasswordRecord:
    """Registro de una contraseña candidata almacenada."""

    @staticmethod
    def hash_full(password: str) -> str:
        """SHA-256 completo de una contraseña (nunca se guarda el texto)."""
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    @staticmethod
    def hash_short(password: str) -> str:
        """Hash corto de 8 caracteres para mostrar en la interfaz."""
        return PasswordRecord.hash_full(password)[:8]

    def __init__(
        self,
        id: int,
        password_hash: str,
        creation_date: datetime,
        origin: str,
    ):
        self.id = id
        self.password_hash = password_hash  # Hash SHA256 corto (8 caracteres) para UI
        self.creation_date = creation_date
        self.origin = origin  # 'manual' o 'file'

    def to_dict(self) -> Dict[str, Any]:
        """Convierte el registro a diccionario (solo hash corto, nunca texto plano)."""
        return {
            "id": self.id,
            "password_hash": self.password_hash,  # Siempre los 8 caracteres cortos
            "creation_date": self.creation_date.isoformat()
            if self.creation_date
            else None,
            "origin": self.origin,
        }

    @staticmethod
    def init_db(db_path: str = None) -> None:
        """Crea o migra el esquema completo sin borrar datos existentes."""
        if db_path is None:
            db_path = _default_db_path()
        init_schema(db_path)

    @staticmethod
    def store_password(
        password: str,
        origin: str,
        db_path: str = None,
    ) -> "PasswordRecord":
        """Almacena una contraseña en la base de datos.

        Solo se persisten hashes (SHA-256 corto para la UI y completo para
        comparaciones); nunca el texto plano.
        """
        if db_path is None:
            db_path = _default_db_path()

        password_hash_full = PasswordRecord.hash_full(password)
        password_hash_short = PasswordRecord.hash_short(password)
        creation_date = datetime.now(timezone.utc)

        with get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO passwords
                    (password_hash_short, password_hash_full, creation_date, origin)
                VALUES (?, ?, ?, ?)
                """,
                (
                    password_hash_short,
                    password_hash_full,
                    creation_date.isoformat(),
                    origin,
                ),
            )
            password_id = cursor.lastrowid

        return PasswordRecord(
            id=password_id,
            password_hash=password_hash_short,
            creation_date=creation_date,
            origin=origin,
        )

    @staticmethod
    def get_all_passwords(
        limit: int = 100, db_path: str = None
    ) -> List["PasswordRecord"]:
        """Obtiene las contraseñas almacenadas (solo hashes)."""
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, password_hash_short, creation_date, origin
                FROM passwords
                ORDER BY creation_date DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            PasswordRecord(
                id=row["id"],
                password_hash=row["password_hash_short"],
                creation_date=datetime.fromisoformat(row["creation_date"])
                if row["creation_date"]
                else datetime.min,
                origin=row["origin"],
            )
            for row in rows
        ]

    @staticmethod
    def get_hash_full(password_id: int, db_path: str = None) -> Optional[str]:
        """Devuelve el hash completo de una contraseña por su ID."""
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT password_hash_full FROM passwords WHERE id = ?",
                (password_id,),
            ).fetchone()
        return row["password_hash_full"] if row else None

    @staticmethod
    def exists_by_hash_full(password_hash_full: str, db_path: str = None) -> bool:
        """Indica si ya hay una contraseña con ese hash completo."""
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM passwords WHERE password_hash_full = ? LIMIT 1",
                (password_hash_full,),
            ).fetchone()
        return row is not None

    @staticmethod
    def delete_password(password_id: int, db_path: str = None) -> bool:
        """Elimina una contraseña por su ID."""
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM passwords WHERE id = ?",
                (password_id,),
            )
            return cursor.rowcount > 0

    @staticmethod
    def get_password_count(db_path: str = None) -> int:
        """Obtiene el total de contraseñas almacenadas."""
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM passwords").fetchone()[0]
            )
