# Despliegue de Ansible Control Center en otra PC (Docker)

Guía autocontenida para instalar Ansible Control Center en un equipo **sin el código
fuente** y **sin acceso a Docker Hub**. Solo necesitas esta carpeta.

```
Navegador ──HTTPS(443)──> nginx ──HTTP(8000)──> app (Gunicorn)
                            │
                            └── ./certs (autofirmado o real)
```

---

## 1. Contenido de la carpeta

| Ruta | Contenido |
|---|---|
| `imagenes/ansible-control-center-images.tar` | Imágenes `ansible-control-center` (app) y `ansible-control-center-nginx` |
| `imagenes/IMAGENES_INFO.txt` | Arquitectura, fecha de empaquetado y digests |
| `docker-compose.yml` | Stack: app Flask + Nginx con TLS |
| `.env.docker.example` | Plantilla de configuración (copiar a `.env.docker`) |
| `certs/` | Vacío; el certificado autofirmado se genera aquí al arrancar |
| `docker/` | Entrypoint y configuración de Nginx (ya dentro de la imagen) |
| `Dockerfile` | Solo si quieres reconstruir; no se usa con `--no-build` |
| `sha256sums.txt` | Sumas SHA-256 para comprobar la integridad del paquete |
| `LEEME.txt` | Resumen de los pasos (este documento amplía cada uno) |

---

## 2. Requisitos en la PC de destino

- Docker Engine 24+ y Docker Compose v2 (`docker compose version`).
- Puertos 80 y 443 libres (o cámbialos, ver §4).
- Arquitectura coincidente con la imagen (`IMAGENES_INFO.txt`).
- Conectividad del contenedor `app` hacia los equipos Ubuntu que se van a
  administrar (misma red o VPN) y salida SSH (22/tcp).

Comprueba la arquitectura:

```bash
docker info --format '{{.Architecture}}'
cat imagenes/IMAGENES_INFO.txt
```

---

## 3. Instalación en 5 pasos

```bash
# 0. Comprobar integridad (opcional)
sha256sum -c sha256sums.txt

# 1. Cargar las imágenes
docker load -i imagenes/ansible-control-center-images.tar

# 2. Crear la configuración
cp .env.docker.example .env.docker
chmod 600 .env.docker
openssl rand -hex 32        # copia el resultado en ACC_SECRET_KEY

# 3. Editar .env.docker
#    ACC_SECRET_KEY=<la clave del paso 2>
#    ACC_ADMIN_PASSWORD=<mínimo 12 caracteres>
#    ACC_ENV=production
#    ACC_ALLOWED_NETWORKS=<redes que puede administrar, p. ej. 10.0.0.0/8>
#    TLS_CN / TLS_SAN=<nombre o IP real de acceso>

# 4. Arrancar (sin reconstruir: no hace falta el código)
docker compose --env-file .env.docker up -d --no-build

# 5. Comprobar
docker compose ps
curl -fk https://localhost/healthz
```

Abre `https://localhost` (o el nombre/IP configurado) e inicia sesión con
`ACC_ADMIN_USERNAME` / `ACC_ADMIN_PASSWORD`.

> Con `ACC_ENV=production` (valor por defecto) la aplicación **no arranca** si
> `ACC_ALLOWED_NETWORKS` está vacía. Es una protección deliberada: indica qué
> redes puede administrar. Si solo quieres probar en local, usa
> `ACC_ENV=development`.

El certificado es **autofirmado**: el navegador mostrará una advertencia que
hay que aceptar una vez. `curl` necesita `-k` (o `--cacert certs/fullchain.pem`).

---

## 4. Configuración (`env.docker`)

| Variable | Por defecto | Descripción |
|---|---|---|
| `ACC_SECRET_KEY` | — | **Obligatoria.** Clave de sesión Flask |
| `ACC_ENV` | `production` | `production` activa las validaciones de seguridad (allowlist, clave y SSH) |
| `ACC_ADMIN_USERNAME` | `admin` | Usuario inicial |
| `ACC_ADMIN_PASSWORD` | — | Contraseña inicial (≥ 12 caracteres). Solo se usa si aún no hay usuarios |
| `ACC_DRY_RUN` | `1` | Modo simulación inicial. Después se controla con el interruptor **Simulación** de la interfaz |
| `ACC_HOST_KEY_CHECKING` | `1` | Verificación estricta de clave SSH |
| `ACC_ALLOWED_NETWORKS` | — | **Obligatoria con `ACC_ENV=production`.** Allowlist de redes destino, p. ej. `10.0.0.0/8,192.168.1.0/24` |
| `ACC_HSTS` | `0` | Déjalo en `0` con certificado autofirmado |
| `ACC_HTTP_PORT` | `80` | Puerto HTTP publicado por Nginx (redirige a HTTPS) |
| `ACC_HTTPS_PORT` | `443` | Puerto HTTPS publicado por Nginx |
| `TLS_CN` | `localhost` | Common Name del certificado |
| `TLS_SAN` | `DNS:localhost,IP:127.0.0.1` | Nombres/IPs válidos del certificado |
| `TLS_DAYS` | `825` | Validez del certificado en días |

> Si cambias `ACC_HTTPS_PORT`, entra siempre por
> `https://host:<puerto>`: la redirección de HTTP a HTTPS no arrastra el puerto
> no estándar.

Ajusta `TLS_CN`/`TLS_SAN` antes del primer arranque para que el certificado
cubra el nombre o IP reales:

```env
TLS_CN=acc.empresa.local
TLS_SAN=DNS:acc.empresa.local,IP:192.168.1.50
```

