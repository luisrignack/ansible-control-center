# Análisis de calidad frente a ISO/IEC 25010:2023

**Producto evaluado:** Ansible Control Center (`fecha`)
**Commit analizado:** `2f0252e`
**Fecha del análisis:** 2026-11-06
**Norma de referencia:** ISO/IEC 25010:2023 — *Systems and software
engineering — Systems and software Quality Requirements and Evaluation
(SQuaRE) — Product quality model* (edición 2, 2023-11)
**Naturaleza:** análisis de brechas basado en evidencia documental y de
repositorio. **No es una evaluación acreditada ni una certificación.**

---

## 1. Objetivo y método

Este documento contrasta el producto con las **9 características** y las
**40 subcaracterísticas** del modelo de calidad de producto de ISO/IEC
25010:2023. Para cada subcaracterística se indica:

- **Nivel de madurez** en una escala 0–4.
- **Evidencia** tomada del código, pruebas, CI, despliegue o documentación.
- **Brecha o acción** recomendada.

La evaluación es de escritorio (*desk review*). No se han ejecutado pruebas de
carga, análisis de accesibilidad con usuarios, auditoría externa de seguridad
ni un proceso formal de evaluación ISO/IEC 25040. Por tanto, los niveles son
una estimación de madurez, no un resultado certificable.

### Escala de madurez

| Nivel | Nombre | Criterio |
|---:|---|---|
| 0 | Ausente | No existe mecanismo ni evidencia |
| 1 | Inicial | Existe de forma puntual, no sistemática |
| 2 | Repetible | Hay práctica establecida, pero sin medición continua |
| 3 | Definido | Práctica documentada, automatizada y verificable |
| 4 | Optimizado | Se mide, se revisa y se mejora con evidencia periódica |

### Base de evidencia

| Evidencia | Dato |
|---|---|
| Suite de pruebas | **287 passed, 1 skipped** (`pytest -q`) |
| Módulos de prueba | 18 archivos de prueba (+ `__init__.py`), 288 funciones |
| Código de producción | `app.py` 4.555 líneas; `models/` 3.018; `services/` 4.472 |
| Código de pruebas | 7.006 líneas |
| Rutas HTTP | 27 |
| Tablas SQLite | 14; esquema **v12** |
| Documentación | 10 documentos en `docs/` + README, DEPLOYMENT y CHANGELOG |
| CI | Matriz Python 3.11–3.13, `compileall`, gate de calidad, YAML de Ansible y `pytest --cov --cov-fail-under=55` |
| Lockfile | `requirements.lock` con pines de dependencias directas |
| Contenedores | App como usuario `acc`, `cap_drop: ALL`, `no-new-privileges`, red interna y healthchecks |
| Limitación local | `pytest-cov` no está instalado en `.venv`; no se pudo medir cobertura localmente |

---

## 2. Resumen ejecutivo

| Característica | Nivel | Valoración breve | Brecha principal |
|---|---:|---|---|
| **Adecuación funcional** | **3** | Flujo horario/NTP completo y bien probado; catálogo con tareas pendientes | Solo 1 de 4 tareas del catálogo está activa |
| **Eficiencia de desempeño** | **2** | Cola acotada, SSE sin sondeo y retención; sin SLOs ni pruebas de carga | Estado en memoria y un solo worker web |
| **Compatibilidad** | **3** | Integra Ansible, WinRM, LDAPS, SMTP y PDF; Docker aislado | Sin API/CLI de ejecución ni exportación interoperable |
| **Capacidad de interacción** | **3** | UI clara, validación, vista previa y accesibilidad básica | Sin auditoría WCAG 2.2 AA ni ayuda contextual |
| **Fiabilidad** | **3** | Recuperación, reconciliación, cola, cancelación y healthcheck | Sin métricas de disponibilidad ni prueba de restauración automatizada |
| **Seguridad** | **4** | RBAC, CSRF, CSP, rate limit, allowlist, vault, auditoría y aprobaciones | Sin MFA, sin escaneo de dependencias y nginx corre como root |
| **Mantenibilidad** | **3** | Modelos/servicios separados, 287 pruebas y documentación extensa | `app.py` monolítico, sin linters/tipos y umbral de cobertura bajo |
| **Flexibilidad** | **2** | Docker, systemd, install.sh y configuración por entorno | Escalabilidad horizontal limitada y soporte de SO restringido |
| **Seguridad funcional (safety)** | **3** | Dry-run, aprobaciones, allowlist, límites y restauración de NTP | Sin análisis formal de riesgos ni clasificación de peligrosidad |

