# Auditoría técnica — Ansible Control Center / `fecha`

**Fecha:** 2026-11-08
**Árbol analizado:** `master`, commit `0a96da7`
**Naturaleza:** verificación de solo lectura del análisis técnico previo y
propuesta de plan de corrección. Este documento no aplica cambios funcionales.
**Ámbito:** seguridad, arquitectura, pruebas, despliegue, CI, dependencias y
documentación.
**Estado del repositorio al analizar:** limpio; suite **359 passed, 1 skipped**
(360 pruebas recopiladas) y `tools/quality_check.py` correcto sobre 28
archivos.

---

## 1. Veredicto general

El proyecto es sólido y desplegable en un entorno controlado. La seguridad de
ejecución está bien resuelta y los hallazgos graves de fases anteriores siguen
corregidos. La deuda principal no es de corrección funcional, sino
**estructural y operativa**:

- `app.py` concentra demasiada responsabilidad.
- El motor de lotes no está extraído y duplica lógica entre vista previa y
  ejecución.
- El estado de tareas vive en memoria y limita el despliegue a un worker.
- La cadena de build y el CI pueden endurecerse.
- Quedan dos defaults de seguridad mejorables en Docker y un hueco de
  autorización en la API de estado.

| Área | Nota orientativa | Comentario |
|---|---:|---|
| Seguridad | 8/10 | Defensa en profundidad real; dos defaults de compose y un endpoint que revisar |
| Corrección funcional | 8/10 | Flujos completos, idempotencia y verificación posterior |
| Arquitectura | 5/10 | Monolito de 5.485 líneas; estado en memoria y motor sin extraer |
| Pruebas | 7/10 | Suite amplia; integración real de LDAP/WinRM/scheduler incompleta |
| Despliegue / CI | 6/10 | Build de imagen no reproducible y gates todavía laxos |
| Documentación | 8/10 | Extensa y mayormente fiel; alguna deriva menor en el paquete portable |

La nota de seguridad del análisis original era 8,5/10. La rebajo a 8/10 por
coherencia entre lo que el código promete y lo que activa el despliegue por
defecto; no por un fallo crítico.

---

## 2. Verificación realizada

Comprobaciones ejecutadas sobre el árbol actual, sin modificar código:

| Comprobación | Resultado |
|---|---|
| `wc -l app.py` | 5.485 líneas |
| Rutas `@app.route` | 30 |
| Funciones en `app.py` (AST) | 126 |
| `execute_with_progress` | 432 líneas, 26 argumentos |
| `_run_apply_batch` | 385 líneas, 28 argumentos |
| `_run_preview_batch` | 252 líneas, 21 argumentos |
| `_parse_operation_form` | 305 líneas |
| Imports directos de `config` en `app.py` | 81 |
| `pytest --collect-only` | 360 pruebas |
| `pytest -q` | 359 passed, 1 skipped |
| `tools/quality_check.py` | Correcto, 28 archivos |
| `ansible-playbook --syntax-check` | Correcto en los playbooks nuevos |
| `git status --short` | Limpio |
| `git log -1` | `0a96da7` en `master` |

La verificación fue estática y de ejecución local. No se probó contra
infraestructura real LDAP, WinRM ni SSH; esas pruebas siguen siendo opcionales
o simuladas, como se indica en los hallazgos.

---

## 3. Fortalezas que conviene preservar

- **Credenciales fuera de `argv` y de los logs:** viajan en ficheros temporales
  `0600` con borrado garantizado y entorno mínimo de Ansible
  (`services/ansible_runner.py`).
- **Arranque fail-closed en producción:** exige allowlist, `SECRET_KEY`
  explícita, verificación de claves SSH y cookie `Secure` (`config.py`).
- **Gobierno del cambio:** vista previa de solo lectura, verificación
  posterior, idempotencia, `DRY_RUN` por defecto, doble aprobación de un solo
  uso y auditoría.
- **Persistencia:** WAL, `busy_timeout`, claves foráneas, esquema versionado
  v15 y guard anti-downgrade (`models/db.py`).
- **Scheduler:** claim atómico y roles de proceso separables.
- **Contenedor:** usuario no root, `cap_drop: ALL` y
  `no-new-privileges:true`.
