# Despliegue de Ansible Control Center en otra PC (Docker)

Guía autocontenida para instalar Ansible Control Center en un equipo **sin el código
fuente** y **sin acceso a Docker Hub**. Solo necesitas esta carpeta.

**Orden de lectura del paquete**: `01-INICIO-RAPIDO.txt` (pasos mínimos) →
`02-GUIA-COMPLETA-DESPLIEGUE.md` (este documento, amplía cada paso) →
`imagenes/03-INFORMACION-IMAGENES.txt` (metadatos de las imágenes).
Este archivo sustituye al antiguo `DESPLIEGUE.md` y ya no repite los pasos
rápidos; para la arrancada resume mira `01`.

```
Navegador ──HTTPS(443)──> nginx ──HTTP(8000)──> app (Gunicorn)
                            │
                            └── ./certs (autofirmado o real)
```

---

## 1. Contenido de la carpeta

| Ruta | Contenido |
|---|---|
| `01-INICIO-RAPIDO.txt` | Los 5 pasos mínimos (empieza aquí) |
| `02-GUIA-COMPLETA-DESPLIEGUE.md` | Esta guía |
| `imagenes/ansible-control-center-images.tar` | Imágenes `ansible-control-center` (app) y `ansible-control-center-nginx` |
| `imagenes/03-INFORMACION-IMAGENES.txt` | Arquitectura, fecha de empaquetado y digests |
| `docker-compose.yml` | Stack: app Flask + Nginx con TLS |
| `.env.docker.example` | Plantilla de configuración (copiar a `.env.docker`) |
| `certs/` | Vacío; el certificado autofirmado se genera aquí al arrancar |
| `docker/` | Entrypoint y configuración de Nginx (ya dentro de la imagen) |
| `Dockerfile` | Solo si quieres reconstruir; no se usa con `--no-build` |
| `sha256sums.txt` | Sumas SHA-256 para comprobar la integridad del paquete |

---

## 2. Requisitos en la PC de destino

- Docker Engine 24+ y Docker Compose v2 (`docker compose version`).
- Puertos 80 y 443 libres (o cámbialos, ver §4).
- Arquitectura coincidente con la imagen
  (`imagenes/03-INFORMACION-IMAGENES.txt`).
- Conectividad del contenedor `app` hacia los equipos objetivo (misma red o
  VPN) y salida SSH (22/tcp) para Linux.
- Para estaciones Windows: WinRM accesible (5985/tcp con NTLM o 5986/tcp con
  TLS) y una cuenta con permisos de administrador local. `pywinrm` ya está
  incluido en la imagen.
- Las conexiones son **salientes**: en la PC de destino solo hay que publicar
  80/443 para la interfaz; no se abren puertos hacia los equipos objetivo.

Comprueba la arquitectura:

```bash
docker info --format '{{.Architecture}}'
cat imagenes/03-INFORMACION-IMAGENES.txt
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
| `ACC_LDAP_ENABLED` | `0` | Estado **inicial** del acceso por LDAPS; después se conmuta en la web (`/usuarios`, solo admin local) |
| `ACC_LDAP_URL` | `ldaps://lds.etecsa.cu:636` | URL del directorio corporativo |
| `ACC_LDAP_BIND_DN` | `uid=rouser,ou=Special Users,dc=etecsa,dc=cu` | Usuario de solo lectura para buscar |
| `ACC_LDAP_BIND_PASSWORD` / `_FILE` | — | Contraseña del bind (preferible `ACC_LDAP_BIND_PASSWORD_FILE` como secret) |
| `ACC_LDAP_USER_BASE` | `ou=etecsa.cu,ou=people,dc=etecsa,dc=cu` | Base de búsqueda de usuarios |
| `ACC_LDAP_USER_FILTER` | `(objectClass=person)` | Filtro de búsqueda |
| `ACC_LDAP_USER_SCOPE` | `onelevel` | `onelevel` o `subtree` |
| `ACC_LDAP_USERNAME_ATTR` | `uid` | Atributo que contiene el nombre de usuario |
| `ACC_LDAP_TLS_VERIFY` | `1` | Validar el certificado TLS del directorio |
| `ACC_LDAP_TLS_CA_FILE` | CA del sistema | Archivo de la CA del directorio (monta el CRT) |
| `ACC_LDAP_DEFAULT_ROLE` | `viewer` | Rol de la cuenta en el primer login corporativo |

