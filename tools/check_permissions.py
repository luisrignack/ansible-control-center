#!/usr/bin/env python3
"""Comprueba los permisos de los directorios que no deben ser legibles.

Los directorios montados en los contenedores (`certs/`, `certs-ca/` y
`secrets.d/`) contienen la clave privada TLS y secretos de aplicación. Un
`0775` heredado del umask del usuario los deja legibles por cualquier cuenta
del equipo anfitrión.

El script no necesita dependencias ni red y es seguro ejecutarlo en CI: si los
directorios no existen (por ejemplo, en un checkout limpio), termina con
código 0.

Uso:
    python tools/check_permissions.py [--root RUTA]
"""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

# (ruta relativa, modo máximo permitido, descripción)
DIRECTORY_RULES = (
    ("certs", 0o750, "certificado TLS de Nginx"),
    ("certs-ca", 0o750, "CA del directorio para LDAPS"),
    ("secrets.d", 0o700, "secretos por fichero de Docker Compose"),
)

# Los ficheros de clave nunca deben ser legibles por grupo ni por otros.
FILE_RULES = (
    ("certs/privkey.pem", 0o600, "clave privada TLS"),
    ("certs/fullchain.pem", 0o644, "certificado público TLS"),
    ("certs-ca/ca-etecsa.pem", 0o640, "CA pública de LDAPS"),
    (".env.docker", 0o600, "variables con secretos del despliegue"),
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def check(root: Path) -> list[str]:
    problems: list[str] = []

    for relative, maximum, description in DIRECTORY_RULES:
        path = root / relative
        if not path.is_dir():
            continue
        mode = _mode(path)
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            problems.append(
                f"{relative}: {mode:04o} permite escritura a grupo u otros "
                f"(máximo recomendado {maximum:04o}) — {description}"
            )
        elif mode & stat.S_IROTH:
            problems.append(
                f"{relative}: {mode:04o} es legible por otros "
                f"(máximo recomendado {maximum:04o}) — {description}"
            )

    for relative, maximum, description in FILE_RULES:
        path = root / relative
        if not path.is_file():
            continue
        mode = _mode(path)
        if mode & ~maximum:
            problems.append(
                f"{relative}: {mode:04o} es más permisivo que {maximum:04o} — "
                f"{description}"
            )

    if (root / "secrets.d").is_dir():
        for path in sorted((root / "secrets.d").iterdir()):
            if not path.is_file():
                continue
            mode = _mode(path)
            if mode != 0o600:
                problems.append(
                    f"secrets.d/{path.name}: {mode:04o}; los secretos deben " "ser 0600"
                )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Directorio base a comprobar (por defecto, la raíz del proyecto).",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"No existe el directorio: {root}", file=sys.stderr)
        return 2

    problems = check(root)
    if problems:
        for problem in problems:
            print(f"PERMISOS: {problem}", file=sys.stderr)
        print(
            "Corrige con:\n"
            "  sudo install -d -m 0750 certs certs-ca\n"
            "  sudo install -m 0600 certs/privkey.pem\n"
            "  chmod 700 secrets.d && chmod 600 secrets.d/*",
            file=sys.stderr,
        )
        return 1

    print("Permisos correctos (o directorios aún no creados).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
