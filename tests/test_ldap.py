# Pruebas de autenticación corporativa LDAPS (cliente + login + /usuarios).
#
# Ningún test accede a la red: la conexión del cliente se sustituye con una
# ``FakeConnection`` que emula la interfaz ldap3 usada por el módulo.
from __future__ import annotations

import types

import pytest

import app as app_module
import config as config_module
import services.ldap_auth as ldap_auth
from models.user import User

LDAP_URL = "ldaps://lds.etecsa.cu:636"

JOSE_DN = "uid=jose.cu,ou=etecsa.cu,ou=people,dc=etecsa,dc=cu"
JOSE = {
    "dn": JOSE_DN,
    "attributes": {
        "uid": ["jose.cu"],
        "mail": ["jose@etecsa.cu"],
        "cn": ["Jose Perez"],
    },
}


# ---------------------------------------------------------------------------
# Fakes de conexión ldap3
# ---------------------------------------------------------------------------
class FakeConnection:
    """Emula lo que ldap_auth usa de ``Connection`` (sin red)."""

    def __init__(self, entries, *, bind_ok=True, rebind_ok=True,
                 rebind_description=""):
        self._entries = list(entries)
        self.bound = False
        self._bind_ok = bind_ok
        self._rebind_ok = rebind_ok
        self._rebind_description = rebind_description
        self.result = {} if bind_ok else {"description": "invalidCredentials"}
        self.response = []

    def bind(self):
        if self._bind_ok:
            self.bound = True
            return True
        return False

    @staticmethod
    def _uid_from(filter_str: str) -> str:
        import re

        match = re.search(r"\(uid=((?:[^()\\\\])+)\)", filter_str)
        if not match:
            return ""
        raw = match.group(1)
        # Des-escapar lo que ldap3.utils.conv escapó en la app real.
        return (
            raw.replace(r"\5c", r"\\")
            .replace(r"\2a", "*")
            .replace(r"\28", "(")
            .replace(r"\29", ")")
        )

    def search(self, base, filter_str, search_scope, attributes,
               size_limit, time_limit):
        uid = self._uid_from(filter_str)
        self.filter_str = filter_str
        self.search_base = base
        self.search_scope = search_scope
        self.response = [
            e for e in self._entries
            if uid in str(e["attributes"].get("uid", ""))
        ]

    def rebind(self, user: str, password: str):
        if self._rebind_ok and password == "clave-ldap":
            return True
        self.result = {
            "description": self._rebind_description or "invalidCredentials"
        }
        return False

    def unbind(self):
        self.bound = False


@pytest.fixture()
def fake_ldap(monkeypatch):
    """Fuerza config LDAPS activo y conexión falsa; devuelve la fábrica."""
    monkeypatch.setattr(config_module, "LDAP_ENABLED", True)
    monkeypatch.setattr(config_module, "LDAP_URL", LDAP_URL)
    monkeypatch.setattr(config_module, "LDAP_BIND_PASSWORD", "r0us3r")
    created: dict = {}
    settings: dict = {"entries": [JOSE], "bind_ok": True, "rebind_ok": True,
                      "rebind_description": ""}

    def factory(entries=None, **kwargs):
        conn = FakeConnection(
            entries if entries is not None else settings["entries"],
            bind_ok=settings["bind_ok"],
            rebind_ok=settings["rebind_ok"],
            rebind_description=settings["rebind_description"],
        )
        created["conn"] = conn
        return conn

    monkeypatch.setattr(ldap_auth, "_connect", factory)
    created["settings"] = settings
    return created


def _logout(client):
    return _post(client, "/logout")


