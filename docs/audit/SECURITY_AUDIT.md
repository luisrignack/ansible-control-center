# Auditoría de seguridad (FASE 1)

Fecha: 2026-11-16 / 2026-11-17
Alcance: `83228cf` (0.9.9) + mejoras autorizadas de esta auditoría.
Referencias: OWASP ASVS 4.0.3, OWASP Top 10 (2021), CWE, NIST SSDF, buenas
prácticas de Ansible.

Método: revisión manual del código y de los playbooks, ejecución de la suite,
`ruff`, `mypy`, `bandit -ll` y pruebas de reproducción con `pytest` y el
cliente de Flask. No se ejecutaron operaciones contra infraestructura real.

Clasificación: **CRITICAL / HIGH / MEDIUM / LOW / INFORMATIONAL**.
Se distingue entre **vulnerabilidad confirmada**, **riesgo potencial** y
**oportunidad de mejora**. No se declara ninguna vulnerabilidad por sospecha.

## Resumen

| Severidad | Confirmadas | Potenciales | Mejoras |
|---|---:|---:|---:|
| CRITICAL | 0 | 0 | 0 |
| HIGH | 0 | 0 | 0 |
| MEDIUM | 3 | 1 | 0 |
| LOW | 2 | 1 | 3 |
| INFORMATIONAL | — | — | 5 |

No se encontraron vulnerabilidades críticas ni altas. Los controles de
ejecución de Ansible, validación de red y secretos son sólidos. Las brechas
principales eran de **integridad de auditoría**, **acceso autorizado a
informes** y **representación de ejecuciones interrumpidas**.

## Hallazgos

### ACC-SEC-001 — Enlaces del historial global deniegan el detalle a un admin autorizado

- **Severidad:** MEDIUM
- **Tipo:** Vulnerabilidad funcional de autorización (denegación de acceso
  autorizado), CWE-863.
- **Archivo:** `templates/ejecuciones.html:123` y
  `acc/web/tasks.py:612` (`_visible_batch`).
- **Descripción:** un administrador que abre `?all=1` ve todos los lotes, pero
  los enlaces a `/batches/<id>` y `/batches/<id>/migracion.pdf` no conservaban
  `all=1`. `_visible_batch` aplica `_owner_scope()`, que sin `all=1` devuelve
  el id del propio admin, por lo que el detalle de un lote ajeno respondía 404.
- **Evidencia:** prueba manual con dos usuarios; el listado mostraba el lote y
  el clic devolvía 404. El comportamiento está fijado en
  `tests/test_historial_privado_2026_11_16.py` (un operador sí debe recibir
  404). No se relajó el aislamiento: solo se propagó el alcance ya concedido.
- **Escenario:** admin revisa el historial global, abre un lote de otro
  operador para investigar un incidente y no puede ver la evidencia ni el PDF.
- **Impacto:** pérdida de capacidad de auditoría/forense; no expone datos a
  quien no está autorizado.
- **Recomendación:** conservar `all=1` en los enlaces de la vista global (o
  permitir explícitamente al admin cualquier lote). Se eligió lo primero para
  mantener el principio de mínimo privilegio y el contrato de privacidad.
- **Reproducción:** `pytest -q
  tests/test_hardening_2026_11_16.py::test_admin_abre_el_detalle_del_lote_desde_la_vista_global`
- **Estado:** **CORREGIDO**.

### ACC-SEC-002 — Auditoría sin integridad ni detección de manipulación

- **Severidad:** MEDIUM
- **Tipo:** Repudio / integridad de registros, CWE-117/778 (OWASP A09).
- **Archivo:** `models/user.py` (`AuthEvent.record`), `models/db.py`
  (`auth_events`).
- **Descripción:** los eventos de autenticación y administración se insertaban
  sin encadenar. `AuthEvent.record` capturaba `sqlite3.Error` y **no hacía
  nada**, de modo que un fallo de auditoría era invisible. No existía
  verificación, exportación ni anclaje externo; borrar o editar filas era
  indetectable.
