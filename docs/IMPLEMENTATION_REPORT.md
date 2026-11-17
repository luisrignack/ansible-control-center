# Informe de implementación

Fecha: 2026-11-17
Repositorio: `ansible-control-center`
Base: `83228cf` (0.9.9), rama `master`
Estado del árbol: cambios locales **sin commit ni push** (el mandato prohíbe
publicar sin autorización).

## 1. Resumen ejecutivo

### Estado inicial

- 57 módulos de aplicación y 46 ficheros de pruebas; Flask + SQLite WAL +
  Ansible + Gunicorn/Nginx/Docker.
- Suite: **592 passed, 1 skipped**; `ruff`, `mypy` y `bandit -ll` limpios.
- Controles sólidos ya presentes: RBAC en backend, CSRF, CSP/cabeceras,
  cookies seguras, argumentos estructurados sin `shell=True`, pin de IP con
  allowlist, temporales 0600, aprobaciones, locks por host, auditoría de
  autenticación.

### Estado final

- Suite: **609 passed, 1 skipped**, cobertura **82 %** (umbral CI 78 %).
- `ruff`, `mypy`, `bandit -ll` y `pip-audit` limpios.
- Esquema SQLite **v21** con cadena de auditoría y estado interrumpido.
- Nuevas capacidades: backup consistente, CSV de inventario, Prometheus,
  catálogo JSON.
- 3 vulnerabilidades MEDIUM confirmadas corregidas/mitigadas; 2 LOW
  corregidas/documentadas; 1 MEDIUM potencial (MFA) y varios riesgos
  residuales documentados.

No se reescribió la aplicación, no se sustituyó Flask ni SQLite, no se añadió
ninguna dependencia de ejecución y no se eliminó ninguna funcionalidad.

## 2. Hallazgos por severidad

| Severidad | Nº | IDs | Estado |
|---|---:|---|---|
| CRITICAL | 0 | — | — |
| HIGH | 0 | — | — |
| MEDIUM | 3 confirmadas + 1 potencial | SEC-001, SEC-002, SEC-003; MFA (potencial) | Corregidas/mitigadas; MFA pendiente |
| LOW | 2 confirmadas + 1 potencial | SEC-004, SEC-005, SEC-007; SEC-008 | Corregidas o documentadas |
| INFORMATIONAL | 4 | SEC-006, SEC-009, SEC-010, SEC-011/012 | Documentadas / sin hallazgo |

Detalle completo, con archivo, línea, evidencia, explotación, impacto,
recomendación y reproducción, en `docs/audit/SECURITY_AUDIT.md`.

## 3. Modificaciones

### 3.1 Ficheros creados

| Fichero | Propósito |
|---|---|
| `services/audit_chain.py` | Canonicalización, SHA-256, HMAC de checkpoints (funciones puras) |
| `tests/test_hardening_2026_11_16.py` | 17 pruebas de las mejoras |
| `docs/audit/BASELINE.md` | FASE 0 |
| `docs/audit/SECURITY_AUDIT.md` | FASE 1 |
| `docs/audit/EXECUTION_RESILIENCE.md` | FASE 2 |
| `docs/audit/AUDIT_INTEGRITY.md` | FASE 3 |
| `docs/audit/DATABASE_HARDENING.md` | FASE 4 |
| `docs/features/CANARY_EXECUTION.md` | FASE 5 (diseño, no implementado) |
| `docs/features/SMART_INVENTORY.md` | FASE 6 |
| `docs/features/OBSERVABILITY.md` | FASE 7 |
| `docs/features/AUTOMATION_CATALOG.md` | FASE 8 |
| `docs/IMPLEMENTATION_REPORT.md` | Este informe |

### 3.2 Ficheros modificados

| Fichero | Cambio |
|---|---|
| `models/db.py` | v21, migración de auditoría/interrupted, `integrity_check`, `backup_database`, `checkpoint_wal` |
| `models/user.py` | `AuthEvent` encadenado, verificación, exportación y checkpoints; log de fallos |
| `models/batch.py` | Columna/atributo `interrupted`, reconciliación, `latest_host_outcomes` |
| `acc/task_state.py` | Tareas interrumpidas distinguibles |
| `acc/web/tasks.py` | `/api/catalogo`; enlaces de historial con `all=1` (plantilla) |
| `acc/web/inventory.py` | Exportación/importación CSV, última ejecución |
| `acc/web/admin.py` | `/metrics` Prometheus protegido |
| `acc/cli.py` | `audit-verify`, `audit-checkpoint`, `audit-export`, `backup-db` |
| `config.py` | `ACC_PROMETHEUS`, `ACC_PROMETHEUS_TOKEN` |
| `templates/batch.html` | Badge y aviso de lote interrumpido |
| `templates/ejecuciones.html` | Enlaces con `all=1`; badge interrumpido |
| `templates/inventory.html` | Botón exportar, formulario importar, columna última ejecución |
| `tests/test_estructura_app.py` | Contrato de rutas actualizado (4 rutas nuevas) |
| `tests/test_historial_privado_2026_11_16.py` | Aserciones de versión referencian `SCHEMA_VERSION` |
| `CHANGELOG.md` | Entrada `[Unreleased]` |

