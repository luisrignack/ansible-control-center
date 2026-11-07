# Análisis profundo — Ansible Control Center / `fecha`

**Fecha:** 2026-11-06
**Árbol analizado:** `master`, commit `33e332a`
**Naturaleza:** auditoría de solo lectura seguida de la aplicación de las
recomendaciones de alta prioridad.
**Ámbito:** código, seguridad, concurrencia, datos, playbooks, pruebas,
despliegue y documentación.
**Estado tras este cambio:** correcciones A1–A6 aplicadas; suite completa
**277 passed, 1 skipped**. Los capítulos 5–7 recogen el trabajo pendiente.

---

## 1. Veredicto general

El proyecto está claramente por encima de un prototipo. Tiene modelo de datos
migrable, auditoría, aprobaciones de dos personas, vault real con
`ansible-vault`, allowlist y fijación de IP, verificación de claves SSH, modo
simulación, cola acotada, progreso SSE, informes PDF y una suite de pruebas
real. Los hallazgos restantes no son “código roto”: son **supuestos no
explicitados** que conviene convertir en invariantes verificadas.

| Área | Nota orientativa | Comentario |
|---|---:|---|
| Seguridad base | 8/10 | CSRF, CSP con nonce, cookies endurecidas, rate limit, bloqueo de cuenta, allowlist y pinning DNS. Quedaban flecos de LDAP y rutas de producción. |
| Corrección funcional | 8/10 | Flujo completo y best-effort; los huecos estaban en carreras y validaciones cruzadas. |
| Concurrencia / escalado | 5/10 | Un solo worker real; scheduler y estado en memoria no soportaban varios procesos. |
| Operación | 7/10 | Docker/nginx/systemd documentados; falta automatizar retención, backups y observabilidad. |
| Calidad de código | 6/10 | Buenas prácticas locales, pero `app.py` concentra 4.308 líneas y hay `assert` en rutas de producción. |
| Pruebas / CI | 6/10 | 265 pruebas y matriz 3.11–3.13, sin lint, tipos, seguridad ni build de Docker. |
| Documentación | 7/10 | Muy completa, con incoherencias puntuales frente al código. |

---

## 2. Verificación realizada

Comandos ejecutados sobre el árbol actual, sin cambios locales al inicio:

| Comprobación | Resultado |
|---|---|
| `.venv/bin/python -m pytest -q` | **265 passed, 1 skipped** |
| `.venv/bin/pip check` | Sin dependencias rotas |
| `python -m compileall` | Correcto |
| `git status --short` | Limpio |
| `git log -1` | `33e332a` en `master` y `origin/master` |

`pytest --cov` no funciona en el entorno local porque `pytest-cov` figura en
`requirements.txt` pero no está instalado en `.venv`. Es deriva
entorno/requisitos, no un fallo del código.

---

## 3. Fortalezas que conviene preservar

- **Seguridad de ejecución:** allowlist, fijación DNS, `known_hosts`
  aprovisionado, verificación de host keys y credenciales en ficheros
  temporales `0600` pasadas con `--extra-vars @fichero` (nunca por `ps`).
- **Vault:** cifrado/descifrado con la CLI oficial y `flock` entre procesos;
  la contraseña maestra no se guarda en disco.
- **Gobierno del cambio:** vista previa, verificación posterior, idempotencia,
  dry-run persistido, aprobaciones two-person y auditoría.
- **Robustez operativa:** reconciliación de lotes huérfanos, recuperación de
  tareas tras reinicio, presupuesto de tiempo por lote y cancelación
  cooperativa.
- **Base de datos:** WAL, `busy_timeout`, permisos `0600` y migraciones por
  `PRAGMA user_version`.
- **Pruebas:** cobertura amplia de seguridad, LDAP, Windows, vault, lotes y CLI.

---

## 4. Hallazgos de alta prioridad

### A1. Efectos secundarios en el arranque y scheduler no multi-proceso

`configure_app()` ejecutaba en **cada** proceso `_purge_retention()`,
`_recover_task_state()`, `ExecutionBatch.reconcile_orphans()` y
`scheduler.start()`. Con `ACC_WORKERS > 1` cada worker podía:

- marcar como `error` tareas que otro worker estaba ejecutando;
- reconciliar lotes ajenos;
- lanzar el mismo schedule duplicado, porque `due_rows()` y `mark_run()` no
  hacían un *claim* atómico.

Además, los comandos CLI importan `app.py`, así que podían arrancar el
scheduler y cerrar tareas en curso.

**Corrección aplicada**

- `ACC_ROLE=all|web|worker|cli` para separar responsabilidades. `cli` no
  arranca scheduler ni mantenimiento.
- Lock de instancia con `flock` en `SECRETS_DIR/scheduler.lock`: solo un
  proceso ejecuta el scheduler, aunque haya varios workers.
- *Claim* atómico por fila en `schedules` (`claimed_at`, `claimed_by`) con
  caducidad, como defensa adicional en despliegues multi-host.
- La recuperación de tareas y la reconciliación de lotes solo tocan registros
  **estancados** (`ACC_RECOVERY_STALE_MINUTES`), no operaciones vivas.
- Nuevo comando `flask scheduler` para ejecutar el programador en un proceso
  dedicado.

### A2. Caída silenciosa a `instance/` en producción

`resolve_db_path()` y `resolve_vault_file()` usaban `instance/` si las rutas de
producción no eran escribibles, dejando solo un `warning`. En un despliegue
real eso podía separar la base y el vault de las rutas respaldadas.

**Corrección aplicada**

