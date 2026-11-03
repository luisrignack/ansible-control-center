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
#   HORA_ADMIN_SECRET_KEY   -> openssl rand -hex 32
#   HORA_ADMIN_ADMIN_PASSWORD
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

### Certificado autofirmado

El contenedor de Nginx genera el certificado en el primer arranque dentro de
`./certs/`:

| Archivo | Contenido |
|---|---|
| `certs/fullchain.pem` | Certificado |
| `certs/privkey.pem` | Clave privada |

Ajusta `TLS_SAN` para que incluya la IP o el DNS de acceso:

```env
TLS_CN=hora.empresa.local
TLS_SAN=DNS:hora.empresa.local,IP:192.168.1.50
```

Para regenerarlo:

```bash
rm -f certs/fullchain.pem certs/privkey.pem
docker compose --env-file .env.docker restart nginx
```

> `HORA_ADMIN_HSTS=0` está puesto a propósito mientras el certificado sea
> autofirmado. Actívalo solo con un certificado real y un dominio estable.

---

## 3. Primer administrador

Si `HORA_ADMIN_ADMIN_PASSWORD` está definida y aún no existe ningún usuario, el
entrypoint crea el administrador al arrancar. La contraseña debe tener al menos
12 caracteres y **solo se usa en el primer arranque**; después los cambios de esa
variable no pisan la contraseña existente.

Para crearlo o rotarlo manualmente:

```bash
docker compose exec app flask --app app init-admin
```

Para comprobar que hay usuarios:

```bash
docker compose exec app flask --app app init-db
```

---

## 4. Acceso SSH a los hosts

La app ejecuta Ansible desde el contenedor. Necesita:

- `sshpass` y `openssh-client` (ya incluidos en la imagen).
- Conectividad hacia los hosts (misma red/VPN).
- `known_hosts` con las claves de los hosts si
  `HORA_ADMIN_HOST_KEY_CHECKING=1` (valor por defecto).

Añade hosts al `known_hosts` persistente:

```bash
docker compose exec app ssh-keyscan 10.0.0.10 10.0.0.11 >> ~/.ssh/known_hosts
docker compose exec app chmod 600 ~/.ssh/known_hosts
```

También puedes pasar las líneas al arrancar con `HORA_ADMIN_KNOWN_HOSTS`, o
desactivar la verificación de forma consciente:

```env
HORA_ADMIN_HOST_KEY_CHECKING=0
```

Las contraseñas candidatas se guardan cifradas con `ansible-vault` en el volumen
`hora_secrets`. La contraseña maestra se introduce en la interfaz y no se
persiste.

---

## 5. Volúmenes y copias de seguridad

| Volumen | Ruta | Contenido |
|---|---|---|
| `hora-admin_hora_data` | `/data` | SQLite, clave de sesión y pepper |
| `hora-admin_hora_secrets` | `/secrets` | Vault cifrado y temporales |
| `hora-admin_hora_logs` | `/logs` | Logs rotados |
| `hora-admin_hora_ssh` | `/home/hora/.ssh` | `known_hosts` |
| `./certs` (bind) | `/etc/nginx/certs` | Certificado TLS |

Copia de seguridad de SQLite y del vault:

```bash
docker compose stop app
docker run --rm \
  -v hora-admin_hora_data:/data:ro \
  -v hora-admin_hora_secrets:/secrets:ro \
  -v "$PWD/backup:/backup" \
  alpine tar czf /backup/hora-admin-$(date +%F).tgz -C / data secrets
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
HORA_ADMIN_HSTS=1
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
| Nginx no arranca | Certificado ausente/corrupto | Borra `certs/*.pem` y reinicia nginx |
| `permission denied` en `/data` | Volumen con propietario incorrecto | Recrea el volumen o ajusta UID 10001 |
| Ansible no conecta | `known_hosts` vacío o red | Añade claves o revisa `HOST_KEY_CHECKING` |
| Progreso SSE se corta | Proxy/timeout | El timeout de Nginx es 360 s; revisa la red intermedia |

---

## 9. Notas de seguridad

- Solo Nginx publica puertos; la app no es accesible directamente.
- `HORA_ADMIN_COOKIE_SECURE=1` y `HORA_ADMIN_TRUST_PROXY=1` ya están fijados.
- Usa `HORA_ADMIN_ALLOWED_NETWORKS` para limitar los destinos.
- Mantén `HORA_ADMIN_DRY_RUN=1` hasta validar el laboratorio.
- El certificado autofirmado **no protege frente a un atacante en la red**:
  solo cifra el tráfico. Sustitúyelo por uno de una CA de confianza.
- No publiques `.env.docker`, `certs/privkey.pem` ni los volúmenes de secretos.