Cualquier otro ajuste de la aplicación (timeouts, límites, NTP, retención) se
pasa en `environment:` del `docker-compose.yml` o en `.env.docker`.

---

## 5. Certificado

El contenedor de Nginx genera el par en el primer arranque:

| Archivo | Contenido |
|---|---|
| `certs/fullchain.pem` | Certificado |
| `certs/privkey.pem` | Clave privada |

Para regenerarlo (los `.pem` los crea el contenedor como `root`):

```bash
sudo rm -f certs/fullchain.pem certs/privkey.pem
docker compose --env-file .env.docker restart nginx
```

Para usar un certificado real: coloca `fullchain.pem` y `privkey.pem` en
`certs/` y reinicia Nginx. El script no sobrescribe archivos existentes.
Cuando el certificado sea de confianza, activa HSTS:

```env
ACC_HSTS=1
```

---

## 6. Acceso SSH a los equipos objetivo

La app ejecuta Ansible desde el contenedor. Necesita `known_hosts` con las
claves de los hosts si `ACC_HOST_KEY_CHECKING=1`:

```bash
docker compose exec app sh -c \
  'ssh-keyscan 10.0.0.10 10.0.0.11 >> /home/acc/.ssh/known_hosts'
docker compose exec app chmod 600 /home/acc/.ssh/known_hosts
```

También puedes pasar las líneas al arrancar con `ACC_KNOWN_HOSTS`, o
desactivar la verificación de forma consciente
(`ACC_HOST_KEY_CHECKING=0`).

---

## 7. Datos, volúmenes y copias de seguridad

| Volumen | Ruta | Contenido |
|---|---|---|
| `ansible-control-center_acc_data` | `/data` | SQLite, clave de sesión y pepper |
| `ansible-control-center_acc_secrets` | `/secrets` | Vault cifrado (`ansible-vault`) |
| `ansible-control-center_acc_logs` | `/logs` | Logs rotados |
| `ansible-control-center_acc_ssh` | `/home/acc/.ssh` | `known_hosts` |
| `./certs` (bind) | `/etc/nginx/certs` | Certificado TLS |

Copia de seguridad (detén la app para no copiar un WAL inconsistente):

```bash
docker compose stop app
mkdir -p backup
docker run --rm \
  -v ansible-control-center_acc_data:/data:ro \
  -v ansible-control-center_acc_secrets:/secrets:ro \
  -v "$PWD/backup:/backup" \
  alpine tar czf /backup/ansible-control-center-$(date +%F).tgz -C / data secrets
docker compose start app
```

---

## 8. Actualizar a un paquete nuevo

```bash
docker compose down                     # conserva los volúmenes
# copia el nuevo contenido del paquete sobre esta carpeta
docker load -i imagenes/ansible-control-center-images.tar
docker compose --env-file .env.docker up -d --no-build
```

Las migraciones de SQLite se aplican solas al arrancar y son no destructivas.
Haz copia de seguridad antes de actualizar.

---

## 9. Operación y diagnóstico

```bash
curl -fk https://localhost/healthz         # salud a través de Nginx
docker compose ps                          # estado y healthchecks
docker compose logs -f app nginx           # logs
docker compose exec app sh                 # shell en el contenedor
docker compose exec app flask --app app reconcile-batches   # lotes huérfanos
```

| Síntoma | Causa probable | Solución |
|---|---|---|
| `image not found` con `--no-build` | El `.tar` no se cargó | `docker load -i imagenes/ansible-control-center-images.tar` |
| `port is already allocated` | 80/443 ocupados | Cambia `ACC_HTTP_PORT`/`ACC_HTTPS_PORT` |
| Nginx no arranca | Certificado corrupto | `sudo rm -f certs/*.pem` y reinicia nginx |
| Aviso de certificado en el navegador | Autofirmado | Acéptalo una vez o instala uno real |
| `permission denied` en `/data` | Volumen con propietario incorrecto | Recrea el volumen (`docker compose down -v` **borra datos**) o ajusta UID 10001 |
| Ansible no conecta | `known_hosts` vacío o red | Añade claves o revisa `ACC_HOST_KEY_CHECKING` |
| Progreso SSE se corta | Proxy/timeout intermedio | El timeout de Nginx es 360 s |
| Lote queda `running` | Proceso reiniciado | Se marca `error` al arrancar; usa `reconcile-batches` |
| NTP revierte el cambio | Política `keep` con NTP activo | Usa `off`, `restore` o `configure` |

---

## 10. Notas de seguridad

- Solo Nginx publica puertos; la app queda en la red interna `backend`.
- `ACC_COOKIE_SECURE=1` y `ACC_TRUST_PROXY=1` ya vienen fijados en
  el compose: no los cambies detrás de este Nginx.
- Mantén `ACC_DRY_RUN=1` hasta validar el flujo en laboratorio. Después, un
  administrador puede cambiar el modo desde el interruptor **Simulación**; el
  estado se guarda en SQLite y sobrevive a los reinicios.
- El certificado autofirmado **cifra** el tráfico pero no protege frente a un
  atacante en la red: sustitúyelo por uno de una CA de confianza.
- No copies fuera de este equipo `.env.docker`, `certs/privkey.pem` ni los
  volúmenes `acc_secrets`/`acc_data`.
- Roles `admin`, `operator` y `viewer` (sin MFA). Usa `ACC_ALLOWED_NETWORKS`
  para acotar los destinos.

---

## 11. Reconstruir las imágenes en esta PC (opcional)

Solo si tienes el código fuente del proyecto y Docker Hub accesible:

```bash
docker compose --env-file .env.docker build --pull
docker compose --env-file .env.docker up -d --build
```

En el equipo de origen, `./docker/export-images.sh` regenera este paquete.
