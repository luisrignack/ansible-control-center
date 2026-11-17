# Resiliencia de ejecución (FASE 2)

Fecha: 2026-11-17
Alcance: cola, workers, scheduler, locks, cancelación, persistencia y
recuperación de Ansible Control Center.

## 1. Arquitectura de ejecución real

```
POST /execute ──► handle_execution ──► validación + aprobación
                        │
                        ▼
                 create_task() ──► task_state (memoria + SQLite)
                        │
                        ▼
              JobQueue.submit(task_id, run_batch, ...)
                        │
        ┌───────────────┴───────────────┐
        │  hilo worker 1                │  hilo worker 2 (JOB_MAX_WORKERS)
        ▼                               ▼
   _run_apply_batch / _run_preview_batch
        │
        ├── resolución de hosts (pin a IP + allowlist)
        ├── lock por host (SQLite, TTL)
        ├── ThreadPoolExecutor(BATCH_HOST_CONCURRENCY)
        │        └── ansible-playbook (Popen + streaming)
        ├── execution_hosts / execution_batches (SQLite)
        └── task_state (instantáneas periódicas)
```

- **Job Manager:** `services/job_queue.py`. Cola FIFO `queue.Queue` con
  `max_workers` hilos daemon, tamaño máximo `JOB_QUEUE_SIZE`, cancelación
  cooperativa por `threading.Event` y apagado ordenado con gracia
  (`JOB_SHUTDOWN_GRACE_SECONDS`).
- **Workers:** hilos del proceso web. No hay procesos externos ni broker.
- **Concurrencia por lote:** `ThreadPoolExecutor` con
  `BATCH_HOST_CONCURRENCY`, más un semáforo global de hosts
  (`GLOBAL_HOST_CONCURRENCY`).
- **Scheduler:** hilo daemon que revisa `schedules` cada intervalo; usa un lock
  de instancia en SQLite y puede ejecutarse en un proceso separado
  (`ACC_ROLE=worker`, `flask scheduler`).
- **Persistencia de tareas:** `task_state(task_id, payload JSON, updated_at)`.
  La memoria es la fuente para el SSE; SQLite permite reconstruir la página de
  progreso tras un reinicio.
- **Locks de host:** tabla `host_locks` con `expires_at`; `INSERT OR IGNORE` +
  `UPDATE` condicional para evitar carreras. TTL `HOST_LOCK_TTL_SECONDS` y
  refresco durante la ejecución.
- **Cancelación:** se atiende entre hosts y dentro del playbook (el runner lee
  la salida línea a línea y termina el grupo de procesos). El proceso Ansible
  se lanza en su propio grupo/sesión para matar también a los hijos.

## 2. Comportamiento ante fallos (análisis)

| Evento | Comportamiento | ¿Pérdida? |
|---|---|---|
| Reinicio de Flask/contenedor con tareas en cola | La cola es en memoria; los trabajos pendientes se pierden | Sí (aceptado) |
| Caída con tareas en ejecución | `_recover_task_state` marca la tarea como interrumpida; `reconcile_orphans` cierra el lote | No se pierde el registro; el resultado remoto es desconocido |
| Terminación de un worker | El estado queda en SQLite; se reconcilia al arrancar | Resultado desconocido |
| Error de SQLite al persistir una tarea | Se registra un aviso una sola vez y la ejecución continúa | Posible pérdida de la instantánea, no del proceso |
| Timeout de Ansible | El runner mata el grupo de procesos y devuelve `timeout` | Se registra por host |
| Cancelación manual | `cancel_event`; el lote cierra hosts pendientes como omitidos | Parcial conservado |
| Lock de host ocupado | El host se omite con motivo, sin esperar indefinidamente | Se registra |

### 2.1 Problema corregido: interrupción vs. fallo

Antes, `_recover_task_state` y `reconcile_orphans` escribían `status="error"` y
un mensaje genérico. Ahora:

