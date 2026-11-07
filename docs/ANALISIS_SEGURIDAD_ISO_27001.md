# Mapeo ISO/IEC 27001:2022 — Ansible Control Center

**Tipo de documento:** autoevaluación y análisis de brechas (no es una auditoría
de certificación).
**Versión:** 1.0 — 2026-11-07
**Alcance:** aplicación Ansible Control Center, su código, configuración,
despliegue en contenedores y evidencias documentales del repositorio.
**Norma de referencia:** ISO/IEC 27001:2022, cláusulas 4–10 y Anexo A (93
controles). Guía de implementación: ISO/IEC 27002:2022.

> **Aviso:** este documento no declara conformidad ni certificación. Una
> certificación ISO/IEC 27001 certifica el **sistema de gestión de seguridad de
> la información (SGSI)** de una organización, con alcance, políticas,
> análisis de riesgos, declaración de aplicabilidad, evidencias, auditoría
> interna y revisión por la dirección. Los controles marcados como `EXT`
> dependen de la organización y no pueden cubrirse solo con el código.

---

## 1. Resumen ejecutivo

El proyecto tiene una base técnica de seguridad superior a la media de una
herramienta interna: autenticación con roles, LDAPS, bloqueo por intentos,
CSRF, CSP, HSTS, allowlist de destinos, verificación de claves SSH, secretos
con permisos `0600`, cifrado Fernet, auditoría de eventos, retención, backups
documentados y controles de criticidad/ventana con bloqueo auditado (S1).

Las brechas principales **no son de código**, sino de **gobierno del SGSI**:

1. No hay política de seguridad formal aprobada ni alcance/documentación del
   SGSI.
2. No hay análisis de riesgos formal, declaración de aplicabilidad (SoA) ni
   registro de riesgos mantenido.
3. No hay plan de respuesta a incidentes, continuidad ni pruebas de
   restauración verificadas.
4. La gestión de vulnerabilidades y el análisis estático no están integrados
   en CI (aunque las herramientas ya están instaladas localmente).
5. No hay MFA, revisión periódica de accesos ni inventario de activos de
   información.
6. Faltan evidencias organizativas de personas, proveedores, cumplimiento
   legal, auditoría interna y revisión por la dirección.

**Conclusión:** el producto está en una posición razonable para **iniciar** un
SGSI con alcance acotado al servicio, pero necesitaría un programa de trabajo
de 6–12 meses antes de una auditoría de certificación.

### Distribución de los 93 controles del Anexo A

| Estado | Controles | Lectura |
|---|---:|---|
| `IMP` implementado | 4 | Controles técnicos con evidencia directa en código. |
| `PAR` parcial | 38 | Hay base técnica o documental, falta completar evidencia o automatización. |
| `BRE` brecha | 15 | Sin control verificable; requiere acción del plan P0/P1. |
| `EXT` externo | 36 | Depende de la organización o infraestructura; no se cubre solo con el repositorio. |
| `N/A` no aplica | 0 | Sin exclusiones aprobadas todavía. |

---

## 2. Alcance y exclusiones

### 2.1. Dentro del alcance

- Código de la aplicación (`app.py`, `config.py`, `models/`, `services/`,
  `templates/`, `static/`).
- Despliegue de referencia: Docker Compose, nginx, gunicorn, SQLite, Ansible.
- Datos tratados: inventario de hosts, credenciales del vault, contraseñas
  maestras cifradas, usuarios/roles, eventos de auditoría, historial de lotes.
- Procesos de operación documentados en `docs/OPERACION.md`,
  `docs/DOCKER.md` y `DEPLOYMENT.md`.

### 2.2. Fuera del alcance del repositorio

- Instalaciones físicas, cableado, control de acceso físico y suministros.
- Red corporativa, segmentación, firewall perimetral y proxy.
- Directorio corporativo LDAPS y proveedor de identidad.
- Sistema operativo de los hosts gestionados y su parcheo.
- Proveedores cloud, hardware y cadena de suministro externa.
- Contratos, acuerdos de confidencialidad y procesos de RR. HH.

Estos elementos **no se excluyen del SGSI**: si el alcance de certificación
incluye el servicio, la organización debe aportar sus propias evidencias.

---

## 3. Metodología y escala

Se revisaron las cláusulas 4–10 y los 93 controles del Anexo A. Para cada
control se indica:

