# Observabilidad (FASE 7)

Estado: **COMPLETED** para el endpoint Prometheus y las métricas existentes.
Quedan mejoras opcionales documentadas (CPU/memoria/disco, tracing).

## 1. Lo que ya existía

| Recurso | Contenido | Protección |
|---|---|---|
| `GET /healthz` | Proceso vivo + `SELECT 1` | Público, sin datos sensibles |
| `GET /api/metricas` | Ventana de latencias p50/p95 por endpoint, cola, locks, resumen | Solo admin |
| Panel «Rendimiento» | Misma información en HTML | Solo admin |
| Logs | Arranque, errores, peticiones lentas (`SLOW_REQUEST_MS`), auditoría | Fichero/rotación del sistema |

`services/metrics.py` mantiene una ventana acotada por endpoint
(`ACC_REQUEST_METRICS_WINDOW`, 500 por defecto), sin persistencia. No hay
dependencias externas.

## 2. Endpoint Prometheus (nuevo)

`GET /metrics`, formato de texto Prometheus 0.0.4.

### Autorización

1. Si `ACC_PROMETHEUS_TOKEN` (o `ACC_PROMETHEUS_TOKEN_FILE`) está definido, se
   exige `Authorization: Bearer <token>` (comparación en tiempo constante).
2. Si no hay token, solo un **administrador con sesión** puede leerlo.
3. `ACC_PROMETHEUS=0` desactiva el endpoint (404).

El endpoint nunca devuelve usuarios, IPs, nombres de host, parámetros ni
credenciales. Las etiquetas están acotadas:

- `endpoint`: nombre de endpoint Flask (conjunto finito de rutas).
- `status`: estados de lote (`running`, `success`, `partial`, `failure`,
  `preview`, `error`, `incomplete`, `cancelled`).
- `event`: tipos de evento de auditoría (conjunto definido por el código).

No se usan etiquetas de alta cardinalidad (IP, usuario, host, batch_id).

### Métricas

| Métrica | Tipo | Descripción |
|---|---|---|
| `acc_info{version,environment}` | gauge | Versión y entorno |
| `acc_requests_total{endpoint}` | counter | Peticiones por endpoint |
| `acc_request_errors_total{endpoint}` | counter | Respuestas 5xx |
| `acc_request_duration_p95_ms{endpoint}` | gauge | p95 de la ventana |
| `acc_jobs_pending` | gauge | Trabajos en cola |
| `acc_jobs_running` | gauge | Trabajos en ejecución |
| `acc_jobs_workers` | gauge | Workers configurados |
| `acc_host_locks_active` | gauge | Reservas de host vigentes |
| `acc_batches_total{status}` | counter | Lotes por estado (SQLite) |
| `acc_auth_events_24h{event}` | counter | Eventos de auth en 24 h |

### Ejemplo de scrape

```yaml
scrape_configs:
  - job_name: acc
    scheme: https
    metrics_path: /metrics
    authorization:
      credentials: "<ACC_PROMETHEUS_TOKEN>"
    static_configs:
      - targets: ["acc.interna:443"]
```

## 3. Salud y capacidad

- `/healthz` comprueba conectividad SQLite. Un fallo devuelve 503 con
  `{"status":"error","database":"unavailable"}` y registra el detalle en el log.
- La profundidad de cola, workers y locks se publican en Prometheus y en
  `/api/metricas`.
- `flask backup-db` + `integrity_check` cubren la salud física de la base; no
  se ejecuta `integrity_check` completo en cada scrape para no penalizar.
- La duración por operación/host ya está en `execution_hosts`; el dashboard
  puede consultarla por SQL o exportarla.

## 4. Calidad operacional y seguridad

- Tasa de éxito/fallo/parcial: `acc_batches_total{status}` y contadores del
  lote.
- Operaciones desconocidas/interrumpidas: badges en la UI y
  `interrupted` en el lote; recomendable añadir
  `acc_batches_total{status="error",interrupted="true"}` en una próxima
  iteración (requiere una etiqueta más, aún acotada).
- Seguridad: `acc_auth_events_24h{event}` permite alertar sobre
  `login_failed`, `login_locked`, `role_change`, `user_create`, etc.
- Aprobaciones: el número de pendientes ya se muestra en el panel; se puede
  añadir `acc_approvals_pending` (baja cardinalidad) si se necesita alerta.

## 5. Pruebas

- `test_metrics_requiere_autenticacion` → 403 sin sesión.
- `test_metrics_prometheus_para_admin` → 200, `acc_info`, cola y lotes.
- `test_metrics_prometheus_con_token` → 403 sin token, 200 con Bearer, 403 con
  token incorrecto.
- La ruta se añadió al contrato de `test_estructura_app.py`.

## 6. Gaps y recomendaciones

| Gap | Prioridad | Nota |
|---|---|---|
| CPU, memoria y disco del proceso | LOW | Se pueden leer de `/proc` o `resource` sin dependencias; no se añadió para mantener el alcance |
| Histogramas Prometheus nativos | LOW | La ventana actual da p95 suficiente; un histograma real requiere más estado |
| Latencia de conexión SSH y errores por host | MEDIUM | Ya hay duración por host en SQLite; falta agregarlo como métrica |
| Tracing distribuido | NO APLICA | Un solo proceso; no se justifica OpenTelemetry |
| Alertas por interrupciones | MEDIUM | Derivar de `interrupted=1` en SQLite o añadir etiqueta acotada |

## 7. Compatibilidad

- `/api/metricas` y el panel no cambian.
- `/metrics` es nuevo; si `ACC_PROMETHEUS=0`, no existe (404).
- Sin token, el scrape necesita una sesión de admin (no apto para Prometheus
  headless). Para producción se recomienda definir el token y restringir la
  ruta por red.