> Si cambias `ACC_HTTPS_PORT`, entra siempre por
> `https://host:<puerto>`: la redirección de HTTP a HTTPS no arrastra el puerto
> no estándar.

### Variables operativas adicionales

| Variable | Por defecto | Descripción |
|---|---|---|
| `ACC_MAX_PASSWORD_ATTEMPTS` | `3` | Contraseñas candidatas que se prueban por host si el playbook falla |
| `ACC_BATCH_TIME_BUDGET` | `3600` | Presupuesto de tiempo por lote (segundos); los hosts pendientes quedan `skipped` |
| `ACC_RECOVERY_STALE_MINUTES` | `15` | Minutos sin actividad para considerar huérfana una tarea o lote |
| `ACC_APPROVAL_TTL_MINUTES` | `30` | Vigencia de una aprobación de tarea destructiva |
| `ACC_PLAYBOOK_TIMEOUT` | `300` | Timeout por playbook (segundos) |
| `ACC_MAX_HOSTS` | `256` | Máximo de hosts por operación |
| `ACC_WINRM_PORT` | `5985` | Puerto WinRM de las estaciones Windows |
| `ACC_WINRM_TRANSPORT` | `ntlm` | `ntlm`, `basic` o `credssp` |
| `ACC_WINRM_SERVER_CERT_VALIDATION` | `validate` | Validación del certificado WinRM; no lo desactives en producción |
| `ACC_REQUEST_METRICS` | `1` | Métricas de rendimiento en `/metricas` (solo admin) |
| `ACC_SLOW_REQUEST_MS` | `1000` | Umbral de petición lenta para el log |

El resto de parámetros (timeouts de red, NTP, retención, notificaciones SMTP)
están comentados en `.env.docker.example` o en `docker-compose.yml`.

Ajusta `TLS_CN`/`TLS_SAN` antes del primer arranque para que el certificado
cubra el nombre o IP reales:

```env
TLS_CN=acc.empresa.local
TLS_SAN=DNS:acc.empresa.local,IP:192.168.1.50
```

Las variables de esta guía ya están mapeadas en `docker-compose.yml`, así que
basta definirlas en `.env.docker`. Para cualquier otro parámetro de `config.py`
que no aparezca, añádelo a `environment:` del compose (o usa un
`docker-compose.override.yml`) y recrea el contenedor.

### Acceso por directorio (LDAPS, opcional)

La app autentica con la cuenta corporativa además del admin local. El
**admin local siempre entra igual** y es quien otorga roles: los usuarios
LDAPS entran con rol `viewer` en su primer login y deben ser promovidos a
`operator`/`admin` desde la vista **Usuarios** de la aplicación.

1. Contraseña del binding de solo lectura en un archivo del volumen
   `/secrets` (nunca en el `.env.docker` en claro):

   ```bash
   # En el host que ejecuta Docker (ajusta el nombre del volumen si el
   # proyecto compose no es "ansible-control-center"):
   echo 'contraseña-del-rouser' \
     | sudo tee /var/lib/docker/volumes/ansible-control-center_acc_secrets/_data/acc_ldap_bind_password
   sudo chmod 600 /var/lib/docker/volumes/ansible-control-center_acc_secrets/_data/acc_ldap_bind_password
   ```

2. Certificado del directorio (si TLS con CA propia) en
   `..._acc_secrets/_data/lds-ca.crt`.

3. En `.env.docker` descomenta el bloque `ACC_LDAP_*` (URL del directorio,
   bind DN, base de búsqueda, `ACC_LDAP_BIND_PASSWORD_FILE=/secrets/acc_ldap_bind_password`,
   `ACC_LDAP_TLS_CA_FILE=/secrets/lds-ca.crt`). Todas las variables ya están
   mapeadas en el `docker-compose.yml` del paquete.

4. `docker compose --env-file .env.docker up -d` y, en la web:
   **Usuarios → “Activar acceso LDAPS”** (solo lo ve y lo usa el admin
   local; un usuario LDAP promovido a admin no puede conmutar el acceso).
   Al activar, la app **prueba el bind del directorio en ese momento**: si
   el servidor no acepta la conexión (URL, bind, contraseña o CA mal
   puestos, o firewall), el interruptor se queda en OFF con el motivo en
   pantalla. El estado queda en la base de datos: los cambios posteriores
   no requieren reiniciar el contenedor. Comprueba con el badge
   “LDAPS activo” de la misma vista.

