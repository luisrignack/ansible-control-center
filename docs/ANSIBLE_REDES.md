# Ansible sobre equipamiento de red

Referencia técnica de las operaciones que Ansible puede ejecutar sobre
switches, routers, firewalls, controladoras inalámbricas y balanceadores, y de
lo que haría falta para llevarlas a Ansible Control Center (ACC).

> **Estado en ACC:** a fecha de este documento (2026-11-10) la aplicación solo
> opera hosts Linux por SSH y Windows por WinRM. Ninguna de las operaciones de
> red descritas aquí está integrada todavía: este documento es la base de
> diseño para hacerlo por fases.

---

## Contenido

1. [Resumen ejecutivo](#1-resumen-ejecutivo)
2. [Qué puede y qué no puede hacer Ansible en red](#2-qué-puede-y-qué-no-puede-hacer-ansible-en-red)
3. [Cómo se conecta Ansible a un equipo de red](#3-cómo-se-conecta-ansible-a-un-equipo-de-red)
4. [Colecciones por fabricante](#4-colecciones-por-fabricante)
5. [Catálogo de operaciones](#5-catálogo-de-operaciones)
6. [Módulos y ejemplos](#6-módulos-y-ejemplos)
7. [Patrón seguro: inspeccionar, comparar, aplicar, verificar](#7-patrón-seguro-inspeccionar-comparar-aplicar-verificar)
8. [Riesgos, límites y controles](#8-riesgos-límites-y-controles)
9. [Qué necesitaría Ansible Control Center](#9-qué-necesitaría-ansible-control-center)
10. [Primeras tarjetas recomendadas](#10-primeras-tarjetas-recomendadas)
11. [Glosario](#11-glosario)
12. [Referencias](#12-referencias)

---

## 1. Resumen ejecutivo

- Ansible **sí** puede gestionar switches, routers y firewalls, pero no con
  `ssh` genérico ni con módulos de sistema operativo: usa **connection
  plugins** (`network_cli`, `netconf`, `httpapi`) y **colecciones de
  fabricante** que aportan módulos con conocimiento del equipo.
- Las operaciones se agrupan en tres niveles de riesgo:
  **solo lectura** (facts, estado, backup), **cambio controlado** (VLAN,
  puertos, NTP, syslog, rutas estáticas) y **alto riesgo** (uplinks, routing,
  ACL de gestión, AAA, firmware, reinicio).
- El mayor valor inmediato y el menor riesgo están en **inventario de red** y
  **backup de configuración**. Son las dos primeras tarjetas que conviene
  implementar.
- Ansible **no** es un sistema de monitorización, **no** garantiza
  transacciones entre equipos y **no** tiene rollback universal: la
  reversibilidad depende del fabricante (Junos `commit confirmed`, NX-OS
  checkpoints, IOS `configure replace`, etc.).
- Para integrarlo en ACC hay que tocar inventario, runner, imagen Docker
  (colecciones), formularios y pruebas. El patrón de dos playbooks
  (`*_estado.yml` de solo lectura + `*.yml` de aplicación con verificación) ya
  existe en la aplicación y se reutiliza tal cual.

---

## 2. Qué puede y qué no puede hacer Ansible en red

### 2.1. Sí puede

| Área | Ejemplos |
|---|---|
| Descubrimiento | Modelo, número de serie, firmware, uptime, módulos, licencias, inventario físico |
| Estado operativo | Interfaces, contadores de errores, VLANs, ARP/MAC, rutas, vecinos LLDP/CDP, spanning-tree, PoE, LAG |
| Configuración | VLANs, puertos access/trunk, direcciones IP, SVIs, rutas estáticas, OSPF, BGP, VRF, ACL, QoS |
| Servicios de gestión | NTP, DNS, syslog, SNMP, AAA (TACACS+/RADIUS), banner, usuarios, claves SSH, certificados |
| Copias de seguridad | `running-config`/`startup-config`, configuración Junos, export de RouterOS, hash y comparación |
| Cumplimiento | Comparar configuración contra una plantilla o baseline, detectar desviaciones |
| Firmware | Copia de imagen y activación (con ventana y procedimiento específico por fabricante) |
| Otros equipos | Firewalls (Fortinet, Palo Alto, Check Point), balanceadores (F5), Wi-Fi (Cisco, Aruba), SD-WAN |

### 2.2. No puede, o no debería

| Límite | Detalle |
|---|---|
| Monitorización continua | Ansible ejecuta tareas puntuales; para métricas y alertas usa Zabbix, LibreNMS, Prometheus o el NMS corporativo |
| Transacciones multi-equipo | Un fallo a mitad de un cambio deja equipos en estados distintos; hay que diseñar por lotes pequeños y con verificación |
| Rollback universal | Solo algunos fabricantes ofrecen reversión nativa; en el resto hay que restaurar un backup |
| `check mode` homogéneo | Muchos módulos de red lo soportan, pero no todos ni con el mismo detalle; el `--check` puede seguir abriendo sesión en el equipo |
| Módulos de sistema operativo | `copy`, `template`, `file`, `service`, `package` no funcionan sobre `network_cli`; el equipo de red no ejecuta Python |
| `raw`/`shell` | Evitarlos: saltan el parseo, la idempotencia y la validación; además pueden dejar la CLI en un modo inesperado |
| Telnet | Existe `ansible.netcommon.telnet`, pero no cifra credenciales; úsalo solo como último recurso en equipos legacy aislados |
| Fuerza bruta de credenciales | No es el caso de uso; ACC ya limita intentos, pero un equipo de red puede bloquear la cuenta o la IP de gestión |

### 2.3. Decisión rápida: ¿qué módulo usar?

| Necesidad | Opción preferida | Por qué |
|---|---|---|
| Leer estado estructurado | `*_facts` del fabricante | Devuelve datos normalizados, sin parsear texto |
| Leer un `show` concreto | `ansible.netcommon.cli_command` | Funciona en cualquier equipo con `network_cli` |
| Convertir un `show` en datos | `ansible.utils.cli_parse` + TextFSM/ntc-templates | Evita expresiones regulares frágiles |
| Configurar un recurso concreto | Módulo de recurso (`ios_vlans`, `ios_l2_interfaces`, …) | Declarativo e idempotente |
| Configurar algo sin módulo de recurso | `*_config` con `lines`/`parents` | Sigue siendo idempotente si se usa `match` |
| Configuración multi-fabricante | `cli_config` | Genérico, pero sin validación semántica |
| Equipo con YANG/NETCONF | `netconf_get` / `netconf_config` | Estructurado y estándar |
| Equipo con RESTCONF | `restconf_get` / `restconf_config` | Estructurado sobre HTTP/TLS |
| Equipo con API propietaria | Colección del fabricante (`httpapi` o módulos locales) | Usa la API oficial y evita la CLI |

---

## 3. Cómo se conecta Ansible a un equipo de red

### 3.1. Conexiones

| Conexión | Plugin | Uso | Puerto típico |
|---|---|---|---|
| CLI sobre SSH | `ansible.netcommon.network_cli` | IOS, NX-OS, EOS, Junos, RouterOS, VRP, AOS-CX… | 22 |
| NETCONF sobre SSH | `ansible.netcommon.netconf` | Junos, IOS-XE, NX-OS, equipos con YANG | 830 |
| HTTP/HTTPS API | `ansible.netcommon.httpapi` | FortiOS, NX-API, eAPI, AOS-CX, Check Point | 443 |
| RESTCONF | `httpapi` con `ansible_network_os: ansible.netcommon.restconf` | Equipos con RESTCONF habilitado | 443 |
| SNMP | Módulo local (`community.general.snmp_facts`) | Solo lectura y monitorización puntual | 161/UDP |
| API propietaria | Módulos con `connection: local` | PAN-OS, F5, AVI… | 443 |

`network_cli` mantiene una **conexión persistente** por host, gestionada por el
proceso `ansible-connection`. Eso acelera varios comandos seguidos, pero añade
particularidades: timeouts propios y un socket temporal que puede quedar vivo
si el proceso se interrumpe.

### 3.2. Variables mínimas de inventario

```yaml
all:
  children:
    switches:
      vars:
        ansible_connection: ansible.netcommon.network_cli
        ansible_network_os: cisco.ios.ios
        ansible_user: acc-readonly
        ansible_password: "{{ vault_network_password }}"
        ansible_become: true
        ansible_become_method: enable
        ansible_become_password: "{{ vault_enable_secret }}"
        ansible_command_timeout: 60
        ansible_connect_timeout: 30
      hosts:
        sw-core-01:
          ansible_host: 192.0.2.10
        sw-access-01:
          ansible_host: 192.0.2.11
```

Notas:

- `ansible_network_os` debe ser el **FQCN** de la colección
  (`cisco.ios.ios`, `cisco.nxos.nxos`, `arista.eos.eos`,
  `junipernetworks.junos.junos`, `community.routeros.routeros`…).
- `ansible_become: true` + `ansible_become_method: enable` es el equivalente a
  entrar en modo privilegiado. La contraseña de `enable` va en
  `ansible_become_password`, **no** en `ansible_password`.
- Para un equipo que solo permite lectura, usa una cuenta de solo lectura y
  deja `ansible_become: false`.
- `ansible_command_timeout` (segundos) evita que un `show` lento o una consola
  bloqueada cuelgue la tarea; `ansible_connect_timeout` controla el
  establecimiento de la sesión.
- En `httpapi` se añaden `ansible_httpapi_use_ssl: true`,
  `ansible_httpapi_validate_certs: true` y, si aplica, `ansible_httpapi_port`.
- En `netconf` se usa `ansible_port: 830` y normalmente `ansible_user` con
  clave SSH.

### 3.3. Ejemplos de inventario por tipo de conexión

**Cisco NX-OS por NX-API (HTTPS):**

```yaml
nxos:
  vars:
    ansible_connection: ansible.netcommon.httpapi
    ansible_network_os: cisco.nxos.nxos
    ansible_httpapi_use_ssl: true
    ansible_httpapi_validate_certs: true
    ansible_httpapi_port: 443
```

**Juniper por NETCONF:**

```yaml
junos:
  vars:
    ansible_connection: ansible.netcommon.netconf
    ansible_network_os: junipernetworks.junos.junos
    ansible_port: 830
```

**FortiGate por API:**

```yaml
fortigates:
  vars:
    ansible_connection: ansible.netcommon.httpapi
    ansible_network_os: fortinet.fortios.fortios
    ansible_httpapi_use_ssl: true
    ansible_httpapi_validate_certs: true
    ansible_httpapi_port: 443
```

**Palo Alto y F5 (módulos locales):**

```yaml
panorama:
  vars:
    ansible_connection: local
```

En estos casos la autenticación va en el parámetro `provider` de cada módulo,
no en variables de conexión.

### 3.4. Convivencia con `ansible.cfg` de ACC

El `ansible/ansible.cfg` actual fuerza:

```ini
[privilege_escalation]
become = True
become_method = sudo
```

Eso es correcto para Linux, pero **rompe un playbook de red**: intentaría `sudo`
en un equipo que no lo tiene. Todo playbook de red debe declarar
`become: false` a nivel de play y activar `enable` solo cuando haga falta:

```yaml
- name: Inventario de switches
  hosts: switches
  gather_facts: false
  become: false
```

Además, `host_key_checking = True` es el valor por defecto de ACC y también se
aplica a los equipos de red. Antes de la primera ejecución hay que poblar
`known_hosts` (o usar `ACC_SSH_HOST_KEYS_AUTO=1` de forma consciente) y
verificar la huella por un canal fuera de banda. Con
`ACC_SSH_HOST_KEYS_AUTO=0` (valor recomendado y por defecto), un equipo cuyo
host key no esté registrado fallará, que es el comportamiento seguro.

### 3.5. Requisitos en el controlador

- `ansible-core` (ACC usa la rama 2.19).
- Colección `ansible.netcommon` (connection plugins y módulos genéricos).
- Colección del fabricante (`cisco.ios`, `cisco.nxos`, `arista.eos`,
  `junipernetworks.junos`, `fortinet.fortios`, `community.routeros`…).
- `sshpass` solo si se autentica por contraseña; con claves SSH no hace falta.
- `ntc-templates` + `textfsm` si se quiere parsear salida CLI.
- Conectividad TCP al puerto de gestión (22, 830 o 443) y reglas de firewall
  que permitan al controlador llegar a la red de gestión.
- Reloj del controlador razonablemente sincronizado: TLS, NETCONF y las
  validaciones de certificados dependen de ello.

### 3.6. Instalación de colecciones

`requirements.yml` de ejemplo:

```yaml
---
collections:
  - name: ansible.netcommon
  - name: ansible.utils
  - name: cisco.ios
  - name: cisco.nxos
  - name: arista.eos
  - name: junipernetworks.junos
  - name: fortinet.fortios
  - name: community.routeros
```

Instalación en línea:

```bash
ansible-galaxy collection install -r requirements.yml -p ./collections
export ANSIBLE_COLLECTIONS_PATH="$PWD/collections"
```

Instalación sin red (build de ACC):

```bash
# En una máquina con acceso a Galaxy:
ansible-galaxy collection download -r requirements.yml -p vendor/collections

# En el build, con los .tar.gz ya copiados:
ansible-galaxy collection install vendor/collections/*.tar.gz \
  -p /usr/share/ansible/collections
```

En producción, **fija la versión** de cada colección en `requirements.yml` y
actualízala solo tras probar en laboratorio. Las colecciones cambian opciones y
nombres de módulos entre versiones mayores.

---

## 4. Colecciones por fabricante

| Fabricante / sistema | Colección | `ansible_network_os` | Conexión habitual |
|---|---|---|---|
| Cisco IOS / IOS-XE | `cisco.ios` | `cisco.ios.ios` | `network_cli` |
| Cisco NX-OS | `cisco.nxos` | `cisco.nxos.nxos` | `network_cli` o `httpapi` |
| Cisco IOS-XR | `cisco.iosxr` | `cisco.iosxr.iosxr` | `network_cli` |
| Cisco ASA | `cisco.asa` | `cisco.asa.asa` | `network_cli` |
| Arista EOS | `arista.eos` | `arista.eos.eos` | `network_cli` o `httpapi` |
| Juniper Junos | `junipernetworks.junos` | `junipernetworks.junos.junos` | `netconf` o `network_cli` |
| Fortinet FortiOS | `fortinet.fortios` | `fortinet.fortios.fortios` | `httpapi` |
| Palo Alto PAN-OS | `paloaltonetworks.panos` | — (módulos locales) | `local` |
| Check Point | `check_point.mgmt` | `check_point.mgmt.checkpoint` | `httpapi` |
| F5 BIG-IP | `f5networks.f5_modules` | — (módulos locales) | `local` |
| Aruba AOS-CX | `arubanetworks.aoscx` | `arubanetworks.aoscx.aoscx` | `network_cli` (API REST en módulos concretos) |
| Dell OS10 | `dellemc.os10` | `dellemc.os10.os10` | `network_cli` |
| Dell OS9 | `dellemc.os9` | `dellemc.os9.os9` | `network_cli` |
| Extreme EXOS | `extreme.exos` | `extreme.exos.exos` | `network_cli` |
| VyOS | `vyos.vyos` | `vyos.vyos.vyos` | `network_cli` |
| MikroTik RouterOS | `community.routeros` | `community.routeros.routeros` | `network_cli` |
| Genérico multi-fabricante | `ansible.netcommon` | según equipo | `network_cli` / `netconf` / `httpapi` |
| Filtros y validación | `ansible.utils` | — | local |

Colecciones **legacy** como `community.network` incluyen módulos para equipos
antiguos (Aruba AireOS, Brocade, Huawei CloudEngine, EdgeOS, etc.). Están sin
mantenimiento activo y muchas han sido sustituidas por colecciones de
fabricante; úsalas solo si no hay alternativa y con aislamiento de red.

---

## 5. Catálogo de operaciones

La clasificación de riesgo es la que debería usar ACC para decidir permisos,
aprobaciones y ventanas de mantenimiento.

| Nivel | Significado | Controles sugeridos |
|---|---|---|
| **R0 — Lectura** | No modifica el equipo | Sin aprobación; programable |
| **R1 — Bajo** | Cambio acotado y reversible | Vista previa + verificación |
| **R2 — Medio** | Afecta servicio de un puerto o segmento | Vista previa + aprobación + ventana |
| **R3 — Alto** | Puede cortar gestión, routing o HA | Doble aprobación + ventana + acceso de consola |
| **R4 — Crítico** | Reset, borrado o recuperación | Fuera del alcance de la herramienta |

### 5.1. Solo lectura (R0)

| Operación | Módulo típico (IOS) | Alternativa genérica |
|---|---|---|
| Inventario: modelo, serie, firmware, uptime | `cisco.ios.ios_facts` | `show version`, `show inventory` |
| Interfaces y contadores | `ios_facts` (`interfaces`) | `show interfaces`, `show interfaces status` |
| VLANs configuradas | `ios_vlans` con `state: gathered` | `show vlan brief` |
| Rutas | `ios_facts` / `ios_static_routes` (`gathered`) | `show ip route` |
| Vecinos LLDP/CDP | — | `show lldp neighbors`, `show cdp neighbors detail` |
| Tabla ARP / MAC | — | `show ip arp`, `show mac address-table` |
| Spanning tree | — | `show spanning-tree` |
| LAG / EtherChannel | `ios_lag_interfaces` (`gathered`) | `show etherchannel summary` |
| PoE | — | `show power inline` |
| NTP | `ios_ntp_global` (`gathered`) | `show ntp status`, `show ntp associations` |
| Syslog | `ios_logging_global` (`gathered`) | `show logging` |
| SNMP | `ios_snmp_server` (`gathered`) | `show snmp` |
| Usuarios y AAA | `ios_user` (`gathered`), `ios_config` | `show running-config \| include username` |
| Licencias y módulos | — | `show license`, `show module` |
| Entorno físico | — | `show environment all` |
| Configuración completa | `ios_config` con `backup: true` | `show running-config` |
| Guardada vs. en ejecución | — | `show startup-config` |

Lecturas que **no** conviene automatizar por CLI si el equipo tiene API: tablas
grandes (ARP, MAC, rutas completas) son más fiables y rápidas por NETCONF,
RESTCONF o SNMP.

### 5.2. Backup y auditoría de configuración (R0)

| Operación | Cómo |
|---|---|
| Copia de `running-config` antes de cambiar | `ios_config`/`nxos_config`/`eos_config`/`junos_config` con `backup: true` |
| Copia programada diaria | Playbook de solo lectura + programación de ACC |
| Hash y comparación | `hash('sha256')` en Jinja + `ansible.utils.fact_diff` |
| Detección de deriva | Comparar contra una configuración de referencia (`diff_against: intended`) |
| Auditoría de cumplimiento | `ansible.utils.validate` con un JSON Schema sobre los facts |
| Export de RouterOS | `community.routeros.command` con `/export` |
| Copia de imagen o ficheros | `ansible.netcommon.net_get` / `net_put` (SCP/SFTP) |

El backup debe guardarse **en el controlador**, no en el equipo, con fecha,
hostname, hash y versión. `ansible.builtin.copy` con `delegate_to: localhost`
es válido para volcar el texto; `net_put` es para transferir ficheros al
equipo.

### 5.3. Capa 2 (R1–R2)

| Operación | Módulo IOS | Riesgo |
|---|---|---|
| Crear, renombrar o borrar VLAN | `ios_vlans` | R1 (borrar una VLAN en uso es R2) |
| Asignar puerto access a una VLAN | `ios_l2_interfaces` | R1 |
| Configurar trunk y VLANs permitidas | `ios_l2_interfaces` | R2 |
| VLAN nativa | `ios_l2_interfaces` | R2 |
| Descripción y estado de puerto | `ios_interfaces` / `ios_config` | R1 |
| Velocidad, dúplex, MTU | `ios_interfaces` | R2 |
| LAG / EtherChannel | `ios_lag_interfaces`, `ios_lacp_interfaces` | R2 |
| Spanning tree (prioridad, PortFast, BPDU Guard) | `ios_config` | R2–R3 |
| Port security | `ios_config` | R2 |
| PoE | `ios_config` | R2 |
| LLDP | `ios_lldp_global`, `ios_lldp_interfaces` | R1 |

**Nunca** cambies a ciegas el puerto, la VLAN o el trunk por el que te conectas
al equipo. Un cambio de trunk o de VLAN de gestión puede dejar la sesión SSH
inalcanzable y obligar a consola.

### 5.4. Capa 3 y routing (R1–R3)

| Operación | Módulo IOS | Riesgo |
|---|---|---|
| IP en interfaz / SVI | `ios_l3_interfaces` | R2 (la SVI de gestión es R3) |
| Ruta estática | `ios_static_routes` | R2 |
| OSPF | `ios_ospfv2`, `ios_ospfv3` | R3 |
| BGP | `ios_bgp_global`, `ios_bgp_address_family` | R3 |
| VRF | `ios_vrf` | R3 |
| HSRP/VRRP | `ios_hsrp_interfaces`, `ios_config` | R3 |
| Route-map y prefix-list | `ios_route_maps`, `ios_prefix_lists` | R3 |
| Redistribución | `ios_config` | R3 |
| IPv6 | `ios_l3_interfaces`, `ios_ospfv3`, `ios_static_routes` | R2–R3 |

Un cambio de routing puede aislar una sede completa. La vista previa debe
mostrar el diff y, si es posible, el resultado de un `show ip route` posterior
comparado con el esperado.

### 5.5. Seguridad y control de acceso (R2–R3)

| Operación | Módulo IOS | Riesgo |
|---|---|---|
| ACL estándar/extendida | `ios_acls` | R2–R3 |
| ACL de gestión (SSH, SNMP, TACACS) | `ios_acls` | R3 |
| Usuarios locales | `ios_user` | R2 |
| AAA / TACACS+ / RADIUS | `ios_config` | R3 |
| Claves SSH y certificados | `ios_config`, `ios_user` | R2–R3 |
| Banner | `ios_banner` | R1 |
| SNMP (comunidades, traps) | `ios_snmp_server` | R2 |
| 802.1X | `ios_config` | R3 |
| Control-plane policing | `ios_config` | R3 |
| Zone-based firewall | `ios_config` | R3 |

Cualquier regla que afecte al plano de gestión (SSH, SNMP, AAA) debe probarse
primero con una sesión de consola abierta y una ventana de reversión.

### 5.6. Servicios de gestión (R1)

| Operación | Módulo IOS | Riesgo |
|---|---|---|
| Hostname | `ios_hostname` | R1 |
| NTP (servidores, autenticación) | `ios_ntp_global` | R1 |
| Syslog | `ios_logging_global` | R1 |
| DNS | `ios_config` | R1 |
| SNMP | `ios_snmp_server` | R2 |
| Banner | `ios_banner` | R1 |
| Dominio y resolución | `ios_config` | R1 |
| Reloj y zona horaria | `ios_config` | R1 |

### 5.7. Guardado, firmware y reinicio (R1–R3)

| Operación | Cómo | Riesgo |
|---|---|---|
| Guardar configuración | `ios_config` con `save_when: modified` | R1 |
| Guardar siempre | `save_when: always` | R1 |
| Copiar imagen al equipo | `net_put` (SCP) o `ios_command` con `copy` | R3 |
| Activar imagen y reiniciar | `ios_command` (`boot system`, `reload`) | R3 |
| Instalar sistema NX-OS | `nxos_install_os` | R3 |
| Verificar arranque | `show boot`, `show version` tras reconectar | R0 |
| Reiniciar | `ios_command: reload` | R3 |

El firmware es el caso más delicado: consume mucho tiempo, puede requerir
licencia, cambia el formato de configuración y a veces obliga a actualizar el
bootloader. No lo mezcles con otras tareas y no lo ejecutes en paralelo.

### 5.8. Otros equipos

| Equipo | Colección | Operaciones habituales |
|---|---|---|
| Firewall FortiGate | `fortinet.fortios` | Interfaces, políticas, rutas, NAT, VPN, NTP, syslog, administradores |
| Firewall Palo Alto | `paloaltonetworks.panos` | Objetos, reglas, NAT, zonas, interfaces, rutas, commit |
| Firewall Check Point | `check_point.mgmt` | Gateways, políticas, reglas, sesiones |
| Balanceador F5 | `f5networks.f5_modules` | Nodos, pools, virtual servers, iRules, sync |
| Wi-Fi Cisco AireOS | `community.network` (legacy) | WLAN, políticas, RF (sin mantenimiento) |
| Wi-Fi Cisco Catalyst 9800 | `cisco.ios` (IOS-XE) | Configuración general; lo específico de wireless suele ir por plantilla/CLI |
| Wi-Fi Aruba | `arubanetworks.aoscx` / legacy | Según modelo y sistema operativo |
| Switch Dell | `dellemc.os10`, `dellemc.os9` | Interfaces, VLAN, routing, usuarios |
| Switch Extreme | `extreme.exos` | VLAN, puertos, routing, usuarios |
| Router MikroTik | `community.routeros` | Interfaces, IP, rutas, firewall, usuarios |
| Router VyOS | `vyos.vyos` | Interfaces, rutas, firewall, NAT, usuarios |

---

## 6. Módulos y ejemplos

Los ejemplos usan Cisco IOS por ser el más extendido. Los nombres cambian de
prefijo en otras colecciones (`nxos_*`, `eos_*`, `junos_*`, `vyos_*`,
`fortios_*`), pero el patrón es el mismo.

### 6.1. Leer facts e inventario

```yaml
---
- name: Inventario de switches Cisco IOS
  hosts: switches
  gather_facts: false
  become: false
  connection: ansible.netcommon.network_cli

  tasks:
    - name: Leer facts del equipo
      cisco.ios.ios_facts:
        gather_subset:
          - all
      register: facts

    - name: Publicar resumen para ACC
      ansible.builtin.debug:
        msg: >-
          RED_STATE_B64={{ {
            'hostname': facts.ansible_facts.ansible_net_hostname,
            'model': facts.ansible_facts.ansible_net_model,
            'serial': facts.ansible_facts.ansible_net_serialnum,
            'version': facts.ansible_facts.ansible_net_version,
            'image': facts.ansible_facts.ansible_net_image,
            'interfaces': facts.ansible_facts.ansible_net_interfaces
          } | to_json | b64encode }}
```

`gather_facts: false` es obligatorio: el módulo `setup` de Ansible no puede
ejecutarse en un equipo de red.

### 6.2. Leer varios `show` sin cambiar nada

```yaml
---
- name: Estado operativo de switches
  hosts: switches
  gather_facts: false
  become: false

  tasks:
    - name: Ejecutar comandos de solo lectura
      ansible.netcommon.cli_command:
        command: "{{ item }}"
      loop:
        - show version
        - show interfaces status
        - show vlan brief
        - show ip route
        - show lldp neighbors
        - show spanning-tree summary
      register: salida
      changed_when: false

    - name: Publicar la salida
      ansible.builtin.debug:
        msg: >-
          RED_STATE_B64={{ {
            'commands': salida.results | map(attribute='stdout') | list
          } | to_json | b64encode }}
```

Ventaja de `cli_command`: funciona en cualquier equipo con `network_cli`, sin
depender de un módulo de recurso. Inconveniente: devuelve texto; si necesitas
datos, parsea con `cli_parse` o usa el módulo de facts del fabricante.

### 6.3. Parsear salida CLI con TextFSM

```yaml
---
- name: Convertir 'show ip interface brief' en datos
  hosts: switches
  gather_facts: false
  become: false

  tasks:
    - name: Parsear la tabla de interfaces
      ansible.utils.cli_parse:
        command: show ip interface brief
        parser:
          name: ansible.netcommon.ntc_templates
      register: interfaces

    - name: Mostrar el resultado estructurado
      ansible.builtin.debug:
        var: interfaces.parsed
```

Requiere `ntc-templates` y `textfsm` en el controlador. Si no están instalados,
`cli_parse` falla con un error de parser, no con un error del equipo.

### 6.4. Backup de configuración

**Opción A: módulo de configuración con `backup`.**

```yaml
---
- name: Respaldar la configuración antes de cambiar
  hosts: switches
  gather_facts: false
  become: true
  become_method: enable

  tasks:
    - name: Copiar running-config al controlador
      cisco.ios.ios_config:
        backup: true
        backup_options:
          dir_path: "{{ acc_backup_dir }}"
          filename: "{{ inventory_hostname }}_running.cfg"
```

**Opción B: comando + copia local (más portable).**

```yaml
---
- name: Respaldar configuración de red
  hosts: switches
  gather_facts: false
  become: false

  tasks:
    - name: Leer la configuración en ejecución
      ansible.netcommon.cli_command:
        command: show running-config
      register: running
      changed_when: false

    - name: Guardar la copia en el controlador
      ansible.builtin.copy:
        content: "{{ running.stdout }}\n"
        dest: "{{ acc_backup_dir }}/{{ inventory_hostname }}.cfg"
        mode: "0600"
      delegate_to: localhost

    - name: Publicar hash y tamaño
      ansible.builtin.debug:
        msg: >-
          RED_STATE_B64={{ {
            'file': inventory_hostname ~ '.cfg',
            'sha256': running.stdout | hash('sha256'),
            'bytes': running.stdout | length
          } | to_json | b64encode }}
```

`delegate_to: localhost` es imprescindible: `ansible.builtin.copy` escribe en
el controlador, no en el equipo. Para subir una imagen o un fichero al equipo,
usa `ansible.netcommon.net_put` (SCP/SFTP) o el `src` del módulo de
configuración.

### 6.5. Crear una VLAN y asignar un puerto access

```yaml
---
- name: Crear VLAN y asignar puerto
  hosts: switches
  gather_facts: false
  become: true
  become_method: enable
  serial: 1

  tasks:
    - name: Crear la VLAN 20
      cisco.ios.ios_vlans:
        config:
          - vlan_id: 20
            name: USUARIOS
            state: active
        state: merged

    - name: Asignar el puerto GigabitEthernet0/5 a la VLAN 20
      cisco.ios.ios_l2_interfaces:
        config:
          - name: GigabitEthernet0/5
            access:
              vlan: 20
        state: merged

    - name: Guardar la configuración si hubo cambios
      cisco.ios.ios_config:
        save_when: modified
```

`state: merged` añade o actualiza sin borrar el resto. `state: replaced`
sustituye la configuración del recurso y `state: deleted` la elimina; usa estos
dos con mucha precaución.

### 6.6. Trunk y VLANs permitidas

```yaml
---
- name: Configurar un trunk
  hosts: switches
  gather_facts: false
  become: true
  become_method: enable
  serial: 1

  tasks:
    - name: Definir el trunk y sus VLANs
      cisco.ios.ios_l2_interfaces:
        config:
          - name: GigabitEthernet0/1
            trunk:
              allowed_vlans:
                - "10"
                - "20"
                - "30"
              native_vlan: 99
        state: merged

    - name: Guardar si hubo cambios
      cisco.ios.ios_config:
        save_when: modified
```

Antes de aplicar, comprueba que el puerto no es el uplink de gestión y que la
VLAN nativa coincide en ambos extremos.

### 6.7. NTP, syslog y servicios de gestión

```yaml
---
- name: Servicios de gestión
  hosts: switches
  gather_facts: false
  become: true
  become_method: enable

  vars:
    ntp_server: 192.0.2.10
    syslog_server: 192.0.2.20

  tasks:
    - name: Configurar NTP y syslog
      cisco.ios.ios_config:
        lines:
          - "ntp server {{ ntp_server }} prefer"
          - "logging host {{ syslog_server }}"
        match: line

    - name: Guardar si hubo cambios
      cisco.ios.ios_config:
        save_when: modified
```

Para SNMP, usuarios o claves, usa variables de vault y `no_log: true`. Un
`debug` de la tarea puede exponer el secreto si no se protege.

### 6.8. Verificar idempotencia

```yaml
---
- name: Comprobar que la VLAN ya existe
  hosts: switches
  gather_facts: false
  become: true
  become_method: enable

  tasks:
    - name: Reaplicar la VLAN
      cisco.ios.ios_vlans:
        config:
          - vlan_id: 20
            name: USUARIOS
            state: active
        state: merged
      register: resultado

    - name: Fallar si la segunda aplicación cambió algo
      ansible.builtin.assert:
        that:
          - not resultado.changed
        fail_msg: "La configuración no es idempotente: revisar el módulo."
```

La idempotencia es la mejor prueba de que el módulo entiende el estado real y
no se limita a enviar comandos.

### 6.9. Diff antes de aplicar

```yaml
---
- name: Comparar configuración actual con la deseada
  hosts: switches
  gather_facts: false
  become: true
  become_method: enable
  check_mode: true
  diff: true

  tasks:
    - name: Mostrar el diff de la VLAN
      cisco.ios.ios_vlans:
        config:
          - vlan_id: 20
            name: USUARIOS
            state: active
        state: merged
      diff: true
```

No todos los módulos de red implementan `check_mode` con el mismo rigor. Si el
módulo no lo soporta, el `--check` puede devolver `changed` sin diff o incluso
no detectar el estado real. Para ACC es más fiable un **playbook de inspección
real** que un `--check` genérico.

### 6.10. Junos: `commit confirmed` y rollback

```yaml
---
- name: Cambio con reversión automática en Junos
  hosts: junos
  gather_facts: false
  become: false

  tasks:
    - name: Aplicar el cambio con confirmación de 5 minutos
      junipernetworks.junos.junos_config:
        lines:
          - set interfaces ge-0/0/1 description "ACC: uplink"
        comment: "ACC cambio 2026-11-10"
        confirm: 5
```

Si el operador no confirma el cambio antes de 5 minutos, Junos revierte solo.
La confirmación se hace en una **segunda ejecución**, cuando el servicio ya se
ha validado, con `junos_config` y `confirm_commit: true` (o con
`junos_command` ejecutando `commit`). Es el mejor mecanismo de red para cambios
de alto riesgo.

Rollback explícito a una revisión anterior:

```yaml
    - name: Volver a la revisión anterior
      junipernetworks.junos.junos_config:
        rollback: 1
```

### 6.11. NETCONF y RESTCONF

**NETCONF (lectura):**

```yaml
---
- name: Leer interfaces por NETCONF
  hosts: junos
  gather_facts: false

  tasks:
    - name: Obtener la configuración de interfaces
      ansible.netcommon.netconf_get:
        source: running
        filter: |
          <configuration>
            <interfaces/>
          </configuration>
      register: netconf
```

**NETCONF (escritura):**

```yaml
    - name: Enviar un cambio estructurado
      ansible.netcommon.netconf_config:
        content: |
          <config>
            <configuration>
              <system>
                <host-name>SW-CORE-01</host-name>
              </system>
            </configuration>
          </config>
```

**RESTCONF (lectura):**

```yaml
    - name: Consultar ietf-interfaces
      ansible.netcommon.restconf_get:
        path: /data/ietf-interfaces:interfaces
      register: rest
```

NETCONF y RESTCONF son preferibles a la CLI cuando el equipo los soporta:
devuelven estructuras YANG, permiten validar y son menos frágiles ante cambios
de formato de la salida.

### 6.12. Plantillas Jinja2

```yaml
---
- name: Aplicar una configuración generada
  hosts: switches
  gather_facts: false
  become: true
  become_method: enable
  serial: 1

  tasks:
    - name: Renderizar y aplicar la plantilla del equipo
      cisco.ios.ios_config:
        src: "templates/{{ inventory_hostname }}.j2"
        save_when: modified
```

La plantilla se renderiza en el controlador y el módulo envía solo las líneas
necesarias. Es potente para estandarizar cientos de equipos, pero peligrosa si
la plantilla contiene errores: valida con `ansible-playbook --syntax-check` y
con un equipo de laboratorio antes de producción.

### 6.13. Guardar, recargar y esperar

```yaml
    - name: Guardar la configuración
      cisco.ios.ios_config:
        save_when: modified

    - name: Recargar el equipo
      cisco.ios.ios_command:
        commands:
          - reload
      vars:
        ansible_command_timeout: 120

    - name: Esperar a que vuelva el puerto SSH
      ansible.builtin.wait_for:
        host: "{{ ansible_host | default(inventory_hostname) }}"
        port: 22
        delay: 60
        timeout: 900
      delegate_to: localhost
```

`reload` suele pedir confirmación; algunos módulos lo gestionan y otros no.
Prueba el procedimiento exacto en laboratorio y no lo ejecutes sin consola
disponible.

### 6.14. Concurrencia y fallos

```yaml
- name: Despliegue por lotes
  hosts: switches
  gather_facts: false
  serial: 5
  max_fail_percentage: 0
  any_errors_fatal: true
```

- `serial` controla cuántos equipos se cambian a la vez.
- `max_fail_percentage: 0` detiene el lote al primer fallo.
- `any_errors_fatal` aborta todo el play, no solo el host.
- `throttle` limita tareas concretas dentro de un play:

```yaml
    - name: Aplicar VLAN
      cisco.ios.ios_vlans:
        config:
          - vlan_id: 20
            name: USUARIOS
        state: merged
      throttle: 10
```

En red, un `forks` alto no siempre acelera: la CLI del equipo y la CPU del
controlador se convierten en el cuello de botella. Empieza con lotes de 5–10.

---

## 7. Patrón seguro: inspeccionar, comparar, aplicar, verificar

ACC ya usa este patrón para hora, tapiz, APT, contraseña y firewall. Para red
se traduce casi igual:

```text
1. Inspección de solo lectura  →  *_estado.yml
2. Comparación con lo deseado  →  diff / facts / plantilla
3. Vista previa al operador    →  resumen y riesgos
4. Aprobación (si aplica)      →  destructive / ventana / doble aprobación
5. Aplicación                  →  *.yml
6. Verificación posterior      →  releer estado y comparar
7. Registro                    →  before/after, comandos, hash, PDF
```

### 7.1. Playbook de inspección

Debe ser **estrictamente de lectura**:

- `gather_facts: false`.
- Solo `*_facts`, `*_command` con `changed_when: false`, `cli_command` o
  `netconf_get`.
- Sin `become: true` salvo que el `show` lo requiera.
- Sin `save`, sin `reload`, sin `configure`.
- Publica un marcador único (por ejemplo `RED_STATE_B64`) para que el runner lo
  decodifique.

### 7.2. Playbook de aplicación

- Valida la entrada con `assert` antes de tocar nada.
- Hace backup si el módulo lo permite.
- Aplica solo el recurso solicitado (no una configuración completa).
- Guarda la configuración (`save_when: modified`) cuando el cambio deba
  persistir.
- Relee el estado y calcula `RED_CHANGED` / `RED_VERIFIED`.
- Publica `RED_ERRORS` si la verificación no coincide.

### 7.3. Verificación

| Qué verificar | Cómo |
|---|---|
| El recurso existe | `*_facts`/`*_config state: gathered` o `show` |
| El valor es el esperado | Comparar JSON con `ansible.utils.fact_diff` |
| No hay cambios pendientes | Reaplicar y comprobar `changed == false` |
| La configuración persiste | Comparar `running-config` con `startup-config` o `save_when` |
| El equipo sigue accesible | `wait_for` al puerto 22/443 y un `show version` final |
| No hay errores en logs | `show logging` acotado a la ventana del cambio |

### 7.4. Rollback por fabricante

| Fabricante | Mecanismo | Notas |
|---|---|---|
| Juniper Junos | `commit confirmed`, `rollback N` | El más robusto; reversión automática si no se confirma |
| Cisco NX-OS | Checkpoints (`checkpoint`, `rollback running-config checkpoint`) | Requiere crear el checkpoint antes |
| Cisco IOS/IOS-XE | `configure replace` con archive, o restaurar backup | No hay rollback universal; depende de `archive` |
| Arista EOS | `configure replace`, sesiones de configuración | Verificar soporte en la versión |
| FortiOS | Revisiones y backup de configuración | Rollback manual desde GUI/CLI |
| RouterOS | Export/import de configuración | No hay rollback transaccional |
| VyOS | `commit`/`rollback` por CLI | Verificar el flujo en la versión |
| Palo Alto | Commit con `panos_commit`, revertir a versión anterior | Depende de Panorama/versiones |
| F5 | Sincronizar desde el par / restaurar UCS | Requiere cuidado con HA |

Antes de cualquier cambio R3, confirma **cómo se revierte** y prueba el
procedimiento en laboratorio. Un backup que nunca se ha restaurado no es un
plan de rollback.

### 7.5. Checklist previo a un cambio

1. ¿Tengo acceso de consola o fuera de banda al equipo?
2. ¿He hecho backup y lo he verificado (no está vacío, tiene el hash)?
3. ¿El cambio está limitado a un equipo o a un lote pequeño?
4. ¿He revisado el diff y sé exactamente qué líneas se van a enviar?
5. ¿El cambio afecta a la VLAN, interfaz o ACL por la que me conecto?
6. ¿Hay una ventana de mantenimiento y un responsable?
7. ¿Sé cómo revertir y cuánto tarda?
8. ¿La verificación posterior está definida antes de aplicar?
9. ¿Las credenciales son las mínimas necesarias y no se registran?
10. ¿El equipo tiene HA? ¿Cómo se comporta el failover durante el cambio?

---

## 8. Riesgos, límites y controles

### 8.1. Riesgos técnicos

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Corte de la sesión de gestión | Pérdida de acceso al equipo | Consola/OOB, no cambiar la VLAN/ACL de gestión sin ventana, `reload in` cuando aplique |
| Comando mal formado | Configuración inválida o parcial | Módulos declarativos, `assert`, laboratorio, diff |
| Timeout en un `show` largo | Tarea colgada o falsa | `ansible_command_timeout`, comandos acotados, paginación desactivada |
| Falta de idempotencia | Cambios repetidos o deriva | Módulos de recurso, `match`, reaplicar y comprobar `changed` |
| Firmware incompatible | Equipo que no arranca | Validar imagen, checksum, bootloader, consola, ventana |
| Límite de API | 429/errores intermitentes | `throttle`, reintentos, espaciado entre llamadas |
| Credenciales en logs | Exposición de secretos | `no_log`, vault, cuentas dedicadas, revisar `-vvv` |
| Configuración completa | Borra lo no incluido | Evitar `replace` salvo con backup y revisión línea a línea |
| HA mal coordinada | Split-brain o corte | Cambiar primero el standby, verificar sync, luego el activo |

### 8.2. Límites de Ansible

- **No es un daemon:** si quieres cumplimiento continuo, necesitas un
  planificador (AWX/Tower, cron, ACC) y ejecuciones periódicas.
- **No hay transacción multi-equipo:** cada host es independiente; un fallo a
  mitad deja estados distintos.
- **No hay rollback automático universal:** depende del fabricante.
- **`check mode` no es uniforme:** algunos módulos lo ignoran o lo implementan
  parcialmente.
- **El rendimiento es secuencial por host:** `network_cli` no paraleliza
  comandos dentro de un mismo equipo.
- **Las colecciones evolucionan:** una actualización puede cambiar opciones,
  nombres o comportamiento; fija versiones y prueba.
- **La API del fabricante manda:** si una función no está expuesta por API o
  módulo, Ansible tendrá que usar CLI o no podrá hacerlo.

### 8.3. Controles de seguridad

- Cuenta de **solo lectura** para inventario y backup; cuenta de cambio
  separada y con privilegio mínimo.
- Credenciales en `ansible-vault` o en el almacén cifrado de ACC; nunca en
  `group_vars` en claro ni en la línea de comandos.
- `no_log: true` en tareas con contraseñas, comunidades SNMP, claves o tokens.
- TACACS+/RADIUS con auditoría por usuario, no una cuenta compartida.
- Restringir el origen de gestión al host de ACC y a la red de administración.
- Registro de comandos enviados, diff, hash del backup y resultado.
- Doble aprobación para R2/R3 y ventana de mantenimiento.
- Prohibir `raw`, `shell` y `telnet` salvo excepción documentada.
- Revisar `show logging` y las sesiones abiertas tras cada cambio.

---

## 9. Qué necesitaría Ansible Control Center

### 9.1. Estado actual (punto de partida)

| Pieza | Situación |
|---|---|
| Plataformas | `models/ops.py`: `OS_FLAVORS = ("linux", "windows")` |
| Conexiones | `OS_CONNECTIONS = ("ssh", "winrm")` |
| Inventario | Grupos con `os_flavor`, `connection`, `ssh_key_path`; hosts con dirección y etiqueta |
| Runner | `_invoke_playbook` construye un inventario inline (`-i host,`) y pasa credenciales por fichero temporal 0600 |
| `ansible.cfg` | `become = True`, `become_method = sudo`, `host_key_checking = True` |
| Catálogo | Tarjetas con `platforms: ["linux"]` o `["linux", "windows"]` |
| Dependencias | `ansible-core` + `pywinrm`; sin colecciones de red |
| Imagen Docker | Build autárquico con wheels vendados; sin acceso a Galaxy |
| Pruebas | Unitarias con Ansible simulado; sin equipos reales |

### 9.2. Cambios por capa

| Capa | Cambio necesario |
|---|---|
| Modelo | Añadir `network` a `OS_FLAVORS` y `network_cli`/`netconf`/`httpapi` a `OS_CONNECTIONS`; añadir `network_os`, `port` y referencia de credenciales al grupo |
| Esquema | Migración v16 para las columnas nuevas (`network_os`, `port`, `credential_ref`); el resto de tablas no cambia |
| Inventario | Selector de plataforma «Red», campo de fabricante/SO, conexión, puerto y credencial de `enable`/API |
| Secretos | `enable_password`, token de API y contraseña de red en el almacén cifrado; nunca en SQLite en claro |
| Runner | Rama `is_network`: variables `ansible_connection`, `ansible_network_os`, `ansible_port`, `ansible_become_method=enable`, timeouts y `httpapi_*`; no pasar `ansible_python_interpreter`; no usar `backup_dir` de zona horaria |
| Playbooks | `red_estado.yml` (solo lectura), `red_backup.yml`, y playbooks por operación con verificación |
| Marcadores | `RED_BEFORE_B64`, `RED_STATE_B64`, `RED_CHANGED`, `RED_VERIFIED`, `RED_ERRORS` |
| Catálogo | Tarjetas con `platforms: ["network"]`, `actions` específicas y `destructive` según riesgo |
| UI | Campos de operación, IP/red, VLAN, puerto, diff y resumen; vista previa con el estado actual |
| Aprobaciones | Reutilizar `destructive`, criticidad y ventana; R2/R3 con doble aprobación |
| Programación | Solo R0 (inventario, backup); los cambios R2/R3, manuales |
| Informes | Antes/después, comandos aplicados, diff, hash del backup y resultado de verificación |
| Docker | Vendorizar colecciones y instalarlas en el build; añadir `ntc-templates`/`textfsm` si se parsea CLI |
| Pruebas | Validación, runner simulado, `--syntax-check` con colecciones y un laboratorio opcional (containerlab/GNS3) |

### 9.3. Cambios concretos en el runner

`_invoke_playbook` ya pasa las credenciales por un fichero temporal 0600, lo
que es correcto. Para red hay que añadir, sin romper Linux/Windows:

```python
if connection in {"network_cli", "netconf", "httpapi"}:
    payload["ansible_connection"] = f"ansible.netcommon.{connection}"
    payload["ansible_network_os"] = network_os
    payload["ansible_command_timeout"] = str(command_timeout)
    if enable_password:
        payload["ansible_become_method"] = "enable"
        payload["ansible_become_password"] = enable_password
    if connection == "httpapi":
        payload["ansible_httpapi_use_ssl"] = "true"
        payload["ansible_httpapi_validate_certs"] = "true"
```

No conviene forzar `ansible_become=false` en las variables: son variables de
conexión y tienen más precedencia que el `become` del play. Deja que cada
playbook declare `become: false` por defecto y active `become: true` solo en
las tareas que necesiten modo privilegiado.

Y el playbook debe declarar `become: false` para que el `ansible.cfg` global no
intente `sudo`. Si la operación requiere modo privilegiado, se activa
`become: true` con `become_method: enable` solo en las tareas que lo necesiten.

### 9.4. Cambios en la imagen Docker

El Dockerfile actual instala solo wheels de Python y no tiene colecciones. Para
red hay dos opciones:

1. **Vendorizar (recomendado para ACC):** descargar las colecciones en
   `vendor/collections/*.tar.gz`, copiarlas en la imagen e instalarlas con
   `ansible-galaxy collection install ... -p /usr/share/ansible/collections`.
2. **Permitir Galaxy en el build:** más simple, pero rompe el principio
   autárquico y hace el build dependiente de la red.

La opción 1 añade tamaño a la imagen (cada colección puede pesar decenas de MB)
y exige actualizar el paquete de despliegue (`dist/hora-admin-despliegue/`).

### 9.5. Fases sugeridas

| Fase | Contenido | Riesgo | Valor |
|---|---|---|---|
| 1 | Inventario de red (facts + `show` de solo lectura) | R0 | Alto: visibilidad real |
| 2 | Backup de configuración + hash + diff | R0 | Alto: recuperación y auditoría |
| 3 | NTP, syslog, SNMP, banner, hostname | R1 | Medio |
| 4 | VLAN, puertos access, rutas estáticas | R1–R2 | Alto |
| 5 | Trunk, LAG, OSPF/BGP, ACL, AAA | R2–R3 | Alto, con doble aprobación |
| 6 | Firmware y reinicio | R3 | Solo con procedimiento dedicado |

No mezcles fases en una misma release. Cada fase debe incluir su playbook de
inspección, su verificación y sus pruebas.

---

## 10. Primeras tarjetas recomendadas

### 10.1. «Inventario de red» (R0)

**Objetivo:** saber qué hay, qué versión corre y cómo está configurado, sin
tocar nada.

| Campo | Valor |
|---|---|
| Plataforma | `network` |
| Acción | `NETWORK_INVENTORY` |
| Conexión | `network_cli` (o `netconf`/`httpapi` según fabricante) |
| Permiso | `operator` (solo lectura) |
| Programable | Sí |
| Aprobación | No |
| Salida | JSON por host: modelo, serie, firmware, uptime, interfaces, VLANs, rutas, vecinos |

Comandos mínimos por IOS:

```text
show version
show inventory
show interfaces status
show ip interface brief
show vlan brief
show ip route
show lldp neighbors
show spanning-tree summary
```

El playbook publica `RED_STATE_B64` y el runner lo guarda en
`before_state`/`after_state` para la vista previa y el PDF.

### 10.2. «Backup de configuración» (R0)

**Objetivo:** copia diaria por equipo, con hash y comparación contra la
anterior.

| Campo | Valor |
|---|---|
| Plataforma | `network` |
| Acción | `NETWORK_BACKUP` |
| Permiso | `operator` o `admin` |
| Programable | Sí |
| Aprobación | No |
| Almacenamiento | `ACC_DATA_DIR/backups/network/<grupo>/<host>/<fecha>.cfg` |
| Retención | Configurable (por ejemplo, 30/90 días) |
| Verificación | Fichero no vacío, hash SHA-256, comparación con la copia anterior |

Comandos por fabricante:

| Fabricante | Comando |
|---|---|
| Cisco IOS/NX-OS | `show running-config` |
| Arista EOS | `show running-config` |
| Juniper Junos | `show configuration \| display set` |
| MikroTik RouterOS | `/export` |
| VyOS | `show configuration commands` |

La tarjeta debe marcar «cambio detectado» cuando el hash difiere del anterior,
sin aplicar nada. Es la señal más útil para auditar cambios manuales.

### 10.3. «VLAN de red» (R2, más adelante)

| Campo | Valor |
|---|---|
| Acción | `NETWORK_VLAN` |
| Operaciones | Crear/actualizar VLAN, asignar puerto access, definir trunk |
| Permiso | `admin` |
| Aprobación | Doble aprobación |
| Programable | No |
| Ventana | Obligatoria |
| Verificación | `ios_vlans state: gathered` + `ios_l2_interfaces state: gathered` |

No la implementes antes de tener inventario y backup funcionando: sin línea
base no se puede demostrar qué cambió.

---

## 11. Glosario

| Término | Significado |
|---|---|
| **Connection plugin** | Componente de Ansible que implementa el transporte (SSH, WinRM, NETCONF, HTTPAPI) |
| **`network_cli`** | Conexión SSH persistente que habla la CLI del equipo y desactiva la paginación |
| **`netconf`** | Protocolo estándar sobre SSH para configuración estructurada YANG |
| **`httpapi`** | Conexión HTTP/HTTPS para APIs de fabricante (NX-API, eAPI, FortiOS…) |
| **`ansible_network_os`** | Identifica el sistema operativo de red y selecciona el plugin de terminal/CLI |
| **Resource module** | Módulo declarativo que modela un recurso (VLAN, interfaz, ACL) y es idempotente |
| **`*_config`** | Módulo que envía líneas de configuración; idempotente si se usa bien |
| **`cli_command`** | Módulo genérico para ejecutar un comando de solo lectura |
| **`cli_config`** | Módulo genérico para enviar configuración sin modelo de recurso |
| **Facts** | Datos estructurados que devuelve el equipo (modelo, versión, interfaces…) |
| **Idempotencia** | Aplicar dos veces el mismo cambio produce el mismo estado y `changed: false` la segunda vez |
| **Check mode** | Simulación de Ansible; no está soportada igual en todos los módulos de red |
| **Commit confirmed** | Confirmación diferida de Junos que revierte automáticamente si no se valida |
| **Checkpoint** | Punto de restauración de NX-OS |
| **OOB** | Acceso fuera de banda (consola, puerto de gestión independiente) |
| **NETCONF/YANG** | Modelo estándar de configuración de red |
| **RESTCONF** | API REST sobre YANG |

---

## 12. Referencias

- Documentación de Ansible para redes:
  <https://docs.ansible.com/ansible/latest/network/index.html>
- `ansible.netcommon`:
  <https://docs.ansible.com/ansible/latest/collections/ansible/netcommon/>
- `cisco.ios`:
  <https://docs.ansible.com/ansible/latest/collections/cisco/ios/>
- `cisco.nxos`:
  <https://docs.ansible.com/ansible/latest/collections/cisco/nxos/>
- `arista.eos`:
  <https://docs.ansible.com/ansible/latest/collections/arista/eos/>
- `junipernetworks.junos`:
  <https://docs.ansible.com/ansible/latest/collections/junipernetworks/junos/>
- `fortinet.fortios`:
  <https://docs.ansible.com/ansible/latest/collections/fortinet/fortios/>
- `community.routeros`:
  <https://docs.ansible.com/ansible/latest/collections/community/routeros/>
- `ansible.utils`:
  <https://docs.ansible.com/ansible/latest/collections/ansible/utils/>
- Ansible Galaxy:
  <https://galaxy.ansible.com/>

---

*Documento de referencia. Las operaciones concretas dependen de la versión de
la colección, del modelo y del firmware del equipo. Valida siempre en
laboratorio antes de aplicar en producción.*
