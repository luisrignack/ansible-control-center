# Cloudflare WARP con `warp-cli` en Ubuntu

Guía operativa para instalar, activar, desactivar y forzar el cambio de IP de
Cloudflare WARP en Ubuntu 26.04 LTS («resolute»).

> Verificado en este equipo: `cloudflare-warp 2026.8.2115.0`, servicio
> `warp-svc` activo, cliente `warp-cli 2026.8.2115.0`.

## 1. Qué es y qué no es

WARP es un túnel **WireGuard** (con reserva MASQUE/HTTP-3) desde **todo el
sistema** hasta el centro de datos de Cloudflare más cercano.

| Sí hace | No hace |
| --- | --- |
| Cifra todo el tráfico, no solo el navegador | No es una VPN de privacidad: Cloudflare ve el tráfico |
| Sustituye el DNS del ISP por el de Cloudflare | No permite elegir país ni servidor |
| Oculta tu IP real a los servicios que visitas | No garantiza anonimato |
| Kill switch mediante `warp-svc` y reglas de rutas | No elude censura de forma fiable (bloqueado en varios países) |

## 2. Instalación (repo oficial)

El repositorio de Cloudflare publica suite para `resolute`, así que no hace
falta forzar codename antiguo.

```bash
# Clave GPG del repositorio
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg -o /tmp/cf-warp.gpg
sudo gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg /tmp/cf-warp.gpg

# Fuente con signed-by (nunca trusted=yes)
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ resolute main" \
  | sudo tee /etc/apt/sources.list.d/cloudflare-client.list

# Instalar
sudo apt-get update
sudo apt-get install -y cloudflare-warp
```

Para comprobar qué suites publica Cloudflare:

```bash
for d in resolute questing noble jammy; do
  echo "$d -> $(curl -s -o /dev/null -w '%{http_code}' https://pkg.cloudflareclient.com/dists/$d/Release)"
done
```

Tras instalar, el servicio queda habilitado en el arranque:

```bash
systemctl is-active warp-svc    # active
systemctl is-enabled warp-svc   # enabled
dpkg -l cloudflare-warp | tail -1
```

## 3. Alta inicial (una sola vez)

`warp-cli` es solo el mando a distancia: habla por socket local con el demonio
`warp-svc`, que es quien crea el túnel. Por eso **no hace falta `sudo`**.

Los Términos de Servicio deben aceptarse de forma explícita. Solo es necesario
la primera vez; después queda guardado y `--accept-tos` es opcional.

```bash
warp-cli --accept-tos registration new     # crea la cuenta anónima (sin email)
warp-cli --accept-tos status
```

Sin `--accept-tos`, cualquier comando responde:

```
Please accept the WARP Terms of Service by running this command in a TTY
or by passing the --accept-tos flag.
```

## 4. Activar WARP

```bash
warp-cli --accept-tos connect
sleep 6
warp-cli --accept-tos status
```

Salida esperada:

```
Status update: Connected
Network: healthy
```

Comprobación de que existe el túnel:

```bash
ip -br link show | grep -i cloudflare
# CloudflareWARP  UNKNOWN  <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP>
```

## 5. Desactivar WARP

```bash
warp-cli --accept-tos disconnect
warp-cli --accept-tos status       # Status update: Disconnected
```

Niveles de «apagado», de menos a más profundo:

| Nivel | Comando | Efecto |
| --- | --- | --- |
| Desconectar | `warp-cli --accept-tos disconnect` | Cierra el túnel; el servicio sigue corriendo |
| Parar el servicio | `sudo systemctl stop warp-svc` | Libera el demonio y el resolver local |
| Impedir el arranque | `sudo systemctl disable warp-svc` | No se inicia en el próximo boot |
| Borrar el registro | `warp-cli --accept-tos registration delete` | Elimina la cuenta anónima y su clave |
| Desinstalar | `sudo apt-get purge cloudflare-warp` | Quita paquete, servicio y cliente |

## 6. Verificar la IP y compararla

### 6.1 IP de salida

```bash
# Antes de conectar
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E "^(ip|loc|colo|warp)="

# Conectar y volver a medir
warp-cli --accept-tos connect && sleep 6
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E "^(ip|loc|colo|warp)="
curl -4 -s https://api.ipify.org; echo      # egress IPv4
```

Ejemplo real medido en este equipo:

| Momento | IP de salida | `warp` | Colo |
| --- | --- | --- | --- |
| Desconectado | `152.206.239.234` (ISP) | `off` | — |
| Conectado | `104.28.244.67` + IPv6 `2a09:bac1:1b60:10::11:205` | `on` | `MIA` (Miami) |

`warp=off` / `warp=on` es el indicador fiable de si el túnel está en uso.

### 6.2 Fuga de DNS

```bash
resolvectl status | grep -E "Current DNS Server|DNS Servers"
dig +short TXT whoami.ds.akahelp.net
```

