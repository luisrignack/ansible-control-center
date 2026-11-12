#!/usr/bin/env python3
"""Comprobaciones de calidad seguras para Ansible Control Center.

Se ejecuta en CI y no necesita dependencias externas: analiza el AST del
código de producción para detectar patrones que ya han causado problemas o
que ocultan fallos en producción (``assert`` desactivables con ``-O``,
ejecución de shell, ``eval``/``exec`` y deserialización insegura).

No sustituye a un linter completo, pero es un gate reproducible que funciona
sin red y sin instalar herramientas adicionales.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / "app.py", ROOT / "config.py"]
TARGETS += sorted((ROOT / "models").glob("*.py"))
TARGETS += sorted((ROOT / "services").glob("*.py"))

# ``assert`` desaparece con ``python -O`` y nunca debe usarse para validar
# datos de entrada o invariantes de seguridad en rutas de producción.
FORBIDDEN_CALLS = {"eval", "exec"}
FORBIDDEN_ATTRS = {("pickle", "loads"), ("pickle", "load")}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    relative = path.relative_to(ROOT)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            problems.append(
                f"{relative}:{node.lineno}: 'assert' en código de producción"
            )
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in FORBIDDEN_CALLS:
                problems.append(
                    f"{relative}:{node.lineno}: llamada peligrosa a '{name}()'"
                )
            for keyword in node.keywords:
                if (
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    problems.append(
                        f"{relative}:{node.lineno}: subprocess con shell=True"
                    )
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            pair = (node.value.id, node.attr)
            if pair in FORBIDDEN_ATTRS:
                problems.append(
                    f"{relative}:{node.lineno}: "
                    f"deserialización insegura {pair[0]}.{pair[1]}"
                )
    return problems


def main() -> int:
    problems: list[str] = []
    checked = 0
    for path in TARGETS:
        if not path.exists():
            continue
        checked += 1
        try:
            problems.extend(check_file(path))
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(ROOT)}: sintaxis inválida: {exc}")
    if problems:
        print("Fallos de calidad:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"Calidad OK: {checked} archivo(s) revisado(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