| Código | Significado |
|---|---|
| `IMP` | Implementado en el proyecto con evidencia verificable. |
| `PAR` | Parcial: existe un control técnico o documental, pero falta completar evidencia, alcance o automatización. |
| `BRE` | Brecha: no existe control o no es verificable. |
| `EXT` | Depende de la organización o de la infraestructura; fuera del código del proyecto. No implica exclusión del SGSI. |
| `N/A` | No aplica al alcance declarado; requiere justificación en la SoA. |

La valoración es cualitativa y se basa en evidencias del repositorio a fecha
2026-11-07. No sustituye a una auditoría independiente.

---

## 4. Conformidad con las cláusulas 4–10

| Cláusula | Requisito | Estado | Evidencia / brecha |
|---|---|---|---|
| 4.1 | Comprensión del contexto | BRE | No hay documento de contexto interno/externo ni partes interesadas. |
| 4.2 | Partes interesadas | BRE | No identificadas formalmente. |
| 4.3 | Alcance del SGSI | BRE | Este documento propone un alcance técnico, pero no está aprobado. |
| 4.4 | SGSI y sus procesos | BRE | No existe SGSI implantado. |
| 5.1 | Liderazgo y compromiso | BRE | Sin evidencia de dirección, recursos ni comunicación. |
| 5.2 | Política de seguridad | PAR | Hay prácticas en `AGENTS.md` y documentación, pero no una política aprobada y revisada. |
| 5.3 | Roles y responsabilidades | PAR | Roles de aplicación (`viewer`/`operator`/`admin`), sin RACI del SGSI. |
| 6.1 | Riesgos y oportunidades | BRE | No hay metodología ni registro de riesgos formal. |
| 6.2 | Objetivos de seguridad | BRE | No definidos con métricas y responsables. |
| 6.3 | Planificación de cambios | PAR | Git y CI, sin gestión formal de cambios. |
| 7.1 | Recursos | EXT | Depende de la organización. |
| 7.2 | Competencia | EXT | Sin plan de formación/competencias. |
| 7.3 | Concienciación | PAR | Documentación técnica abundante; falta formación registrada. |
| 7.4 | Comunicación | BRE | Sin plan de comunicación interna/externa. |
| 7.5 | Información documentada | PAR | Documentos versionados en git; falta control documental del SGSI. |
| 8.1 | Planificación y control operacional | PAR | Runbooks de operación; faltan procedimientos de seguridad. |
| 8.2 | Evaluación de riesgos | BRE | No realizada. |
| 8.3 | Tratamiento de riesgos | BRE | No hay plan de tratamiento ni SoA aprobada. |
| 9.1 | Seguimiento y medición | PAR | `/metricas`, `/healthz`, tests y SLO; no métricas de SGSI. |
| 9.2 | Auditoría interna | BRE | No existe programa de auditoría interna. |
| 9.3 | Revisión por la dirección | BRE | No existe. |
| 10.1 | Mejora continua | PAR | Changelog y análisis periódicos; falta proceso formal. |
| 10.2 | No conformidades y acciones correctivas | BRE | No hay registro ni flujo de CAPA. |

---

## 5. Anexo A — Mapeo de los 93 controles

### 5.1. Controles organizativos (A.5)