**Madurez global no ponderada: 2,9 / 4** (nivel *definido* con brechas
claras). La mayor fortaleza es **seguridad**; las mayores brechas son
**escalabilidad**, **completitud funcional del catálogo** y **medición
continua de calidad**.

> La puntuación no pondera criticidad. En un sistema que cambia la hora de
> equipos, seguridad y fiabilidad deberían pesar más que interacción o
> flexibilidad en una evaluación final.

---

## 3. Análisis por característica

### 3.1. Adecuación funcional — nivel 3

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Completitud funcional | 2 | Flujo de zona horaria/NTP, vista previa, ejecución, lotes, historial, programaciones, aprobaciones, auditoría y PDF | Solo `zona-todas` está activa; `kaspersky`, `contrasena` y `tapiz` figuran como `planned` |
| Corrección funcional | 4 | Validador único manual/programado, resolución DST, verificación posterior de hora/NTP, tolerancia configurable y 287 pruebas | Sin pruebas de carga ni de campo con hosts reales heterogéneos |
| Pertinencia funcional | 4 | Catálogo de tareas, vista previa con `would_change`, dry-run, ejecución best-effort y resultados por host | La consola sigue dependiendo de marcadores de texto de Ansible |

**Lectura:** el producto cumple bien el objetivo operativo para el que está
activo, pero el catálogo promete más de lo que entrega. O se completan las
tareas planificadas o se separa explícitamente “disponible” de “anunciado”.

---

### 3.2. Eficiencia de desempeño — nivel 2

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Comportamiento temporal | 3 | SSE dirigido por evento, stream acotado a 90 s, cola con 2 workers y cancelación cooperativa | Sin medición de latencia p95 ni tiempo de espera en cola |
| Utilización de recursos | 3 | Consola acotada a 300 líneas, `task_state` a 500 filas, 200 tareas en memoria, persistencia agrupada y retención periódica | El estado completo vive en memoria por proceso |
| Capacidad | 2 | Límite de 256 hosts, cola de 50, presupuesto de lote de 3.600 s y timeouts | Un worker web, SQLite y estado local impiden escalar horizontalmente |

**Lectura:** hay contención razonable de recursos para un despliegue pequeño o
mediano. Falta convertir las hipótesis de capacidad en **SLOs medidos**:
latencia de UI, tiempo de lote por número de hosts, conexiones SSE simultáneas
y espera máxima en cola.

---

### 3.3. Compatibilidad — nivel 3

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Coexistencia | 3 | Docker con red interna, `cap_drop: ALL`, `no-new-privileges`, volúmenes separados y healthchecks | Nginx corre como `root`; no hay límites de CPU/memoria en compose |
| Interoperabilidad | 3 | Ansible, WinRM, LDAPS, SMTP, PDF y SQLite documentado | Sin API/CLI de ejecución, webhooks, importación CSV ni exportación estándar |

**Lectura:** la integración con el ecosistema Ansible y corporativo es buena.
La interoperabilidad hacia fuera está limitada a la interfaz web.

---

### 3.4. Capacidad de interacción — nivel 3

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Reconocibilidad de la adecuación | 3 | Catálogo con nombre, resumen, estado e iconos; distinción visible entre simulación y ejecución real | Las tareas `planned` pueden generar expectativa de disponibilidad |
| Aprendizaje | 3 | README, DEPLOYMENT, OPERACION, REFERENCIA y mensajes flash | Sin ayuda contextual dentro de cada formulario |
| Operabilidad | 3 | Progreso, consola, descarga de log, cancelación, filtros de historial y modo oscuro/claro | Sin atajos de teclado documentados ni acciones masivas |
| Protección frente a errores | 4 | CSRF, validación servidor, vista previa, dry-run, aprobación de tareas destructivas, UUID idempotente y confirmaciones | La cancelación es cooperativa: no interrumpe un host en curso |
| Motivación | 3 | Interfaz Bootstrap, estados visuales, progreso y badges | Sin medición de satisfacción ni pruebas con usuarios |
| Inclusividad | 2 | `lang="es"`, skip link, `aria-label`, `aria-live`, `aria-current`, foco y contraste Bootstrap | Sin auditoría WCAG 2.2 AA, sin i18n y sin pruebas con lector de pantalla |
| Asistencia al usuario | 3 | Documentación extensa, mensajes de error concretos y páginas de error | Sin ayuda contextual ni enlaces directos desde el error a la solución |
| Autodescripción | 3 | Etiquetas, resúmenes, badges de estado y metadatos de ejecución | La consola técnica puede ser difícil para un operador no experto |

