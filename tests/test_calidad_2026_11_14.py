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
        "bandit": "python -m bandit -q -r app.py config.py acc models services -ll",
    }
    for name, command in expected.items():
        assert command in workflow, f"El CI no ejecuta {name}: falta {command!r}"
    assert (
        "requirements-dev.txt" in workflow
    ), "El CI debe instalar requirements-dev.txt para usar las versiones fijadas"


def test_mypy_covers_production_code():
    config = _read("mypy.ini")
    assert "files = app.py, config.py, acc, models, services" in config
    assert "python_version = 3.12" in config


def _requirements_names(text: str) -> set[str]:
    """Nombres de paquete declarados, ignorando comentarios y opciones."""
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def test_runtime_requirements_exclude_tests_and_sca():
    """La imagen no debe instalar pytest, pip-audit ni herramientas de CI."""
    runtime = _requirements_names(_read("requirements.txt"))
    for unwanted in ("pytest", "pytest-cov", "pip-audit", "pyyaml"):
        assert unwanted not in runtime, (
            f"{unwanted} no es dependencia de ejecución: muévelo a "
            "requirements-test.txt o requirements-dev.txt"
        )
    for tool in ("ruff", "mypy", "bandit", "black"):
        assert tool not in runtime

    test_deps = _read("requirements-test.txt")
    for required in ("pytest-timeout", "pytest-cov", "PyYAML"):
        assert required in test_deps, f"Falta {required} en requirements-test.txt"
    assert "-r requirements.txt" in test_deps


def test_docker_builds_from_lockfile():
    dockerfile = _read("Dockerfile")
    assert "pip install -r requirements.lock" in dockerfile
    assert "pip install -r requirements.txt" not in dockerfile


def test_ci_builds_images_and_uses_test_requirements():
    workflow = _read(".github/workflows/ci.yml")
    assert "requirements-test.txt" in workflow
    assert "requirements.lock" in workflow
    assert "docker build" in workflow
    assert "docker/nginx" in workflow
    assert '"3.14"' in workflow
    assert "--cov-fail-under=78" in workflow


def test_pytest_has_a_timeout():
    config = _read("pytest.ini")
    assert re.search(r"^timeout\s*=\s*\d+", config, re.MULTILINE)
    assert "timeout_method" in config


def test_frontend_logic_lives_in_static_js():
    """A6: la UI no debe volver a acumular cientos de líneas inline.

    La CSP ya permite Ficheros de ``static/js`` con ``script-src 'self'`` y así
    el navegador puede cachearlos. Solo se admiten bloques inline de datos
    (``application/json``), que no se ejecutan.
    """
    js_dir = ROOT / "static" / "js"
    expected = {
        "app-shell.js",
        "panel.js",
        "progress.js",
        "credentials.js",
        "schedule-edit.js",
        "tasks.js",
        "theme-preference.js",
        "login.js",
    }
    assert expected <= {path.name for path in js_dir.glob("*.js")}

    script = re.compile(r"<script([^>]*)>(.*?)</script>", re.DOTALL)
    offenders = []
    for template in (ROOT / "templates").rglob("*.html"):
        content = template.read_text(encoding="utf-8")
        for attrs, body in script.findall(content):
            if "src=" in attrs or 'type="application/json"' in attrs:
                continue
            if body.strip():
                offenders.append(template.name)
    assert offenders == [], f"JS inline en plantillas: {sorted(set(offenders))}"


def test_static_scripts_do_not_interpolate_jinja():
    """El JS estático no se renderiza: la configuración llega por JSON/data-*."""
    for path in (ROOT / "static" / "js").glob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert "{{" not in source and "{%" not in source, path.name


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