# ---------------------------------------------------------------------------
# Fixture de app (igual que tests/test_app.py)
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update(
        "admin", "admin-password-123", db_path=db_path
    )
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def _csrf_token(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _post(client, url, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload, **kwargs)


def _login_admin(client):
    return _post(
        client, "/login",
        {"username": "admin", "password": "admin-password-123"},
    )


def _login_ldap(client, fake_ldap, username="jose.cu", password="clave-ldap"):
    return _post(
        client, "/login", {"username": username, "password": password}
    )


# ---------------------------------------------------------------------------
# Cliente LDAPS (services/ldap_auth.py)
# ---------------------------------------------------------------------------
def test_parse_ldap_url_supports_ports_and_schemes():
    assert ldap_auth.parse_ldap_url("ldaps://lds.etecsa.cu") == (
        "lds.etecsa.cu", 636, True)
    # ldap:// solo se admite de forma explícita; por defecto se rechaza.
    assert ldap_auth.parse_ldap_url(
        "ldap://lds.etecsa.cu:389", allow_insecure=True) == (
        "lds.etecsa.cu", 389, False)
    with pytest.raises(ldap_auth.LdapError, match="sin cifrar"):
        ldap_auth.parse_ldap_url(
            "ldap://lds.etecsa.cu:389", allow_insecure=False)
    with pytest.raises(ldap_auth.LdapError):
        ldap_auth.parse_ldap_url("no-es-una-url")


def test_search_filter_escapes_injection_chars():
    filter_str = ldap_auth._search_filter('jose*)(uid=admin')
    assert "(uid=jose" in filter_str
    # El asterisco y los paréntesis fueron escapados: no rompen el filtro.
    assert filter_str.count("\\") >= 3


def test_search_user_returns_entry(fake_ldap):
    user = ldap_auth.search_user("jose.cu")
    assert user is not None
    assert user.username == "jose.cu"
    assert user.email == "jose@etecsa.cu"
    assert user.display_name == "Jose Perez"
    assert "uid=jose.cu" in user.dn


def test_search_user_unknown_uid_returns_none(fake_ldap):
    assert ldap_auth.search_user("nadie.tal") is None


def test_search_user_multiple_entries_raises(fake_ldap):
    conn = FakeConnection([JOSE, dict(JOSE)])
    conn.response = [JOSE, dict(JOSE)]
    with pytest.raises(ldap_auth.LdapError):
        ldap_auth.search_user("jose.cu", connection=conn)


def test_service_bind_failure_raises_clear_hint(fake_ldap):
    fake_ldap["settings"]["bind_ok"] = False
    with pytest.raises(ldap_auth.LdapError) as excinfo:
        ldap_auth.authenticate("jose.cu", "clave-ldap")
    assert "ACC_LDAP_BIND_DN" in str(excinfo.value)


def test_bind_socket_error_becomes_ldap_error():
    """Un fallo de TLS/red al conectar se reporta como LdapError, no en crudo."""

    class Broken:
        bound = False
        result = {}

        def bind(self):
            raise RuntimeError(
                "('socket ssl wrapping error: [SSL: CERTIFICATE_VERIFY_FAILED] "
                "certificate verify failed: self-signed certificate in "
                "certificate chain',)"
            )

    with pytest.raises(ldap_auth.LdapError) as excinfo:
        ldap_auth._bind_or_fail(Broken(), "servicio de búsqueda")
    message = str(excinfo.value)
    assert "servicio de búsqueda" in message
    assert "ACC_LDAP_TLS_CA_FILE" in message


def test_invalid_password_returns_none_not_created(fake_ldap):
    fake_ldap["settings"]["rebind_ok"] = False
    result = ldap_auth.authenticate("jose.cu", "contraseña-mala")
    assert result is None


def test_correct_password_returns_user(fake_ldap):
    user = ldap_auth.authenticate("jose.cu", "clave-ldap")
    assert user is not None and user.username == "jose.cu"


def test_ca_file_missing_raises(fake_ldap, tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(config_module, "LDAP_TLS_CA_FILE",
                            str(tmp_path / "no-existe.crt"))
        with pytest.raises(ldap_auth.LdapError):
            ldap_auth._tls_object()
    finally:
        monkeypatch.undo()


def test_empty_password_never_touched_ldap(fake_ldap):
    assert ldap_auth.authenticate("jose.cu", "") is None


# ---------------------------------------------------------------------------
# Login vía LDAPS
# ---------------------------------------------------------------------------
def test_ldap_first_login_creates_viewer_shadow(client, fake_ldap):
    assert User.count(db_path=app_module.get_db_path()) == 1  # solo admin
    response = _login_ldap(client, fake_ldap)
    assert response.status_code == 302

    shadow = User.get_by_username("jose.cu", db_path=app_module.get_db_path())
    assert shadow is not None
    assert shadow.auth_source == "ldaps"
    assert shadow.role == config_module.LDAP_DEFAULT_ROLE == "viewer"
    assert shadow.password_hash == ""


# Entrada extra de directorio para probar promoción/desactivación de cuentas.
PEPE = {
    "dn": "uid=pepe.cu,ou=etecsa.cu,ou=people,dc=etecsa,dc=cu",
    "attributes": {"uid": ["pepe.cu"], "mail": [], "cn": ["Pepe"]},
}


def test_ldap_second_login_reuses_shadow(client, fake_ldap):
    _login_ldap(client, fake_ldap)
    _post(client, "/logout")
    response = _login_ldap(client, fake_ldap)
    assert response.status_code == 302


def test_ldap_wrong_password_creates_nothing(client, fake_ldap):
    fake_ldap["settings"]["rebind_ok"] = False
    response = _login_ldap(client, fake_ldap, password="contraseña-mala")
    assert response.status_code == 200
    assert b"Incorrectos" in response.data or b"incorrectos" in response.data
    assert User.count(db_path=app_module.get_db_path()) == 1


def test_ldap_unknown_uid_creates_nothing(client, fake_ldap):
    response = _login_ldap(client, fake_ldap, username="nadie.tal")
    assert response.status_code == 200
    assert User.count(db_path=app_module.get_db_path()) == 1


def test_ldap_local_username_is_rejected(client, fake_ldap, monkeypatch):
    """Un uid del directorio no debe secuestrar la cuenta local 'admin'."""
    conn = FakeConnection([{
        "dn": "uid=admin,ou=etecsa.cu,ou=people,dc=etecsa,dc=cu",
        "attributes": {"uid": ["admin"], "cn": ["Impostor"]},
    }])
    monkeypatch.setattr(ldap_auth, "_connect", lambda *a, **kw: conn)

    response = _post(client, "/login", {
        "username": "admin", "password": "clave-ldap"
    })
    # Sin la contraseña local correcta, el login LDAP con ese nombre es
    # rechazado aunque el directorio lo acepte. El mensaje al navegador es
    # genérico: no revela qué nombres son cuentas locales (enumeración).
    assert response.status_code == 200
    assert b"Usuario o contrase" in response.data
    assert b"cuenta local" not in response.data
    admin = User.get_by_username("admin", db_path=app_module.get_db_path())
    assert admin.auth_source == "local"
    # El detalle queda en la auditoría.
    import sqlite3

    with sqlite3.connect(app_module.get_db_path()) as conn:
        events = conn.execute(
            "SELECT event FROM auth_events WHERE username = 'admin'"
        ).fetchall()
    assert any(e[0] == "login_rejected_local_account" for e in events)


def test_local_login_still_works_with_ldap_enabled(client, fake_ldap):
    response = _login_admin(client)
    assert response.status_code == 302


def test_ldap_unreachable_flashes_service_error(client, fake_ldap, monkeypatch):
    def boom(*_args, **_kwargs):
        raise ldap_auth.LdapError("Servidor no responde")

    monkeypatch.setattr(ldap_auth, "authenticate", boom)
    response = _login_ldap(client, fake_ldap)
    assert response.status_code == 200
    assert "no disponible" in response.get_data(as_text=True)
    assert User.count(db_path=app_module.get_db_path()) == 1


def test_ldap_disabled_falls_back_to_local_failure(client, fake_ldap,
                                                   monkeypatch):
    monkeypatch.setattr(config_module, "LDAP_ENABLED", False)
    response = _login_ldap(client, fake_ldap)
    assert response.status_code == 200
    assert User.count(db_path=app_module.get_db_path()) == 1


# ---------------------------------------------------------------------------
# Página /usuarios (solo admin)
# ---------------------------------------------------------------------------
def test_usuarios_rejects_anonymous_and_non_admin(client, fake_ldap):
    assert client.get("/usuarios").status_code == 302

    User.create_or_update(
        "pepe.cu", "", db_path=app_module.get_db_path(),
        role="viewer", auth_source="ldaps",
    )
    import services.ldap_auth as ldap_module

    ldap_module._connect = lambda *a, **kw: FakeConnection([JOSE, PEPE])

    response = _login_ldap(client, fake_ldap, username="pepe.cu")
    assert response.status_code == 302
    # pepe.cu (viewer) no ve /usuarios
    assert client.get("/usuarios").status_code == 403


def test_usuarios_page_lists_local_and_ldap_accounts(client, fake_ldap):
    User.create_or_update(
        "maria.cu", "", db_path=app_module.get_db_path(),
        role="viewer", auth_source="ldaps",
    )
    _login_admin(client)
    body = client.get("/usuarios").get_data(as_text=True)
    assert "maria.cu" in body
    assert "LDAPS" in body
    assert "admin-password-123" not in body


def test_admin_assigns_role_to_ldap_user(client, fake_ldap):
    _login_ldap(client, fake_ldap)  # crea jose.cu (viewer)
    _logout(client)
    _login_admin(client)
    response = _post(client, "/usuarios/rol", {
        "username": "jose.cu", "role": "operator",
    })
    assert response.status_code == 302
    shadow = User.get_by_username("jose.cu", db_path=app_module.get_db_path())
    assert shadow.role == "operator"


def test_admin_cannot_change_own_role(client, fake_ldap):
    _login_admin(client)
    response = _post(client, "/usuarios/rol", {
        "username": "admin", "role": "viewer",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "No puedes cambiar tu propio rol" in response.get_data(as_text=True)
    admin = User.get_by_username("admin", db_path=app_module.get_db_path())
    assert admin.is_admin


def test_set_role_validates_unknown_role(client, fake_ldap):
    User.create_or_update(
        "ana.cu", "", db_path=app_module.get_db_path(),
        role="viewer", auth_source="ldaps",
    )
    _login_admin(client)
    response = _post(client, "/usuarios/rol", {
        "username": "ana.cu", "role": "superusuario",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "Rol no válido" in response.get_data(as_text=True)


def test_admin_disables_and_reenables_ldap_account(client, fake_ldap):
    User.create_or_update(
        "luis.cu", "", db_path=app_module.get_db_path(),
        role="viewer", auth_source="ldaps",
    )
    _login_admin(client)
    response = _post(client, "/usuarios/estado", {
        "username": "luis.cu", "active": "0",
    })
    assert response.status_code == 302
    disabled = User.get_by_username(
        "luis.cu", db_path=app_module.get_db_path()
    )
    assert not disabled.is_active

    # Una cuenta desactivada no puede volver a entrar (login LDAP).
    _logout(client)
    response = _login_ldap(client, fake_ldap, username="luis.cu")
    assert response.status_code == 200
    assert b"desactivada" in response.data

    # Y el admin la reactiva.
    _login_admin(client)
    response = _post(client, "/usuarios/estado", {
        "username": "luis.cu", "active": "1",
    })
    assert response.status_code == 302
    active = User.get_by_username("luis.cu", db_path=app_module.get_db_path())
    assert active.is_active


def test_admin_cannot_disable_self(client, fake_ldap):
    _login_admin(client)
    response = _post(client, "/usuarios/estado", {
        "username": "admin", "active": "0",
    }, follow_redirects=True)
    assert response.status_code == 200
    assert "No puedes desactivar" in response.get_data(as_text=True)
    assert User.get_by_username(
        "admin", db_path=app_module.get_db_path()
    ).is_active


# ---------------------------------------------------------------------------
# Interruptor del acceso LDAPS (solo admin local)
# ---------------------------------------------------------------------------
def test_ldap_access_toggle_persists_and_gates_login(client, fake_ldap,
                                                    monkeypatch):
    # Estado inicial: ACC_LDAP_ENABLED=0 (fixture fake_ldap lo activó).
    monkeypatch.setattr(config_module, "LDAP_ENABLED", False)
    assert app_module.load_ldap_access_setting() is False

    _login_admin(client)
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert response.status_code == 200
    assert "Acceso por LDAPS activado." in response.get_data(as_text=True)
    assert app_module.load_ldap_access_setting() is True

    # El login LDAP funciona mientras el interruptor está en ON...
    _logout(client)
    response = _login_ldap(client, fake_ldap)
    assert response.status_code == 302

    _logout(client)
    _login_admin(client)
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert "desactivado" in response.get_data(as_text=True).lower()
    assert app_module.load_ldap_access_setting() is False

    # ...y fuera, el login LDAP falla sin tocar el directorio (la sombra
    # creada mientras estuvo ON se queda en la BD).
    _logout(client)
    response = _login_ldap(client, fake_ldap)
    assert response.status_code == 200
    assert User.count(db_path=app_module.get_db_path()) == 2  # admin + jose.cu


def test_ldap_toggle_requires_local_admin(client, fake_ldap):
    """Un LDAP promovido a admin administra cuentas, no el acceso."""
    User.create_or_update(
        "jefe.cu", "", db_path=app_module.get_db_path(),
        role="admin", auth_source="ldaps",
    )
    jefe = {
        "dn": "uid=jefe.cu,ou=etecsa.cu,ou=people,dc=etecsa,dc=cu",
        "attributes": {"uid": ["jefe.cu"], "mail": [], "cn": ["Jefe"]},
    }
    ldap_auth._connect = lambda *a, **kw: FakeConnection([JOSE, jefe])

    _login_ldap(client, fake_ldap, username="jefe.cu")
    # Puede ver /usuarios, pero sin botón (no es admin local).
    body = client.get("/usuarios").get_data(as_text=True)
    assert "Activar acceso LDAPS" not in body
    assert "Desactivar acceso LDAPS" not in body
    # Y la ruta le devuelve 403.
    assert _post(client, "/usuarios/ldaps", {}).status_code == 403


def test_ldap_toggle_without_directory_config(client, fake_ldap,
                                              monkeypatch):
    monkeypatch.setattr(config_module, "LDAP_URL", "")
    monkeypatch.setattr(config_module, "LDAP_ENABLED", False)
    _login_admin(client)
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert response.status_code == 200
    assert "no está configurado" in response.get_data(as_text=True)
    assert app_module.load_ldap_access_setting() is False


# ---------------------------------------------------------------------------
# Refuerzos de seguridad de la revisión
# ---------------------------------------------------------------------------
JEFE = {
    "dn": "uid=jefe.cu,ou=etecsa.cu,ou=people,dc=etecsa,dc=cu",
    "attributes": {"uid": ["jefe.cu"], "mail": [], "cn": ["Jefe"]},
}


def test_safe_redirect_rejects_backslash_trick():
    """``/\\host`` se normaliza como protocolo relativo: no es destino válido."""
    assert app_module._is_safe_redirect("/tarea/x")
    assert app_module._is_safe_redirect("/tarea/x?q=1")
    assert not app_module._is_safe_redirect("//evil.com")
    assert not app_module._is_safe_redirect("/\\evil.com")
    assert not app_module._is_safe_redirect("https://evil.com")
    assert not app_module._is_safe_redirect("")


def test_login_rejects_empty_username_to_ldap(client, fake_ldap, monkeypatch):
    """Sin nombre no se consulta el directorio: fallo genérico local."""
    seen = []
    monkeypatch.setattr(ldap_auth, "authenticate", lambda u, p: seen.append(u))
    response = _post(client, "/login", {"username": "", "password": "algo"})
    assert response.status_code == 200
    assert seen == []
    assert b"Usuario o contrase" in response.data


def test_ldap_activation_checks_search_bind(client, fake_ldap):
    """Activar LDAPS exige que el bind del servicio funcione ya mismo."""
    _login_admin(client)

    # Estado inicial del fixture: ON (false_ldap LDAP_ENABLED=True). Se
    # desactiva para probar la ruta de activación.
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert "desactivado" in response.get_data(as_text=True).lower()

    # Bind roto: no se activa y el estado queda en OFF.
    fake_ldap["settings"]["bind_ok"] = False
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert response.status_code == 200
    assert "No se pudo activar" in response.get_data(as_text=True)
    assert app_module.load_ldap_access_setting() is False

    # Bind OK: activación normal.
    fake_ldap["settings"]["bind_ok"] = True
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert "Acceso por LDAPS activado." in response.get_data(as_text=True)
    assert app_module.load_ldap_access_setting() is True

    # Desactivar no necesita contactar el directorio.
    fake_ldap["settings"]["bind_ok"] = False
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert "desactivado" in response.get_data(as_text=True).lower()
    assert app_module.load_ldap_access_setting() is False


def test_ldap_activation_tls_failure_renders_page_not_500(client, fake_ldap,
                                                         monkeypatch):
    """Una CA no confiada avisa en /usuarios en vez de romper con 500."""
    _login_admin(client)
    # Deja el interruptor en OFF para probar la ruta de activación.
    _post(client, "/usuarios/ldaps", {}, follow_redirects=True)

    def _tls_failure(*args, **kwargs):
        raise RuntimeError(
            "('socket ssl wrapping error: [SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed: self-signed certificate in "
            "certificate chain',)"
        )

    monkeypatch.setattr(ldap_auth, "_connect", _tls_failure)
    response = _post(client, "/usuarios/ldaps", {}, follow_redirects=True)
    assert response.status_code == 200
    assert "No se pudo activar" in response.get_data(as_text=True)
    assert app_module.load_ldap_access_setting() is False


def test_ldap_admin_cannot_demote_or_disable_last_local_admin(client,
                                                              fake_ldap):
    """Un admin LDAPS no deja al sistema sin administración local."""
    User.create_or_update(
        "jefe.cu", "", db_path=app_module.get_db_path(),
        role="admin", auth_source="ldaps",
    )
    # Solo hay un admin local activo: 'admin'.
    ldap_auth._connect = lambda *a, **kw: FakeConnection([JEFE])
    login = _login_ldap(client, fake_ldap, username="jefe.cu")
    assert login.status_code == 302

    response = _post(client, "/usuarios/estado", {
        "username": "admin", "active": "0",
    }, follow_redirects=True)
    assert "último administrador local" in response.get_data(as_text=True)
    assert User.get_by_username(
        "admin", db_path=app_module.get_db_path()
    ).is_active

    response = _post(client, "/usuarios/rol", {
        "username": "admin", "role": "viewer",
    }, follow_redirects=True)
    assert "último administrador local" in response.get_data(as_text=True)
    assert User.get_by_username(
        "admin", db_path=app_module.get_db_path()
    ).is_admin


def test_ldap_admin_can_manage_when_local_admins_remain(client, fake_ldap):
    """Con dos admins locales, quitar uno de ellos sí está permitido."""
    User.create_or_update(
        "maria.loc", "otra-clave-123", db_path=app_module.get_db_path(),
        role="admin", auth_source="local",
    )
    User.create_or_update(
        "jefe.cu", "", db_path=app_module.get_db_path(),
        role="admin", auth_source="ldaps",
    )
    ldap_auth._connect = lambda *a, **kw: FakeConnection([JEFE])
    assert _login_ldap(client, fake_ldap, username="jefe.cu").status_code == 302

    response = _post(client, "/usuarios/rol", {
        "username": "maria.loc", "role": "operator",
    })
    assert response.status_code == 302
    assert User.get_by_username(
        "maria.loc", db_path=app_module.get_db_path()
    ).role == "operator"

    response = _post(client, "/usuarios/estado", {
        "username": "maria.loc", "active": "0",
    })
    assert response.status_code == 302
    assert not User.get_by_username(
        "maria.loc", db_path=app_module.get_db_path()
    ).is_active


# ---------------------------------------------------------------------------
# Semántica de User.create_or_update
# ---------------------------------------------------------------------------
def test_create_or_update_preserves_role_of_existing_account(tmp_path):
    """El upsert no degrada roles: recrear una cuenta no cambia su rol."""
    db_path = str(tmp_path / "users.db")
    User.init_db(db_path)
    User.create_or_update("op.loc", "clave-1", db_path=db_path, role="operator")
    User.create_or_update(
        "op.loc", "clave-2", db_path=db_path, role="admin"
    )
    user = User.get_by_username("op.loc", db_path=db_path)
    assert user.role == "operator"
    assert user.auth_source == "local"
    assert user.check_password("clave-2")


def test_create_or_update_refuses_conflicting_auth_source(tmp_path):
    """Un upsert nunca transforma una cuenta local en LDAPS (ni al revés)."""
    db_path = str(tmp_path / "users.db")
    User.init_db(db_path)
    User.create_or_update("dueno.loc", "clave-local", db_path=db_path)

    with pytest.raises(ValueError, match="origen"):
        User.create_or_update(
            "dueno.loc", "", db_path=db_path,
            role="viewer", auth_source="ldaps",
        )
    intact = User.get_by_username("dueno.loc", db_path=db_path)
    assert intact.auth_source == "local"
    assert intact.check_password("clave-local")

    # Caso inverso: tampoco se puede crear local sobre una sombra LDAPS.
    User.create_or_update(
        "sombra.cu", "", db_path=db_path, role="viewer", auth_source="ldaps"
    )
    with pytest.raises(ValueError, match="origen"):
        User.create_or_update(
            "sombra.cu", "clave-local", db_path=db_path, auth_source="local"
        )
    clean = User.get_by_username("sombra.cu", db_path=db_path)
    assert clean.auth_source == "ldaps"
    assert not clean.password_hash
