# models/user.py
# Usuarios administradores de la aplicación (login con Flask-Login).
from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from config import resolve_db_path
from models.db import get_connection, init_schema
from services.audit_chain import (
    canonical_event,
    compute_hash,
    sign_checkpoint,
    verify_checkpoint,
)

logger = logging.getLogger("acc")

VALID_ROLES = ("viewer", "operator", "admin")

# Los nombres locales admiten los mismos caracteres que un usuario corporativo
# habitual (``jose.cu``, ``jose_cu`` o ``jose@dominio``). Se rechazan espacios
# y caracteres de control para que el nombre sea seguro en formularios y logs.
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


def validate_username(username: str) -> str:
    """Normaliza y valida el nombre de una cuenta local."""
    value = (username or "").strip()
    if not value:
        raise ValueError("El nombre de usuario es obligatorio.")
    if not USERNAME_PATTERN.match(value):
        raise ValueError(
            "El usuario solo puede contener letras, números, punto, guion, "
            "guion bajo o @ (máximo 64 caracteres, sin espacios)."
        )
    return value


def _default_db_path() -> str:
    """Ruta de SQLite resuelta en tiempo de ejecución (producción o instance/)."""
    return str(resolve_db_path())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class User(UserMixin):
    """Usuario administrador almacenado en SQLite."""

    def __init__(
        self,
        id: int,
        username: str,
        password_hash: str,
        is_active: bool = True,
        created_at: str | None = None,
        role: str = "admin",
        failed_attempts: int = 0,
        locked_until: str | None = None,
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
        return datetime.now(UTC) < self.locked_until

    def register_success(self, db_path: str | None = None) -> None:
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
        self, threshold: int, lock_minutes: int, db_path: str | None = None
    ) -> bool:
        """Suma un fallo y bloquea la cuenta al alcanzar el umbral.

        Devuelve ``True`` si la cuenta quedó bloqueada en esta llamada.
        """
        if db_path is None:
            db_path = _default_db_path()
        attempts = self.failed_attempts + 1
        locked_until = None
        if threshold > 0 and attempts >= threshold:
            locked_until = datetime.now(UTC) + timedelta(minutes=max(1, lock_minutes))
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
    def set_role(cls, username: str, role: str, db_path: str | None = None) -> bool:
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
    def init_db(db_path: str | None = None) -> None:
        """Crea o migra el esquema completo sin borrar datos existentes."""
        if db_path is None:
            db_path = _default_db_path()
        init_schema(db_path)

    @classmethod
    def create_or_update(
        cls,
        username: str,
        password: str,
        db_path: str | None = None,
        is_active: bool = True,
        role: str = "admin",
        auth_source: str = "local",
    ) -> User:
        """Crea el usuario o actualiza sus credenciales si ya existe.

        Sobre una cuenta **existente del mismo ``auth_source``** solo
        actualiza ``password_hash``, ``is_active`` y el bloqueo; el ``role``
        y el ``auth_source`` no se tocan (el upsert no puede degradar ROLES
        implicitamente). Si la cuenta existe con **otro** ``auth_source``
        (p. ej. aparecio una cuenta local mientras un primer login LDAP
        creaba la sombra), lanza ``ValueError`` para que el llamador
        rechaze el intento en lugar de sobrescribir la cuenta.

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
                "Una cuenta LDAPS no guarda contraseñas locales; " "pasa password='' ."
            )

        password_hash = (
            generate_password_hash(password) if auth_source == "local" else ""
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
                    failed_attempts = 0,
                    locked_until = NULL
                WHERE users.auth_source = excluded.auth_source
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
        if user is None:  # pragma: no cover - el upsert acaba de ejecutarse
            raise RuntimeError(
                f"No se pudo recuperar el usuario recién creado: {username}"
            )
        if user.auth_source != auth_source:
            raise ValueError(
                f"La cuenta '{username}' ya existe con origen "
                f"'{user.auth_source}': no se puede re-crear como "
                f"'{auth_source}'."
            )
        return user

    @classmethod
    def create_local(
        cls,
        username: str,
        password: str,
        role: str = "viewer",
        is_active: bool = True,
        db_path: str | None = None,
    ) -> User:
        """Crea una cuenta local nueva con contraseña propia.

        A diferencia de ``create_or_update``, exige que el nombre no exista:
        el alta desde la interfaz no debe sobrescribir la contraseña de una
        cuenta ya creada. El rol es explícito y la cuenta nace como ``local``.
        """
        if db_path is None:
            db_path = _default_db_path()
        username = validate_username(username)
        if role not in VALID_ROLES:
            raise ValueError(f"Rol no válido: {role}")
        if not password:
            raise ValueError("La contraseña es obligatoria.")
        if cls.get_by_username(username, db_path=db_path) is not None:
            raise ValueError(f"El usuario '{username}' ya existe.")

        password_hash = generate_password_hash(password)
        try:
            with get_connection(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO users
                        (username, password_hash, is_active, created_at, role,
                         auth_source)
                    VALUES (?, ?, ?, ?, ?, 'local')
                    """,
                    (
                        username,
                        password_hash,
                        1 if is_active else 0,
                        _now_iso(),
                        role,
                    ),
                )
        except sqlite3.IntegrityError as exc:  # carrera con otra alta
            raise ValueError(f"El usuario '{username}' ya existe.") from exc

        user = cls.get_by_username(username, db_path=db_path)
        if user is None:  # pragma: no cover - el insert acaba de ejecutarse
            raise RuntimeError(
                f"No se pudo recuperar el usuario recién creado: {username}"
            )
        return user

    _SELECT = (
        "SELECT id, username, password_hash, is_active, created_at, role, "
        "failed_attempts, locked_until, auth_source FROM users "
    )

    @classmethod
    def get_by_username(cls, username: str, db_path: str | None = None) -> User | None:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(
                cls._SELECT + "WHERE username = ?", (username,)
            ).fetchone()

        return cls(*row) if row else None

    @classmethod
    def get_by_id(cls, user_id: int, db_path: str | None = None) -> User | None:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            row = conn.execute(cls._SELECT + "WHERE id = ?", (user_id,)).fetchone()

        return cls(*row) if row else None

    @classmethod
    def count(cls, db_path: str | None = None) -> int:
        if db_path is None:
            db_path = _default_db_path()

        with get_connection(db_path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    @classmethod
    def all(cls, db_path: str | None = None) -> list[User]:
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
        cls, username: str, active: bool, db_path: str | None = None
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

    @classmethod
    def set_password(
        cls, username: str, password: str, db_path: str | None = None
    ) -> None:
        """Cambia la contraseña de una cuenta local y limpia su bloqueo.

        Las cuentas LDAPS no tienen contraseña local: su credencial vive en el
        directorio y aquí se rechaza el cambio para no dar una falsa sensación
        de que se ha modificado el acceso corporativo.
        """
        if db_path is None:
            db_path = _default_db_path()
        user = cls.get_by_username(username, db_path=db_path)
        if user is None:
            raise ValueError(f"El usuario '{username}' no existe.")
        if user.auth_source != "local":
            raise ValueError(
                "Las cuentas LDAPS validan su contraseña en el directorio; "
                "no se puede cambiar desde aquí."
            )
        if not password:
            raise ValueError("La contraseña es obligatoria.")
        with get_connection(db_path) as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, failed_attempts = 0, "
                "locked_until = NULL WHERE id = ?",
                (generate_password_hash(password), user.id),
            )

    @classmethod
    def delete_local(cls, username: str, db_path: str | None = None) -> bool:
        """Elimina una cuenta local. Las cuentas LDAPS no se borran aquí."""
        if db_path is None:
            db_path = _default_db_path()
        user = cls.get_by_username(username, db_path=db_path)
        if user is None:
            return False
        if user.auth_source != "local":
            raise ValueError(
                "Las cuentas LDAPS se gestionan en el directorio corporativo; "
                "desactívalas aquí si quieres bloquear su acceso."
            )
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM users WHERE id = ? AND auth_source = 'local'",
                (user.id,),
            )
            return bool(cursor.rowcount)

    @classmethod
    def count_active_local_admins_excluding(
        cls, username: str, db_path: str | None = None
    ) -> int:
        """Admins LOCALES activos distintos de ``username``.

        Lo usan las rutas de /usuarios para impedir que quede sin cuenta de
        administración local el sistema (el admin LDAPS depende del
        directorio; si deja de alcanzarse, nadie recupera el control).
        """
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM users "
                    "WHERE role = 'admin' AND is_active = 1 "
                    "AND auth_source = 'local' AND username != ?",
                    (username,),
                ).fetchone()[0]
            )