| Control | Estado | Evidencia en el proyecto | Brecha / acción |
|---|---|---|---|
| A.5.1 Políticas de seguridad | PAR | `AGENTS.md`, README y documentación operativa. | Aprobar política formal, versión, revisión anual y difusión. |
| A.5.2 Roles y responsabilidades | PAR | Roles `viewer`/`operator`/`admin` en `models/user.py` y `roles_required`. | Definir RACI del SGSI (responsable, propietario, custodio). |
| A.5.3 Segregación de funciones | PAR | Doble aprobación de tareas destructivas (`ApprovalRequest`), roles. | Extender 4-eyes a operaciones críticas y revisar que un admin no pueda aprobarse a sí mismo. |
| A.5.4 Responsabilidades de la dirección | EXT | — | Evidencia de asignación y revisión por dirección. |
| A.5.5 Contacto con autoridades | EXT | — | Registro de contactos legales/regulatorios. |
| A.5.6 Grupos de interés | EXT | — | Contactos con CERT, comunidad y proveedores. |
| A.5.7 Inteligencia de amenazas | BRE | `pip-audit` instalado localmente; CI sin escaneo. | Suscripción a avisos, revisión mensual y seguimiento de CVEs. |
| A.5.8 Seguridad en proyectos | PAR | `AGENTS.md`, gate de calidad y 315 pruebas. | Requisitos de seguridad y threat model por cambio relevante. |
| A.5.9 Inventario de activos | PAR | Inventario de hosts/grupos (`models/ops.py`). | Inventariar BD, claves, logs, vault, imágenes y dependencias. |
| A.5.10 Uso aceptable de activos | EXT | — | Política de uso aceptable y aceptación por usuarios. |
| A.5.11 Devolución de activos | EXT | — | Procedimiento de baja y devolución. |
| A.5.12 Clasificación de información | BRE | Criticidad de hosts (S1) no es clasificación de información. | Esquema de clasificación (pública/interna/confidencial/secreta). |
| A.5.13 Etiquetado de información | BRE | — | Etiquetas para exports, informes y datos sensibles. |
| A.5.14 Transferencia de información | PAR | HTTPS/TLS en nginx, LDAPS, SMTP STARTTLS opcional. | Revisar TLS de SMTP, acuerdos de transferencia y DLP si aplica. |
| A.5.15 Control de acceso | IMP | `login_required`, `roles_required`, RBAC, ownership de cancelación. | Mantener y probar regresiones de autorización. |
| A.5.16 Gestión de identidades | PAR | Usuarios locales y sombra LDAPS; alta/baja desde `/usuarios`. | Proceso joiner/mover/leaver y revisión periódica. |
| A.5.17 Información de autenticación | IMP | `generate_password_hash`, mínimo 12, bloqueo, secretos `0600`, Fernet. | Añadir MFA y política de rotación de secretos. |
| A.5.18 Derechos de acceso | PAR | Roles y panel de usuarios. | Recertificación trimestral y evidencia de aprobación. |
| A.5.19 Seguridad con proveedores | EXT | — | Evaluación y cláusulas de seguridad con proveedores. |
| A.5.20 Acuerdos con proveedores | EXT | — | Contratos con requisitos de seguridad y notificación de incidentes. |
| A.5.21 Cadena de suministro TIC | PAR | `requirements.lock`, imagen Docker, dependencias declaradas. | SBOM, pin por digest, firma de imágenes y verificación de origen. |
| A.5.22 Monitorización de proveedores | EXT | — | Revisiones periódicas de servicios externos. |
| A.5.23 Servicios cloud | EXT | Despliegue on-prem de referencia. | Aplicar si se despliega en cloud. |
| A.5.24 Planificación de incidentes | BRE | Auditoría y logs disponibles, sin plan formal. | Plan de respuesta, roles, contactos y escalado. |
| A.5.25 Evaluación de eventos | BRE | `auth_events` registra eventos; sin triaje. | Criterios de severidad y procedimiento de triaje. |
| A.5.26 Respuesta a incidentes | BRE | — | Runbook de contención, erradicación y recuperación. |
| A.5.27 Aprendizaje de incidentes | BRE | — | Postmortems y acciones correctivas. |
| A.5.28 Recogida de evidencias | PAR | Historial de lotes, `auth_events`, logs con retención. | Cadena de custodia, exportación y protección de evidencia. |
| A.5.29 Seguridad durante interrupción | BRE | Cola de trabajos en memoria; sin BCP. | Plan de continuidad y modo degradado documentado. |
| A.5.30 Preparación TIC para continuidad | BRE | Backups documentados en `docs/OPERACION.md`. | Automatizar backups y probar restauración periódicamente. |
| A.5.31 Requisitos legales | EXT | — | Registro legal y regulatorio aplicable. |
| A.5.32 Propiedad intelectual | EXT | — | Licencias de dependencias y contenido. |
| A.5.33 Protección de registros | PAR | Retención (`ACC_HISTORY_DAYS`, `AUTH_EVENT_RETENTION_DAYS`) y backups. | Protección WORM/inmutable para auditoría crítica. |
| A.5.34 Privacidad y PII | PAR | Solo usuarios, IPs y eventos; sin análisis de privacidad. | Evaluación de impacto y minimización de datos. |
| A.5.35 Revisión independiente | BRE | — | Auditoría interna o revisión externa anual. |
| A.5.36 Cumplimiento de políticas | PAR | Gate de calidad, tests y revisiones en git. | Auditoría de cumplimiento y muestreo de evidencias. |
| A.5.37 Procedimientos documentados | IMP | `docs/OPERACION.md`, `docs/DOCKER.md`, `DEPLOYMENT.md`, README. | Mantener versionado y revisión periódica. |

