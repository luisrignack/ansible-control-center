# 00 — Inventario del repositorio

- **Proyecto auditado (verificado):** Ansible Control Center (ACC), según `README.md`, estructura y componentes presentes.
- **Commit auditado:** `b77f1533f83d20e36d0f54d5f2e0876d8f5dde2a` (`git rev-parse HEAD`), mensaje: `Corrige hallazgos QA y actualiza la imagen a pytest 9` (`git log -1`).
- **Rama/remoto:** `master` / `origin/master`. Visibilidad del repositorio: desconocida (`repository_stats`).
- **Fecha de referencia de la auditoría:** 2026-11-12 (contexto de ejecución proporcionado). Estado declarado en prompt: Python 3.11+, Flask 3.1.3, Ansible Core 2.19.15, Gunicorn 23.0.0, SQLite esquema v18, Nginx 1.27 Alpine, etc. (declaraciones no asumidas como correctas sin contraste en código/configuración).
- **Alcance de inspección:** lectura/inspección de archivos del repositorio de trabajo, búsquedas estáticas, revisión de playbooks/modelos/servicios/configuración Docker/Nginx/CI y ejecución de comprobaciones seguras limitadas (ver `audit/12_testing_and_quality.md`). **No se modificó ningún archivo original**; solo se creó documentación en `audit/`.
- **Directorio `audit/` previo:** inexistente antes de la auditoría.

## Tamaño y composición (evidencia de comandos)

- `app.py`: **5960 líneas** (`wc -l app.py`).
- Conteo agregado `wc -l services/*.py models/*.py ansible/*.yml ansible/tasks/*.yml templates/*.html` → **16174 líneas** en esos conjuntos.
- Archivos de proyecto indexados (metadato proporcionado): **167**. Pruebas detectadas por convención: **29**.
- Historial git (metadato): 65 commits, 1 contribuidor humano, 0 bots, 0 PRs mergeados detectados.

## Árbol principal relevante (inventario funcional)

| Área | Rutas principales | Rol |
|---|---|---|
| Punto de entrada web | `app.py`, `wsgi.py`, `gunicorn.conf.py` | Rutas Flask, UI, API/SSE, validación, colas/estado, arranque Gunicorn |
| Configuración | `config.py`, `.env.example`, `.env.docker.example`, `certs/` | Parámetros, defaults, validación de seguridad, TLS material versionado (revisar contenido con cautela) |
| Modelos/persistencia | `models/db.py`, `models/batch.py`, `models/ops.py`, `models/user.py`, `models/password.py`, `models/execution.py` | SQLite, migraciones, lotes, inventario/aprobaciones/programaciones, usuarios, credenciales candidatas/historial |
| Servicios | `services/ansible_runner.py`, `services/vault_manager.py`, `services/host_resolver.py`, `services/job_queue.py`, `services/host_locks.py`, `services/scheduler.py`, `services/ldap_auth.py`, `services/rate_limiter.py`, `services/csrf.py`, `services/safety.py`, `services/password_tester.py`, `services/ssh_host_keys.py`, `services/notifier.py`, `services/pdf_report.py`, `services/wallpaper.py`, `services/task_catalog.py`, `services/time_utils.py`, `services/metrics.py`, `services/modules_panel.py`, `services/secure_temp.py` | Ejecución Ansible, secretos, resolución/allowlist, concurrencia, planificación, autenticación externa, notificación, reporting, utilidades |
| Automatización Ansible | `ansible/ansible.cfg`, `ansible/*.yml`, `ansible/tasks/*.yml`, `ansible/files/aplicar_tapiz_sesiones.sh` | Playbooks Linux/Windows y tareas reutilizables |
| UI | `templates/*.html` (27 plantillas), `static/js/`, `static/css/`, `static/vendor/` | Renderizado, formularios, progreso/SSE, recursos |
| Infra/contenedores | `Dockerfile`, `docker-compose.yml`, `docker-compose.lab.yml`, `docker/entrypoint.sh`, `docker/export-images.sh`, `docker/nginx/Dockerfile`, `docker/nginx/default.conf`, `docker/nginx/self-signed.sh` | Imagen app, proxy TLS, arranque, exportación offline |
| CI/herramientas | `.github/workflows/ci.yml`, `tools/quality_check.py`, `tools/load_test_concurrency.py`, `install.sh` | Puertas de calidad, pruebas de carga, instalación |
| Pruebas | `tests/*.py`, `conftest.py`, `pytest.ini` | Cobertura funcional/seguridad/concurrencia declarada (29 archivos) |
| Documentación | `README.md`, `CHANGELOG.md`, `AGENTS.md`, `docs/*.md` | Contrato funcional, QA, rendimiento, seguridad, operación |