No se eliminó ningún fichero.

## 4. Mejoras implementadas

### 4.1 Integridad de auditoría (FASE 3)

- Evento canónico JSON determinista; `event_hash = SHA256(prev_hash + canónico)`.
- Inserción con `BEGIN IMMEDIATE` para no bifurcar la cadena entre procesos.
- Migración v21 con backfill de eventos históricos.
- `verify_chain` detecta edición, borrado intermedio, reordenación, duplicados
  y truncamiento posterior a un checkpoint.
- Checkpoints HMAC-SHA256 sobre la cabeza de la cadena.
- Exportación JSON y tres comandos CLI.
- Los errores de escritura de auditoría ya no se silencian.

### 4.2 Ejecución interrumpida (FASE 2)

- `_recover_task_state`: `interrupted=True`, `overall_result="interrupted"` y
  mensaje que indica resultado remoto desconocido.
- `reconcile_orphans`: `interrupted=1` en lotes huérfanos.
- UI: badge «Interrumpido» y aviso explícito de no reintentar a ciegas.
- Sin reintentos automáticos.

### 4.3 Historial global (ACC-SEC-001)

- Los enlaces del listado global conservan `all=1` hacia el detalle y el PDF.
- El aislamiento para operadores no cambia (siguen recibiendo 404).
- Pruebas de regresión para ambos comportamientos.

### 4.4 SQLite (FASE 4)

- `backup_database` con API de backup, temporal 0600, `quick_check` y rename
  atómico.
- `integrity_check` y `checkpoint_wal` reutilizables.
- CLI `flask backup-db --output`.

### 4.5 Inventario (FASE 6)

- Exportación CSV con alcance por usuario, BOM UTF-8 y neutralización de
  fórmulas.
- Importación CSV solo-añadir, con validación IPv4/allowlist, límites,
  duplicados omitidos y errores acotados.
- Última ejecución/resultado por host en la vista y en el CSV.

### 4.6 Observabilidad (FASE 7)

- `/metrics` Prometheus con token Bearer o sesión admin.
- Etiquetas acotadas (endpoint, estado, evento); sin datos personales.
- Métricas de aplicación, cola, locks, lotes y autenticación (24 h).

### 4.7 Catálogo (FASE 8)

- `/api/catalogo` filtrado por rol, sin rutas de playbook ni parámetros
  internos; `requires_approval` derivado de `destructive`.

## 5. Pruebas

### 5.1 Ejecución

```bash
python -m pytest -q          # 609 passed, 1 skipped
python -m pytest -q --cov=app --cov=acc --cov=config --cov=models --cov=services
                             # cobertura total 82 %
python -m ruff check .       # limpio
python -m mypy               # limpio (58 módulos)
python -m bandit -q -r app.py config.py acc models services -ll   # sin hallazgos
python tools/audit_dependencies.py                                # 0 vulnerabilidades
```

- **Correctas:** 609.
- **Fallidas:** 0.
- **Omitidas:** 1 (preexistente, dependiente del entorno).
- **Cobertura:** 82 % (sube desde el umbral 78 %; no se usa como sustituto de
  las pruebas de escenarios).
- **Inestables:** ninguna observada en dos ejecuciones completas.

### 5.2 Pruebas nuevas (17)

Cadena de auditoría (4), estado interrumpido (2), backup/integridad (2),
historial global (1), inventario CSV (3), Prometheus (3), catálogo (2).

### 5.3 Ajustes a pruebas existentes (sin debilitarlas)

- `tests/test_estructura_app.py`: se añadieron las 4 rutas nuevas al contrato
  de URLs. Es un cambio esperado al ampliar la API.
- `tests/test_historial_privado_2026_11_16.py`: las dos aserciones que fijaban
  literalmente `20` ahora comparan con `SCHEMA_VERSION`, para que el contrato
  sea la versión soportada y no un número obsoleto. No se relajó ninguna
  comprobación de seguridad.

## 6. Migraciones

### v21 — auditoría e interrupción

- Añade `auth_events.event_id`, `prev_hash`, `event_hash` e índices.
- Crea `audit_checkpoints`.
- Añade `execution_batches.interrupted` (0/1).
- Backfill único de la cadena sobre eventos existentes.
- No destructiva, idempotente; `user_version` se actualiza al final.

### Procedimiento de recuperación

1. Detener el servicio.
2. `flask --app app backup-db --output /backups/acc-v20.db` **antes** de
   actualizar (si aún no se ha arrancado la nueva versión).
3. Arrancar la nueva versión: la migración se aplica sola.
4. `flask --app app audit-verify` y `PRAGMA quick_check`.
5. Si algo falla: restaurar el backup y arrancar la versión anterior; el
   esquema v20 no se modifica hasta que la v21 termina.

No se requiere migración de datos manual ni cambio de configuración.

## 7. Riesgos pendientes

