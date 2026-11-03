# models/user.py
# Usuarios administradores de la aplicación (login con Flask-Login).
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from config import resolve_db_path
from models.db import get_connection, init_schema


def _default_db_path() -> str:
    """Ruta de SQLite resuelta en tiempo de ejecución (producción o instance/)."""
    return str(resolve_db_path())


class User(UserMixin):
    """Usuario administrador almacenado en SQLite."""

    def __init__(
        self,
        id: int,
        username: str,
        password_hash: str,
        is_active: bool = True,
        created_at: Optional[str] = None,
    ):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self._is_active = bool(is_active)
        self.created_at = created_at

    @property
    def is_active(self) -> bool:  # Flask-Login
        return self._is_active

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @staticmethod
    def init_db(db_path: str = None) -> None:
        """Crea o migra el esquema completo sin borrar datos existentes."""
        if db_path is None:
            db_path = _default_db_path()
        init_schema(db_path)

    @classmethod
    def create_or_update(
        cls,
        username: str,
        password: str,
        db_path: str = None,
        is_active: bool = True,
    ) -> "User":
        """Crea el usuario o actualiza su contraseña si ya existe."""
        if db_path is None:
            db_path = _default_db_path()

        password_hash = generate_password_hash(password)
        created_at = datetime.now(timezone.utc).isoformat()

        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, is_active, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    is_active = excluded.is_active
                """,
                (username, password_hash, 1 if is_active else 0, created_at),
            )

        user = cls.get_by_username(username, db_path=db_path)
        assert user is not None
        return user

    @classmethod
    def get_by_username(cls, username: str, db_path: str = None) -> Optional["User"]:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, is_active, created_at "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        return cls(*row) if row else None

    @classmethod
    def get_by_id(cls, user_id: int, db_path: str = None) -> Optional["User"]:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, is_active, created_at "
                "FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

        return cls(*row) if row else None

    @classmethod
    def count(cls, db_path: str = None) -> int:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