- **Evidencia:** el código original hacía `except sqlite3.Error: pass`. No
  había columnas de hash ni pruebas de manipulación.
- **Escenario:** un atacante con acceso de escritura a SQLite borra sus
  intentos fallidos o el evento de cambio de rol. El operador no puede
  demostrar la manipulación.
- **Impacto:** pérdida de valor probatorio de la auditoría; ocultación de
  fuerza bruta o cambios administrativos.
- **Recomendación:** cadena SHA-256 determinista por evento, transacción
  `BEGIN IMMEDIATE`, verificación, checkpoints HMAC y exportación. Registrar
  los fallos de escritura en el log.
- **Reproducción:**
  `pytest -q tests/test_hardening_2026_11_16.py -k audit`
- **Estado:** **CORREGIDO / MITIGADO**. La cadena detecta edición, borrado
  intermedio y truncamiento posterior a un checkpoint. No protege frente a un
  administrador que controle también la clave y reescriba toda la cadena; se
  documenta en `AUDIT_INTEGRITY.md`.

### ACC-SEC-003 — Una ejecución interrumpida se presenta como fallo genérico

- **Severidad:** MEDIUM
- **Tipo:** Riesgo operacional con implicación de integridad, CWE-755.
- **Archivo:** `acc/task_state.py:397` (`_recover_task_state`),
  `models/batch.py:811` (`reconcile_orphans`).
- **Descripción:** al arrancar, las tareas y lotes que quedaron en curso se
  marcaban `status="error"` sin distinguir un fallo funcional de una
  interrupción con resultado remoto desconocido. La UI no advertía del riesgo
  de reintentar una operación no idempotente (contraseña, firewall, APT).
- **Evidencia:** el mensaje original era «El proceso se reinició durante la
  ejecución.» y el lote quedaba como `error` sin más.
- **Escenario:** se reinicia el contenedor mientras se cambia la contraseña de
  un host. El operador ve «error», asume que no se aplicó y relanza. El host
  puede haber quedado con la contraseña nueva y el vault con la anterior.
- **Impacto:** inconsistencia de credenciales/estado y falsos negativos.
- **Recomendación:** mantener `status="error"` por compatibilidad, añadir
  `interrupted=True`, `overall_result="interrupted"`, mensaje explícito de
  resultado desconocido y no reintentar automáticamente.
- **Reproducción:**
  `pytest -q tests/test_hardening_2026_11_16.py -k interrump`
- **Estado:** **CORREGIDO** (representación y aviso). El reintento sigue siendo
  manual y deliberado, como exige el mandato.

### ACC-SEC-004 — Límite de intentos de login por proceso

- **Severidad:** LOW
- **Tipo:** Riesgo potencial de fuerza bruta distribuida, CWE-307.
- **Archivo:** `services/rate_limiter.py`, `acc/web/auth.py`.
- **Descripción:** el limitador es una ventana deslizante en memoria por
  proceso. Con N workers, el umbral efectivo se multiplica por N; además el
  bloqueo por cuenta y el rate limit por IP son independientes.
- **Evidencia:** comentario explícito en `rate_limiter.py`; el despliegue por
  defecto usa `ACC_WORKERS=1`, donde el control es correcto.
- **Escenario:** un atacante rota IPs o aprovecha un despliegue multi-worker
  para multiplicar intentos contra una cuenta.
- **Impacto:** mayor ventana de fuerza bruta; el bloqueo de cuenta sigue
  limitando el daño.
- **Recomendación:** mantener el worker único (documentado), añadir límite en
  Nginx (`limit_req`) y/o backend compartido si se escala. No se introduce
  Redis en esta auditoría.
- **Estado:** **DOCUMENTADO** (riesgo residual aceptado).

### ACC-SEC-005 — Purga de retención trunca el prefijo de la cadena

