# Catálogo de automatizaciones (FASE 8)

Estado: **PARTIALLY_COMPLETED**

Ya existía un catálogo declarativo interno (`services/task_catalog.py`) que
controla qué tareas existen, su rol mínimo, plataformas, acciones y si son
destructivas/programables. En esta fase se expone un contrato JSON de solo
lectura y se documenta el modelo. No se añadieron operaciones nuevas ni
metadatos de verificación/reversibilidad al código, para no declarar
capacidades que los playbooks aún no ofrecen de forma homogénea.

## 1. Modelo actual

Cada entrada de `TASK_CATALOG` es un diccionario con:

| Campo | Uso |
|---|---|
| `id` | Identificador estable (`zona-todas`, `contrasena`, …) |
| `name`, `summary`, `icon`, `tone` | Presentación |
| `playbook` | Fichero de `ansible/` (constante, no editable por el cliente) |
| `status` | `active` o `planned` |
| `min_role` | `operator` o `admin` |
| `destructive` | Exige aprobación de un segundo admin en modo real |
| `platforms` | `linux`, `windows` |
| `actions` | Acciones que el motor entiende (`TZ`, `TIME`, `PASSWORD`, …) |
| `schedulable` | Puede programarse (con contraseña maestra y ventana) |
| `fixed_value`, `chips`, `steps` | Ayuda visual |

El backend **no acepta** el playbook ni la ruta desde el formulario: la tarea
se busca por `id` en el catálogo y el playbook sale de una constante. Las
acciones se validan con allowlists en `acc/operations.py`.

## 2. API nueva

`GET /api/catalogo` (sesión requerida):

```json
{
  "version": 1,
  "tasks": [
    {
      "id": "zona-todas",
      "name": "Zona horaria",
      "summary": "...",
      "status": "active",
      "min_role": "operator",
      "destructive": false,
      "platforms": ["linux", "windows"],
      "actions": ["TZ"],
      "schedulable": false,
      "fixed_value": "America/Caracas",
      "requires_approval": false
    }
  ]
}
```

- Filtra por rol: un `viewer` no recibe tareas que exigen `operator`/`admin`.
- No publica `playbook`, rutas, comandos, credenciales ni parámetros internos.
- `requires_approval` se deriva de `destructive` (comportamiento real del
  motor: una tarea destructiva en modo `execute` crea una solicitud).
- `Cache-Control: no-store`.

## 3. Controles de seguridad

El catálogo **no** es un mecanismo para ejecutar comandos arbitrarios:

1. El `id` debe existir en el catálogo; si no, se usa la tarea por defecto o se
   rechaza según el flujo.
2. El playbook es una constante del servidor.
3. Los valores se validan con allowlists (zonas existentes, IPs, política NTP,
   usuario local, tamaños de contraseña, etc.).
4. Las tareas destructivas requieren aprobación y rol `admin`.
5. Los comandos dentro de los playbooks usan `argv`; los datos no se
   interpolan en scripts `shell`.
6. Los destinos se validan contra la allowlist y se pinean a IP.

## 4. Evolución propuesta a catálogo declarativo completo

Añadir a cada entrada (sin romper las claves actuales):

```python
{
    "id": "contrasena",
    "params": [
        {"name": "target_user", "type": "username", "required": True},
        {"name": "new_password", "type": "secret", "required": True,
         "min_length": 8, "max_length": 512, "confirm": True},
    ],
    "approval": {"required": True, "role": "admin"},
    "dry_run": {"supported": False, "reason": "cambio no reversible"},
    "verification": {"type": "marker", "marker": "PASSWORD_VERIFIED"},
    "reversible": {"supported": False, "reason": "no se conserva la anterior"},
}
```

Reglas de aceptación para una operación nueva:

1. Entrada en el catálogo con todos los campos obligatorios.
2. Playbook con `argv`/módulos declarativos, sin `shell` con datos.
3. Validación de entradas en el backend (no solo en el formulario).
4. Verificación posterior (marcador o consulta de estado).
5. Pruebas unitarias de validación y de salida (con mocks, sin hosts reales).
6. Documentación en `docs/` y entrada en el catálogo.
7. Si es destructiva: aprobación, ventana y auditoría.

## 5. Qué falta (honestamente)

| Elemento | Estado |
|---|---|
| Catálogo interno declarativo | SÍ (existente) |
| API de lectura filtrada por rol | SÍ (nuevo) |
| Metadatos formales de parámetros/tipos | NO |
| Metadatos de verificación/reversibilidad | NO |
| Validación genérica dirigida por esquema | NO (la validación es código específico) |
| Tareas nuevas | NO se añadieron |
| Tests del contrato API | SÍ |

## 6. Pruebas

- `test_api_catalogo_filtra_por_rol`: admin ve todo; viewer no ve tareas
  privilegiadas; no se filtra `playbook`; `requires_approval` correcto.
- `test_api_catalogo_requiere_sesion`: sin sesión no se sirve.
- `test_url_map_y_endpoints_no_cambian`: la ruta queda fijada en el contrato.

## 7. Recomendaciones

1. Formalizar `params` y validar con un único motor de esquema para reducir
   código duplicado entre formulario, programación y aprobación.
2. Incorporar `verification` y `reversible` antes de permitir que un operador
   añada tareas sin revisión de código.
3. Mantener el catálogo **cerrado**: cualquier extensión dinámica desde la UI
   debe limitarse a parámetros validados, nunca a playbooks o comandos.
4. Exponer la versión del catálogo en `/api/catalogo` (ya `version: 1`) y
   documentar cambios incompatibles.
