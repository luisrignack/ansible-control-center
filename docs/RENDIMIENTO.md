# Rendimiento y SLO

Este documento fija los objetivos de nivel de servicio (SLO) del proyecto y
explica cómo medirlos con la instrumentación incluida. No sustituye una prueba
de carga formal: sirve para detectar regresiones y comparar releases con el
mismo criterio.

---

## 1. Objetivos (SLO)

| Métrica | Objetivo | Dónde se mide |
|---|---|---|
| p95 de vista previa (256 hosts) | < 1.000 ms | Prueba de carga / `duration_ms` del lote |
| p95 de UI | < 500 ms | `/metricas` (endpoint `index`, `history`, …) |
| p95 de API de estado | < 200 ms | `/metricas` (endpoint `api_task_status`) |
| Primer evento SSE | < 1 s | Log de la app / prueba manual |
| Espera p95 en cola | < 30 s | `/metricas` (`queue.wait.p95_seconds`) |
| Lote de 256 hosts | < 60 min | `execution_batches` + `execution_hosts` |
| Errores 5xx | 0 en operación normal | `/metricas` (`summary.errors`) |

Los umbrales son deliberadamente conservadores: un fallo de SLO debe abrir una
investigación, no bloquear un despliegue por sí solo.

---

## 2. Qué se mide

### 2.1. Latencia por ruta (en memoria)

Cada petición HTTP (salvo `/healthz`, estáticos y el stream SSE) registra su
duración en una ventana acotada por endpoint. Se calculan media, p50, p95 y
máximo con la muestra reciente.

- Panel: `GET /metricas` (solo `admin`).
- JSON: `GET /api/metricas` (solo `admin`).
- Configuración:
  - `ACC_REQUEST_METRICS=1` activa la instrumentación (por defecto).
  - `ACC_REQUEST_METRICS_WINDOW=500` muestras por endpoint (50–10.000).
  - `ACC_SLOW_REQUEST_MS=1000` registra en el log las peticiones que superan
    ese umbral.

Las métricas **no se persisten**: se reinician con el proceso. Para comparar
releases, ejecuta la misma secuencia de navegación o la misma prueba de carga
tras cada despliegue.

### 2.2. Latencia por host (SQLite)

`execution_hosts.duration_ms` guarda la duración real de cada host. El detalle
del lote (`/batches/<batch_id>`) muestra p50, p95 y máximo del lote, calculados
con `ExecutionBatch.get_duration_stats()`.

Esta métrica sí sobrevive a reinicios y sirve para comparar lotes históricos.

### 2.3. Cola y reservas de host

El panel y el JSON de métricas incluyen `queue` con:

- `depth`: trabajos esperando ahora mismo.
- `tasks_pending` y `tasks_running`: tareas en memoria en cada estado.
- `oldest_pending_seconds`: antigüedad del trabajo que más lleva esperando.
- `wait`: media, p50, p95 y máximo del tiempo de espera de los trabajos que ya
  arrancaron (ventana de 500 muestras).
- `active_host_locks`: hosts reservados por lotes en curso.

La estimación que ve el operador en una tarea encolada usa la mediana de
duración de los últimos lotes cerrados de la misma acción. Es una orientación,
no una promesa: depende del número de hosts, de la red y de los reintentos.

### 2.4. Otros datos ya disponibles

- `execution_batches.created_at` / `finished_at`: duración total del lote.
- `task_state.updated_at`: última actualización del progreso.
- Log de Gunicorn: peticiones y errores por worker.
- `auth_events`: accesos y bloqueos (no es rendimiento, pero ayuda a descartar
  causas).

---

## 3. Cómo usarlo

```bash
# Panel HTML (requiere sesión de administrador)
https://<host>/metricas

# JSON para scripts o comprobaciones de SLO
curl -ks -b cookies.txt https://<host>/api/metricas | jq '.summary, .endpoints'

# Detalle de un lote con p50/p95 por host
https://<host>/batches/<batch_id>
```

Secuencia mínima para una comparación reproducible:

1. Reinicia la app (ventana de métricas limpia).
2. Inicia sesión y visita `/`, `/history`, `/inventario`.
3. Abre una vista previa de 256 hosts.
4. Consulta `/metricas` y anota p50/p95.
5. Repite con la misma secuencia tras el cambio.

### 3.1. Prueba sintética de 4 operadores

`tools/load_test_concurrency.py` simula cuatro operadores enviando lotes por la
ruta real (cola, `execution_batches`, `host_locks` y paralelismo acotado), con
Ansible sustituido por una latencia fija. Sirve para comparar releases y
ajustar los cupos; **no mide la red ni los hosts reales**.

```bash
.venv/bin/python tools/load_test_concurrency.py
# 4 usuarios, 20 hosts por lote, 200 ms por playbook, 2 lotes activos
.venv/bin/python tools/load_test_concurrency.py --batch-concurrency 1
```

Resultado de referencia (2026-11-12, 80 hosts, 200 ms sintéticos):

| Configuración | Pared | Throughput | Playbooks máx. | Espera máx. en cola |
|---|---:|---:|---:|---:|
| 1 host por lote | 10,0 s | 8,0 hosts/s | 2 | 6,2 s |
| 4 hosts por lote | 3,3 s | 24,4 hosts/s | 8 | 1,6 s |

La mejora es de ~3× en esta carga; con hosts reales el límite suele ser la red
o la CPU del controlador, no el pool. Ajusta `ACC_BATCH_HOST_CONCURRENCY` y
`ACC_GLOBAL_HOST_CONCURRENCY` y repite la prueba antes de subirlos.

---

## 4. Decisiones de diseño

- **Sin Prometheus/StatsD**: el despliegue portable no debe exigir un servicio
  extra. La ventana en memoria cubre la comparación entre releases.
- **Muestras acotadas**: cada endpoint guarda como máximo
  `ACC_REQUEST_METRICS_WINDOW` valores; el coste de memoria es constante.
- **p95 por rango más cercano**: con muestras pequeñas es más honesto que
  interpolar; no se inventan valores entre observaciones.
- **SSE fuera de la métrica de ruta**: la duración de un stream es el tiempo
  que el cliente mantiene la conexión, no el coste de atenderlo.

---

## 5. Limitaciones actuales

- La ejecución de trabajos y la cancelación siguen siendo **por proceso**. Las
  lecturas de estado y el SSE ya refrescan desde SQLite, pero para varios
  workers de ejecución hace falta la cola compartida (roadmap).
- La prueba de carga incluida (`tools/load_test_concurrency.py`) es sintética:
  mide el planificador con latencia simulada, no Ansible ni la red real. El SLO
  de 256 hosts sigue requiriendo una prueba contra un inventario de laboratorio.
- La ventana en memoria no distingue por worker: si algún día se ejecutan
  varios workers web, cada uno mide su propio tráfico.
- `duration_ms` incluye el tiempo de Ansible y de red; no separa conexión,
  playbook y verificación.

---

## 6. Próximos pasos

1. Cola de trabajos en SQLite para permitir varios workers de ejecución.
2. Prueba de carga con inventario sintético de 256 hosts (la actual cubre el
   planificador, no Ansible real).
3. Exportación de métricas a un formato estándar (OpenMetrics) si se integra
   con un sistema de observabilidad corporativo.
4. Alertas sobre p95 y tasa de errores en el sistema de monitorización externo.