- **Severidad:** LOW
- **Tipo:** Limitación de diseño de la evidencia, CWE-778.
- **Archivo:** `models/user.py::AuthEvent.purge_old`.
- **Descripción:** la retención borra eventos antiguos. La cadena sigue siendo
  verificable desde el primer evento conservado, pero un borrado de prefijo no
  es distinguible de la retención si no existe un checkpoint previo.
- **Recomendación:** crear un checkpoint firmado antes de cada purga y
  exportarlo; la verificación compara la cabeza y detecta truncamientos
  posteriores al checkpoint.
- **Estado:** **DOCUMENTADO**; el CLI `audit-checkpoint` permite anclarlo.

### ACC-SEC-006 — Sin MFA para cuentas administrativas

- **Severidad:** LOW (potencial; podría elevarse según el entorno)
- **Tipo:** Oportunidad de mejora, ASVS V2.8.
- **Archivo:** `acc/web/auth.py`, `models/user.py`.
- **Descripción:** solo contraseña local o LDAPS. El rol admin puede ejecutar
  tareas destructivas y cambiar roles. No hay segundo factor.
- **Mitigaciones existentes:** bloqueo, rate limit, sesión `strong`, cookies
  `HttpOnly/Secure/SameSite=Lax`, aprobación por un segundo admin para tareas
  destructivas.
- **Recomendación:** TOTP/WebAuthn para `admin` en una fase posterior, sin
  romper el flujo LDAPS. No se implementa aquí por alcance y para no introducir
  dependencias.
- **Estado:** **PENDIENTE**.

### ACC-SEC-007 — Inyección de fórmulas en exportaciones CSV

- **Severidad:** LOW
- **Tipo:** Vulnerabilidad de salida, CWE-1236.
- **Archivo:** `acc/web/inventory.py::_csv_cell` (nuevo export).
- **Descripción:** un valor del inventario que empiece por `=`, `+`, `-` o `@`
  puede ejecutarse como fórmula al abrir el CSV en Excel/LibreOffice.
- **Evidencia:** prueba unitaria del neutralizado; no existía export CSV antes
  de esta auditoría.
- **Recomendación:** anteponer `'` a esos valores (implementado).
- **Estado:** **CORREGIDO** antes de publicar la funcionalidad.

### ACC-SEC-008 — Migraciones con commit implícito

- **Severidad:** LOW
- **Tipo:** Riesgo potencial de consistencia, CWE-460.
- **Archivo:** `models/db.py::init_schema` y `_migrate_*`.
- **Descripción:** `executescript` hace commit de la transacción pendiente
  antes de ejecutar; una caída a mitad de migración puede dejar un esquema
  parcial. Mitigación: todas las migraciones son idempotentes, usan
  `IF NOT EXISTS`/`_ensure_column` y `user_version` se actualiza al final.
- **Recomendación:** probar siempre con una copia y mantener el backup previo;
  una migración transaccional completa exigiría reescribir el runner y no es
  proporcionada al riesgo actual.
- **Estado:** **DOCUMENTADO** (ver `DATABASE_HARDENING.md`).

### ACC-SEC-009 — Sin recuperación de contraseña self-service

- **Severidad:** INFORMATIONAL / mejora
- **Descripción:** un usuario que olvida su contraseña depende de un admin.
  Es una decisión operativa razonable para una herramienta interna; no hay
  vector adicional. Recomendación: flujo de reseteo con token de un solo uso si
  el volumen lo justifica.
- **Estado:** **PENDIENTE**.

### ACC-SEC-010 — Superficie del endpoint de métricas

- **Severidad:** INFORMATIONAL
- **Descripción:** se añadió `/metrics` (Prometheus). Sin protección filtraría
  agregados internos. Se implementó con dos modos: `Bearer` token
  (`ACC_PROMETHEUS_TOKEN`/`_FILE`) o sesión de administrador. No expone
  usuarios, IPs, hosts ni credenciales; las etiquetas están acotadas a
  endpoints, estados de lote y tipos de evento.