- **Higiene de código:** sin `shell=True`, `eval` ni `exec`; sin secretos en el
  historial de Git.
- **Documentación:** amplia, con runbook, referencia, arquitectura y análisis
  previos.

---

## 4. Hallazgos

Severidades: **Media**, **Baja** y **Deuda técnica**. No se encontraron
hallazgos críticos ni altos.

### 4.1. Seguridad

| ID | Hallazgo | Severidad | Estado |
|---|---|---|---|
| S1 | TOFU SSH activado por defecto en Docker | Media | Abierto |
| S2 | WinRM sin TLS por defecto (5985 + NTLM) | Media | Abierto |
| S3 | `/api/task-status` permite leer consolas ajenas | Media-baja | Abierto |
| S4 | SMTP sin cifrar si se usan credenciales fuera del puerto 587 | Baja | Abierto |
| S5 | Rate limiter en memoria por proceso | Baja | Limitación aceptada con 1 worker |
| S6 | `certs/` e `instance/` con permisos 0775 | Baja | Abierto |
| S7 | Secretos por variable de entorno en `docker inspect` | Baja-media | Abierto |

#### S1. TOFU SSH por defecto en Docker

**Evidencia:** `docker-compose.yml:38` define
`ACC_SSH_HOST_KEYS_AUTO: "${ACC_SSH_HOST_KEYS_AUTO:-1}"`, mientras que
`config.py:484` usa `False` por defecto.

**Impacto:** `ssh-keyscan` acepta la primera huella sin verificación previa. Un
MITM en el primer contacto podría recibir después las credenciales SSH.

**Recomendación:** alinear compose con el default seguro (`0`), documentar el
modo automático como perfil de laboratorio o primera instalación controlada, y
exigir `known_hosts` pre-poblado en producción.

#### S2. WinRM sin TLS por defecto

**Evidencia:** `config.py:260` usa el puerto 5985 y `config.py:262` el
transporte `ntlm`; `docker-compose.yml:52-53` repite esos valores.
`cert_validation=validate` no protege una conexión HTTP.

**Impacto:** MITM o relay NTLM en redes no confiables.

**Recomendación:** usar 5986 + TLS + `validate` como default de producción y
dejar 5985/NTLM en un perfil `lab` explícito.

#### S3. `/api/task-status` sin control de propiedad

**Evidencia:** `app.py:4537` define `api_task_status` solo con
`@login_required`; devuelve el diccionario completo de la tarea, incluidos
`console`, `log` y `password_hash`. El SSE sí comprueba propietario o admin en
`app.py:4563`.

**Impacto:** divulgación de consolas y metadatos entre usuarios autenticados.
Mitigado porque los IDs son UUID no adivinables y la consola redacta secretos.

**Recomendación:** devolver el estado completo solo al propietario o admin y
`403` (o un subconjunto mínimo) al resto, igual que el SSE. Añadir pruebas de
autorización.

> **Corrección al análisis original:** el hallazgo “viewer ve consolas ajenas”
> es correcto en el fondo, pero la ruta citada (`app.py:4521`, SSE) no es el
> origen. El SSE está protegido; el hueco real es `api_task_status`.

#### S4. SMTP sin cifrar con credenciales

**Evidencia:** `services/notifier.py:58-62` usa `smtplib.SMTP` y solo llama a
`starttls()` si `SMTP_STARTTLS` es verdadero. `config.py:450` y `config.py:454`
hacen que STARTTLS sea verdadero solo en el puerto 587.

**Impacto:** con usuario y contraseña SMTP en el puerto 25, las credenciales
viajan sin cifrar. Mitigado porque el notificador está apagado por defecto.

**Recomendación:** rechazar el envío si hay credenciales y no hay STARTTLS o
TLS implícito; valorar soporte de 465 con `SMTP_SSL`.

#### S5. Rate limiter en memoria

**Evidencia:** `services/rate_limiter.py:31-32` guarda eventos y lock en el
proceso. Con `gunicorn.conf.py:14` fijando un worker no hay impacto actual.

**Impacto:** al subir workers, cada proceso aplica su propio límite y los
resets de reinicio se pierden.

**Recomendación:** mantenerlo documentado mientras haya un worker; si se sube
el número, mover el contador a SQLite o Redis con operaciones atómicas.

