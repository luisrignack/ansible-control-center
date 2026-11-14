"""Gates de calidad reproducibles (2026-11-14).

El CI ejecutaba solo pytest. Estas pruebas fijan el contrato: las
herramientas están declaradas con una versión exacta y el workflow las
invoca, de modo que una regresión de tipos o de formato se detecta en la
pull request y no en el despliegue.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEV_TOOLS = ("ruff", "mypy", "bandit", "black")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_requirements_dev_pins_exact_versions():
    """Una versión flotante de black/mypy rompe la reproducibilidad."""
    text = _read("requirements-dev.txt")
    for tool in DEV_TOOLS:
        matches = re.findall(rf"^{tool}==([0-9][^\s#]*)", text, re.MULTILINE)
        assert matches, f"{tool} debe estar fijado con == en requirements-dev.txt"


def test_ci_executes_every_gate():
    workflow = _read(".github/workflows/ci.yml")
    expected = {
        "ruff": "python -m ruff check .",
        "black": "python -m black --check .",
        "mypy": "python -m mypy",
        "bandit": "python -m bandit",
    }
    for name, command in expected.items():
        assert command in workflow, f"El CI no ejecuta {name}: falta {command!r}"
    assert (
        "requirements-dev.txt" in workflow
    ), "El CI debe instalar requirements-dev.txt para usar las versiones fijadas"


def test_mypy_covers_production_code():
    config = _read("mypy.ini")
    assert "files = app.py, config.py, acc, models, services" in config


def test_production_code_has_no_bandit_findings():
    """Bandit no debe necesitar silencios: si aparece un hallazgo, se arregla."""
    from tools.quality_check import check_file  # noqa: F401  (import de humo)

    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "tests", "tools"} for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8")
        assert (
            "# nosec" not in source
        ), f"{path.relative_to(ROOT)} silencia bandit; corrige el patrón en su lugar"