### 5.2. Controles de personas (A.6)

| Control | Estado | Evidencia en el proyecto | Brecha / acción |
|---|---|---|---|
| A.6.1 Selección de personal | EXT | — | Verificación de antecedentes según política de RR. HH. |
| A.6.2 Términos y condiciones | EXT | — | Cláusulas de seguridad en contratos. |
| A.6.3 Concienciación y formación | PAR | Documentación técnica y mensajes de la UI. | Plan anual de formación y registro de asistencia. |
| A.6.4 Proceso disciplinario | EXT | — | Procedimiento de RR. HH. |
| A.6.5 Responsabilidades tras baja | EXT | — | Retirada de accesos y activos. |
| A.6.6 Acuerdos de confidencialidad | EXT | — | NDA firmados. |
| A.6.7 Teletrabajo | EXT | — | Política de trabajo remoto y acceso seguro. |
| A.6.8 Notificación de eventos | BRE | Auditoría interna, sin canal formal para usuarios. | Canal de reporte y procedimiento de notificación. |

### 5.3. Controles físicos (A.7)

| Control | Estado | Evidencia en el proyecto | Brecha / acción |
|---|---|---|---|
| A.7.1 Perímetros de seguridad física | EXT | — | Depende de la instalación. |
| A.7.2 Entrada física | EXT | — | Control de acceso al CPD/sala. |
| A.7.3 Protección de oficinas | EXT | — | Depende de la instalación. |
| A.7.4 Vigilancia física | EXT | — | Depende de la instalación. |
| A.7.5 Amenazas ambientales | EXT | — | Inundación, incendio, temperatura, alimentación. |
| A.7.6 Trabajo en áreas seguras | EXT | — | Política de acceso a salas técnicas. |
| A.7.7 Escritorio limpio/pantalla limpia | EXT | — | Política de bloqueo de sesión. |
| A.7.8 Ubicación y protección de equipos | EXT | — | Depende de la instalación. |
| A.7.9 Activos fuera de las instalaciones | EXT | — | Portátiles, discos y acceso remoto. |
| A.7.10 Soportes de almacenamiento | EXT | — | Gestión y destrucción de discos. |
| A.7.11 Suministros de soporte | EXT | — | SAI, generador, redundancia. |
| A.7.12 Seguridad del cableado | EXT | — | Depende de la instalación. |
| A.7.13 Mantenimiento de equipos | EXT | — | Plan de mantenimiento. |
| A.7.14 Eliminación/reutilización | EXT | — | Borrado seguro y certificado. |

### 5.4. Controles tecnológicos (A.8)