## Playbooks y acciones (evidencia: `services/task_catalog.py`, `ansible/`)

| ID tarea | Playbook | Estado | Rol mín. | Destructiva | Plataformas | Notas |
|---|---|---|---|---|---|---|
| `zona-todas` | `cambiar_hora.yml` | activa | operator | no | linux, windows | Valor fijo America/Caracas; verificación/estado |
| `kaspersky` | `kaspersky.yml` (referenciado) | planned | admin | sí | linux | No operativa en catálogo |
| `contrasena` | `contrasena.yml` | activa | admin | sí | linux | `schedulable=False`; confirmación de contraseña; no persiste secreto |
| `tapiz` | `tapiz.yml` | activa | operator | no | linux | `schedulable=False`; imagen subida/validada |
| `apt-upgrade` | `apt_upgrade.yml` | activa | admin | no | linux | programable; política de omisión/espacio |
| `firewall` | `firewall.yml` | activa | admin | no | linux | `schedulable=False`; ufw/firewalld |

Playbooks adicionales presentes: `estado_hora.yml`, `apt_check.yml`, `apt_space.yml`, `apt_state.yml`, `win_cambiar_zona.yml`, `win_estado_zona.yml`, `estado_tapiz.yml`, `tasks/leer_estado.yml`, `tasks/apt_facts.yml`, script `files/aplicar_tapiz_sesiones.sh`.

## Puntos de entrada y superficies expuestas (evidencia parcial: `grep` en `app.py`, lecturas)

- **HTTP UI/API:** login/logout (`/login`, `/logout` POST), índice/catálogo `/`, tareas `/tarea/<task_id>`, progreso `/progress/<task_id>` y SSE `/progress/<task_id>/events`, API estado `/api/task-status/<task_id>`, cancelación `/tasks/<task_id>/cancel`, configuración/módulos, inventario, lotes/historial/auditoría, aprobaciones/ejecución de aprobaciones, programaciones, usuarios/auth, métricas, informes PDF, healthcheck `/healthz` (y otras rutas a confirmar en lectura exhaustiva de `app.py`).
- **Ejecución asíncrona:** `services/job_queue.py` (cola acotada, workers), `services/scheduler.py`, `ansible_runner.run_*` / streaming.
- **Procesos externos:** `ansible-playbook`, `ansible`, `ansible-vault`, `ssh-keyscan` (según servicios inspeccionados).
- **Integraciones:** LDAP/LDAPS (`services/ldap_auth.py`), SMTP (`services/notifier.py`), SQLite local (`models/db.py`).

## Dependencias declaradas

- **Directas:** `requirements.txt` incluye Flask/Flask-Login/Werkzeug, Gunicorn, Ansible/ansible-core, ldap3, cryptography/Fernet, reportlab (PDF), herramientas de calidad/test (según archivo; lectura combinada parcial en sesión).
- **Fijadas/lock:** `requirements.lock` presente. CI ejecuta `pip install -r requirements.lock` (`.github/workflows/ci.yml`).
- **Base de contenedor:** `Dockerfile` basado en `python:3.12-slim-bookworm` (inspección de archivo).

## Búsquedas rápidas de patrones (código propio, excluyendo `.venv`)

| Patrón | Resultado observado |
|---|---|
| `shell=True` | Solo texto de regla en `tools/quality_check.py:63` (`grep -rn "shell=True" --exclude-dir=.venv`). **No se observaron llamadas propias con `shell=True` en ese grep.** |
| `eval(`/`exec(` | Sin coincidencias propias (`grep -rn "eval(\|exec(" --exclude-dir=.venv`). |
| `render_template_string` / `Markup` / `\|safe` | Sin evidencia clara de uso inseguro propio en el grep mostrado; requiere confirmación manual en plantillas/JS (ver fases 2/11). |
| `CERT_NONE` / `verify=False` / `check_hostname` | `services/ldap_auth.py:145` devuelve `Tls(validate=ssl.CERT_NONE)` cuando la verificación TLS está desactivada por configuración. |

## Limitaciones del inventario

- `app.py` (5960 líneas) y parte de servicios/modelos/plantillas/tests no fueron leídos íntegramente línea a línea en esta sesión; el inventario combina lectura directa, `grep`, conteos y metadatos del entorno.
- No se realizó enumeración dinámica de rutas en runtime ni inspección de base de datos real de producción.
- Presencia de `.env.docker`/material en `certs/` no fue evaluada como contenido sensible expuesto en este informe (evitar exfiltrar secretos).