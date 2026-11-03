# Hora Admin

Aplicación web Flask para automatizar la configuración de hora y zona horaria
en equipos Ubuntu mediante Ansible. Incluye autenticación, gestión de
contraseñas candidatas cifradas con `ansible-vault`, progreso en tiempo real
(SSE) e historial de ejecuciones en SQLite.

> **Uso responsable:** la aplicación prueba credenciales contra hosts. Úsala
> únicamente sobre equipos para los que tengas autorización explícita.

## Estado actual (fases P0, P1 y P2)

Completado en P0:

- [x] Estructura unificada en la raíz (`app.py`, `config.py`, `models/`,
      `services/`, `templates/`, `tests/`).
- [x] La aplicación arranca en desarrollo sin permisos de root (fallback a
      `instance/`).
- [x] Login real con Flask-Login, usuarios en SQLite y CLI `init-admin`.
- [x] Logout por `POST`, navbar condicional y sesión protegida.
- [x] Nombres de campos del formulario coherentes con el backend.
- [x] Páginas `404`, `500` y de progreso; SSE con estados terminales correctos.
- [x] `init_db` ya no borra las tablas en cada arranque.
- [x] Importaciones rotas corregidas y API `ansible.runner` eliminada.
- [x] Tests de humo y suite en verde.

Completado en P1 (integridad de datos):

- [x] Acceso SQLite centralizado en `models/db.py`.
- [x] Migraciones no destructivas y versionadas con `PRAGMA user_version`
      (versión actual: 2).
- [x] `WAL`, `busy_timeout` y transacciones con commit/rollback para tolerar
      escrituras concurrentes.
- [x] Historial sin tabla de resúmenes desincronizada: las estadísticas se
      agregan bajo demanda con `ExecutionRecord.get_stats()`.
- [x] Reparación de bases de datos parciales y normalización de la tabla
      heredada `execution_summaries`.
- [x] Pruebas de migración, idempotencia, paginación y concurrencia.

Completado en P2 (vault y Ansible reales):

- [x] Cifrado/descifrado real del vault con `ansible-vault` (fichero completo).
- [x] Contraseñas candidatas guardadas solo cifradas; la contraseña maestra
      nunca se persiste ni se registra en logs.
- [x] Inyección segura de la contraseña en Ansible mediante un fichero temporal
      `0600` y `--extra-vars @fichero` (nunca por línea de comandos).
- [x] Playbook `ansible/cambiar_hora.yml` funcional con `timedatectl`, `date`
      y `hwclock`.
- [x] Modo temporal `.txt`: el archivo se procesa en memoria y no se guarda.
- [x] Importación de `.txt` y de vaults `.yml/.yaml` ya cifrados.
- [x] Borrado sincronizado: elimina la contraseña del vault y de SQLite.
- [x] Historial real desde la interfaz (éxitos y fallos).

Pendiente (siguiente fase):

- **P3:** CSRF, rate limiting, límites de CIDR/hosts, cabeceras de seguridad y
      endurecimiento del despliegue.

## Requisitos

- Python 3.11+ (probado con 3.14).
- `ansible-core` (`ansible`, `ansible-playbook`, `ansible-vault`).
- `sshpass` en el servidor para autenticación SSH por contraseña.
- Gunicorn para producción.
- Los equipos objetivo deben ser Ubuntu con `systemd`/`timedatectl`.

## Arranque rápido (desarrollo)

```bash
python3 -m venv .venv
source .venv/bin/activate

# Si tu equipo usa un mirror corporativo en /etc/pip.conf y no estás en esa
# red, fuerza PyPI público solo para esta instalación:
# PIP_CONFIG_FILE=/dev/null python -m pip install -r requirements.txt

python -m pip install -r requirements.txt

# Base de datos y usuario administrador
flask --app app init-db
flask --app app init-admin

# Servidor de desarrollo (solo 127.0.0.1 por defecto)
python app.py
```

Abre `http://127.0.0.1:5000` e inicia sesión con el usuario creado.

Si no hay ningún administrador, la pantalla de login lo indica y muestra el
comando `flask --app app init-admin`.

