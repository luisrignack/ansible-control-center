# Ejecución canary y despliegues controlados (FASE 5)

Estado: **NOT_STARTED** (análisis y diseño completados; implementación no
iniciada)

## 1. Qué existe hoy

| Capacidad | Estado | Evidencia |
|---|---|---|
| Selección de hosts por grupo/manual | Sí | `_resolve_batch_targets`, `host_resolver` |
| Resultado por host con estado y duración | Sí | `execution_hosts`, páginas de lote |
| Parada manual (cancelación cooperativa) | Sí | `job_queue.cancel`, `cancel_event` |
| Aprobación de tareas destructivas | Sí | `approvals`, `ApprovalRequest` |
| Verificación funcional por host | Parcial | Cada playbook devuelve estado posterior; no hay umbral agregado por etapa |
| Límite por host y concurrencia | Sí | `host_locks`, `BATCH_HOST_CONCURRENCY` |
| Etapas porcentuales / canary | **No** | — |
| Umbral de errores con parada automática | **No** | — |
| Reanudación segura de una etapa | **No** | — |

El lote actual procesa **todos** los hosts con concurrencia acotada y guarda
resultados parciales. Si se cancela, conserva lo ya hecho, pero no existe un
concepto de «etapa».

## 2. Por qué no se implementó en esta iteración

La ejecución canary toca simultáneamente:

- El modelo de lote (`execution_batches`) y la reanudación (qué hosts faltan y
  con qué parámetros).
- La cola y la cancelación (parada automática entre etapas).
- Aprobaciones (una aprobación por etapa o una que cubra el plan completo).
- La UI de ejecución y la de progreso.
- La verificación funcional (no basta `rc=0`).

Una implementación apresurada podría dejar estados inconsistentes o permitir
relanzar operaciones no idempotentes. Se prefirió documentar el diseño y no
declarar una mejora simulada. El resto de fases prioritarias (integridad,
interrupciones, inventario, métricas) sí se completaron.

## 3. Diseño propuesto

### 3.1 Modelo de datos (v22)

```sql
CREATE TABLE execution_stages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    stage_index   INTEGER NOT NULL,
    label         TEXT NOT NULL,          -- canary, 10%, 25%, 50%, resto
    percent       INTEGER NOT NULL,
    status        TEXT NOT NULL,          -- pending/running/success/failed/aborted
    max_failures  REAL NOT NULL,          -- umbral (ratio 0..1)
    planned_hosts INTEGER NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    UNIQUE (batch_id, stage_index)
);
CREATE TABLE execution_stage_hosts (
    stage_id INTEGER NOT NULL,
    host     TEXT NOT NULL,
    PRIMARY KEY (stage_id, host)
);
```

`execution_batches` gana `staged INTEGER NOT NULL DEFAULT 0` y
`current_stage INTEGER NOT NULL DEFAULT 0`.

### 3.2 Selección determinista

- Orden estable por IP ascendente (no por orden de inserción) y `SHA256(batch_id
  + ip)` como desempate reproducible.
- Tamaños: `1` host (canary), luego porcentajes configurables
  `ACC_CANARY_STAGES="10,25,50,rest"`.
- El conjunto se materializa en `execution_stage_hosts` **antes** de ejecutar,
  para que la reanudación no dependa de una selección distinta.

### 3.3 Ejecución y parada

1. Etapa canary: se ejecuta y se espera verificación funcional.
2. Entre etapas se evalúa:
   - `failures / executed > max_failures` → `aborted`.
   - Cualquier host `error`/`timeout` en canary → parada por defecto
     (`ACC_CANARY_STRICT=1`).
3. La parada no reintenta; deja las etapas restantes `pending` y el lote
   `partial`/`interrupted` según corresponda.
4. La cancelación manual usa el mismo `cancel_event` y marca la etapa
   `aborted`.
5. La reanudación (`POST /batches/<id>/continuar`) exige:
   - que el lote esté `partial`/`aborted` y no `interrupted`;
   - que los parámetros sigan siendo válidos (mismo hash);
   - que no exista otro lote activo sobre los mismos hosts;
   - una nueva aprobación si la tarea es destructiva y la aprobación anterior
     ya se consumió.

### 3.4 Verificación funcional

No se aceptará `rc=0` como éxito de etapa. Se reutilizarán los marcadores que
ya emiten los playbooks:

- Zona/hora: comparación de `timedatectl`/`tzutil` con el valor objetivo.
- Contraseña: `PASSWORD_VERIFIED` y ping con la nueva credencial.
- APT: estado posterior y espacio libre.
- Firewall: reglas activas.
- Tapiz: URI aplicada en dconf.

Si un playbook no expone verificación, la etapa se marcará
`attention` y no se avanzará sin confirmación explícita.

### 3.5 UI

- Checkbox «Ejecución por etapas» en el formulario (solo tareas activas).
- Página del lote con la tabla de etapas, hosts planificados/ejecutados y
  botón «Continuar» cuando proceda.
- Aviso visible de que una etapa abortada no se reintenta sola.

## 4. Plan de implementación por pasos

| Paso | Entregable | Pruebas |
|---|---|---|
| 1 | Migración v22 + modelos `ExecutionStage` | Migración idempotente, compatibilidad v21 |
| 2 | Cálculo determinista de etapas y materialización | Mismos hosts para el mismo `batch_id` |
| 3 | Bucle de etapas en `_run_apply_batch` con umbral | Parada al superar umbral; canary estricto |
| 4 | Reanudación y validación de aprobación | No reanuda interrumpidos; no duplica hosts |
| 5 | UI y SSE por etapa | Render y eventos |
| 6 | Verificación funcional por tarea | Mocks de salida de Ansible |
| 7 | Documentación y operación | Este documento actualizado |

## 5. Criterios de aceptación

- [ ] Dos ejecuciones con el mismo `batch_id` producen las mismas etapas.
- [ ] Una etapa con fallos por encima del umbral se detiene y no lanza la
      siguiente.
- [ ] La cancelación manual detiene la etapa en curso y conserva resultados.
- [ ] La reanudación no repite hosts ya completados ni relanza operaciones
      interrumpidas.
- [ ] La verificación funcional (no solo `rc=0`) decide el avance.
- [ ] Las pruebas de regresión siguen en verde.

## 6. Riesgos y decisiones pendientes

- **Idempotencia:** contraseña y firewall pueden no serlo; la reanudación debe
  ser explícita y con verificación previa.
- **Aprobaciones:** definir si una aprobación cubre todo el plan o etapa a
  etapa. Recomendación: cubrir el plan completo con el hash de parámetros y
  volver a pedirla si cambia cualquier parámetro.
- **Tamaño del lote:** con pocos hosts, los porcentajes deben colapsar a enteros
  sin dejar etapas vacías.
- **DRY RUN:** permitir simular el plan de etapas sin ejecutar (vista previa).
