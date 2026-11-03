# Análisis funcional — Hora Admin

**Objetivo del sistema:** cambiar la hora y/o la zona horaria a un grupo de
equipos de una red local, pudiendo elegir entre una hora exacta o una zona
horaria concreta.

**Fecha:** 2026-11-03
**Estado del proyecto:** fases P0–P4.2 completadas.
**Alcance de este documento:** análisis funcional y de posibilidades. No
describe una implementación cerrada, sino el espacio de opciones y la brecha
entre lo que existe hoy y lo que puede llegar a ser el sistema.

---

## 1. Resumen ejecutivo

El proyecto ya tiene una base sólida y, tras P4, cubre el ciclo completo de una
operación sobre un grupo de equipos:

- Autenticación con Flask-Login.
- Vault real cifrado con `ansible-vault`.
- Ejecución de Ansible con inyección segura de credenciales.
- Ejecución en **todos** los hosts con best-effort y reintento de contraseñas.
- Vista previa de solo lectura antes de aplicar cambios.
- Verificación posterior con `timedatectl show`, `date` y `hwclock`.
- Idempotencia y opción explícita “Forzar”.
- Semántica explícita **UTC/local** y política NTP (`keep`, `off`, `restore`,
  `configure`).
- Lotes auditables (`execution_batches`) con resultado por host
  (`execution_hosts`), estado previo/posterior, error y duración.
- Historial por lotes y progreso SSE.
- CSRF, rate limiting, límites de CIDR/hosts y cabeceras de seguridad.

Quedan brechas importantes para producción a gran escala, analizadas en
[ANALISIS_Y_RECOMENDACIONES](docs/ANALISIS_Y_RECOMENDACIONES.md):

- `TIME` con política NTP `keep` puede ser revertido por NTP; se avisa, pero
  no se bloquea.
- Ejecución secuencial, sin paralelismo, cola persistente ni cancelación. Los
  intentos por host y el tiempo del lote sí están acotados.
- Estado SSE en memoria con instantáneas en SQLite y reconciliación al
  arrancar; varios workers seguirían necesitando un almacén compartido.
- Sin retención automática del historial, RBAC ni MFA. La allowlist de redes
  ya existe.
- Un único worker recomendado; SSE y timeout de Gunicorn pueden interferir.

Las dos decisiones que más condicionan el diseño —semántica UTC/local y
política NTP— ya están **implementadas como opciones explícitas** en el
formulario y en el playbook. La decisión pendiente es cuál debe ser el valor
por defecto operativo y qué combinaciones bloquear.

---

## 2. Flujo actual

1. El usuario introduce en el panel:
   - IP o rango CIDR;
   - usuario administrador;
   - origen de contraseñas (`stored` o `temporal`);
   - contraseña maestra del vault o archivo `.txt`;
   - acción (`TZ` o `TIME`);
   - valor (zona horaria o fecha/hora).
2. `services/host_resolver.py` valida el objetivo y resuelve el CIDR a una
   lista de IPs.
3. Se descifra el vault con `ansible-vault` o se procesa el `.txt` en memoria.
4. Opcionalmente, `app.py` ejecuta una **vista previa**: inspecciona todos los
   hosts sin cambiar nada y guarda un lote en estado `preview`.
5. `app.py` recorre **todos** los hosts y hasta
   `HORA_ADMIN_MAX_PASSWORD_ATTEMPTS` contraseñas candidatas por host:
   - ejecuta `ansible -m ping`;
   - si el ping funciona, ejecuta `ansible/cambiar_hora.yml` con la semántica
     de hora y la política NTP elegidas;
   - verifica el estado posterior;
   - registra un lote (`execution_batches`) y un resultado por host
     (`execution_hosts`);
   - continúa con el resto de hosts aunque uno falle (best-effort).
6. El navegador sigue el progreso mediante SSE y enlaza al detalle del lote.
7. El historial muestra los lotes con contadores y el detalle por host.

### 2.1. Comportamiento real del playbook

`ansible/cambiar_hora.yml`:

- **TZ:** ejecuta `timedatectl set-timezone <zona>` solo si la zona actual es
  distinta (salvo `force`).