| Control | Estado | Evidencia en el proyecto | Brecha / acción |
|---|---|---|---|
| A.8.1 Dispositivos de usuario final | EXT | — | Gestión de endpoints y MDM. |
| A.8.2 Derechos de acceso privilegiado | PAR | Roles y separación admin/operator. | MFA, PAM y revisión de cuentas privilegiadas. |
| A.8.3 Restricción de acceso a información | IMP | Autorización por rol, ownership y filtros de vistas. | Probar regresiones de acceso en CI. |
| A.8.4 Acceso al código fuente | PAR | Repositorio git y CI. | Protección de rama, revisión obligatoria y firmas de commit. |
| A.8.5 Autenticación segura | PAR | Hash Werkzeug, bloqueo, rate limit, LDAPS con TLS. | Añadir MFA/TOTP para admin/operator. |
| A.8.6 Gestión de capacidad | PAR | Límites de cola, workers, métricas y p50/p95. | Plan de capacidad y alertas de saturación. |
| A.8.7 Protección contra malware | EXT | Imagen mínima, sin shell interactiva. | Antimalware/EDR en hosts e imágenes. |
| A.8.8 Gestión de vulnerabilidades | BRE | `pip-audit` y `bandit` instalados; CI no los ejecuta. | Integrar escaneo, SLA de parcheo y seguimiento. |
| A.8.9 Gestión de configuración | PAR | `.env`, Compose, validación de configuración insegura. | Gestión de configuración como código y detección de deriva. |
| A.8.10 Eliminación de información | PAR | Retención y borrado de lotes/hosts. | Borrado seguro de BD, backups y secretos. |
| A.8.11 Enmascaramiento de datos | PAR | Redacción de secretos en logs (`_normalize_line`). | Revisar campos sensibles en exports e informes. |
| A.8.12 Prevención de fuga de datos | BRE | — | DLP o controles equivalentes según riesgo. |
| A.8.13 Copias de seguridad | PAR | Procedimiento manual en `docs/OPERACION.md` (SQLite `.backup`, vault, `.env`). | Automatizar, cifrar, verificar restauración y alertar fallos. |
| A.8.14 Redundancia de instalaciones | BRE | Instancia única app+nginx+SQLite. | Definir HA o aceptar RTO/RPO documentados. |
| A.8.15 Registro de eventos | PAR | `auth_events`, historial de lotes, logs con rotación y retención. | Centralizar en SIEM, proteger integridad y alertar. |
| A.8.16 Monitorización | PAR | `/metricas`, `/api/metricas`, `/healthz`, logs. | Alertas proactivas y revisión periódica. |
| A.8.17 Sincronización de relojes | PAR | La app gestiona NTP en hosts; el reloj del servidor depende de infraestructura. | Verificar NTP del host y trazabilidad temporal. |
| A.8.18 Utilidades privilegiadas | PAR | Sin `shell=True`, `eval`, `pickle`; subprocess acotado. | Inventariar utilidades y restringir su uso. |
| A.8.19 Instalación de software | PAR | Imagen Docker y `requirements.lock`. | Allowlist de paquetes y verificación de procedencia. |
| A.8.20 Seguridad de redes | PAR | Allowlist de destinos, host key checking, TLS en nginx. | Segmentación, firewall y revisión de reglas. |
| A.8.21 Seguridad de servicios de red | PAR | TLS 1.2/1.3, ciphers, HSTS, `server_tokens off`. | mTLS o VPN para administración y revisión de configuración. |
| A.8.22 Segregación de redes | EXT | — | VLAN/firewall entre web, app y gestión. |
| A.8.23 Filtrado web | EXT | — | Depende de la red corporativa. |
| A.8.24 Uso de criptografía | PAR | Fernet, TLS, hashes de contraseña, secretos `0600`. | Política criptográfica, rotación y KMS/HSM si aplica. |
| A.8.25 Ciclo de desarrollo seguro | PAR | Tests, gate AST, revisión en git. | Threat model, requisitos de seguridad y puertas de calidad. |
| A.8.26 Requisitos de seguridad de aplicaciones | PAR | Validación estricta, CSRF, CSP, allowlist, dry-run. | Formalizar requisitos tipo ASVS y pruebas de abuso. |
| A.8.27 Arquitectura segura | PAR | Separación web/app/BD, mínimo privilegio, contenedor no root. | Documento de arquitectura de seguridad y límites de confianza. |
| A.8.28 Codificación segura | PAR | Gate AST, SQL parametrizado, sin `shell=True`/`eval`. | Integrar ruff/black/mypy/bandit en CI y corregir hallazgos. |
| A.8.29 Pruebas de seguridad | PAR | 315 pruebas, incluidas autenticación, CSRF, safety y roles. | SAST/DAST y escaneo de dependencias en CI. |
| A.8.30 Desarrollo externalizado | EXT | — | Cláusulas de seguridad si se externaliza. |
| A.8.31 Separación de entornos | PAR | Configuración por entorno y `.env` separados. | Aislamiento real dev/test/prod y datos ficticios. |
| A.8.32 Gestión de cambios | PAR | Git, commits descriptivos, CI. | Aprobación formal, ventana de cambio y rollback probado. |
| A.8.33 Información de pruebas | PAR | Tests con bases temporales y datos ficticios. | Política de uso de datos productivos en pruebas. |
| A.8.34 Protección durante auditoría | BRE | — | Procedimiento para pruebas de auditoría y no interferencia. |

---

## 6. Declaración de aplicabilidad (borrador de SoA)

La SoA definitiva debe aprobarla la dirección. Este borrador propone:

### 6.1. Controles incluidos con evidencia técnica

`A.5.15`, `A.5.17`, `A.5.37`, `A.8.3`, y parcialmente `A.5.3`, `A.5.16`,
`A.5.18`, `A.8.2`, `A.8.5`, `A.8.9`, `A.8.10`, `A.8.11`, `A.8.13`,
`A.8.15`, `A.8.16`, `A.8.20`, `A.8.21`, `A.8.24`–`A.8.29`, `A.8.31`–`A.8.33`.

