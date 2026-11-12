# QA y corrección de hallazgos — 2026-11-12

Informe de la revisión de calidad sobre el commit de paralelización
(`2199350`) y de las correcciones aplicadas antes de reconstruir la imagen.

## Veredicto

La suite funcional y las comprobaciones estáticas quedan en verde. El cambio
es apto para commit y para reconstruir la imagen. La validación de la pila
desplegada se realiza después del build y se registra en este mismo documento.

| Nivel | Comprobación | Resultado |
|---|---|---|
| 1 | Unitarias de paralelismo, cancelación y migración | 9 pasadas |
| 2 | Integración (BD, operaciones, app, aprobaciones, rendimiento, seguridad) | 115 pasadas |
| 3 | Humo HTTP (`/healthz`, `/`, `/login`, `/api/metricas`) | 4 correctas |
| 4 | Regresión completa | 428 pasadas, 1 omitida |
| 5 | `compileall`, `quality_check.py`, YAML, Compose y entrypoint | correctos |
| 5 | Ruff | 0 hallazgos |
| 5 | Black | 61 archivos sin cambios |
| 5 | mypy | 0 errores |
| 5 | Bandit | 0 altos, 0 medios, 27 bajos |
| 5 | pip-audit | 0 vulnerabilidades conocidas |

Salida de referencia:

```text
428 passed, 1 skipped, 3 warnings
All checks passed!          # ruff
61 files would be left unchanged  # black
Success: no issues found in 32 source files  # mypy
No known vulnerabilities found  # pip-audit
```

## Hallazgos corregidos

### Tipado y retornos

- `_run_preview_batch` y `_run_apply_batch` estaban anotadas `-> None` pero
  devolvían `"completed"`, `"incomplete"` o `"cancelled"`. Ahora declaran
  `-> str`; la herramienta de carga ya no propaga `None`.
- Se corrigieron 90 parámetros con `Optional` implícito (`db_path: str = None`
  y equivalentes) a `str | None`, incluidos los modelos y servicios.
- Se normalizaron identificadores de `cursor.lastrowid` a `int` y se añadieron
  comprobaciones explícitas cuando la lectura podía ser `None`.
- Se corrigieron el tipo de `min_free_gb`, el estrechamiento de `next_url` en
  el login y la asignación de `ProxyFix` al `wsgi_app` de Flask.

### Lint y formato

- Ruff pasó de 879 a 0 hallazgos. Se aplicaron 790 correcciones automáticas
  seguras y se resolvieron manualmente las restantes: variables muertas,
  `dict.keys()`, retornos redundantes, aperturas de ficheros y concatenaciones
  de cadenas.
- Black se aplicó a 51 archivos; la comprobación final no reformatea ninguno.
- Las capturas amplias de `Exception` que son fronteras deliberadas de
  resiliencia (auditoría, cierre de LDAP, limpieza de reservas, salvaguardas
  por host) conservan `# noqa: BLE001` con su justificación. No se usa `noqa`
  para ocultar errores de lógica.

### Seguridad y dependencias

- Bandit: los dos hallazgos medios `B608` de `models/batch.py` eran falsos
  positivos. `counter` sale de una tabla interna tras validar el resultado y
  `stale_filter` solo añade SQL fijo; los valores de fecha siguen viajando como
  parámetros. Se documentó con `# nosec B608`.
- `pytest` se actualizó de 8.4.2 a 9.0.3 en `requirements.txt` y
  `requirements.lock`, y la suite completa se validó con esa versión.
- `pip` del entorno se actualizó a 26.3. `pip-audit --local` audita 72 paquetes
  instalados sin vulnerabilidades conocidas.
- Los 27 avisos bajos de Bandit son en su mayoría falsos positivos de pruebas y
  herramientas (`B106` por contraseñas de laboratorio, `B110` en limpiezas
  best-effort). No hay hallazgos altos ni medios.

### Pruebas añadidas

En `tests/test_paralelismo_2026_11_12.py`:

1. Cancelación mientras un host está en vuelo: los hosts en espera se omiten,
   no arranca ninguno nuevo y el resultado global es `cancelled`.
2. Deadline mientras un worker está activo: el host en vuelo termina, los
   pendientes se omiten y el resultado es `incomplete`.
3. Fallo interno de un worker: el cupo global se libera y el lote siguiente
   puede ejecutarse.
4. Migración v17→v18 con datos existentes: `position` se rellena con el `id`,
   el orden se conserva y una segunda pasada no pisa posiciones ya asignadas.

## Riesgos residuales

- Bandit mantiene 27 avisos bajos, revisados como falsos positivos o casos de
  laboratorio; no bloquean el despliegue.
- La prueba de carga sintética no se repitió en esta pasada; la referencia de
  rendimiento sigue siendo la del commit `2199350`.
- La auditoría y el build pueden depender de la red corporativa. Si el registro
  no está disponible, el build fallará de forma explícita y no se publicará una
  imagen parcial.
- El contenedor se validó con el código y la suite local; la comprobación de
  salud posterior al `up -d` es la evidencia final de despliegue.

## Despliegue verificado

- Imagen `ansible-control-center:latest` reconstruida desde el árbol corregido
  (Python 3.12, Gunicorn 23.0.0, pytest 9.2.0 instalado por el rango de
  `requirements.txt`).
- El primer `up -d` sin `.env.docker` fue rechazado correctamente por
  `validate_security_config` (faltaban allowlist y clave). El servicio se
  recreó con `docker compose --env-file .env.docker up -d app`.
- Estado final: `hora-admin-app-1` y `hora-admin-nginx-1` en `healthy`.
- `https://127.0.0.1/healthz` devuelve 200 con
  `{"database":"ok","status":"ok"}` y `/login` devuelve 200 con el campo de
  contraseña.
- DaoCloud se probó como mirror de la imagen base, pero el blob de
  `python:3.12-slim` respondió 403 desde CloudFront. No se cambió el Dockerfile;
  el build se completó con la imagen base local y los repositorios oficiales
  cuando la red se restableció.