- **TIME:**
  1. normaliza `AAAA-MM-DDTHH:MM`;
  2. calcula el instante objetivo como epoch UTC o como hora local del host
     según `modo_hora`;
  3. aplica la política NTP (`keep`, `off`, `restore` o `configure`);
  4. ejecuta `date -s "@<epoch>"` solo si la desviación supera la tolerancia;
  5. opcionalmente ejecuta `hwclock --systohc`.
- Al final vuelve a leer el estado con `timedatectl show`, `date` y `hwclock`,
  publica los marcadores `HORA_*` y solo se considera éxito si la verificación
  coincide con el objetivo.

---

## 3. Hallazgos críticos

> **Nota:** esta tabla describe el estado previo a P4. La implementación de P4
> corrige los hallazgos 1-8 (ejecución completa del lote, best-effort,
> reintento de contraseñas, política NTP, semántica UTC/local, verificación,
> idempotencia y modelo de lote con resultados por host).

| # | Hallazgo | Impacto |
|---|---|---|
| 1 | Solo se configura el primer host con éxito | Un “grupo” no se configura completo |
| 2 | Un fallo del playbook detiene todo el lote | No hay best-effort ni reporte completo |
| 3 | Si una contraseña autentica pero falla el playbook, no se prueban las demás | Se pierden credenciales válidas |
| 4 | `TIME` deja NTP desactivado | El reloj vuelve a derivar y el equipo queda sin sincronización |
| 5 | `TIME` usa hora local sin zona | El mismo valor produce instantes distintos en hosts con TZ distinta |
| 6 | No hay verificación posterior | No se sabe si el cambio se aplicó realmente |
| 7 | No hay idempotencia | Siempre se marca “changed” y se ejecutan comandos innecesarios |
| 8 | El historial no modela el lote | No hay `batch_id`, operador, duración ni estado por host completo |
| 9 | No hay inventario ni grupos | Solo IP/CIDR |
| 10 | No hay claves SSH | Solo contraseña |
| 11 | Solo Ubuntu/systemd | No hay soporte para otras plataformas |
| 12 | No hay programación ni notificaciones | Todo es manual y sin aviso |

---

## 4. Capacidades actuales

Leyenda: ✅ implementado · ⚠️ parcial o con matices · ❌ no existe.

| Capacidad | Estado | Detalle |
|---|---:|---|
| Un solo host | ✅ | Funciona |
| Rango CIDR | ✅ | Se resuelve y se procesan todos los hosts |
| Lista de hosts / CSV | ❌ | No existe |
| Inventario Ansible / grupos / etiquetas | ❌ | No existe |
| Exclusión de hosts | ❌ | No existe |
| Zona horaria | ✅ | `timedatectl set-timezone` idempotente |
| Hora exacta | ✅ | `date -s`, modo UTC/local explícito, sin segundos |
| Desactivar NTP | ✅ | Política `off` |
| Reactivar NTP | ✅ | Políticas `restore` y `configure` |
| Configurar servidores NTP | ✅ | Política `configure` (chrony/timesyncd) |
| Sincronizar RTC | ✅ | Opcional con `hwclock --systohc`; puede fallar en VMs |
| Verificar estado antes/después | ✅ | `timedatectl show`, `date`, `hwclock` |
| Idempotencia | ✅ | Salvo `force` |
| Dry-run | ✅ | Visible, persistido, métricas separadas y vista previa real |
| Reintentos | ⚠️ | Candidatas acotadas por host; no se reintentan hosts fallidos |
| Paralelismo | ❌ | Secuencial |
| Continuar si un host falla | ✅ | Best-effort |
| Resultado por host | ✅ | Estado previo/posterior, error y duración |
| Historial | ✅ | Por lote y detalle por host |
| Vault cifrado | ✅ | `ansible-vault` |
| Archivo temporal `.txt` | ✅ | En memoria |
| SSH con contraseña | ✅ | Requiere `sshpass` |
| SSH con clave | ❌ | No existe |
| Verificación estricta de host | ✅ | `known_hosts` |
| CSRF | ✅ | P3 |
| Rate limiting | ✅ | P3 |
| Cabeceras de seguridad | ✅ | P3 |
| RBAC / MFA / aprobaciones | ❌ | Un único administrador |
| Programación / cron | ❌ | No existe |
| Notificaciones | ❌ | No existe |
| Exportación CSV/JSON | ❌ | No existe |
| API pública / CLI de ejecución | ❌ | Solo endpoints internos |
| Rollback automático | ❌ | Estado previo guardado; reversión manual |
| Retención automática | ⚠️ | Tareas sí; historial de lotes sin job de purga |
| Multiplataforma | ❌ | Solo Ubuntu/systemd |

