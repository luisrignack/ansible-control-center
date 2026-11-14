"""WinRM sobre HTTPS por defecto y validación en producción (2026-11-14).

El riesgo S2 de la auditoría de 2026-11-08 era que el despliegue usaba
WinRM sobre HTTP (5985) con NTLM: las credenciales de las estaciones Windows
viajaban sin cifrar. Ahora el valor por defecto es 5986 (HTTPS) y el arranque
en producción rechaza HTTP, el transporte ``basic`` y la validación
desactivada salvo que se active el escape de laboratorio.
"""

from __future__ import annotations

import ipaddress

import pytest

import config


@pytest.fixture()
def production(monkeypatch):
    """Deja la configuración en un estado de producción válido."""
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        config, "ALLOWED_NETWORKS", (ipaddress.ip_network("10.0.0.0/8"),)
    )
    monkeypatch.setattr(config, "SECRET_KEY", "clave-explicita")
    monkeypatch.setattr(config, "HOST_KEY_CHECKING", True)
    monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", True)
    monkeypatch.setattr(config, "LDAP_ENABLED", False)
    monkeypatch.setattr(config, "WINRM_PORT", 5986)
    monkeypatch.setattr(config, "WINRM_TRANSPORT", "ntlm")
    monkeypatch.setattr(config, "WINRM_SERVER_CERT_VALIDATION", "validate")
    monkeypatch.setattr(config, "WINRM_ALLOW_INSECURE", False)
    return config


def test_default_port_is_https():
    # conftest no define ACC_WINRM_PORT, así que se usa el valor por defecto.
    assert config.WINRM_PORT == 5986
    assert config.WINRM_TRANSPORT == "ntlm"
    assert config.WINRM_SERVER_CERT_VALIDATION == "validate"
    assert config.WINRM_ALLOW_INSECURE is False


def test_production_accepts_https_ntlm(production):
    production.validate_security_config()


def test_production_rejects_plain_http(production, monkeypatch):
    monkeypatch.setattr(production, "WINRM_PORT", 5985)
    with pytest.raises(RuntimeError, match="5985"):
        production.validate_security_config()


def test_production_rejects_basic_transport(production, monkeypatch):
    monkeypatch.setattr(production, "WINRM_TRANSPORT", "basic")
    with pytest.raises(RuntimeError, match="basic"):
        production.validate_security_config()


def test_production_rejects_certificate_validation_off(production, monkeypatch):
    monkeypatch.setattr(production, "WINRM_SERVER_CERT_VALIDATION", "ignore")
    with pytest.raises(RuntimeError, match="ignore"):
        production.validate_security_config()


def test_lab_escape_allows_insecure_winrm(production, monkeypatch):
    monkeypatch.setattr(production, "WINRM_PORT", 5985)
    monkeypatch.setattr(production, "WINRM_TRANSPORT", "basic")
    monkeypatch.setattr(production, "WINRM_SERVER_CERT_VALIDATION", "ignore")
    monkeypatch.setattr(production, "WINRM_ALLOW_INSECURE", True)

    production.validate_security_config()


def test_compose_defaults_to_5986():
    from pathlib import Path

    compose = Path(__file__).resolve().parent.parent / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert 'ACC_WINRM_PORT: "${ACC_WINRM_PORT:-5986}"' in text