#### S6. Permisos de directorios

**Evidencia:** `certs/` e `instance/` están en modo 0775 (grupo escribible).
Los archivos sensibles conocidos sí están en 0600.

**Impacto:** un miembro del grupo podría sustituir o borrar contenido. Riesgo
marginal, pero evitable.

**Recomendación:** crear ambos directorios con 0750 y verificar permisos en el
instalador y en el entrypoint.

#### S7. Secretos por entorno en Compose

**Evidencia:** `docker-compose.yml:33` y `docker-compose.yml:64` pasan
`ACC_ADMIN_PASSWORD` y `ACC_LDAP_BIND_PASSWORD` como variables. El entrypoint
soporta `_FILE`, pero Compose no lo cablea.

**Impacto:** los valores son visibles en `docker inspect` y en herramientas de
introspección del contenedor.

**Recomendación:** usar `secrets:` y `ACC_*_PASSWORD_FILE` en el compose de
producción; dejar las variables planas solo para desarrollo.

### 4.2. Arquitectura y código

| ID | Hallazgo | Severidad | Estado |
|---|---|---|---|
| A1 | `app.py` monolítico (5.485 líneas, 30 rutas, 126 funciones) | Media | Abierto |
| A2 | Funciones gigantes con 21–28 parámetros | Media | Abierto |
| A3 | Duplicación entre vista previa y ejecución | Media | Abierto |
| A4 | Estado de tareas en memoria: limita a 1 worker | Media | Abierto |
| A5 | 81 imports directos de `config` | Baja | Abierto |
| A6 | Lógica de frontend duplicada y 760 líneas de JS inline | Baja | Abierto |
| A7 | Manejo de errores inconsistente (404 sin rama JSON) | Baja | Abierto |
| A8 | Gate de calidad estrecho | Baja | Abierto |

#### A1. Monolito

`app.py` mezcla rutas, validación, orquestación de lotes, SQL directo, SSE y
CLI. El motor de ejecución ocupa aproximadamente las líneas 3.319–4.504 y no se
ha extraído pese a las fases P0–P4.

**Recomendación:** aplicar el patrón *strangler*: extraer primero validación,
luego motor de lotes, después estado de tareas y finalmente blueprints. No
reescribir de golpe.

#### A2. Funciones gigantes

`execute_with_progress` (432 líneas, 26 argumentos), `_run_apply_batch`
(385/28), `_parse_operation_form` (305) y `_run_preview_batch` (252/21)
concentran demasiadas ramas y parámetros. Cualquier funcionalidad nueva toca
firmas con varias llamadas.

**Recomendación:** introducir dataclasses de opciones (`BatchOptions`,
`OperationForm`) y dividir por responsabilidades.

#### A3. Duplicación vista previa / ejecución

Los bucles de hosts, cancelación, deadline y reintento de contraseñas son casi
idénticos y ya divergen: `console_secrets` solo existe en ejecución porque la
vista previa no recibe la contraseña nueva. La divergencia está justificada,
pero la duplicación no.

**Recomendación:** un único motor con modo `preview`/`apply` y callbacks
compartidos de consola, cancelación y deadline.

#### A4. Estado en memoria

`_tasks`, `_task_events` y `_task_persist_times` viven en el proceso
(`app.py:209`). Gunicorn fija un worker (`gunicorn.conf.py:14`) y la cancelación
puede no alcanzar al worker que ejecuta si se sube el número.

**Recomendación:** extraer un `TaskStore` con implementación SQLite y caché
opcional; después permitir varios workers.

#### A5–A8. Deuda menor

- **A5:** 81 nombres importados de `config` acoplan `app.py` a la configuración.
- **A6:** validación IPv4/CIDR y badges de estado reimplementados en Jinja/JS;
  760 líneas de JS inline sin módulos.
- **A7:** el handler 404 no tiene rama JSON, a diferencia de 400/403/429.
- **A8:** `tools/quality_check.py` solo detecta `assert`, `eval`/`exec`,
  `shell=True` y `pickle`; no mide complejidad ni cubre templates/JS.

### 4.3. Pruebas

| ID | Hallazgo | Severidad | Estado |
|---|---|---|---|
| T1 | Integración real de SSH/LDAP/WinRM/scheduler incompleta | Media | Abierto |
| T2 | `conftest.py` usa `setdefault` para variables sensibles | Baja | Abierto |
| T3 | Tests organizados por sprint/fecha | Baja | Abierto |