class AuthEvent:
    """Registro duradero de intentos de autenticación.

    Cada evento se encadena con el anterior mediante SHA-256
    (``event_hash = SHA256(prev_hash + canonical_event)``). La cadena se
    escribe dentro de una transacción ``BEGIN IMMEDIATE`` para que dos
    procesos no calculen el mismo ``prev_hash`` y la bifurquen.

    Nota de modelo de amenazas: la cadena detecta modificaciones y borrados
    internos, pero quien controla el fichero SQLite y el código puede
    reescribirla entera. Para detectar truncamientos se usan los checkpoints
    firmados (``create_checkpoint``/``verify_chain``) y su exportación.
    """

    @staticmethod
    def _last_hash(conn: sqlite3.Connection) -> str:
        row = conn.execute(
            """
            SELECT event_hash FROM auth_events
            WHERE event_hash <> ''
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        return str(row["event_hash"]) if row is not None else ""

    @staticmethod
    def record(
        username: str,
        ip: str,
        event: str,
        detail: str = "",
        db_path: str | None = None,
    ) -> None:
        if db_path is None:
            db_path = _default_db_path()
        timestamp = _now_iso()
        event_id = uuid.uuid4().hex
        username = username or ""
        ip = ip or ""
        detail = detail or ""
        canonical = canonical_event(
            event_id=event_id,
            timestamp=timestamp,
            username=username,
            ip=ip,
            event=event,
            detail=detail,
        )
        try:
            with get_connection(db_path) as conn:
                # Serializa el cálculo de prev_hash+hash con el resto de
                # escritores; SQLite no permite dos BEGIN IMMEDIATE a la vez.
                conn.execute("BEGIN IMMEDIATE")
                previous = AuthEvent._last_hash(conn)
                current = compute_hash(previous, canonical)
                conn.execute(
                    """
                    INSERT INTO auth_events
                        (timestamp, username, ip, event, detail,
                         event_id, prev_hash, event_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        username,
                        ip,
                        event,
                        detail,
                        event_id,
                        previous,
                        current,
                    ),
                )
        except sqlite3.Error as exc:
            # La auditoría es importante, pero no debe tumbar la operación
            # principal (por ejemplo, un login correcto). Se registra el
            # fallo para que sea visible y no se pierda en silencio.
            logger.warning(
                "No se pudo registrar el evento de auditoría '%s': %s", event, exc
            )

    @staticmethod
    def recent(limit: int = 100, db_path: str | None = None):
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            return conn.execute(
                "SELECT * FROM auth_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    @staticmethod
    def export_events(
        since_id: int = 0,
        limit: int = 10_000,
        db_path: str | None = None,
    ) -> list[dict]:
        """Exporta eventos en orden de inserción para anclaje externo."""
        if db_path is None:
            db_path = _default_db_path()
        limit = max(1, min(int(limit), 100_000))
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, username, ip, event, detail,
                       event_id, prev_hash, event_hash
                FROM auth_events
                WHERE id > ?
                ORDER BY id
                LIMIT ?
                """,
                (int(since_id), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def create_checkpoint(
        secret: str,
        note: str = "",
        db_path: str | None = None,
    ) -> dict:
        """Firma y guarda un ancla del estado actual de la cadena."""
        if db_path is None:
            db_path = _default_db_path()
        created_at = _now_iso()
        with get_connection(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, event_hash FROM auth_events
                WHERE event_hash <> ''
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            upto = int(row["id"]) if row is not None else 0
            last_hash = str(row["event_hash"]) if row is not None else ""
            signature = sign_checkpoint(secret, upto, last_hash, created_at)
            conn.execute(
                """
                INSERT INTO audit_checkpoints
                    (created_at, upto_event_id, last_hash, signature, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (created_at, upto, last_hash, signature, note or ""),
            )
        return {
            "created_at": created_at,
            "upto_event_id": upto,
            "last_hash": last_hash,
            "signature": signature,
            "note": note or "",
        }

    @staticmethod
    def latest_checkpoint(db_path: str | None = None) -> dict | None:
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM audit_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def verify_chain(
        db_path: str | None = None,
        secret: str | None = None,
    ) -> dict:
        """Recalcula la cadena y comprueba el último checkpoint.

        Devuelve un diccionario con ``ok`` (bool), el número de eventos
        verificados, el hash de cabeza y, si falla, el evento y el motivo.
        Los eventos anteriores a la v21 sin ``event_hash`` se consideran
        legado y solo se toleran al principio de la tabla.
        """
        if db_path is None:
            db_path = _default_db_path()
        with get_connection(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, username, ip, event, detail,
                       event_id, prev_hash, event_hash
                FROM auth_events ORDER BY id
                """
            ).fetchall()

        previous: str | None = None
        head = ""
        hashed = 0
        legacy = 0
        seen_ids: set[str] = set()
        for row in rows:
            event_hash = str(row["event_hash"] or "")
            if not event_hash:
                if hashed:
                    return {
                        "ok": False,
                        "events": len(rows),
                        "hashed": hashed,
                        "legacy": legacy,
                        "head": head,
                        "broken_at": int(row["id"]),
                        "reason": "evento sin hash después de eventos encadenados",
                    }
                legacy += 1
                continue
            event_id = str(row["event_id"] or "")
            if not event_id:
                return {
                    "ok": False,
                    "events": len(rows),
                    "hashed": hashed,
                    "legacy": legacy,
                    "head": head,
                    "broken_at": int(row["id"]),
                    "reason": "evento encadenado sin event_id",
                }
            if event_id in seen_ids:
                return {
                    "ok": False,
                    "events": len(rows),
                    "hashed": hashed,
                    "legacy": legacy,
                    "head": head,
                    "broken_at": int(row["id"]),
                    "reason": f"event_id duplicado: {event_id}",
                }
            seen_ids.add(event_id)
            if previous is not None and str(row["prev_hash"] or "") != previous:
                return {
                    "ok": False,
                    "events": len(rows),
                    "hashed": hashed,
                    "legacy": legacy,
                    "head": head,
                    "broken_at": int(row["id"]),
                    "reason": "el enlace con el evento anterior no coincide",
                }
            canonical = canonical_event(
                event_id=event_id,
                timestamp=row["timestamp"],
                username=row["username"],
                ip=row["ip"],
                event=row["event"],
                detail=row["detail"],
            )
            expected = compute_hash(str(row["prev_hash"] or ""), canonical)
            if expected != event_hash:
                return {
                    "ok": False,
                    "events": len(rows),
                    "hashed": hashed,
                    "legacy": legacy,
                    "head": head,
                    "broken_at": int(row["id"]),
                    "reason": "el hash del evento no coincide con su contenido",
                }
            previous = event_hash
            head = event_hash
            hashed += 1

        result = {
            "ok": True,
            "events": len(rows),
            "hashed": hashed,
            "legacy": legacy,
            "head": head,
            "broken_at": None,
            "reason": "",
            "checkpoint": None,
        }
        checkpoint = AuthEvent.latest_checkpoint(db_path=db_path)
        if checkpoint is not None:
            result["checkpoint"] = {
                "upto_event_id": int(checkpoint["upto_event_id"]),
                "created_at": checkpoint["created_at"],
                "verified_signature": (
                    verify_checkpoint(
                        secret or "",
                        int(checkpoint["upto_event_id"]),
                        str(checkpoint["last_hash"]),
                        str(checkpoint["created_at"]),
                        str(checkpoint["signature"]),
                    )
                    if secret
                    else None
                ),
            }
            last_row = rows[-1] if rows else None
            last_id = int(last_row["id"]) if last_row is not None else 0
            if int(checkpoint["upto_event_id"]) > last_id:
                result.update(
                    ok=False,
                    broken_at=last_id or None,
                    reason="el checkpoint apunta a eventos que ya no existen",
                )
            elif (
                last_row is not None
                and int(checkpoint["upto_event_id"]) == int(last_row["id"])
                and str(checkpoint["last_hash"]) != str(last_row["event_hash"] or "")
            ):
                result.update(
                    ok=False,
                    broken_at=last_id,
                    reason="la cabeza de la cadena no coincide con el checkpoint",
                )
        return result

    @staticmethod
    def purge_old(days: int, db_path: str | None = None) -> int:
        if db_path is None:
            db_path = _default_db_path()
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
        with get_connection(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM auth_events WHERE timestamp < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)