---

## 5. Límites de red actuales

Valores por defecto:

- `HORA_ADMIN_MIN_CIDR_PREFIX=22`
- `HORA_ADMIN_MAX_HOSTS=256`

Un `/22` tiene 1022 direcciones y sería rechazado por `MAX_HOSTS`; un `/23`
tiene 510 y también. **En la práctica, el rango máximo aceptado por defecto es
`/24` (254 hosts).**

La validación ocurre antes de enumerar el rango, por lo que entradas como
`0.0.0.0/0` o `10.0.0.0/8` no agotan la memoria del proceso.

---

## 6. Espacio completo de opciones

### 6.1. Qué se quiere cambiar

| Opción | Descripción | Recomendación |
|---|---|---|
| Solo zona horaria | Cambia `TZ`, no el instante | Seguro y persistente |
| Hora exacta en UTC | Mismo instante absoluto en todos los PCs | Recomendado para grupos |
| Hora local por host | Misma hora de pared, distinto instante si cambia la TZ | Solo casos concretos |
| Ajuste relativo | `+/- N segundos/minutos` | Útil para corregir deriva |
| Sincronizar desde el servidor | Usa el reloj del controlador | Rápido, pero no sustituye a NTP |
| Sincronizar desde NTP | Configura chrony/timesyncd | La opción más correcta |
| Activar/desactivar NTP | Política explícita | Necesario para `TIME` |
| Configurar servidores NTP | Lista de servidores | Muy recomendable |
| Sincronizar RTC | `hwclock --systohc` / `--hctosys` | Opcional según VM |
| RTC en UTC o local | Evita problemas con dual boot | Decisión de política |
| Verificar sin cambiar | Leer hora, TZ, NTP y RTC | Imprescindible |
| Detectar deriva | Comparar con una referencia | Útil para monitorización |
| Rollback | Volver a TZ/hora anterior | Necesita guardar estado previo |
| Actualizar `tzdata` | Nuevas reglas DST | Mantenimiento |
| Reinicio | Rara vez necesario | Evitar por defecto |

> Cambiar la zona horaria no es lo mismo que cambiar la hora. La zona horaria
> cambia la representación; la hora cambia el instante. El sistema debe dejar
> claro cuál de las dos cosas hace.

### 6.2. A qué equipos

| Opción | Descripción |
|---|---|
| IP única | Ya existe |
| Hostname | Resolución DNS |
| CIDR | Ya existe, pero limitado |
| Lista separada por comas/saltos | Muy útil |
| Rango `192.168.1.10-20` | Cómodo para lotes pequeños |
| Inventario Ansible INI/YAML | La opción más escalable |
| Grupos y etiquetas del inventario | `webservers`, `aula1`, `tz-europe` |
| CSV con columnas | host, usuario, puerto, TZ, credencial |
| Exclusiones | “Todo el aula menos estas 5 IPs” |
| Grupos guardados en BD | “Aula 2”, “Servidores de laboratorio” |
| Historial reciente | Repetir una operación |
| Descubrimiento de red | `nmap`/ARP; requiere autorización explícita |
| IPv6 | Soportado por `ipaddress`, no probado en el flujo |
| Overrides por host | Distinta TZ o distinta credencial |
| Bastión / jump host | Redes segmentadas |
| Puerto SSH distinto | Equipos no estándar |

### 6.3. Cómo autenticarse

