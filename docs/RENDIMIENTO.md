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
| Espera en cola con 50 trabajos | < 30 s | Pendiente: métrica de cola (roadmap) |
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

### 2.3. Otros datos ya disponibles

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
- No hay prueba de carga automatizada todavía. El SLO de 256 hosts se valida
  con una prueba manual o con el futuro `tools/load_test.py`.
- La ventana en memoria no distingue por worker: si algún día se ejecutan
  varios workers web, cada uno mide su propio tráfico.
- `duration_ms` incluye el tiempo de Ansible y de red; no separa conexión,
  playbook y verificación.

---

## 6. Próximos pasos

1. Cola de trabajos en SQLite para permitir varios workers de ejecución.
2. Métrica de espera en cola (encolado → inicio) y de trabajos activos.
3. Prueba de carga con inventario sintético de 256 hosts.
4. Exportación de métricas a un formato estándar (OpenMetrics) si se integra
   con un sistema de observabilidad corporativo.
5. Alertas sobre p95 y tasa de errores en el sistema de monitorización externo.
