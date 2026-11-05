# Despliegue con Docker

Este despliegue publica **solo Nginx** (HTTP/HTTPS). La aplicación Flask corre
en una red interna con Gunicorn y no expone su puerto al exterior.

```
Navegador ──HTTPS(443)──> nginx ──HTTP(8000)──> app (Gunicorn)
                            │
                            └── /etc/nginx/certs (autofirmado o real)
```

---

## 1. Requisitos

- Docker Engine 24+ y Docker Compose v2.
- Acceso al registro de imágenes (`python:3.12-slim`, `nginx:1.27-alpine`).
- Puertos 80 y 443 libres (o cámbialos en `.env.docker`).

---

## 2. Puesta en marcha

```bash
# 1. Variables de entorno
cp .env.docker.example .env.docker
# Edita .env.docker:
#   ACC_SECRET_KEY   -> openssl rand -hex 32
#   ACC_ADMIN_PASSWORD
#   TLS_CN / TLS_SAN        -> nombre o IP real de acceso

# 2. Construir y arrancar
docker compose --env-file .env.docker up -d --build

# 3. Estado
docker compose ps
docker compose logs -f app
```

Abre `https://localhost` (o la IP/nombre configurado). El certificado es
**autofirmado**: el navegador mostrará una advertencia que debes aceptar
manualmente.