**Lectura:** la interacción está por encima de un prototipo, pero la
accesibilidad no está **medida**. Un análisis WCAG 2.2 AA con axe/Lighthouse
y pruebas manuales sería la acción de mayor retorno.

---

### 3.5. Fiabilidad — nivel 3

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Ausencia de fallos | 3 | 287 pruebas, gate de calidad, `compileall`, CI en 3 versiones y validación de YAML | Sin telemetría de errores en producción ni tasa de fallos por release |
| Disponibilidad | 3 | Healthcheck de app y nginx, `restart: unless-stopped`, SQLite WAL y `busy_timeout` | Un solo worker y una sola instancia; sin HA ni failover |
| Tolerancia a fallos | 3 | Best-effort por host, reintento de contraseñas, cola acotada, cancelación, claim del scheduler y reconciliación de huérfanos | No hay reintento selectivo de hosts fallidos ni circuit breaker |
| Recuperabilidad | 3 | Snapshot de tareas en SQLite, recuperación de tareas estancadas, retención y procedimiento de backup/restore documentado | Sin simulacro de restauración automatizado ni RTO/RPO medidos |

**Lectura:** el diseño es resiliente para un proceso único. La fiabilidad
**operativa** depende de backups y restauración que todavía son manuales.

---

### 3.6. Seguridad — nivel 4

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Confidencialidad | 4 | RBAC, vault `ansible-vault`, Fernet para credenciales programadas, ficheros temporales `0600`, cookies seguras y TLS | Sin MFA; secretos de compose vía entorno |
| Integridad | 4 | CSRF, CSP con nonce, cabeceras de seguridad, permisos de SQLite, allowlist, host key checking y aprobaciones ligadas a hosts | Sin firma de artefactos ni verificación de integridad de la imagen |
| No repudio | 4 | `auth_events`, aprobaciones con solicitante/aprobador, propietario de tarea y auditoría de cancelación | Los eventos de ejecución no tienen una cadena de auditoría inmutable |
| Responsabilidad | 4 | Roles `viewer`/`operator`/`admin`, ownership de cancelación y trazabilidad de cambios de rol | Sin revisión periódica de accesos desde la aplicación |
| Autenticidad | 4 | Hash de contraseña, LDAPS con verificación TLS, sesión rotada y bloqueo por intentos | Sin MFA ni integración SSO/OIDC |
| Resistencia | 3 | Rate limit, bloqueo de cuenta, CSRF, CSP, allowlist y validación de entrada | Sin `pip-audit`, escaneo de imagen, WAF ni prueba de penetración |

**Lectura:** es la característica más madura. Las brechas restantes son de
**defensa en profundidad** y de **verificación continua**, no de controles
básicos ausentes.

---

### 3.7. Mantenibilidad — nivel 3

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Modularidad | 2 | `models/` y `services/` separados; `app.py` concentra rutas, validación, tareas, SSE y CLI | 4.555 líneas en un solo módulo; modularizar por blueprints |
| Reutilización | 3 | Servicios de Ansible, vault, scheduler, cola, catálogo y validador común | Algunas utilidades siguen acopladas a `app.py` |
| Analizabilidad | 2 | Documentación muy completa y gate de calidad | Sin `ruff`/`black`/`mypy`/`bandit`, sin tipos verificados y sin trazas estructuradas |
| Modificabilidad | 3 | Migraciones idempotentes, pruebas de regresión y configuración por entorno | El monolito aumenta el riesgo de cambios laterales |
| Testabilidad | 3 | 288 funciones de prueba, fixtures y pruebas de servicios | Umbral de cobertura del 55%; sin cobertura de ramas ni mutación |

**Lectura:** buena base de pruebas y documentación, pero la **calidad
estática** no está automatizada más allá del gate propio. El umbral de
cobertura del 55% es bajo para un sistema que modifica equipos.

---

### 3.8. Flexibilidad — nivel 2

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Adaptabilidad | 2 | Configuración por variables de entorno y despliegue Docker/systemd | Solo Ubuntu/Debian con systemd y Windows por WinRM; sin RHEL/macOS |
| Escalabilidad | 1 | Cola y workers configurables; SQLite WAL | Estado SSE/tareas en memoria, un worker web y sin almacén compartido |
| Instalabilidad | 4 | `install.sh`, Docker Compose, imágenes exportables, healthchecks, lockfile y documentación de despliegue | Faltan pruebas automatizadas del compose y del build de imagen |
| Reemplazabilidad | 2 | Esquema SQLite documentado y datos exportables por SQL | Sin API/exportación estándar ni guía de migración desde otra herramienta |