- **Estado:** **CORREGIDO por diseño**; cubierto por pruebas.

### ACC-SEC-011 — Validación de entradas y XSS/CSRF

- **Severidad:** INFORMATIONAL (sin hallazgos)
- **Evidencia:** autoescape de Jinja activo; no se usa `|safe` con datos de
  usuario; los `innerHTML` de `static/js` solo escriben literales de plantilla.
  CSRF global en métodos mutables, token ligado a sesión y con caducidad.
  CSP con nonce, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`.
  Redirecciones validadas con `_is_safe_redirect`.
- **Conclusión:** no se encontraron XSS ni CSRF explotables en el código
  revisado.

### ACC-SEC-012 — Ejecución de Ansible y SSRF/DNS rebinding

- **Severidad:** INFORMATIONAL (sin hallazgos)
- **Evidencia:**
  - `subprocess.run/Popen` con listas de argumentos y `shell=False` en
    `services/ansible_runner.py` y `services/password_tester.py`.
  - Playbook y binarios provienen de constantes de configuración; el cliente
    no puede elegir una ruta arbitraria.
  - Variables sensibles en fichero temporal 0600 pasado con `--extra-vars @`;
    nunca en `argv` ni en logs.
  - `host_resolver.resolve_target/pin_target` valida IPv4/CIDR/rango, aplica
    allowlist y devuelve la IP literal; Ansible se invoca contra esa IP, por lo
    que un cambio DNS entre validación y conexión no altera el destino.
  - Los playbooks usan `argv` para comandos con datos; el único `shell` con
    script fijo (`apt_facts.yml`) recibe valores validados por variables de
    entorno, no por interpolación en el script.
- **Conclusión:** no se encontró inyección de comandos ni ejecución arbitraria
  de playbooks. Los nuevos endpoints no amplían esta superficie.

## Cobertura por dominio ASVS (resumen)

| Dominio | Estado | Notas |
|---|---|---|
| V1 Arquitectura | OK | Capas claras, mínimo privilegio, sin secretos en repo |
| V2 Autenticación | Parcial | Contraseñas scrypt, bloqueo, LDAPS con validación; falta MFA |
| V3 Sesión | OK | Cookies seguras, `session_protection=strong`, expiración, logout POST |
| V4 Control de acceso | OK tras SEC-001 | RBAC backend en todas las rutas; scoping por dueño |
| V5 Validación | OK | Allowlists, IPv4, límites de tamaño, CSV validado |
| V6 Criptografía | OK | Fernet/Vault, HMAC-SHA256 con pepper, TLS; gestión de claves documentada |
| V7 Errores/logs | OK | Mensajes genéricos al cliente, detalle al log, auditoría encadenada |
| V8 Protección de datos | OK | Secretos en ficheros 0600, DB 0600, sin secretos en logs |
| V9 Comunicaciones | OK | Nginx TLS 1.2/1.3, HSTS, SSH host key checking |
| V10 Código malicioso | OK | Sin uploads ejecutables; imágenes validadas por firma |
| V11 Lógica de negocio | OK | Aprobaciones, ventanas, límites, idempotencia de locks |
| V12 Archivos | OK | Temporales 0600, sin path traversal, CSV acotado |
| V13 API | OK | JSON con autenticación, sin secretos, sin cardinalidad ilimitada |
| V14 Configuración | OK | Producción falla si falta clave/allowlist/SSH checking |

## Conclusión

El diseño previo ya aplicaba defensas en profundidad en la parte más sensible
(ejecución remota). Las correcciones de esta fase cierran las brechas de
integridad y de acceso autorizado sin relajar ningún control. Los riesgos
residuales (MFA, rate limit multi-proceso, truncamiento por retención,
migraciones no transaccionales) están documentados y tienen mitigaciones
proporcionadas.