> Si necesitas llevar el despliegue a otra PC, usa el empaquetado automático de
> la [sección 10](#10-llevar-el-despliegue-a-otra-pc-sin-registro).

> Con puertos personalizados (`ACC_HTTPS_PORT`) entra siempre por
> `https://host:<puerto>`: la redirección de HTTP a HTTPS no incluye el puerto
> no estándar.

### Certificado autofirmado

El contenedor de Nginx genera el certificado en el primer arranque dentro de
`./certs/`:

| Archivo | Contenido |
|---|---|
| `certs/fullchain.pem` | Certificado |
| `certs/privkey.pem` | Clave privada |

Ajusta `TLS_SAN` para que incluya la IP o el DNS de acceso:

```env
TLS_CN=acc.empresa.local
TLS_SAN=DNS:acc.empresa.local,IP:192.168.1.50
```

Para regenerarlo:

```bash
sudo rm -f certs/fullchain.pem certs/privkey.pem
docker compose --env-file .env.docker restart nginx
```

> El contenedor de Nginx corre como `root`, así que los dos `.pem` quedan con
> propietario `root`. Para regenerarlos hace falta `sudo`.

> `ACC_HSTS=0` está puesto a propósito mientras el certificado sea
> autofirmado. Actívalo solo con un certificado real y un dominio estable.

---

## 3. Primer administrador

Si `ACC_ADMIN_PASSWORD` está definida y aún no existe ningún usuario, el
entrypoint crea el administrador al arrancar. La contraseña debe tener al menos
12 caracteres y **solo se usa en el primer arranque**; después los cambios de esa
variable no pisan la contraseña existente.

Como alternativa a las variables de entorno (visibles con `docker inspect`),
la aplicación admite `ACC_SECRET_KEY_FILE`, `ACC_PASSWORD_PEPPER_FILE` y
`ACC_ADMIN_PASSWORD_FILE`. Apunta cada una al fichero de un Docker secret o de
una credencial montada; si existen a la vez, el valor directo tiene prioridad.

Para crearlo o rotarlo manualmente:

```bash
docker compose exec app flask --app app init-admin
```

Para comprobar cuántos usuarios hay:

```bash
docker compose exec app python -c \
  "from config import resolve_db_path; from models.user import User; \
   print(User.count(db_path=str(resolve_db_path())))"
```

---

## 4. Acceso SSH a los hosts

La app ejecuta Ansible desde el contenedor. Necesita:

- `sshpass` y `openssh-client` (ya incluidos en la imagen).
- Conectividad hacia los hosts (misma red/VPN).
- `known_hosts` con las claves de los hosts si
  `ACC_HOST_KEY_CHECKING=1` (valor por defecto).

Añade hosts al `known_hosts` persistente:

```bash
docker compose exec app sh -c \
  'ssh-keyscan 10.0.0.10 10.0.0.11 >> /home/acc/.ssh/known_hosts'
docker compose exec app chmod 600 /home/acc/.ssh/known_hosts
```

También puedes pasar las líneas al arrancar con `ACC_KNOWN_HOSTS`, o
desactivar la verificación de forma consciente:

```env
ACC_HOST_KEY_CHECKING=0
```

Las contraseñas candidatas se guardan cifradas con `ansible-vault` en el volumen
`acc_secrets`. La contraseña maestra se introduce en la interfaz y no se
persiste.

---

## 5. Volúmenes y copias de seguridad

| Volumen | Ruta | Contenido |
|---|---|---|
| `ansible-control-center_acc_data` | `/data` | SQLite, clave de sesión y pepper |
| `ansible-control-center_acc_secrets` | `/secrets` | Vault cifrado y temporales |
| `ansible-control-center_acc_logs` | `/logs` | Logs rotados |
| `ansible-control-center_acc_ssh` | `/home/acc/.ssh` | `known_hosts` |
| `./certs` (bind) | `/etc/nginx/certs` | Certificado TLS |

Copia de seguridad de SQLite y del vault:

```bash
docker compose stop app
docker run --rm \
  -v ansible-control-center_acc_data:/data:ro \
  -v ansible-control-center_acc_secrets:/secrets:ro \
  -v "$PWD/backup:/backup" \
  alpine tar czf /backup/ansible-control-center-$(date +%F).tgz -C / data secrets
docker compose start app
```

Detén la app antes de copiar SQLite en caliente para no arrastrar un WAL
inconsistente.

---

## 6. Actualizar

```bash
git pull
docker compose --env-file .env.docker build --pull
docker compose --env-file .env.docker up -d
```

Las migraciones de SQLite se aplican solas al arrancar. Haz copia de seguridad
antes de actualizar entre versiones.

---

## 7. Certificado real

1. Coloca el certificado y la clave en `./certs/`:
   - `certs/fullchain.pem`
   - `certs/privkey.pem`
2. Ajusta `TLS_CN`/`TLS_SAN` a tu dominio si hace falta.
3. Reinicia Nginx:

```bash
docker compose restart nginx
```

El script solo genera un certificado si faltan los archivos, así que no
sobrescribe los reales. Cuando el certificado sea de confianza, activa HSTS:

```env
ACC_HSTS=1
```

---

## 8. Operación y diagnóstico

```bash
# Salud de la app y de Nginx
curl -fk https://localhost/healthz

# Logs
docker compose logs -f app nginx

# Shell en el contenedor
docker compose exec app sh

# Reconciliar lotes huérfanos manualmente
docker compose exec app flask --app app reconcile-batches
```

| Síntoma | Causa probable | Solución |
|---|---|---|
| `403 Forbidden` al construir | Registro Docker bloqueado o sin login | Configura un mirror o `docker login` |
| `pull access denied` o límite de pulls | Límite anónimo de Docker Hub | Autentica con `docker login` y repite |
| `image not found` con `--no-build` | El `.tar` no se cargó o es de otra etiqueta | `docker load -i imagenes/ansible-control-center-images.tar` |
| Nginx no arranca | Certificado ausente/corrupto | Borra `certs/*.pem` (con `sudo`: los crea el contenedor) y reinicia nginx |
| `permission denied` en `/data` | Volumen con propietario incorrecto | Recrea el volumen o ajusta UID 10001 |
| Ansible no conecta | `known_hosts` vacío o red | Añade claves o revisa `HOST_KEY_CHECKING` |
| Progreso SSE se corta | Proxy/timeout | El timeout de Nginx es 360 s; revisa la red intermedia |

---

## 9. Notas de seguridad

- Solo Nginx publica puertos; la app no es accesible directamente.
- `ACC_COOKIE_SECURE=1` y `ACC_TRUST_PROXY=1` ya están fijados.
- Usa `ACC_ALLOWED_NETWORKS` para limitar los destinos.
- Mantén `ACC_DRY_RUN=1` hasta validar el laboratorio.
- El certificado autofirmado **no protege frente a un atacante en la red**:
  solo cifra el tráfico. Sustitúyelo por uno de una CA de confianza.
- No publiques `.env.docker`, `certs/privkey.pem` ni los volúmenes de secretos.

---

## 10. Llevar el despliegue a otra PC sin registro

Si la PC de destino no tiene acceso a Docker Hub (o no quieres publicar las
imágenes), empaqueta todo en el origen:

```bash
./docker/export-images.sh              # construye si hace falta
# ./docker/export-images.sh --build    # fuerza la reconstrucción con --pull
# ./docker/export-images.sh --no-build # usa las imágenes locales
```

El script deja una carpeta autocontenida (ignorada por git) en
`dist/hora-admin-despliegue/`, con los documentos numerados por orden
de lectura:

| Ruta | Contenido |
|---|---|
| `imagenes/ansible-control-center-images.tar` | Imágenes `ansible-control-center` y `ansible-control-center-nginx` |
| `imagenes/03-INFORMACION-IMAGENES.txt` | Arquitectura, fecha y digests de las imágenes |
| `docker-compose.yml` | Stack App + Nginx con TLS |
| `.env.docker.example` | Plantilla de configuración |
| `certs/` | Vacío; el certificado se genera al arrancar |
| `docker/` | Entrypoint, configuración de Nginx y el propio script |
| `Dockerfile` | Solo para reconstruir; no se usa con `--no-build` |
| `02-GUIA-COMPLETA-DESPLIEGUE.md` | Guía completa, autocontenida |
| `01-INICIO-RAPIDO.txt` | Pasos mínimos (punto de entrada) |
| `sha256sums.txt` | Integridad de todo el paquete |

Copia esa carpeta a la PC de destino y allí:

```bash
sha256sum -c sha256sums.txt
docker load -i imagenes/ansible-control-center-images.tar
cp .env.docker.example .env.docker   # ajusta claves, TLS_CN y TLS_SAN
docker compose --env-file .env.docker up -d --no-build
curl -fk https://localhost/healthz
```

No hace falta el código fuente: `--no-build` no lee el `Dockerfile`, así que el
paquete funciona aunque falten `app.py`, `models/` o `services/`.

Las imágenes deben ser para la misma arquitectura que la PC de destino.
Comprueba con:

```bash
docker image inspect ansible-control-center:latest --format '{{.Os}}/{{.Architecture}}'
```

Si no coincide, construye en el origen con `--platform linux/amd64` (o
`linux/arm64`) y carga ese mismo paquete en el destino.

> El primer arranque genera el certificado autofirmado y aplica las migraciones
> de SQLite. No copies volúmenes de otra instalación salvo que quieras restaurar
> sus datos.

Alternativa manual, si prefieres no usar el script:

```bash
docker compose --env-file .env.docker build
docker save ansible-control-center:latest ansible-control-center-nginx:latest -o ansible-control-center-images.tar
```
