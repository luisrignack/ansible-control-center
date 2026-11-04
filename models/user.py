# models/user.py
# Usuarios administradores de la aplicación (login con Flask-Login).
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from config import resolve_db_path
from models.db import get_connection, init_schema

VALID_ROLES = ("viewer", "operator", "admin")


def _default_db_path() -> str:
    """Ruta de SQLite resuelta en tiempo de ejecución (producción o instance/)."""
    return str(resolve_db_path())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class User(UserMixin):
    """Usuario administrador almacenado en SQLite."""

    def __init__(
        self,
        id: int,
        username: str,
        password_hash: str,
        is_active: bool = True,
        created_at: Optional[str] = None,
        role: str = "admin",
        failed_attempts: int = 0,
        locked_until: Optional[str] = None,
        auth_source: str = "local",
    ):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self._is_active = bool(is_active)
        self.created_at = created_at
        self.role = role if role in VALID_ROLES else "viewer"
        self.failed_attempts = int(failed_attempts or 0)
        self.locked_until = _parse_datetime(locked_until)
        # Origen de la cuenta: ``local`` (creada aquí, con contraseña) o
        # ``ldaps`` (creada al primer login corporativo exitoso, sin
        # contraseña local; el rol lo asigna el admin en /usuarios).
        self.auth_source = auth_source if auth_source in ("local", "ldaps") else "local"

    @property
    def is_active(self) -> bool:  # Flask-Login
        return self._is_active

    def check_password(self, password: str) -> bool:
        # Las cuentas LDAPS no tienen contraseña local: nunca coinciden.
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    # ------------------------------------------------------------------
    # Roles y bloqueo
    # ------------------------------------------------------------------
    def has_role(self, *roles: str) -> bool:
        """Devuelve ``True`` si el usuario tiene alguno de los roles dados."""
        return self.role in roles

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        return datetime.now(timezone.utc) < self.locked_until

    def register_success(self, db_path: str = None) -> None:
        """Reinicia el contador de fallos tras un login correcto."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE users SET failed_attempts = 0, locked_until = NULL "
                "WHERE id = ?",
                (self.id,),
            )
        self.failed_attempts = 0
        self.locked_until = None

    def register_failure(
        self, threshold: int, lock_minutes: int, db_path: str = None
    ) -> bool:
        """Suma un fallo y bloquea la cuenta al alcanzar el umbral.

        Devuelve ``True`` si la cuenta quedó bloqueada en esta llamada.
        """
        if db_path is None:
            db_path = _default_db_path()
        attempts = self.failed_attempts + 1
        locked_until = None
        if threshold > 0 and attempts >= threshold:
            locked_until = datetime.now(timezone.utc) + timedelta(
                minutes=max(1, lock_minutes)
            )
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE users SET failed_attempts = ?, locked_until = ? "
                "WHERE id = ?",
                (attempts, locked_until.isoformat() if locked_until else None, self.id),
            )
        self.failed_attempts = attempts
        self.locked_until = locked_until
        return locked_until is not None

    @classmethod
    def set_role(cls, username: str, role: str, db_path: str = None) -> bool:
        if role not in VALID_ROLES:
            raise ValueError(f"Rol no válido: {role}")
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "UPDATE users SET role = ? WHERE username = ?", (role, username)
            )
            return bool(cursor.rowcount)

    # ------------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------------
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
        role: str = "admin",
        auth_source: str = "local",
    ) -> "User":
        """Crea el usuario o actualiza su contraseña si ya existe.

        Con ``auth_source='ldaps'`` la cuenta es una sombra corporativa: no
        lleva contraseña local (``password_hash`` vacío) y su contraseña la
        valida el servidor LDAPS en cada login.
        """
        if db_path is None:
            db_path = _default_db_path()
        if role not in VALID_ROLES:
            raise ValueError(f"Rol no válido: {role}")
        if auth_source not in ("local", "ldaps"):
            raise ValueError(f"Origen de cuenta no válido: {auth_source}")
        if auth_source == "ldaps" and password:
            raise ValueError(
                "Una cuenta LDAPS no guarda contraseñas locales; "
                "pasa password='' ."
            )

        password_hash = (
            generate_password_hash(password)
            if auth_source == "local"
            else ""
        )
        created_at = _now_iso()

        with get_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO users
                    (username, password_hash, is_active, created_at, role,
                     auth_source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    is_active = excluded.is_active,
                    role = excluded.role,
                    auth_source = excluded.auth_source,
                    failed_attempts = 0,
                    locked_until = NULL
                """,
                (
                    username,
                    password_hash,
                    1 if is_active else 0,
                    created_at,
                    role,
                    auth_source,
                ),
            )

        user = cls.get_by_username(username, db_path=db_path)
        assert user is not None
        return user

    _SELECT = (
        "SELECT id, username, password_hash, is_active, created_at, role, "
        "failed_attempts, locked_until, auth_source FROM users "
    )

    @classmethod
    def get_by_username(cls, username: str, db_path: str = None) -> Optional["User"]:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                cls._SELECT + "WHERE username = ?", (username,)
            ).fetchone()

        return cls(*row) if row else None

    @classmethod
    def get_by_id(cls, user_id: int, db_path: str = None) -> Optional["User"]:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                cls._SELECT + "WHERE id = ?", (user_id,)
            ).fetchone()

        return cls(*row) if row else None

    @classmethod
    def count(cls, db_path: str = None) -> int:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    @classmethod
    def all(cls, db_path: str = None) -> list["User"]:
        """Todos los usuarios (administración de la página /usuarios)."""
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            rows = conn.execute(
                cls._SELECT + "ORDER BY username COLLATE NOCASE"
            ).fetchall()

        return [cls(*row) for row in rows]

    @classmethod
    def set_active(
        cls, username: str, active: bool, db_path: str = None
    ) -> bool:
        """Activa o desactiva la cuenta por nombre."""
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "UPDATE users SET is_active = ? WHERE username = ?",
                (1 if active else 0, username),
            )
            return bool(cursor.rowcount)


class AuthEvent:
    """Registro duradero de intentos de autenticación."""

    @staticmethod
    def record(
        username: str,
        ip: str,
        event: str,
        detail: str = "",
        db_path: str = None,
    ) -> None:
        if db_path is None:
            db_path = _default_db_path()
        try:
            with get_connection(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO auth_events
                        (timestamp, username, ip, event, detail)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (_now_iso(), username or "", ip or "", event, detail or ""),
                )
        except Exception:  # pragma: no cover - la auditoría no debe tumbar el login
            pass

    @staticmethod
    def recent(limit: int = 100, db_path: str = None):
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            return conn.execute(
                "SELECT * FROM auth_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    @staticmethod
    def purge_old(days: int, db_path: str = None) -> int:
        if db_path is None:
            db_path = _default_db_path()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        ).isoformat()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM auth_events WHERE timestamp < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)
