# 02 — Auditoría de arquitectura y diseño

## Resumen

ACC es un monolito modular en Flask con servicios y modelos separados, pero con **concentración significativa de orquestación y lógica en `app.py`** (~5960 líneas). La separación en `models/` y `services/` existe, aunque varios flujos críticos (validación de formularios, gates de seguridad, ejecución, SSE, aprobaciones) están implementados o coordinados desde `app.py` (evidencia: `wc -l app.py`; funciones/rutas como `_parse_operation_form`, `handle_execution`, `progress_sse`, `_try_ldap_login`, `cancel_task`).

## 5.1 `app.py` (evidencia parcial)

| Aspecto | Observación | Evidencia | Riesgo |
|---|---|---|---|
| Tamaño y responsabilidades | Mezcla rutas, validación, autorización contextual, estado en memoria/DB, SSE, lanzamiento de trabajos, aprobaciones, integraciones | `app.py:5960` líneas; `grep` de rutas/handlers muestra muchos `@app.route` y helpers | **Medio**: mayor probabilidad de omisiones en controles y regresiones |
| Lógica de negocio en rutas/handlers | `index` POST delega en `handle_execution`; parsing centralizado pero voluminoso (`_parse_operation_form` ~874+), con ramas por acción/tarea/objetivo/archivo | `app.py:2282,3477,3504,874-1100` | **Medio**: complejidad y mantenimiento |
| Estado mutable/global | Uso de estructuras en proceso para tareas/SSE/rate limit/métricas/cola (parcialmente inferido) y tabla `task_state` en SQLite | `task_state` DDL en `models/db.py`; `LIMITER` en `app.py`; `gunicorn.conf.py` advierte estado en proceso | **Medio/Alto** según despliegue multi-worker |
| Manejo de errores | En `handle_execution` hay limpieza en fallo de cola/excepción (remove task, release operation_id, discard wallpaper, release reserved approval) | `app.py:3650-3715` (lectura) | Positivo; requiere cobertura exhaustiva de otros flujos |
| Consistencia de validación | Preview y ejecución comparten `_parse_operation_form` (`default_mode`, misma función) | `app.py:874`, `app.py:3504` | Positivo (reduce divergencias) |

**Refactorización propuesta (no implementar):** extraer a módulos/coordinadores: (1) parsing/normalización de operaciones, (2) políticas de autorización y aprobación, (3) orquestación de ejecución/lotes, (4) estado/SSE, (5) autenticación/LDAP, manteniendo contratos HTTP y de DB.

## 5.2 Configuración (`config.py`, despliegue)

| Aspecto | Observación | Evidencia | Clasificación |
|---|---|---|---|
| Defaults y endurecimiento | `validate_security_config()` exige en producción `ALLOWED_NETWORKS` no vacío, `SECRET_KEY` robusta, `HOST_KEY_CHECKING`, cookies seguras, proxy HTTPS de confianza, y rechazo de LDAP sin verificación salvo override explícito | Lectura de `config.py` (inicio/medio) | Positivo; depende de ejecución efectiva en arranque |
| DRY_RUN | Configurable por entorno (`ACC_DRY_RUN`) con comportamiento documentado | `config.py` / `ansible_runner.py` | Riesgo operativo si mal configurado (ver funcional/ops) |
| Rutas/secretos | Resolución de DB/vault/clave/known_hosts bajo directorios configurables | `config.py` | Debe auditarse permisos efectivos en despliegue |
| Fallbacks | Algunos parámetros con defaults operativos (p.ej. workers/gthreads/timeout Gunicorn, límites) | `gunicorn.conf.py`, `config.py` | **Bajo/Medio** según contexto |
| Entornos dev/prod | Diferencias esperables; validación orientada a producción | `validate_security_config()` | No verificado en runtime productivo |

## 5.3 Contratos internos y coherencia

| Aspecto | Observación | Evidencia | Riesgo |
|---|---|---|---|
| Estados de lote/host | `models/batch.py` define estados y transiciones (`status_counts`, `finish_batch`, reconciliación de huérfanos, `attention_count`) | `models/batch.py` (lectura completa) | Positivo; requiere pruebas de carreras/fallos parciales |
| Marcadores Ansible | `ansible_runner.py` parsea marcadores `*_B64/*STATE/*_CHANGED/*_VERIFIED` y exige verificación según flujo | `services/ansible_runner.py`, playbooks (`APT_*`, `TAPIZ_*`, `FIREWALL_*`, `PASSWORD_*`) | **Medio**: robustez depende de formato/salida no manipulable por host (defensa en profundidad) |
| Excepciones/retornos | Servicios devuelven tuplas/`None`/estructuras variadas (p.ej. `pin_target -> (pinned, error)`, `_send -> error|None`) | `host_resolver.py`, `notifier.py` | Riesgo de manejo inconsistente si no se propaga |
| Tipos/fechas/timeouts | Mezcla de `int/float/str/None`, tiempos en epoch/ISO; timeouts configurables | múltiples servicios | **Bajo/Medio** |

## Hallazgos arquitectónicos (agrupados)

| ID | Severidad | Confianza | Estado | Resumen |
|---|---|---|---|---|
| ACC-ARC-001 | Medium | Alta | Confirmado | Monolito funcional concentrado en `app.py` (~5960 líneas) aumenta acoplamiento y riesgo de bypass/inconsistencia en controles. |
| ACC-ARC-002 | Medium | Media | Probable | Dependencia de estado en proceso (rate limit, métricas, ejecución/SSE/cola) con configuración multi-worker posible (`ACC_WORKERS`) puede romper invariantes de cancelación/visibilidad; mitigado por defaults y advertencias (`gunicorn.conf.py`). |
| ACC-ARC-003 | Low | Media | Confirmado | Contratos heterogéneos entre servicios (formatos de retorno/errores) dificultan verificación uniforme. |

*Detalle completo por hallazgo en la matriz consolidada (`audit/FINAL_AUDIT_REPORT.md`, sección hallazgos) y fichas donde aplique.*