> Requisito de red: el contenedor `app` debe alcanzar el puerto **636** del
> servidor LDAPS. Si el firewall lo bloquea, el login corporativo mostrará
> “Servidor LDAP no disponible”; el admin local sigue entrando con
> normalidad.

### Cuentas locales y roles

En **Usuarios** (`/usuarios`, solo `admin`) se gestionan las cuentas locales
además de las corporativas:

- **Crear**: «Nuevo usuario local» pide usuario, contraseña, confirmación, rol
  y estado inicial. El nombre admite letras, números, punto, guion, guion bajo
  o `@`, sin espacios, y no puede repetir una cuenta existente. La contraseña
  debe tener al menos `ACC_ADMIN_PASSWORD_MIN_LENGTH` caracteres (12 por
  defecto).
- **Cambiar contraseña**: botón «Contraseña» de la fila. Sustituye el hash,
  desbloquea la cuenta si estaba bloqueada por intentos fallidos y no cierra
  las sesiones ya abiertas.
- **Eliminar**: botón «Eliminar», escribiendo el nombre exacto para confirmar.
  Sus programaciones y grupos privados de inventario pasan al administrador que
  ejecuta el borrado, nunca a todos los usuarios.
- **LDAPS**: no tienen contraseña local ni se eliminan desde la aplicación. Su
  contraseña se cambia en el directorio y, para bloquear el acceso, se usa
  «Desactivar». El sistema impide eliminar, desactivar o retirar el rol a la
  última cuenta de administración **local** activa.

El alta, el cambio de contraseña y la baja quedan registrados en `/audit`
como `local_user_created`, `password_changed` y `local_user_deleted`.

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

## 6. Acceso a los equipos objetivo (SSH y WinRM)

La app ejecuta Ansible desde el contenedor. Necesita `known_hosts` con las
claves de los hosts si `ACC_HOST_KEY_CHECKING=1`. El `docker-compose.yml`
incluido activa `ACC_SSH_HOST_KEYS_AUTO=1`: antes de cada lote se añaden las
huellas que falten, incluidas las IPs nuevas, y el volumen `acc_ssh` las
conserva entre reinicios.

Para precargar de una vez todos los hosts SSH del inventario:

```bash
docker compose exec app flask --app app sync-known-hosts
```

También puedes pasar las líneas al arrancar con `ACC_KNOWN_HOSTS`, indicar
otro fichero con `ACC_KNOWN_HOSTS_FILE`, o añadir huellas manualmente:

```bash
docker compose exec app sh -c \
  'ssh-keyscan 10.0.0.10 10.0.0.11 >> /home/acc/.ssh/known_hosts'
docker compose exec app chmod 600 /home/acc/.ssh/known_hosts
```

El aprovisionamiento automático acepta la huella en el primer contacto
(TOFU): úsalo solo en redes de gestión de confianza. Para máxima seguridad,
precarga huellas verificadas fuera de banda o usa certificados SSH de host.
No desactives `ACC_HOST_KEY_CHECKING` en producción.

### Estaciones Windows (WinRM)

Las estaciones Windows se administran por WinRM con las credenciales que se
introducen en la ejecución. En cada estación:

1. Habilita WinRM (`winrm quickconfig` o la política corporativa equivalente).
2. Permite el puerto en el firewall: **5985/tcp** con NTLM o **5986/tcp** con
   TLS.
3. Usa una cuenta con permisos de administrador local. Con `ntlm` suele
   bastar; `basic` puede requerir `LocalAccountTokenFilterPolicy` en equipos
   fuera de dominio.

Valores por defecto en el contenedor:

```env
ACC_WINRM_PORT=5985
ACC_WINRM_TRANSPORT=ntlm
ACC_WINRM_SERVER_CERT_VALIDATION=validate
```

No desactives la validación del certificado WinRM en producción. Si usas
5986 con certificado autofirmado, instala la CA correspondiente en el
contenedor o usa un certificado de confianza; evita
`ACC_WINRM_SERVER_CERT_VALIDATION=ignore` salvo en laboratorio aislado.

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

Vistas útiles de la interfaz (según rol):

