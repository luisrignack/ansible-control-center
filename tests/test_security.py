# tests/test_security.py
# Pruebas de CSRF, rate limiting y límites de red (fase P3).
from __future__ import annotations

import re
from pathlib import Path

import pytest

import app as app_module
from acc import bootstrap as acc_bootstrap
from acc import runtime
from services import rate_limiter as rate_limiter_module
from services.csrf import (
    generate_csrf_token,
    rotate_csrf_token,
    validate_csrf_token,
)
from services.host_resolver import (
    pin_target,
    resolve_target,
    validate_ip_format,
    validate_target,
)
from services.rate_limiter import RateLimiter


def test_csrf_token_roundtrip_and_rotation():
    with app_module.app.test_request_context("/"):
        token = generate_csrf_token()
        assert token
        assert validate_csrf_token(token)
        assert not validate_csrf_token("token-invalido")

        new_token = rotate_csrf_token()
        assert new_token != token
        assert not validate_csrf_token(token)
        assert validate_csrf_token(new_token)


def test_csrf_rejects_empty_token():
    with app_module.app.test_request_context("/"):
        generate_csrf_token()
        assert not validate_csrf_token(None)
        assert not validate_csrf_token("")


def test_templates_do_not_use_inline_event_handlers():
    """La CSP basada en nonce bloquea los handlers inline; usar data-confirm."""
    templates = Path(__file__).resolve().parent.parent / "templates"
    offenders = []
    for template in templates.rglob("*.html"):
        content = template.read_text(encoding="utf-8")
        if "onclick=" in content or "onsubmit=" in content:
            offenders.append(template.name)
    assert offenders == []

    base = (templates / "base.html").read_text(encoding="utf-8")
    assert "dataset.confirm" in base


def test_vault_master_password_fields_disable_autofill():
    """La contraseña maestra del vault no debe autocompletarse ni persistirse.

    Un gestor de contraseñas que rellena el campo deja la clave en el DOM y
    puede ofrecerla en la siguiente sesión. Los campos deben pedir
    ``autocomplete="new-password"`` y marcar las extensiones conocidas.
    """
    templates = Path(__file__).resolve().parent.parent / "templates"
    pattern = re.compile(
        r'<input[^>]*(?:name="master_password"|id="[^"]*[Mm]aster[^"]*")[^>]*>'
    )
    fields = []
    for template in templates.rglob("*.html"):
        content = template.read_text(encoding="utf-8")
        for tag in pattern.findall(content):
            fields.append((template.name, tag))

    assert {"index.html", "credenciales.html", "ejecutar_aprobacion.html"} <= {
        name for name, _ in fields
    }

    for name, tag in fields:
        assert 'autocomplete="new-password"' in tag, (name, tag)
        assert 'autocomplete="current-password"' not in tag, (name, tag)
        for marker in (
            "data-lpignore",
            "data-1p-ignore",
            "data-bwignore",
            "data-protonpass-ignore",
        ):
            assert marker in tag, (name, tag)

    base = (templates / "base.html").read_text(encoding="utf-8")
    assert "hardenVaultMasterFields" in base


def test_rate_limiter_blocks_and_recovers(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "monotonic", lambda: now[0])

    limiter = RateLimiter()
    assert limiter.check("clave", 2, 60) == (True, 0)
    assert limiter.check("clave", 2, 60) == (True, 0)

    allowed, retry_after = limiter.check("clave", 2, 60)
    assert allowed is False
    assert retry_after >= 1

    now[0] += 61
    assert limiter.check("clave", 2, 60) == (True, 0)


def test_rate_limiter_keys_are_independent():
    limiter = RateLimiter()
    assert limiter.check("a", 1, 60)[0] is True
    assert limiter.check("a", 1, 60)[0] is False
    assert limiter.check("b", 1, 60)[0] is True


def test_rate_limiter_purges_stale_keys(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "monotonic", lambda: now[0])

    limiter = RateLimiter()
    assert limiter.check("viejo", 1, 60)[0] is True

    now[0] += 3600
    # La limpieza se dispara cada CLEANUP_EVERY comprobaciones.
    for index in range(limiter.CLEANUP_EVERY - 1):
        limiter.check(f"nueva-{index}", 1, 60)

    assert "viejo" not in limiter._events


def test_rate_limiter_bounds_key_count(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limiter_module.time, "monotonic", lambda: now[0])

    limiter = RateLimiter()
    limiter.MAX_KEYS = 5
    for index in range(50):
        limiter.check(f"clave-{index}", 1, 60)

    assert len(limiter._events) <= limiter.MAX_KEYS


def test_client_ip_does_not_trust_unverified_forwarded_for(monkeypatch):
    monkeypatch.setattr(acc_bootstrap, "TRUST_PROXY", True)
    with app_module.app.test_request_context(
        "/",
        headers={"X-Forwarded-For": "203.0.113.50"},
        environ_base={"REMOTE_ADDR": "10.0.0.9"},
    ):
        # ProxyFix solo se aplica al WSGI real; en la petición de prueba la IP
        # fiable es REMOTE_ADDR y la cabecera no debe usarse a mano.
        assert app_module._client_ip() == "10.0.0.9"