**Lectura:** la instalación es excelente; la **escalabilidad horizontal** es la
brecha estructural más importante del producto.

---

### 3.9. Seguridad funcional (*safety*) — nivel 3

| Subcaracterística | Nivel | Evidencia | Brecha / acción |
|---|---:|---|---|
| Restricción operativa | 3 | Allowlist, máximo de hosts, dry-run por defecto, rechazo de `TIME` con NTP `keep`, límites de contraseñas y timeouts | No hay cuotas por operador ni por franja horaria |
| Identificación de riesgos | 2 | Vista previa, aprobación de tareas destructivas y auditoría | Sin análisis formal de peligros (FMEA/HAZOP) ni registro de riesgos |
| Modo seguro ante fallo | 3 | Cancelación, best-effort por host, restauración de NTP y cierre de tareas huérfanas | Un cambio de hora puede afectar a un host crítico antes de detectarse |
| Advertencia de peligro | 3 | Banner de simulación, confirmación de tareas destructivas, mensajes de error y vista previa | Sin clasificación de criticidad por host/grupo |
| Integración segura | 3 | Host key checking, LDAPS con TLS, ausencia de `shell=True` y contenedor sin privilegios | Sin análisis de impacto por integración con sistemas críticos |

**Lectura:** el producto aplica controles de seguridad razonables para una
herramienta de administración, pero no sustituye un **análisis de seguridad
funcional** del proceso operativo: qué hosts son críticos, qué ventana es
segura y cómo se revierte un cambio de hora.

---

## 4. Calidad en uso (complemento)

ISO/IEC 25010 también se apoya en la calidad en uso. Sin un estudio con
usuarios, la valoración es cualitativa:

| Dimensión | Valoración | Comentario |
|---|---|---|
| Eficacia | Alta | El operador puede previsualizar, ejecutar, cancelar y auditar |
| Eficiencia | Media | Buena para lotes pequeños; sin métricas de tiempo por tarea |
| Satisfacción | Media-alta | UI clara y documentación amplia; sin encuestas |
| Ausencia de riesgo | Media-alta | Muchos controles, pero sin análisis formal de criticidad |
| Cobertura de contexto | Media | Un solo idioma y sistemas operativos limitados |

---

## 5. Matriz de métricas propuestas

| Métrica | Objetivo | Estado actual | Cómo medir | Frecuencia |
|---|---|---|---|---|
| Cobertura de líneas | ≥ 75 % | CI exige 55 %; local no medido | `pytest --cov` en CI con artefacto | Cada PR |
| Cobertura de ramas | ≥ 65 % | No medida | `--cov-branch` | Cada PR |
| Mutación | ≥ 60 % en módulos críticos | No medida | `mutmut`/`cosmic-ray` | Mensual |
| Tareas activas del catálogo | 100 % de lo anunciado o alcance declarado | 1 de 4 | Inspección de `TASK_CATALOG` | Cada release |
| Latencia p95 de UI | < 500 ms | No medida | Prueba de carga HTTP | Cada release |
| Tiempo de lote de 256 hosts | < 3.600 s | No medido | Prueba de carga con inventario simulado | Trimestral |
| Conexiones SSE simultáneas | ≥ 50 sin degradación | No medida | Prueba de carga SSE | Trimestral |
| Disponibilidad mensual | ≥ 99,5 % | No medida | Healthchecks + logs | Mensual |
| MTTR | < 30 min | No medido | Incidentes y postmortems | Mensual |
| Restauración de backup | 100 % exitosa | Solo procedimiento manual | Simulacro cronometrado | Trimestral |
| Vulnerabilidades altas/críticas | 0 | No escaneadas | `pip-audit`, Trivy/Grype | Cada PR |
| Hallazgos WCAG 2.2 AA críticos | 0 | No auditados | axe + revisión manual | Cada release |
| Acciones privilegiadas auditadas | 100 % | Parcial: auth, roles, cancelación, aprobaciones | Revisión de eventos | Cada release |
| Imagen sin root | 100 % de contenedores | App sí; nginx no | Inspección de `USER` | Cada release |

---

## 6. Plan de mejora priorizado

### P0 — Riesgo alto o bloqueo de calidad