| Vista | Uso |
|---|---|
| `/inventario` | Grupos, hosts, criticidad y ventana de mantenimiento (UTC) |
| `/programaciones` | Ejecuciones únicas, diarias o semanales |
| `/aprobaciones` | Doble aprobación de tareas destructivas |
| `/usuarios` | Cuentas locales (alta, contraseña, baja), roles y acceso corporativo LDAPS (solo admin local para el interruptor) |
| `/history` y `/audit` | Historial de lotes y eventos de seguridad |
| `/metricas` | p50/p95 por ruta y peticiones lentas (solo admin) |
| `/acerca-de` | Identidad del servicio, entorno y fecha del host |

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
| Ejecución bloqueada en un host `critical` | Fuera de la ventana de mantenimiento | Ajusta la ventana en `/inventario` o autoriza una excepción con motivo (solo admin) |
| WinRM no conecta | Puerto, transporte o certificado | Revisa 5985/5986, `ACC_WINRM_TRANSPORT` y `ACC_WINRM_SERVER_CERT_VALIDATION` |
| «Actualización del sistema» no se puede ejecutar | Tarjeta en estado “En preparación” | Usa el procedimiento `apt-get` de §10 mientras no se habilite la acción `APT` |
| El tapiz falla en una estación | Sin GNOME/dconf, sin `gsettings` o imagen no válida | Comprueba que sea Linux con GNOME; la imagen se valida por firma (PNG/JPEG/WebP/GIF/BMP) y el informe PDF indica la causa |

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
- **Cuentas LDAPS**: un uid del directorio nunca puede tomar el nombre de la
  cuenta local `admin` (la local tiene prioridad). Un usuario LDAP promovido
  a `admin` administra cuentas y roles, pero no puede activar/desactivar el
  acceso LDAPS: ese interruptor es exclusivo del admin local. Tampoco puede
  retirar el rol admin ni desactivar al **último admin local activo**: si
  el directorio deja de alcanzarse, el sistema conserva una puerta de
  administración local.
- La tarjeta operativa **Zona horaria** aplica `America/Caracas` (UTC-4)
  permanente en Linux y Windows; antes del cambio guarda
  `/etc/timezone.bak` en la estación y una copia en el volumen
  `acc_data` (`/data/timezone_backups/<lote>/<estación>/`).
- **Tapiz oficial (Linux/GNOME)**: sube una imagen PNG, JPEG, WebP, GIF o BMP
  (máximo `ACC_MAX_UPLOAD_BYTES`, 2 MB por defecto) y la tarjeta muestra una
  miniatura de vista previa local. La imagen se valida por
  su firma real, se guarda con permisos `0600` fuera de la raíz web y se
  borra al terminar el lote. El fondo se publica en
  `/etc/dconf/db/local.d/00-acc-tapiz` y se aplica a las sesiones GNOME
  activas: persiste tras reiniciar y al iniciar sesión. No se puede programar
  (la programación no transporta la imagen) y no soporta Windows.
- **Criticidad y ventanas**: cada grupo del inventario tiene criticidad
  (`low`/`medium`/`high`/`critical`) y, opcionalmente, una ventana en UTC.
  Una ejecución real sobre hosts `critical` fuera de su ventana se bloquea y
  se audita como `safety_blocked`. La vista previa y el DRY RUN nunca se
  bloquean. Un administrador puede autorizar una excepción con motivo
  (mínimo 10 caracteres), auditada como `safety_override`; las programaciones
  no admiten excepciones y se posponen hasta la próxima apertura.
- **Doble aprobación**: las tareas destructivas requieren la aprobación de un
  administrador distinto del ejecutor. La aprobación caduca
  (`ACC_APPROVAL_TTL_MINUTES`) y es de un solo uso.
- **Actualización del sistema (apt)**: la tarjeta `apt-upgrade` está en estado
  “En preparación”; el motor actual solo ejecuta `TZ`/`TIME`. El playbook
  `apt_upgrade.yml` ya define la política segura. Mientras no se active, el
  equivalente manual es:

  ```bash
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get -y \
    -o Dpkg::Options::=--force-confdef \
    -o Dpkg::Options::=--force-confold \
    -o DPkg::Lock::Timeout=300 \
    upgrade
  ```

  Usa `upgrade` (seguro): no elimina paquetes. No uses
  `dist-upgrade`/`full-upgrade` por defecto. No reinicia: comprueba
  `/var/run/reboot-required` y programa el reinicio en ventana.

---

## 11. Reconstruir las imágenes en esta PC (opcional)

Solo si tienes el código fuente del proyecto y Docker Hub accesible:

```bash
docker compose --env-file .env.docker build --pull
docker compose --env-file .env.docker up -d --build
```

En el equipo de origen, `./docker/export-images.sh` regenera este paquete.