| Riesgo | Nivel | Motivo / siguiente paso |
|---|---|---|
| Sin MFA para admin | MEDIUM | Requiere diseño de factores y dependencia; planificado |
| Cola en memoria pierde trabajos pendientes al reiniciar | MEDIUM | Limitación aceptada; cola durable con lease/heartbeat en fase futura |
| Rate limit por proceso | LOW | Despliegue de 1 worker + límite en Nginx recomendado |
| Purga de retención trunca el prefijo de la cadena | LOW | Checkpoint + exportación antes de purgar |
| Migraciones con `executescript` no totalmente transaccionales | LOW | Idempotencia + backup previo |
| Sin ejecución canary | MEDIUM | Diseño en `CANARY_EXECUTION.md`; no se simuló |
| Inventario sin facts de OS/CPU/memoria | LOW | Playbook de solo lectura pendiente |
| Semgrep/Gitleaks no integrados en CI | LOW | Ruff/Mypy/Bandit/pip-audit ya cubren parte; valorar añadir |
| Endpoint `/metrics` sin token requiere sesión | LOW | Definir `ACC_PROMETHEUS_TOKEN` en producción |

## 8. Compatibilidad

- **Requisitos:** sin cambios; no se añadieron dependencias.
- **Python:** 3.12 en la imagen; la suite local se ejecutó con 3.14.
- **Esquema:** v20 → v21 automático. Bases más nuevas que la app se rechazan
  con un mensaje claro.
- **URLs existentes:** intactas. Nuevas: `/inventario/exportar.csv`,
  `/inventario/importar`, `/metrics`, `/api/catalogo`.
- **Variables nuevas:** `ACC_PROMETHEUS` (por defecto `1`) y
  `ACC_PROMETHEUS_TOKEN`/`ACC_PROMETHEUS_TOKEN_FILE` (opcional).
- **CLI nuevo:** `audit-verify`, `audit-checkpoint`, `audit-export`,
  `backup-db`.
- **Contratos:** `AuthEvent.recent()` sigue devolviendo las mismas columnas más
  tres nuevas; los consumidores con `SELECT *` deben tolerarlas.
- **UI:** no se eliminó ninguna pantalla ni acción.

## 9. Estado por fase

| Fase | Estado | Justificación |
|---|---|---|
| 0 — Línea base | **COMPLETED** | Inventario, arquitectura, flujos, pruebas y riesgos documentados |
| 1 — Auditoría de seguridad | **COMPLETED** | Revisión ASVS/Top 10/CWE, hallazgos con reproducción |
| 2 — Resiliencia de ejecución | **PARTIALLY_COMPLETED** | Interrupción vs. fallo corregido y documentado; cola durable no implementada (limitación declarada) |
| 3 — Integridad de auditoría | **COMPLETED** | Cadena, checkpoints, verificación, exportación, pruebas |
| 4 — Hardening SQLite | **COMPLETED** | Backup consistente, integridad, migración, pruebas y límites documentados |
| 5 — Canary | **NOT_STARTED** | Análisis y diseño completos; implementación aplazada para no simular una mejora |
| 6 — Inventario inteligente | **PARTIALLY_COMPLETED** | CSV, última ejecución y resultado; facts remotos y tags pendientes |
| 7 — Observabilidad | **COMPLETED** | `/metrics` protegido + métricas existentes documentadas |
| 8 — Catálogo | **PARTIALLY_COMPLETED** | API y modelo documentados; metadatos de verificación/reversibilidad pendientes |
| 9 — Calidad y CI/CD | **PARTIALLY_COMPLETED** | Ruff/Mypy/Bandit/pip-audit/Pytest/cobertura/Docker ya existen; Semgrep/Gitleaks pendientes |
| 10 — Documentación | **PARTIALLY_COMPLETED** | Entregables creados y CHANGELOG actualizado; revisión global de documentación pendiente |

## 10. Recomendaciones y próximo orden

1. **MFA para administradores** (MEDIUM): TOTP/WebAuthn, sin romper LDAPS.
2. **Cola durable opcional** (MEDIUM): solo si se opera con reinicios
   frecuentes o varios workers.
3. **Canary por etapas** (MEDIUM): seguir el plan de `CANARY_EXECUTION.md`.
4. **Facts de inventario de solo lectura** (LOW/MEDIUM): `ansible.builtin.setup`
   y tabla de hechos con `collected_at`.
5. **Checkpoint/exportación programados** (LOW): cron/systemd timer con copia
   fuera del host.
6. **Semgrep/Gitleaks** (LOW): añadir al CI si el coste de mantenimiento es
   aceptable; no sustituyen a las pruebas de escenarios.
7. **Límite de tasa en Nginx** (LOW): defensa en profundidad para login.

## 11. Nota sobre publicación

Conforme al mandato, **no se ha hecho commit, tag, push ni release**. Los
cambios están en el árbol de trabajo para revisión. Cuando se autorice:

```bash
git add -A
git commit -m "feat(security): integridad de auditoría, inventario CSV y observabilidad"
git push origin master
```