def test_validate_ip_format_accepts_valid_targets():
    assert validate_ip_format("192.168.1.10")
    assert validate_ip_format("192.168.1.0/24")
    assert validate_ip_format("::1")
    assert validate_ip_format("2001:db8::/64")


def test_validate_ip_format_rejects_invalid_targets():
    assert not validate_ip_format("999.1.1.1")
    assert not validate_ip_format("192.168.1.0/33")
    assert not validate_ip_format("no-es-una-ip")
    assert not validate_ip_format("")


def test_validate_target_rejects_oversized_ranges():
    error = validate_target("10.0.0.0/8", max_hosts=256, min_prefix=22)
    assert error is not None
    assert "demasiado grande" in error.lower() or "máximo" in error.lower()

    error = validate_target("192.168.0.0/16", max_hosts=256, min_prefix=22)
    assert error is not None


def test_validate_ip_format_accepts_ipv4_range():
    assert validate_ip_format("172.17.128.118-172.17.128.235")
    assert validate_ip_format("172.17.128.118 - 172.17.128.235")


def test_validate_ip_format_rejects_invalid_ipv4_range():
    assert not validate_ip_format("172.17.128.235-172.17.128.118")
    assert not validate_ip_format("172.17.128.118-")
    assert not validate_ip_format("999.1.1.1-999.1.1.2")
    assert not validate_ip_format("172.17.128.118-172.17.128.235-172.17.128.240")


def test_resolve_target_expands_ipv4_range():
    hosts = resolve_target(
        "172.17.128.118-172.17.128.120",
        max_hosts=256,
        min_prefix=22,
    )
    assert hosts == [
        "172.17.128.118",
        "172.17.128.119",
        "172.17.128.120",
    ]


def test_resolve_target_rejects_oversized_ipv4_range():
    assert (
        resolve_target(
            "172.17.128.118-172.17.129.118",
            max_hosts=256,
            min_prefix=22,
        )
        == []
    )


def test_pin_target_normalizes_ipv4_range():
    pinned, error = pin_target(
        " 172.17.128.118 - 172.17.128.235 ",
        max_hosts=256,
        min_prefix=22,
    )
    assert error is None
    assert pinned == "172.17.128.118-172.17.128.235"


def test_pin_target_rejects_reversed_ipv4_range():
    pinned, error = pin_target(
        "172.17.128.235-172.17.128.118",
        max_hosts=256,
        min_prefix=22,
    )
    assert pinned is None
    assert "inicio" in error.lower()


def test_ipv4_range_respects_allowlist(monkeypatch):
    import ipaddress

    import config as config_module

    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/24"),),
    )

    assert resolve_target("10.0.0.5-10.0.0.7") == [
        "10.0.0.5",
        "10.0.0.6",
        "10.0.0.7",
    ]
    assert resolve_target("10.0.0.250-10.0.1.5") == []
    assert pin_target("10.0.0.250-10.0.1.5")[0] is None


def test_validate_target_accepts_small_range():
    assert validate_target("192.168.1.0/24", max_hosts=256, min_prefix=22) is None
    assert validate_target("192.168.1.10", max_hosts=256, min_prefix=22) is None


def test_validate_target_accepts_resolvable_hostname():
    assert validate_target("localhost", max_hosts=256, min_prefix=22) is None
    assert resolve_target("localhost") == ["127.0.0.1"]


def test_validate_target_rejects_unresolvable_hostname():
    error = validate_target("host.invalid", max_hosts=256, min_prefix=22)
    assert error is not None
    assert "IP o rango CIDR" in error


def test_resolve_target_does_not_enumerate_huge_range():
    assert resolve_target("10.0.0.0/8", max_hosts=256, min_prefix=22) == []
    assert resolve_target("0.0.0.0/0", max_hosts=256, min_prefix=22) == []


def test_resolve_target_single_ip_and_small_cidr():
    assert resolve_target("192.168.1.10", max_hosts=256, min_prefix=22) == [
        "192.168.1.10"
    ]

    hosts = resolve_target("192.168.1.0/30", max_hosts=256, min_prefix=22)
    assert hosts == ["192.168.1.1", "192.168.1.2"]


def test_resolve_cidr_rejects_range_above_max_hosts():
    assert resolve_target("192.168.1.0/24", max_hosts=10, min_prefix=22) == []


def test_allowlist_limits_single_ips_and_cidrs(monkeypatch):
    import ipaddress

    import config as config_module

    monkeypatch.setattr(
        config_module,
        "ALLOWED_NETWORKS",
        (ipaddress.ip_network("10.0.0.0/24"),),
    )

    assert resolve_target("10.0.0.5") == ["10.0.0.5"]
    assert resolve_target("10.0.1.5") == []
    assert resolve_target("10.0.0.0/28")[0] == "10.0.0.1"
    assert resolve_target("10.0.1.0/24") == []

    assert validate_target("10.0.0.5") is None
    assert "permitidas" in validate_target("10.0.1.5")
    assert "permitidas" in validate_target("10.0.1.0/24")