### 6.2. Controles que requieren evidencia organizativa

`A.5.1`, `A.5.2`, `A.5.4`–`A.5.8`, `A.5.10`–`A.5.14`, `A.5.16`,
`A.5.18`–`A.5.23`, `A.5.24`–`A.5.36`, `A.6.*`, `A.7.*`, `A.8.1`,
`A.8.4`, `A.8.6`–`A.8.8`, `A.8.12`, `A.8.14`, `A.8.17`–`A.8.19`,
`A.8.22`–`A.8.34`.

### 6.3. Justificación de exclusiones

No se propone excluir controles completos sin análisis. Las exclusiones
razonables dependen del alcance:

- **A.7.\*** (físicos): pueden quedar fuera si la certificación cubre solo el
  software y la infraestructura es de un proveedor con su propia certificación;
  debe documentarse la dependencia.
- **A.8.30** (desarrollo externalizado): no aplica si todo el desarrollo es
  interno.
- **A.8.23** (filtrado web): no aplica a la aplicación, pero sí al entorno de
  trabajo de los operadores.
- **A.5.23** (cloud): no aplica al despliegue on-prem de referencia.

Toda exclusión debe registrar: control, motivo, riesgo residual y aprobación.

---

## 7. Registro de riesgos priorizado

Escala: probabilidad e impacto `1`–`5`; riesgo = probabilidad × impacto.

| ID | Riesgo | P | I | Nivel | Tratamiento | Controles |
|---|---|---:|---:|---|---|---|
| R-01 | Robo o reutilización de credenciales de administrador | 3 | 5 | Alto | MFA, PAM, revisión de accesos, formación | A.5.15, A.5.17, A.8.2, A.8.5 |
| R-02 | Operación destructiva sobre host crítico | 2 | 5 | Alto | Criticidad + ventana + doble aprobación + auditoría (S1) | A.5.3, A.8.3, A.8.15 |
| R-03 | Vulnerabilidad en dependencia o imagen | 4 | 4 | Alto | CI con `pip-audit`, Dependabot/Renovate, SBOM, SLA de parcheo | A.5.21, A.8.8, A.8.19 |
| R-04 | Pérdida o corrupción de BD, vault o claves | 2 | 5 | Alto | Backup automático cifrado, restauración probada, separación de claves | A.5.30, A.8.10, A.8.13 |
| R-05 | Manipulación o borrado de auditoría | 2 | 4 | Medio | Retención, exportación, SIEM/integridad, control de acceso | A.5.28, A.5.33, A.8.15 |
| R-06 | Caída de LDAPS o del directorio | 3 | 3 | Medio | Break-glass local documentado, cuenta de emergencia, monitoreo | A.5.16, A.8.5 |
| R-07 | Compromiso de la cadena de suministro | 2 | 5 | Alto | Digest pinning, firma, SBOM, verificación de imágenes | A.5.21, A.8.8 |
| R-08 | Inconsistencia con múltiples workers | 3 | 3 | Medio | Documentar límite; un worker o cola distribuida | A.8.6, A.8.14 |
| R-09 | Falta de respuesta a incidentes | 3 | 4 | Alto | Plan, roles, contactos, simulacro | A.5.24–A.5.27 |
| R-10 | Exposición de secretos en logs o argumentos | 2 | 4 | Medio | Redacción, ficheros temporales `0600`, revisión | A.5.17, A.8.11, A.8.24 |
| R-11 | Phishing o reutilización de contraseña | 3 | 3 | Medio | MFA, concienciación, bloqueo | A.6.3, A.8.5 |
| R-12 | Host gestionado sin parchear | 3 | 4 | Alto | Inventario, escaneo y ventana de parcheo (fuera de la app) | A.8.8, A.5.9 |

---

## 8. Plan de acción priorizado

### P0 — 0 a 30 días (fundamentos)

1. **Aprobar política de seguridad y alcance del SGSI** (A.5.1, A.5.2).
2. **Análisis de riesgos inicial** con esta tabla como punto de partida
   (cláusula 6.1).
3. **Plan de respuesta a incidentes** con roles, contactos y escalado
   (A.5.24–A.5.27).
4. **Backup automatizado + prueba de restauración documentada** (A.8.13,
   A.5.30).