- **T1:** SSH real es opcional y no corre en CI; LDAP usa `FakeConnection`;
  WinRM está mockeado; el scheduler y las carreras multiproceso no se prueban
  con carga real. El claim atómico se prueba de forma secuencial.
- **T2:** `conftest.py:19` y siguientes usan `os.environ.setdefault`; un
  entorno cargado puede imponer rutas reales. Es una precaución válida aunque
  el flujo normal de CI esté limpio.
- **T3:** 9 de 24 archivos de prueba llevan fecha o nombre de sprint. Dificulta
  mantener regresiones por dominio.

### 4.4. Despliegue, CI y dependencias

| ID | Hallazgo | Severidad | Estado |
|---|---|---|---|
| D1 | La imagen Docker no usa el lockfile | Media | Abierto |
| D2 | CI sin linters, auditoría de dependencias ni build de imagen | Media | Abierto |
| D3 | Dependencias envejecidas o sin cota superior | Baja-media | Abierto |
| D4 | Caches de mypy/ruff fuera de `.gitignore` | Baja | Abierto |
| D5 | `dist/` es un artefacto ignorado que puede quedar obsoleto | Baja | Aceptado |
| D6 | Deriva menor en la guía portable | Baja | Abierto |

- **D1:** `Dockerfile:31-34` instala `requirements.txt` (rangos), no
  `requirements.lock`. El lock solo fija dependencias directas y no tiene
  hashes.
- **D2:** cobertura mínima del 55 %, gate propio estrecho, sin `ruff`, `mypy`,
  `pip-audit`, servicio Docker ni integración SSH. Existe un job `lock`, pero
  no construye la imagen.
- **D3:** `ldap3==2.9.1` sigue congelado; `pywinrm` y `cryptography` no tienen
  cota superior en `requirements.txt`.
- **D4:** `.mypy_cache/` y `.ruff_cache/` existen en el árbol y no figuran en
  `.gitignore`; se auto-ignoran de forma frágil.
- **D5:** `dist/hora-admin-despliegue/` se regeneró el 2026-11-08 y apunta al
  commit `0a96da7b115e`, pero sigue siendo un artefacto local ignorado. Debe
  regenerarse tras cada release.
- **D6:** el `01-INICIO-RAPIDO.txt` generado por
  `docker/export-images.sh` todavía describe la tarjeta APT como “en
  preparación” y no menciona la tarjeta de contraseña. Es una incoherencia
  documental del paquete, no del producto.

### 4.5. Lo que no es un problema

Se verificó que **no** hay:

- inyección de comandos ni uso de `shell=True`;
- credenciales en `argv` o en logs;
- secretos en el historial de Git;
- LDAP en claro por defecto (LDAPS obligatorio, con escape correcto);
- fallos de CSRF, sesión o autorización por roles en las rutas principales;
- problemas en la subida del tapiz;
- ausencia de doble aprobación o de verificación posterior;
- contenedor ejecutándose como root.

La lectura de lotes e historial por parte de un `viewer` es intencionada
(rol de solo lectura); no se considera hallazgo.

---

## 5. Correcciones al análisis original

| Punto del análisis | Matiz verificado |
|---|---|
| “Viewer ve consolas ajenas (`app.py:4521`)” | El SSE está protegido; el hueco real es `api_task_status` (`app.py:4537`) |
| “`execute_with_progress` 27–28 parámetros” | Son **26** argumentos por AST; `_run_apply_batch` sí tiene 28 |
| “8 de 24 tests con fecha” | Ahora son **9 de 24** tras añadir `test_contrasena_2026_11_07.py` |
| “344 tests en verde” | El estado actual es **359 passed, 1 skipped** (360 recopilados) |
| “`dist/` obsoleto” | Se regeneró y apunta a `0a96da7b115e`; sigue siendo un artefacto a regenerar |
| “Build no reproducible” | Correcto para la imagen; el CI sí tiene un job `lock` que instala el lockfile |
| “Seguridad 8,5/10” | Se ajusta a **8/10** por los defaults de compose y el endpoint de estado |

---

## 6. Plan de corrección propuesto