| Opción | Estado |
|---|---|
| Contraseña en vault | ✅ |
| Contraseña temporal `.txt` | ✅ |
| Clave SSH | ❌ |
| `ssh-agent` | ❌ |
| Contraseña de `sudo` distinta de la SSH | ❌ |
| `sudo` sin contraseña | ⚠️ Depende del host |
| Credencial por host/grupo | ❌ |
| `known_hosts` estricto | ✅ |
| Aceptar host nuevo (TOFU) | ❌ |
| Desactivar verificación | ✅ pero inseguro |
| WinRM para Windows | ❌ |
| SSH/NETCONF para dispositivos de red | ❌ |

### 6.4. Cómo se ejecuta

| Opción | Descripción |
|---|---|
| Dry-run | Ya existe |
| Check mode / simulación | Mostrar qué cambiaría sin tocar |
| Canario | Probar primero en 1 host |
| Tamaño de lote | 1, 10, 50 hosts |
| Paralelismo (`forks`) | Acelerar grupos grandes |
| Continuar si falla un host | Best-effort |
| Parar al primer fallo | Fail-fast |
| Reintentos con backoff | Redes inestables |
| Timeout por host y global | Evitar bloqueos |
| Idempotencia | No cambiar si ya está correcto |
| Forzar cambio | Aunque ya esté correcto |
| Programar | Ahora, fecha futura o cron |
| Cancelar / reanudar | Operaciones largas |
| Aprobación previa | Dos personas para cambios masivos |
| Ventana de mantenimiento | Evitar horas críticas |

### 6.5. Qué se muestra y registra

| Opción | Descripción |
|---|---|
| Vista previa | “Se cambiará TZ de 40 hosts a Europe/Madrid” |
| Estado actual por host | Hora, TZ, NTP, RTC |
| Estado posterior | Confirmación real |
| Tabla por host | Éxito/fallo, duración, error |
| Reintentar solo fallidos | Muy útil |
| Historial por lote | Un registro agrupado, no solo por host |
| Operador | Quién lanzó el cambio |
| Duración | Por host y total |
| Error clasificado | Conexión, permisos, NTP, comando |
| Logs descargables | CSV/JSON |
| Filtros | Fecha, host, resultado, operador |
| Retención | Política de limpieza |
| Integración SIEM | Envío de auditoría |

### 6.6. Seguridad y gobierno

| Opción | Estado |
|---|---|
| CSRF | ✅ |
| Rate limiting | ✅ |
| Límites CIDR/hosts | ✅ |
| Cabeceras de seguridad | ✅ |
| TLS detrás de proxy | ⚠️ Configurable |
| RBAC (viewer/operator/admin) | ❌ |
| Permisos por grupo | ❌ |
| MFA | ❌ |
| Lista blanca de IPs | ❌ |
| Aprobaciones | ❌ |
| Auditoría a prueba de manipulación | ❌ |
| Rotación de secretos | ❌ |
| Copias de seguridad | ⚠️ Documentado |
| Confirmación para grupos grandes | ❌ |
| Autorización legal del usuario | Responsabilidad operativa |

### 6.7. Plataformas

| Plataforma | Herramienta | Estado |
|---|---|---|
| Ubuntu/Debian | `timedatectl`, `date`, `hwclock` | ✅ |
| RHEL/Rocky/Fedora | `timedatectl` | ❌ No probado |
| Alpine/OpenRC | `date`, `hwclock`, sin `timedatectl` | ❌ |
| Windows | `w32tm`, `Set-TimeZone` | ❌ |
| macOS | `systemsetup` | ❌ |
| ESXi | `esxcli` | ❌ |
| Cisco/Juniper | NTP por CLI/NETCONF | ❌ |
| Contenedores | Comparten reloj del host | ❌ No aplicable |
| VMs | Sincronización con hipervisor | ⚠️ Caso especial |

### 6.8. Integración y escala

| Opción | Descripción |
|---|---|
| API REST | Ejecutar y consultar desde otros sistemas |
| CLI | Automatización y scripts |
| Webhooks | Avisar a Slack/Teams/Telegram |
| Inventario dinámico | CMDB, LDAP, Ansible |
| Monitorización | Prometheus/Zabbix |
| Ticketing | Jira/ServiceNow |
| Cola de trabajos | Celery/RQ para operaciones largas |
| Redis | Rate limit y estado compartido |
| PostgreSQL | Sustituir SQLite al escalar |
| Varios workers | Requiere estado compartido |
| Health checks | `/healthz` |
| Observabilidad | Logs, métricas, trazas |

