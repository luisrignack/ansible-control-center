# tests/test_security.py
# Pruebas de CSRF, rate limiting y límites de red (fase P3).
from __future__ import annotations

import pytest

import app as app_module
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
    assert resolve_target(
        "172.17.128.118-172.17.129.118",
        max_hosts=256,
        min_prefix=22,
    ) == []


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