- Tarea: `status="error"`, `interrupted=True`,
  `overall_result="interrupted"`, mensaje que indica que el resultado remoto es
  desconocido y que se verifique antes de reintentar.
- Lote: `status="error"` (compatibilidad), `interrupted=1` (nueva columna v21),
  badge «Interrumpido» y aviso en `batch.html`.
- **No se reintenta automáticamente** ninguna operación.

### 2.2 Límites conocidos

1. **La cola no es durable.** Un reinicio pierde los trabajos que aún no
   arrancaron. Implementar una cola durable exigiría una tabla de trabajos con
   reserva atómica y lease/heartbeat; es una línea de trabajo futura, no se
   introdujo Redis/Celery.
2. **El estado de la tarea vive en memoria.** SQLite es una copia de respaldo
   con escrituras agrupadas (`TASK_PERSIST_INTERVAL_SECONDS`), no la fuente de
   verdad en caliente.
3. **Un solo proceso web** es el despliegue previsto. Con varios procesos, la
   cola, el rate limiter y el estado en memoria no se comparten; el scheduler
   ya usa un lock para no duplicar disparos.
4. **No hay heartbeat de worker.** La recuperación usa `updated_at` y
   `RECOVERY_STALE_MINUTES` para no cerrar tareas de otra instancia viva.

## 3. Concurrencia y SQLite

- WAL permite lecturas concurrentes; las escrituras se serializan con
  `busy_timeout=5000`.
- Los contadores del lote se actualizan con sentencias atómicas
  (`success_count = success_count + 1`), no con lectura-modificación-escritura.
- Los locks de host usan una transacción con `INSERT OR IGNORE` y `UPDATE`
  condicionado por expiración; dos lotes no pueden ejecutar el mismo host.
- La cadena de auditoría usa `BEGIN IMMEDIATE` para serializar el cálculo de
  `prev_hash` y evitar bifurcaciones.
- Las pruebas de concurrencia existentes (`tests/test_paralelismo_2026_11_12.py`)
  cubren locks, contadores y cancelación; se mantienen en verde.

## 4. Pruebas añadidas

| Prueba | Qué verifica |
|---|---|
| `test_tarea_interrumpida_se_distingue_de_un_fallo` | `interrupted`, `overall_result` y mensaje de resultado desconocido |
| `test_lote_huerfano_se_marca_interrumpido` | Columna `interrupted` y estado compatible `error` |
| `test_reconcile_orphan_running_batches` (existente) | Idempotencia y no tocar lotes `preview` |
| `test_incomplete_tasks_are_closed_after_restart` (existente) | Compatibilidad del contrato previo |
| `tests/test_hardening_2026_11_16.py::test_audit_chain_*` | Serialización de escritores de auditoría |

## 5. Recomendaciones (prioridad)

1. **Cola durable opcional** (MEDIUM): tabla `jobs` con estados
   `queued/running/done`, reserva atómica `UPDATE ... WHERE state='queued'`,
   lease con `expires_at`, heartbeat del worker y reencolado solo de trabajos
   idempotentes. No implementar mientras el despliegue sea de un worker.
2. **Heartbeat explícito** (LOW): actualizar `updated_at` también durante
   fases largas sin salida de Ansible.
3. **Límite por usuario** (LOW): además del límite global, acotar lotes
   concurrentes por `owner_user_id`.
4. **Verificación funcional post-etapa** (ver FASE 5).
5. **Métricas de reintentos/interrupciones** (ver FASE 7): ya se expone
   `acc_batches_total{status}`; añadir un contador de interrupciones si se
   opera con reinicios frecuentes.

## 6. Conclusión

La ejecución es razonablemente robusta para el despliegue de un worker:
locks persistentes, cancelación real del árbol de procesos, timeouts, límites
y reconciliación al arrancar. La limitación principal —cola en memoria— es
estructural y está declarada; el cambio de esta fase elimina la ambigüedad más
peligrosa (confundir una interrupción con un fallo) sin alterar contratos
existentes ni reintentar operaciones de forma automática.