Con WARP activo el resolver pasa a `127.0.2.2` / `127.0.2.3` (locales de WARP) y
`whoami.ds.akahelp.net` devuelve una IP de Cloudflare (por ejemplo
`108.162.213.204`). Si devuelve la IP de tu router o de tu ISP, hay fuga.

En Firefox, el DNS over HTTPS propio del navegador **ignora** el DNS del
sistema: si quieres que respete el de WARP, desactiva DoH en
*Ajustes → Privacidad y seguridad*.

### 6.3 Fuga de IPv6

```bash
curl -6 -s https://api.ipify.org || echo "sin salida IPv6"
```

WARP transporta IPv6 propio de Cloudflare. Si aparece tu IPv6 original del ISP,
el túnel no está cubriendo ese tráfico.

## 7. Cambiar la IP

WARP **no permite elegir país**: siempre asigna el centro más cercano. Lo que sí
se puede hacer es pedir una IP distinta dentro del rango de Cloudflare.

### 7.1 Cambio simple (reconectar)

```bash
warp-cli --accept-tos disconnect
sleep 2
warp-cli --accept-tos connect
sleep 6
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E "^ip="
```

Muchas veces reutiliza la misma IP del pool; no está garantizado que cambie.

### 7.2 Cambio forzado (registro nuevo)

Garantiza una identidad y una IP nuevas, pero pierde el registro anterior:

```bash
warp-cli --accept-tos registration delete
warp-cli --accept-tos registration new
warp-cli --accept-tos connect
sleep 6
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E "^(ip|warp)="
```

### 7.3 Cambiar de país o de región

No es posible con WARP Free. `loc=` seguirá indicando tu país real aunque la IP
sea de Cloudflare (comprobado: IP de Miami con `loc=CU`). Para aparentar otro
país hace falta una VPN con selección de servidor; en este equipo están
disponibles **Proton VPN** (país automático dentro de 10 en el plan gratuito) y
**Windscribe** (permite elegir ubicación). No ejecutes dos clientes VPN a la vez:
se pelean por las rutas y el kill switch.

## 8. Modos de funcionamiento

```bash
warp-cli --accept-tos settings      # muestra el modo actual (línea "Mode:")
warp-cli --accept-tos mode doh      # solo cifra DNS, sin túnel
warp-cli --accept-tos mode warp     # túnel completo (por defecto)
```

En esta versión el subcomando es `mode` (el antiguo `set-mode` ya no existe) y
exige el argumento: `warp-cli mode` sin valor devuelve error. Para consultar el
modo en uso se usa `settings`.

| Modo | Efecto |
| --- | --- |
| `warp` | Túnel completo con DNS proxificado por UDP (por defecto) |
| `doh` | Sin túnel: solo DNS over HTTPS; conserva tu IP real |
| `warp+doh` | Túnel con DNS over HTTPS |
| `dot` | Sin túnel: solo DNS over TLS |
| `warp+dot` | Túnel con DNS over TLS |
| `proxy` | Túnel expuesto como proxy SOCKS5 local |
| `tunnel_only` | Túnel sin proxificar DNS |

Filtro de malware (bloquea dominios maliciosos):

```bash
warp-cli --accept-tos dns families off       # sin filtro
warp-cli --accept-tos dns families malware   # bloquea malware
warp-cli --accept-tos dns families full      # malware + contenido adulto
```

## 9. Diagnóstico

```bash
warp-cli --accept-tos status          # estado y motivo de la desconexión
warp-cli --accept-tos tunnel stats
journalctl -u warp-svc -n 50 --no-pager
systemctl status warp-svc
```

Motivos de estado vistos y su significado:

| `Reason` | Interpretación |
| --- | --- |
| `Manual Disconnection` | Alguien lanzó `disconnect` |
| `Settings Changed` | Se cambió modo, DNS o configuración y el túnel se reinició |
| `Unable to connect` | Red bloquea el handshake WireGuard/MASQUE |

Si el túnel no levanta:

1. `systemctl restart warp-svc` y reintentar `connect`.
2. Revisar que no haya otra VPN activa: `ip -br link | grep -E "tun|wg|proton"`.
3. Probar `warp-cli --accept-tos mode doh` para aislar si el problema es el
   túnel o solo el DNS.
4. En redes que bloquean WARP (frecuente en algunos países y operadores), el
   handshake no completa aunque el registro sea correcto: no es un fallo de
   instalación.

## 10. Referencia rápida

```bash
# Estado
warp-cli --accept-tos status

# Activar / desactivar
warp-cli --accept-tos connect
warp-cli --accept-tos disconnect

# IP actual y si el túnel está en uso
curl -s https://www.cloudflare.com/cdn-cgi/trace | grep -E "^(ip|loc|colo|warp)="

# Forzar IP nueva
warp-cli --accept-tos disconnect && warp-cli --accept-tos connect

# Reset completo del registro
warp-cli --accept-tos registration delete && warp-cli --accept-tos registration new

# Modo y ajustes
warp-cli --accept-tos settings
warp-cli --accept-tos mode doh

# Servicio
sudo systemctl restart warp-svc
journalctl -u warp-svc -n 50 --no-pager
```
