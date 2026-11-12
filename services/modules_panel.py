"""Preferencias del panel de módulos (tarjetas de tareas ocultas).

Cada usuario decide qué tarjetas del catálogo quiere ver en la portada y
cuáles deja en la vista «Módulos». La preferencia es **cosmética**: no concede
ni retira permisos, y una tarjeta oculta sigue accesible por su URL
(``/tarea/<id>``).

El almacenamiento reutiliza ``app_settings`` con una clave por usuario, en
lugar de una tabla nueva. Así la migración del esquema no cambia (sigue en
v15), el despliegue es reversible con la variable ``ACC_UI_CONFIG_PANEL=0`` y
una versión anterior del código puede seguir arrancando sobre la misma base de
datos ignorando estas filas.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from models.db import get_setting, set_setting

KEY_PREFIX = "ui.modules.hidden."


def _key(user_id: object) -> str:
    return f"{KEY_PREFIX}{user_id}"


def hidden_task_ids(user_id: object, db_path: str | None = None) -> set[str]:
    """Devuelve los identificadores de tarea ocultos por el usuario."""
    raw = get_setting(_key(user_id), "", db_path=db_path)
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data if isinstance(item, str) and item}


def _write(user_id: object, task_ids: Iterable[str], db_path: str | None) -> None:
    set_setting(
        _key(user_id),
        json.dumps(sorted(set(task_ids)), ensure_ascii=False),
        db_path=db_path,
    )


def set_task_hidden(
    user_id: object,
    task_id: str,
    hidden: bool,
    db_path: str | None = None,
) -> set[str]:
    """Oculta o restaura una tarea. Devuelve el conjunto resultante."""
    current = hidden_task_ids(user_id, db_path=db_path)
    if hidden:
        current.add(task_id)
    else:
        current.discard(task_id)
    _write(user_id, current, db_path)
    return current


def restore_all(user_id: object, db_path: str | None = None) -> None:
    """Vuelve a mostrar todas las tarjetas del usuario."""
    _write(user_id, [], db_path)


def split_catalog(
    catalog: Iterable[dict], hidden: Iterable[str]
) -> tuple[list[dict], list[dict]]:
    """Separa el catálogo en ``(visibles, ocultas)`` conservando el orden."""
    hidden_set = set(hidden)
    visible: list[dict] = []
    parked: list[dict] = []
    for task in catalog:
        if task.get("id") in hidden_set:
            parked.append(task)
        else:
            visible.append(task)
    return visible, parked