- En producción el arranque falla si `ACC_DATA_DIR` o `ACC_SECRETS_DIR` no son
  escribibles.
- La clave de sesión y el pepper tampoco caen a `instance/` en producción.
- El fallback de desarrollo queda limitado por `ACC_ALLOW_DEV_FALLBACK`.

### A3. La aprobación de grupos no fijaba los hosts

El objetivo de grupo se guardaba como `inventory:<id>`. Un admin podía aprobar
10 hosts y ejecutarse sobre 40 si el grupo cambiaba después.

**Corrección aplicada**

- La aprobación incluye una instantánea ordenada de hosts (`target_hosts`) y
  metadatos del grupo (`group_os_flavor`, `group_connection`).
- La ejecución manual usa exactamente esa instantánea; si el grupo cambia, el
  hash ya no coincide y se exige una nueva aprobación.
- Las programaciones conservan la resolución en caliente (decisión de
  producto), pero la validación de plataforma se mantiene.

### A4. La aprobación se consumía antes de encolar

La aprobación se gastaba antes de crear la tarea y encolarla. Si la cola estaba
llena o fallaba el envío, la autorización de un solo uso se perdía.

**Corrección aplicada**

- Flujo en dos fases: `reserve_if_approved()` → encolar → `confirm_reserved()`.
- Si el envío falla, `release_reserved()` devuelve la aprobación a `approved`.
- Las reservas abandonadas caducan y vuelven a `approved` automáticamente.
- `consume_if_approved()` se conserva como envoltorio para compatibilidad.

### A5. LDAP aceptaba `ldap://` en claro

`parse_ldap_url()` aceptaba cualquier esquema y solo activaba TLS con `ldaps`.
Con `ldap://`, la contraseña viajaba sin cifrar.

**Corrección aplicada**

- `ldap://` se rechaza por defecto.
- Solo se permite con `ACC_LDAP_ALLOW_INSECURE=1` explícito.
- En producción, `validate_security_config()` rechaza la combinación insegura.

### A6. La copia de `/etc/timezone.bak` podía ser obsoleta

El playbook usaba `force: false`. Si el equipo ya tenía un `.bak` de un cambio
anterior, se enviaba al operador una copia que no representaba el estado previo.

**Corrección aplicada**

- La copia se sobrescribe siempre (`force: true`) antes de cambiar la zona.
- El `fetch` sigue enviando `/etc/timezone.bak` al directorio de backups del
  operador.

---

## 5. Hallazgos de prioridad media (pendientes)

1. Validación de programaciones sin comprobación de plataforma del grupo.
2. SSE bloqueante con `time.sleep(1)` y solo 4 hilos `gthread`.
3. Retención aplicada únicamente al arrancar.
4. Persistencia de la tarea completa (incluida consola) en cada actualización.
5. Cancelación sin *ownership*: cualquier `operator` puede cancelar cualquier
   tarea por ID.
6. `assert` en rutas de producción; `python -O` los elimina.
7. Sesión no limpiada antes de `login_user` (endurecimiento ante fijación).
8. `datetime.now()` naive en `/acerca-de`.
9. Secretos en `docker-compose.yml` vía variable de entorno.
10. Redirección nginx basada en `$host` y ausencia de HSTS en nginx.
11. Contraseñas candidatas deformadas por `.strip()` y líneas con `#`.

## 6. Deuda de calidad y operación (pendiente)

- `app.py` monolítico: modularizar por *blueprints* de forma incremental.
- Salida de Ansible basada en marcadores de texto; valorar callback JSON.
- CI sin `ruff`/`black`, `mypy`, `bandit`, `pip-audit`, `ansible-lint`, build
  de Docker ni pruebas del compose.
- `requirements.txt` sin lockfile reproducible.
- `pytest-cov` declarado pero ausente del `.venv`; Python local 3.14 frente a
  Docker 3.12 y matriz 3.11–3.13.
- Faltan `LICENSE`, `SECURITY.md` y `CONTRIBUTING.md`.
- Incoherencias documentales puntuales (ubicación de claves SSH del inventario,
  conteo de pruebas, detalles de operación).

---

## 7. Roadmap recomendado

### Sprint 1 — riesgos altos (aplicado en este cambio)

1. Aprobaciones con instantánea de hosts y consumo posterior al encolado.
2. LDAP sin texto claro por defecto.
3. Arranque de producción *fail-closed*.
4. Scheduler con lock, *claim* atómico y roles de proceso.
5. Backup de timezone siempre actualizado.

### Sprint 2 — operación y calidad

6. Retención periódica y persistencia *throttled*.
7. SSE sin bloqueo de hilos.
8. Validador único manual/programado, incluida plataforma.
9. CI con lint, tipos, seguridad, build Docker y lockfile.
10. *Ownership* en cancelación y auditoría.

### Sprint 3 — estructural

11. Estado de tareas fuera de memoria para permitir varios workers.
12. División de `app.py` en *blueprints*.
13. Salida estructurada de Ansible y observabilidad (métricas/healthcheck real).

---

## 8. Conclusión

El proyecto es sólido y desplegable en un entorno controlado de un solo
worker. Las recomendaciones de alta prioridad se han aplicado para que los
supuestos de “un solo proceso”, “el grupo no cambia durante la aprobación” y
“las rutas de producción siempre son escribibles” dejen de ser supuestos y
pasen a ser invariantes verificadas o fallos explícitos.

El estado exacto de cada corrección se refleja en `CHANGELOG.md` y en las
pruebas añadidas junto a este documento.