def test_empty_allowlist_keeps_previous_behavior(monkeypatch):
    import config as config_module

    monkeypatch.setattr(config_module, "ALLOWED_NETWORKS", ())
    assert resolve_target("192.168.50.10") == ["192.168.50.10"]
    assert resolve_target("192.168.50.0/30") == [
        "192.168.50.1",
        "192.168.50.2",
    ]


def test_invalid_allowlist_entry_is_a_configuration_error():
    import config as config_module

    with pytest.raises(ValueError, match="ALLOWED_NETWORKS"):
        config_module._parse_allowed_networks("10.0.0.0/24, no-es-una-red")


def test_resolve_target_accepts_comma_separated_entries():
    hosts = resolve_target(
        "192.168.50.10, 192.168.50.0/30, 192.168.50.10",
        max_hosts=256,
        min_prefix=22,
    )
    assert hosts == ["192.168.50.10", "192.168.50.1", "192.168.50.2"]


def test_pin_target_normalizes_comma_separated_entries():
    pinned, error = pin_target(
        " 172.17.128.118 - 172.17.128.120 , 10.1.1.5 ",
        max_hosts=256,
        min_prefix=22,
    )
    assert error is None
    assert pinned == "172.17.128.118-172.17.128.120,10.1.1.5"


def test_pin_target_rejects_comma_list_above_max_hosts():
    pinned, error = pin_target(
        "10.0.0.0/30,10.0.1.0/30,10.0.2.0/30",
        max_hosts=4,
        min_prefix=22,
    )
    assert pinned is None
    assert "máximo" in error


def test_resolve_target_rejects_comma_list_above_max_hosts():
    assert (
        resolve_target(
            "10.0.0.0/30,10.0.1.0/30,10.0.2.0/30",
            max_hosts=4,
            min_prefix=22,
        )
        == []
    )


# ---------------------------------------------------------------------------
# Autorización de tareas: consola, log y estado solo para dueño o admin
# ---------------------------------------------------------------------------
@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    """Cliente con tres roles y base de datos propia por prueba."""
    from models.user import User

    db_path = str(tmp_path / "auth.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path, role="admin")
    User.create_or_update(
        "operador", "operador-password-123", db_path=db_path, role="operator"
    )
    User.create_or_update(
        "viewer", "viewer-password-123", db_path=db_path, role="viewer"
    )
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as client:
        yield client


def _auth_token(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _login_as(client, username: str, password: str):
    with client.session_transaction() as sess:
        sess.clear()
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": _auth_token(client),
        },
    )


def test_task_status_only_for_owner_or_admin(auth_client):
    task_id = app_module.create_task(owner="operador")

    _login_as(auth_client, "operador", "operador-password-123")
    response = auth_client.get(f"/api/task-status/{task_id}")
    assert response.status_code == 200
    assert response.get_json()["exists"] is True

    _login_as(auth_client, "viewer", "viewer-password-123")
    response = auth_client.get(f"/api/task-status/{task_id}")
    assert response.status_code == 200
    assert response.get_json() == {"status": "not_found", "exists": False}

    _login_as(auth_client, "admin", "admin-password-123")
    response = auth_client.get(f"/api/task-status/{task_id}")
    assert response.status_code == 200
    assert response.get_json()["exists"] is True


def test_progress_page_and_sse_match_task_ownership(auth_client):
    task_id = app_module.create_task(owner="operador")

    _login_as(auth_client, "viewer", "viewer-password-123")
    assert auth_client.get(f"/progress/{task_id}").status_code == 404
    assert auth_client.get(f"/progress/{task_id}/events").status_code == 404

    _login_as(auth_client, "operador", "operador-password-123")
    assert auth_client.get(f"/progress/{task_id}").status_code == 200


def test_ownerless_task_is_reserved_to_admins(auth_client):
    task_id = app_module.create_task()

    _login_as(auth_client, "operador", "operador-password-123")
    response = auth_client.get(f"/api/task-status/{task_id}")
    assert response.get_json() == {"status": "not_found", "exists": False}

    _login_as(auth_client, "admin", "admin-password-123")
    response = auth_client.get(f"/api/task-status/{task_id}")
    assert response.get_json()["exists"] is True


def test_compose_does_not_enable_ssh_tofu_by_default():
    """El default de despliegue no acepta huellas SSH del primer contacto."""
    root = Path(__file__).resolve().parent.parent
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'ACC_SSH_HOST_KEYS_AUTO: "${ACC_SSH_HOST_KEYS_AUTO:-0}"' in compose

    lab = (root / "docker-compose.lab.yml").read_text(encoding="utf-8")
    assert 'ACC_SSH_HOST_KEYS_AUTO: "1"' in lab