5. **Integrar en CI**: `pip-audit`, `bandit`, `ruff check` con configuración,
   `black --check` y `mypy` gradual; actualizar `pytest` a 9.0.3
   (A.8.8, A.8.28, A.8.29).
6. **MFA para `admin` y, si es viable, `operator`** (A.8.2, A.8.5).
7. **Inventario de activos de información** y clasificación (A.5.9, A.5.12).

### P1 — 30 a 90 días (control)

8. **Gestión de vulnerabilidades** con SLA y seguimiento de CVEs (A.8.8).
9. **SBOM, pin por digest y firma de imágenes** (A.5.21, A.8.19).
10. **Logging centralizado y alertas** para login fallido, bloqueos, overrides
    y errores 5xx (A.8.15, A.8.16).
11. **Recertificación trimestral de accesos** (A.5.18).
12. **Threat model y requisitos ASVS** para las rutas críticas (A.8.25–A.8.27).
13. **Prueba de continuidad/DR** y decisión HA documentada (A.8.14,
    A.5.29, A.5.30).
14. **Procedimiento de cambios** con aprobación, ventana y rollback
    (A.8.32).

### P2 — 90 a 180 días (madurez y certificación)

15. **Auditoría interna y revisión por la dirección** (cláusulas 9.2, 9.3).
16. **Métricas del SGSI** y registro de no conformidades/CAPA (9.1, 10.2).
17. **Seguridad de proveedores** y acuerdos (A.5.19–A.5.22).
18. **Evaluación de privacidad/PII** (A.5.34).
19. **Concienciación y formación** con registro (A.6.3).
20. **Preparación de certificación**: SoA aprobada, evidencias, auditoría
    interna y auditoría de etapa 1/2.

---

## 9. Métricas propuestas del SGSI

| Métrica | Objetivo | Fuente |
|---|---|---|
| Cobertura de controles con evidencia | ≥ 90 % antes de certificar | SoA y repositorio |
| Vulnerabilidades críticas fuera de SLA | 0 | `pip-audit`, escaneo de imagen |
| Backups verificados con restauración | 100 % mensual | Registro de backups |
| Tiempo medio de respuesta a incidentes | ≤ 24 h (críticos) | Registro de incidentes |
| Usuarios privilegiados con MFA | 100 % | Configuración de identidad |
| Cambios con pruebas y revisión | 100 % | CI/git |
| Accesos revisados trimestralmente | 100 % | Recertificación |
| Formación de seguridad completada | ≥ 95 % anual | RR. HH. |
| Hallazgos de auditoría cerrados en plazo | ≥ 90 % | CAPA |

---

## 10. Evidencias técnicas reproducibles

```bash
# Pruebas y gate de calidad
.venv/bin/python -m pytest -q
.venv/bin/python tools/quality_check.py

# Análisis de seguridad y calidad (requiere instalación desde PyPI directo)
.venv/bin/pip-audit -r requirements.txt
.venv/bin/bandit -q -r app.py config.py models services tools
.venv/bin/ruff check app.py config.py models services tools tests
.venv/bin/black --check app.py config.py models services tools tests
.venv/bin/mypy --ignore-missing-imports app.py config.py models services tools

# Estado del despliegue
docker compose --env-file .env.docker ps
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1/healthz

# Esquema y auditoría
sqlite3 /var/lib/ansible-control-center/acc.db 'PRAGMA user_version;'
sqlite3 /var/lib/ansible-control-center/acc.db \
  'SELECT timestamp, username, event FROM auth_events ORDER BY id DESC LIMIT 20;'
```

---

## 11. Limitaciones y siguiente paso

- Este mapeo es una **autoevaluación técnica**, no una auditoría independiente.
- Los controles `EXT` requieren evidencias de la organización; el repositorio
  no puede aportarlas.
- La valoración no sustituye un análisis de riesgos formal ni una SoA
  aprobada.
- Las herramientas de análisis están instaladas localmente, pero **aún no se
  ejecutan en CI**; integrarlas es parte del plan P0.
- El alcance de certificación debe decidirse antes de invertir en evidencias:
  ¿solo el software, el servicio operado o toda la organización?

**Siguiente paso recomendado:** aprobar el alcance y la política, convertir el
registro de riesgos en un registro vivo y ejecutar el bloque P0. Con eso, el
proyecto pasaría de “base técnica sólida” a “SGSI en implantación”.