### Bloque 0 — Seguridad inmediata (0,5–1 día)

1. Autorizar `api_task_status` por propietario/admin.
2. Alinear compose con el default seguro de SSH y pasar WinRM a 5986/TLS.
3. SMTP fail-closed si hay credenciales sin TLS.
4. Cablear `_FILE` para secretos en Compose.
5. Ajustar permisos de `certs/` e `instance/` a 0750.
6. Documentar el modelo de confianza y los perfiles de laboratorio.

**Criterio de cierre:** un despliegue nuevo no acepta TOFU ni WinRM HTTP sin
una activación explícita; ningún usuario puede leer la consola de otro; las
pruebas de autorización y TLS pasan.

### Bloque 1 — Cadena de suministro y CI (1–2 días)

1. `requirements.in` + `requirements.lock` con hashes.
2. `Dockerfile` con `--require-hashes`.
3. `ruff`, `mypy`, `pip-audit` y build de imagen en CI.
4. Cobertura progresiva 55 → 65 → 75 %.
5. Job de integración SSH con contenedor.
6. Cotas de versión y actualización controlada de `ldap3`.

**Criterio de cierre:** dos builds de la imagen producen las mismas versiones;
el CI falla ante vulnerabilidades conocidas o errores de tipos nuevos.

### Bloque 2 — Pruebas críticas (1–2 días)

1. Autorización de `/api/task-status`, SSE y cancelación.
2. Redacción de secretos en PASSWORD/APT/TZ.
3. SMTP con y sin TLS.
4. Carrera real de dos procesos reclamando la misma programación.
5. Dos workers Gunicorn con SSE y cancelación.
6. Sustituir `setdefault` en `conftest.py`.
7. Marcadores de pytest y migración gradual por dominio.

### Bloque 3 — Refactor estructural (8–12 días)

1. Extraer `services/operation_form.py`.
2. Extraer `services/batch_engine.py` con un único bucle.
3. Extraer `services/task_store.py` con backend SQLite.
4. Dividir `app.py` en blueprints.
5. Mover JS inline a `static/js/` y centralizar constantes.
6. Unificar handlers de error.

**Criterio de cierre:** `app.py` por debajo de ~1.500 líneas; ninguna función de
más de ~100 líneas; preview/apply comparten motor; dos workers funcionan con
cancelación y SSE; comportamiento idéntico.

### Bloque 4 — Escalado y operación (3–5 días)

1. Rate limiter compartido (SQLite atómico; Redis solo si hace falta).
2. Estado y eventos compartidos entre workers.
3. Scheduler como rol separado por defecto.
4. Cola persistente si se quiere reintento y recuperación real.
5. Métricas de cola, saturación y duración por host.
6. Backups verificados de SQLite, vault y certificados.

---

## 7. Decisiones pendientes

1. **TOFU SSH:** ¿desactivar por defecto y exigir `known_hosts`, o mantenerlo
   solo en un perfil de laboratorio?
2. **WinRM:** ¿5986/TLS como único default, o compatibilidad explícita con
   5985/NTLM?
3. **Multi-worker:** ¿objetivo real o mantener un worker documentado?
4. **Refactor:** ¿congelar funcionalidad nueva durante el bloque 3 o hacer
   extracciones pequeñas intercaladas?

---

## 8. Lo que no conviene hacer

- Reescribir la aplicación desde cero: la lógica de seguridad y negocio es
  buena; el problema es la estructura.
- Subir `ACC_WORKERS` antes de extraer el estado de tareas.
- Actualizar todas las dependencias a la vez.
- Relajar defaults de seguridad para que funcione el laboratorio.
- Refactorizar el motor de lotes sin pruebas de caracterización previas.

---

## 9. Conclusión

El análisis previo es fiable y sus conclusiones son correctas en lo esencial.
La prioridad no es añadir funcionalidad, sino **cerrar el bloque 0**, endurecer
la cadena de suministro y después pagar la deuda estructural con extracciones
pequeñas y verificables. El proyecto puede seguir creciendo sin una reescritura
si se respeta ese orden.

Este documento es la referencia de la auditoría del 2026-11-08. Las
correcciones que se apliquen deben actualizar su estado, el `CHANGELOG.md` y
las pruebas correspondientes.
