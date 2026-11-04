"""Catálogo de tareas operativas de Ansible Control Center.

La pantalla principal funciona como catálogo: cada tarea de Ansible es una
tarjeta con nombre, descripción y estado. Las tareas en estado ``active``
exponen su configuración y ejecución; las de ``planned`` están anunciadas
pero todavía no tienen playbook integrado.

Añadir una tarea nueva = añadir una entrada aquí (+ su playbook en
``ansible/`` y su página de configuración cuando esté operativa).
"""
from typing import Any, Dict, List, Optional

TASK_ID_HORA = "hora"
TASK_ID_KASPERSKY = "kaspersky"
TASK_ID_CONTRASENA = "contrasena"
TASK_ID_TAPIZ = "tapiz"

DEFAULT_TASK_ID = TASK_ID_HORA

STATUS_ACTIVE = "active"
STATUS_PLANNED = "planned"

TASK_CATALOG: List[Dict[str, Any]] = [
    {
        "id": TASK_ID_HORA,
        "name": "Hora y zona horaria",
        "icon": "bi-clock",
        "tone": "blue",
        "summary": (
            "Configura hora, zona horaria, NTP y RTC en un grupo de equipos "
            "Ubuntu. Incluye vista previa, idempotencia y verificación."
        ),
        "playbook": "cambiar_hora.yml",
        "status": STATUS_ACTIVE,
        "min_role": "operator",
        "destructive": False,
        "chips": ["Operativa", "Acción TZ / TIME", "254 hosts"],
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
        "chips": ["En preparación", "Escritorio", "Plantilla"],
        "steps": [
            "Copiar la imagen del tapiz a cada host y fijar permisos.",
            "Aplicar la configuración de escritorio para el usuario local.",
            "Comprobar que no quedan tapices personalizados previos.",
        ],
    },
]

_CATALOG_INDEX: Dict[str, Dict[str, Any]] = {
    task["id"]: task for task in TASK_CATALOG
}


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