---

## 7. Decisiones de diseño que cambian todo

### 7.1. ¿Hora exacta en UTC o hora local?

Son cosas distintas:

- **Mismo instante UTC en todos:** todos los PCs quedan sincronizados entre sí.
  Es lo recomendable para un grupo.
- **Misma hora de pared:** si un PC está en `Europe/Madrid` y otro en `UTC`,
  escribir `14:30` produce instantes distintos.
- **Hora local por host:** cada PC recibe su hora local correcta, pero no es
  una sincronización global.

La interfaz actual usa `datetime-local` sin zona horaria en el control, pero
P4 añade una elección explícita `time_mode`:

- `utc`: el valor se interpreta como UTC y se convierte a epoch; todos los
  hosts reciben el mismo instante.
- `local`: el valor es la hora de pared de cada host; hosts con zonas distintas
  quedan en instantes distintos.

La decisión pendiente es cuál debe ser el valor por defecto y si `local` debe
avisar o bloquearse en grupos heterogéneos.

### 7.2. ¿Qué hacemos con NTP?

Opciones:

1. **No tocar NTP** al cambiar solo la zona horaria. Es lo más seguro.
2. **Desactivar NTP, fijar hora y reactivar NTP.** Útil para un ajuste puntual.
3. **Desactivar NTP permanentemente.** Solo si el equipo no debe sincronizarse.
4. **Configurar servidores NTP.** La solución correcta para mantener la hora.
5. **Verificar el estado de sincronización.** Debería ser obligatorio tras un
   cambio.

P4 implementa las cinco opciones como política explícita (`keep`, `off`,
`restore`, `configure`) y verifica el estado final. Queda un matiz importante:

- `TIME` + `keep` deja NTP activo y este puede revertir la hora fijada; la
  vista previa avisa, pero no se bloquea.

`restore` ya respeta el estado previo: solo reactiva NTP si estaba activo
antes. La política por defecto sigue siendo `keep`, adecuada para `TZ` pero no
para `TIME`.

### 7.3. ¿Qué significa “grupo”?

Puede significar:

- “Aplica a todos y falla si alguno falla” (atómico, difícil en redes).
- “Aplica a todos y reporta éxitos/fallos” (best-effort).
- “Aplica hasta el primer éxito” (incorrecto para grupos).
- “Aplica solo a un lote pequeño y luego al siguiente” (canario).

P4 implementa la semántica **best-effort**: aplica a todos, continúa ante
fallos y reporta éxitos/fallos por host. Las alternativas atómica, canario y
por tandas siguen sin implementarse.

### 7.4. ¿La operación debe ser idempotente?

Sí, casi siempre. Antes de cambiar:

- leer la TZ actual;
- leer la hora actual;
- leer el estado de NTP;
- comparar con el objetivo;
- cambiar solo si difiere;
- verificar después.

P4 implementa esta comparación antes de cambiar (salvo `force`) y verifica el
estado final. La vista previa muestra `would_change` para cada host.

### 7.5. ¿Qué pasa con RTC, VMs y dual boot?

- `hwclock --systohc` puede fallar en contenedores y algunas VMs.
- En dual boot con Windows, el RTC puede estar en hora local y no en UTC.
- En VMs, el hipervisor puede resincronizar el reloj y deshacer el cambio.
- En contenedores no se puede cambiar el reloj; solo el del host.

---

## 8. Brecha actual y roadmap recomendado

### P4 — Corregir la semántica de grupo (completado)

1. **Ejecutar en todos los hosts**, no solo en el primero.
2. **Continuar ante fallos** y agregar resultados por host.
3. **Probar más contraseñas** si una autentica pero el playbook falla, con un
   tope por host y un presupuesto de tiempo por lote.
4. **Modelo de lote** en BD: `batch_id`, operador, objetivo, acción, valor y
   estado global.
5. **Resultado por host:** estado previo, estado posterior, error y duración.
6. **Vista previa** antes de ejecutar.
7. **Verificación posterior** con `timedatectl show`, `date` y `hwclock`.
8. **Idempotencia:** no cambiar si ya está correcto.
9. **Semántica explícita UTC/local** en el formulario.
10. **Política NTP explícita:** mantener, desactivar, desactivar y reactivar, o
    configurar servidores.