## Variables de entorno

Se leen de `.env` (opcional) o del entorno real. Las principales:

| Variable | Por defecto | Descripción |
|---|---|---|
| `HORA_ADMIN_SECRET_KEY` | generada en `instance/` | Clave de sesión Flask |
| `HORA_ADMIN_SECRETS_DIR` | `/etc/hora-admin/secrets` | Secretos y vault |
| `HORA_ADMIN_DATA_DIR` | `/var/lib/hora-admin` | SQLite y clave de sesión |
| `HORA_ADMIN_LOG_DIR` | `/var/log/hora-admin` | Logs |
| `HORA_ADMIN_DRY_RUN` | `1` | No ejecuta Ansible real |
| `HORA_ADMIN_HOST_KEY_CHECKING` | `1` | Verificación estricta de la clave SSH |
| `HORA_ADMIN_DEBUG` | `0` | Depurador Flask (nunca en producción) |
| `HORA_ADMIN_HOST` / `HORA_ADMIN_PORT` | `127.0.0.1` / `5000` | Servidor de desarrollo |

Copia `.env.example` a `.env` y ajusta los valores. `install.sh` genera una
clave aleatoria automáticamente.

## Vault y ejecución

- **Contraseñas guardadas:** se cifran con `ansible-vault` en
  `passwords_vault.yml` (por defecto en `SECRETS_DIR`). El fichero se escribe
  con permisos `0600` y de forma atómica.
- **Formato:** una contraseña por línea dentro del fichero cifrado. Las líneas
  vacías y las que empiezan por `#` se ignoran.
- **Contraseña maestra:** se solicita en cada operación y no se guarda en disco.
- **Modo temporal:** en el panel, elige “Archivo temporal (.txt)” y sube un
  `.txt` con una contraseña por línea. Se procesa en memoria y no se almacena.
- **Ejecución real:** por defecto `HORA_ADMIN_DRY_RUN=1` simula todo. Para
  tocar equipos reales, define `HORA_ADMIN_DRY_RUN=0` y asegúrate de tener
  `sshpass` instalado en el servidor.
- **Borrado:** eliminar una contraseña desde la interfaz requiere la contraseña
  maestra, porque también la elimina del vault.

Para inspeccionar el vault manualmente:

```bash
ansible-vault view /etc/hora-admin/secrets/passwords_vault.yml
# te pedirá la contraseña maestra por terminal
```

## Producción

```bash
sudo ./install.sh
sudo systemctl edit --full hora-admin   # ver DEPLOYMENT.md
gunicorn -c gunicorn.conf.py app:app
```

El servidor de producción debe ir detrás de un proxy inverso con HTTPS. No
expongas `python app.py` (servidor de desarrollo) a la red.

## Estructura

```
app.py                 Rutas, login, tareas SSE y CLI
config.py              Configuración central y rutas con fallback
gunicorn.conf.py       Configuración de Gunicorn
models/db.py           Conexiones SQLite, WAL, busy_timeout y migraciones
models/                Usuarios, contraseñas y ejecuciones
services/              Vault, resolución de hosts, Ansible
templates/             Vistas Jinja2
tests/                 Pruebas (pytest)
ansible/               Playbook de hora/zona horaria y ansible.cfg
```

## Tests

```bash
source .venv/bin/activate
python -m pytest -q
```

## Seguridad

- No hay contraseña maestra por defecto: se pide en cada operación.
- `SECRET_KEY` se genera y guarda con permisos `0600` si no se define.
- El depurador Flask está desactivado por defecto.
- Las contraseñas candidatas se cifran con `ansible-vault`; solo se guardan
  hashes cortos en SQLite para la interfaz.
- Las credenciales de conexión de Ansible viajan en ficheros temporales `0600`
  y se borran tras cada ejecución; nunca aparecen en la línea de comandos.
- En producción, usa HTTPS, `HORA_ADMIN_COOKIE_SECURE=1` y un usuario de
  servicio sin privilegios.
- Las credenciales candidatas deben tratarse como material sensible.