1. **Q-01 Escalabilidad explícita.** O se documenta y prueba el límite de un
   worker, o se mueve el estado de tareas a un almacén compartido
   (SQLite/Redis) para permitir varios workers. Añadir prueba de carga.
2. **Q-02 Cobertura real.** Subir el umbral a 70–75 %, publicar el informe de
   cobertura como artefacto y añadir cobertura de ramas.
3. **Q-03 Análisis estático.** Incorporar `ruff`, `black --check`, `mypy`,
   `bandit` y `pip-audit` al CI. El entorno local no tiene red para
   instalarlos; el CI sí puede.
4. **Q-04 Lockfile completo.** Generar lock con dependencias transitivas y
   hashes (`pip-compile --generate-hashes`), no solo dependencias directas.
5. **Q-05 Pruebas de carga y SLOs.** Medir latencia, throughput, cola y SSE
   con un inventario simulado de 256 hosts.
6. **Q-06 Restauración verificada.** Automatizar un simulacro de backup y
   documentar RTO/RPO.
7. **Q-07 Accesibilidad.** Auditoría WCAG 2.2 AA con axe/Lighthouse y
   corrección de hallazgos críticos.

### P1 — Brechas funcionales y operativas

8. **Q-08 Completitud del catálogo.** Implementar `kaspersky`, `contrasena` y
   `tapiz`, o retirarlas de la vista de tareas disponibles y declarar el
   alcance real.
9. **Q-09 Interoperabilidad.** API autenticada mínima, exportación CSV/JSON y
   webhooks o notificaciones estructuradas.
10. **Q-10 Matriz de compatibilidad.** Soportar RHEL/macOS o declarar
    formalmente la matriz soportada y probarla en CI.
11. **Q-11 MFA/SSO.** Añadir TOTP o OIDC para roles `admin`/`operator`.
12. **Q-12 Observabilidad.** Métricas Prometheus, logs estructurados, trazas
    y alertas sobre cola, fallos y salud.
13. **Q-13 Análisis de seguridad funcional.** FMEA/HAZOP del cambio de hora y
    NTP: hosts críticos, ventanas, reversión y criterios de aborto.
14. **Q-14 Modularización.** Extraer blueprints de `app.py` sin cambiar
    comportamiento, con pruebas de regresión.

### P2 — Mejora continua

15. **Q-15 Reemplazabilidad.** Exportación completa y guía de migración.
16. **Q-16 Asistencia e i18n.** Ayuda contextual y extracción de textos.
17. **Q-17 Endurecimiento de nginx.** Ejecución sin root, sistema de archivos
    de solo lectura y límites de recursos.
18. **Q-18 Cadena de suministro.** SBOM, escaneo de imagen, firma de imágenes
    y política de actualización de dependencias.

---

## 7. Conclusión

El producto tiene una base sólida y por encima de un prototipo: flujo completo,
pruebas reales, controles de seguridad maduros, auditoría, recuperación y
documentación operativa. Frente a ISO/IEC 25010:2023, destaca en **seguridad**
y **fiabilidad** para un despliegue de un solo proceso.

Las brechas que más limitan una evaluación formal son:

1. **Medición insuficiente**: cobertura del 55 %, ausencia de linters, pruebas
   de carga, accesibilidad y escaneo de dependencias.
2. **Escalabilidad estructural**: estado en memoria y un solo worker web.
3. **Completitud del catálogo**: solo una de cuatro tareas está activa.
4. **Flexibilidad de plataforma**: soporte limitado a Ubuntu/Debian y Windows.
5. **Seguridad funcional**: controles operativos buenos, pero sin análisis
   formal de riesgos ni clasificación de criticidad.

Cerrar las acciones P0 y P1 permitiría pasar de un nivel **definido (3)** a un
nivel **medido y optimizado (4)** en la mayoría de características, y sostener
una evaluación de calidad con evidencia objetiva.

---

## 8. Referencias internas

- `CHANGELOG.md` — cambios y pruebas de cada fase.
- `docs/ANALISIS_PROFUNDO_2026-11-06.md` — hallazgos técnicos y roadmap.
- `docs/ARQUITECTURA.md` — modelo de concurrencia y flujos.
- `docs/OPERACION.md` — runbook, backups, retención y errores.
- `docs/REFERENCIA.md` — rutas, modelos, esquema y variables.
- `DEPLOYMENT.md` — producción, systemd, nginx, Docker y troubleshooting.
- `.github/workflows/ci.yml` — gates actuales de CI.
- `tools/quality_check.py` — gate de calidad sin dependencias externas.