### P5 — Operación

- Listas, CSV, inventario Ansible, grupos y exclusiones.
- Canario, lotes, paralelismo y reintentos.
- Programación y ventanas de mantenimiento.
- Notificaciones y exportación.
- RBAC, auditoría de operador y aprobaciones.
- API/CLI.
- Filtros y paginación del historial.

### P6 — Multiplataforma

- RHEL/Rocky.
- Windows (`w32tm`).
- Dispositivos de red.
- Casos especiales de VMs y contenedores.

---

## 9. Decisiones pendientes de confirmar

Decisiones ya tomadas en P4:

- **Hora exacta:** se ofrecen `utc` y `local` como opciones explícitas; falta
  fijar el valor por defecto operativo.
- **NTP:** se ofrecen `keep`, `off`, `restore` y `configure`; falta decidir si
  se bloquea `TIME` + `keep` y si `restore` debe respetar el estado previo.
- **Semántica de grupo:** best-effort con reporte por host.

Decisiones todavía abiertas:

1. **Plataformas objetivo:** ¿solo Ubuntu o también RHEL/Windows?
2. **Selección de equipos:** ¿basta con IP/CIDR o hace falta inventario, CSV,
   listas y grupos guardados?
3. **Programación:** ¿se necesita ejecución diferida o recurrente?
4. **Gobierno:** ¿hacen falta roles, aprobaciones y auditoría de operador?
5. **Escala:** ¿cuántos equipos como máximo y con cuántos workers?
6. **Retención:** ¿cuánto tiempo conservar lotes y auditoría?
7. **Red:** ¿hay que restringir objetivos a una allowlist corporativa?

---

## 10. Referencias de código

| Elemento | Ruta |
|---|---|
| Rutas, login, ejecución y SSE | `app.py` |
| Configuración central | `config.py` |
| Resolución de IP/CIDR y límites | `services/host_resolver.py` |
| Ejecución segura de Ansible | `services/ansible_runner.py` |
| Vault con `ansible-vault` | `services/vault_manager.py` |
| Ficheros temporales `0600` | `services/secure_temp.py` |
| CSRF | `services/csrf.py` |
| Rate limiting | `services/rate_limiter.py` |
| Playbook de hora/zona | `ansible/cambiar_hora.yml` |
| Playbook de inspección | `ansible/estado_hora.yml` |
| Lectura de estado | `ansible/tasks/leer_estado.yml` |
| Lotes y resultados por host | `models/batch.py` |
| Historial heredado y estadísticas | `models/execution.py` |
| Esquema SQLite y migraciones | `models/db.py` |
| Formulario principal | `templates/index.html` |
| Detalle de lote | `templates/batch.html` |
| Gestión de contraseñas | `templates/passwords.html` |
| Historial | `templates/history.html` |
| Documentación de despliegue | `DEPLOYMENT.md` |
| Arquitectura y flujos | `docs/ARQUITECTURA.md` |
| Referencia técnica | `docs/REFERENCIA.md` |
| Operación y runbook | `docs/OPERACION.md` |
| Análisis y recomendaciones | `docs/ANALISIS_Y_RECOMENDACIONES.md` |

---

## 11. Conclusión

Tras P4, el proyecto ejecuta operaciones completas sobre grupos de equipos con
credenciales protegidas, best-effort, vista previa, verificación, idempotencia,
semántica UTC/local explícita y política NTP configurable. El modelo de lotes y
resultados por host permite auditar qué ocurrió en cada equipo.

Para considerarlo una herramienta de producción a gran escala faltan,
principalmente:

- bloquear (no solo avisar) `TIME` + `keep`;
- paralelismo, cola persistente, cancelación y reintento selectivo;
- RBAC, MFA y auditoría independiente (la allowlist de redes ya existe);
- retención automática del historial y rollback asistido.

El análisis detallado, los hallazgos priorizados y el roadmap están en
[ANALISIS_Y_RECOMENDACIONES](docs/ANALISIS_Y_RECOMENDACIONES.md).
