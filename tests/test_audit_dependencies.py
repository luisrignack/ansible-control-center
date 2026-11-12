# tests/test_audit_dependencies.py
# Fallback de pip-audit: Nexus primero y PyPI directo si no responde.
from __future__ import annotations

from pathlib import Path

from tools import audit_dependencies as audit


def test_parse_index_list_reads_quoted_global_index():
    output = (
        "global.index-url='https://nexus.example/simple'\n"
        "global.timeout='30'\n"
    )
    assert audit.parse_index_list(output) == "https://nexus.example/simple"


def test_parse_index_list_ignores_missing_key():
    assert audit.parse_index_list("global.timeout='30'\n") == ""


def test_candidate_indexes_puts_configured_first_and_deduplicates():
    assert audit.candidate_indexes("https://nexus.example/simple") == [
        "https://nexus.example/simple",
        audit.DIRECT_INDEX,
    ]
    # Si el índice configurado ya es PyPI, no se repite.
    assert audit.candidate_indexes(audit.DIRECT_INDEX) == [audit.DIRECT_INDEX]


def test_choose_index_falls_back_to_direct_when_configured_is_down(monkeypatch):
    reachable = {audit.DIRECT_INDEX}
    monkeypatch.setattr(
        audit,
        "index_reachable",
        lambda url, timeout=5.0: url in reachable,
    )
    assert audit.choose_index(["https://nexus.example/simple", audit.DIRECT_INDEX]) == (
        audit.DIRECT_INDEX
    )


def test_choose_index_prefers_configured_when_available(monkeypatch):
    monkeypatch.setattr(audit, "index_reachable", lambda url, timeout=5.0: True)
    assert audit.choose_index(["https://nexus.example/simple", audit.DIRECT_INDEX]) == (
        "https://nexus.example/simple"
    )


def test_choose_index_returns_none_when_nothing_responds(monkeypatch):
    monkeypatch.setattr(audit, "index_reachable", lambda url, timeout=5.0: False)
    assert audit.choose_index(["https://nexus.example/simple", audit.DIRECT_INDEX]) is None


def test_audit_command_includes_selected_index():
    command = audit.audit_command(
        "https://pypi.org/simple",
        Path("requirements.txt"),
        local=False,
    )
    assert "--index-url" in command
    assert "https://pypi.org/simple" in command
    assert "-r" in command
    assert "--local" not in command


def test_audit_command_local_has_no_index():
    command = audit.audit_command(None, None, local=True)
    assert "--local" in command
    assert "--index-url" not in command
