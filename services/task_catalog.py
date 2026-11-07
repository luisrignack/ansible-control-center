"""Catálogo de tareas operativas de Ansible Control Center.

La pantalla principal funciona como catálogo: cada tarea de Ansible es una
tarjeta con nombre, descripción y estado. Las tareas en estado ``active``
exponen su configuración y ejecución; las de ``planned`` están anunciadas
pero todavía no tienen playbook integrado.

Añadir una tarea nueva = añadir una entrada aquí (+ su playbook en
``ansible/`` y su página de configuración cuando esté operativa).
"""
from typing import Any, Dict, List, Optional
import sys

TASK_ID_ZONA_ALL = "zona-todas"
TASK_ID_KASPERSKY = "kaspersky"
TASK_ID_CONTRASENA = "contrasena"
TASK_ID_TAPIZ = "tapiz"
TASK_ID_APT_UPGRADE = "apt-upgrade"

# Tarea por defecto cuando el formulario no envía el campo ``task``.
DEFAULT_TASK_ID = TASK_ID_ZONA_ALL

STATUS_ACTIVE = "active"
STATUS_PLANNED = "planned"

TASK_CATALOG: List[Dict[str, Any]] = [
    {
        "id": TASK_ID_ZONA_ALL,
        "name": "Zona horaria",
        "icon": "bi-globe-americas",
        "tone": "blue",
        "summary": (
            "Establece la zona horaria America/Caracas (UTC-4) en las "
            "estaciones: Linux (Ubuntu y derivadas de Debian, via "
            "timedatectl, permanente tras reiniciar) y Windows (tzutil por "
            "WinRM). Antes de cambiar, guarda una copia del archivo "
            "/etc/timezone anterior en el servidor de operación."
        ),
        "playbook": "cambiar_hora.yml",
        "status": STATUS_ACTIVE,
        "min_role": "operator",
        "destructive": False,
        "platforms": ["linux", "windows"],
        "actions": ["TZ"],
        # Valor fijo de esta tarea: America/Caracas (UTC-4). El formulario
        # lo muestra solo lectura; la app lo revalida igual que siempre.
        "fixed_value": "America/Caracas",
        "chips": [
            "Operativa",
            "America/Caracas (UTC-4)",
            "Permanente tras reinicio",
            "Linux + Windows",
        ],
        "steps": [],
    },
    {
        "id": TASK_ID_KASPERSKY,
        "name": "Kaspersky Antivirus",
        "icon": "bi-shield-exclamation",
        "tone": "red",
        "summary": (
            "Desinstala la versión antigua de Kaspersky Endpoint Security e "
            "instala la versión específica indicada en la configuración."
        ),
        "playbook": "kaspersky.yml",
        "status": STATUS_PLANNED,
        "min_role": "admin",
        "destructive": True,
        "platforms": ["linux"],
        "actions": ["TZ", "TIME"],
        "chips": ["En preparación", "2 pasos", "Seguridad"],
        "steps": [
            "Desinstalar la versión en vigor de KES en cada host.",
            "Instalar la versión específica de KES junto al agente de red.",
            "Verificación posterior del producto instalado.",
        ],
    },
    {
        "id": TASK_ID_CONTRASENA,
        "name": "Contraseña administrativa",
        "icon": "bi-key",
        "tone": "amber",
        "summary": (
            "Rota y sincroniza la contraseña del usuario administrador local "
            "en los hosts objetivo, con el vault como origen."
        ),
        "playbook": "contrasena.yml",
        "status": STATUS_PLANNED,
        "min_role": "admin",
        "destructive": True,
        "platforms": ["linux"],
        "actions": ["TZ", "TIME"],
        "chips": ["En preparación", "Credenciales", "Rotación"],
        "steps": [
            "Validar la contraseña candidata contra un host de muestra.",
            "Aplicar la rotación en todos los hosts (best-effort).",
            "Actualizar el vault cifrado con la nueva contraseña.",
        ],
    },
    {
        "id": TASK_ID_TAPIZ,
        "name": "Tapiz oficial",
        "icon": "bi-image",
        "tone": "teal",
        "summary": (
            "Aplica el tapiz oficial corporativo en los escritorios de los "
            "hosts y deja fijada la política de imagen de fondo."
        ),
        "playbook": "tapiz.yml",
        "status": STATUS_PLANNED,
        "min_role": "operator",
        "destructive": False,
        "platforms": ["linux"],
        "actions": ["TZ", "TIME"],
        "chips": ["En preparación", "Escritorio", "Plantilla"],
        "steps": [
            "Copiar la imagen del tapiz a cada host y fijar permisos.",
            "Aplicar la configuración de escritorio para el usuario local.",
            "Comprobar que no quedan tapices personalizados previos.",
        ],
    },
    {
        "id": TASK_ID_APT_UPGRADE,
        "name": "Actualización del sistema",
        "icon": "bi-arrow-repeat",
        "tone": "teal",
        "summary": (
            "Actualiza el índice de paquetes y aplica una actualización "
            "segura (apt-get upgrade) en hosts Linux. No elimina paquetes ni "
            "reinicia: deja constancia de si el host requiere reinicio y "
            "debe ejecutarse dentro de la ventana de mantenimiento."
        ),
        "playbook": "apt_upgrade.yml",
        "status": STATUS_PLANNED,
        "min_role": "admin",
        "destructive": False,
        "platforms": ["linux"],
        "actions": ["APT"],
        "chips": ["En preparación", "Mantenimiento", "Linux"],
        "steps": [
            "Actualizar el índice de paquetes con apt-get update.",
            "Aplicar apt-get upgrade sin eliminar paquetes ni abrir preguntas interactivas.",
            "Verificar que no quedan paquetes pendientes y avisar si el host requiere reinicio.",
        ],
    },
]

_CATALOG_INDEX: Dict[str, Dict[str, Any]] = {
    task["id"]: task for task in TASK_CATALOG
}


def _register_test_task(task: Dict[str, Any], monkeypatch) -> None:
    """Registra una tarea temporal (uso exclusivo de las pruebas).

    Ciertas piezas del motor (acción TIME, políticas NTP, RTC) ya no están
    expuestas en el catálogo visible tras la tarjeta única de zona horaria;
    estos tests las cubren registrando una tarea equivalente solo para el
    test, tocando el índice real y la lista de catálogo (también la copia
    ligada por valor en ``app``) con ``monkeypatch`` para que se restaure al
    terminar.
    """
    monkeypatch.setitem(_CATALOG_INDEX, task["id"], task)
    # Se agrega al frente: el fallback del formulario elige la primera
    # tarea activa de la lista.
    new_catalog = [task, *TASK_CATALOG]
    monkeypatch.setattr(sys.modules[__name__], "TASK_CATALOG", new_catalog)
    app_module = sys.modules.get("app")
    if app_module is not None and hasattr(app_module, "TASK_CATALOG"):
        monkeypatch.setattr(app_module, "TASK_CATALOG", new_catalog)


def get_task(task_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Devuelve la entrada del catálogo con ese id, o ``None``."""
    if not task_id:
        return None
    return _CATALOG_INDEX.get(task_id.strip().lower())


def get_task_or_default(task_id: Optional[str]) -> Dict[str, Any]:
    """Devuelve la tarea pedida o la tarea por defecto si no existe."""
    return get_task(task_id) or _CATALOG_INDEX[DEFAULT_TASK_ID]


def active_tasks() -> List[Dict[str, Any]]:
    return [task for task in TASK_CATALOG if task["status"] == STATUS_ACTIVE]
