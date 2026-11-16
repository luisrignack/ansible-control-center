# Checklist de paso a producción en otra PC

Lista de comprobación para llevar Ansible Control Center desde el equipo de
desarrollo a la PC de producción usando esta carpeta autocontenida.

> Regla: en producción **siempre** se usa la versión exacta
> (`ACC_IMAGE_TAG=<VERSION>`) y **siempre** `--env-file .env.docker`.
> Nunca `latest`, nunca sin allowlist.

---

## 1. En la PC de origen (antes de copiar)

- [ ] El árbol de trabajo está limpio: `git status` sin cambios pendientes.
- [ ] La suite está en verde: `pytest -q` y `ruff/black/mypy`.
- [ ] La versión está decidida y `VERSION` coincide con el tag de Git.
- [ ] Se ha generado el paquete:
      `./docker/export-images.sh`
- [ ] El paquete incluye imágenes de la arquitectura de destino.
- [ ] Si se migran datos, se ha ejecutado el respaldo **desde la carpeta del
      paquete antiguo**:
      `./herramientas/respaldo-datos.sh`
- [ ] Se copian a un medio cifrado: la carpeta del paquete nuevo, el
      `respaldo/` y la configuración (`.env.docker`, `certs/`, `certs-ca/`).

## 2. En la PC de destino (requisitos)

- [ ] Docker Engine 24+ y `docker compose version` funcionan.
- [ ] Docker arranca con el sistema (`sudo systemctl enable --now docker`).
- [ ] Arquitectura compatible:
      `docker info --format '{{.Architecture}}'` coincide con
      `imagenes/03-INFORMACION-IMAGENES.txt`.
- [ ] Puertos **80/443** libres (o se cambian `ACC_HTTP_PORT`/`ACC_HTTPS_PORT`).
- [ ] Salida hacia los equipos objetivo: **22/tcp** (Linux) y **5986/tcp**
      (Windows/WinRM). LDAPS opcional: **636/tcp**.
- [ ] La PC tiene IP/hostname estable (o DNS interno) para `TLS_SAN`.
- [ ] Hay espacio suficiente para los volúmenes y las copias.

## 3. Verificar el paquete

```bash
cd hora-admin-despliegue
sha256sum -c sha256sums.txt
docker load -i imagenes/ansible-control-center-images.tar
```

- [ ] Las sumas verifican.
- [ ] Las imágenes cargan sin error.
- [ ] `cat VERSION` y `ACC_IMAGE_TAG` coinciden en `.env.docker`.

## 4. Configuración de producción

```bash
cp .env.docker.example .env.docker
chmod 600 .env.docker
openssl rand -hex 32        # pégalo en ACC_SECRET_KEY
```

Edita `.env.docker` y comprueba:

- [ ] `ACC_ENV=production`
- [ ] `ACC_SECRET_KEY=<clave aleatoria de 32 bytes>`
- [ ] `ACC_ADMIN_PASSWORD=<mínimo 12 caracteres>`
- [ ] `ACC_ALLOWED_NETWORKS=<redes de destino, nunca vacío>`
      Ejemplo: `10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`
- [ ] `ACC_HOST_KEY_CHECKING=1`
- [ ] `ACC_WINRM_PORT=5986`, `ACC_WINRM_TRANSPORT=ntlm`,
      `ACC_WINRM_SERVER_CERT_VALIDATION=validate`
- [ ] `ACC_SSH_HOST_KEYS_AUTO=0` (o `1` solo si aceptas TOFU conscientemente)
- [ ] `TLS_CN` y `TLS_SAN` con el nombre/IP real de acceso
- [ ] `ACC_HSTS=0` mientras el certificado sea autofirmado; `1` con CA real
- [ ] LDAPS (si aplica): `ACC_LDAP_ENABLED=1`, `ACC_LDAP_URL=ldaps://...`,
      `ACC_LDAP_TLS_CA_FILE=/certs/<ca>.pem` y la CA copiada en `certs-ca/`
- [ ] Ningún secreto real queda en el historial de shell ni en Git

> Con `ACC_ENV=production` la app **no arranca** si faltan
> `ACC_SECRET_KEY`, `ACC_ALLOWED_NETWORKS`, `ACC_COOKIE_SECURE=1` o la
> verificación SSH. Es una protección deliberada.

## 5. Migrar los datos existentes (si se conservan)

En la PC de origen, con el paquete antiguo parado:

```bash
./herramientas/respaldo-datos.sh
# genera respaldo/<fecha>/datos.tgz y configuracion.tgz
```

Copia `respaldo/` junto al paquete nuevo. En la PC de destino:

```bash
# 1) Configuración y certificados
tar xzf respaldo/<fecha>/configuracion.tgz -C .
chmod 600 .env.docker

# 2) Datos (SQLite, vault, known_hosts)
./herramientas/restaurar-datos.sh respaldo/<fecha>/datos.tgz

# 3) Si el destino ya tenía datos y quieres sustituirlos:
#    ./herramientas/restaurar-datos.sh --limpiar respaldo/<fecha>/datos.tgz
```

- [ ] El respaldo se copió completo y con permisos 0600.
- [ ] La restauración termina sin errores.
- [ ] La app arranca y el historial/inventario/usuario admin siguen ahí.
- [ ] La contraseña maestra del vault es la misma que en origen.

> `docker compose down -v` **borra** los volúmenes. No lo uses en producción
> salvo que quieras destruir los datos.

## 6. Certificado TLS

- [ ] Opción autofirmado: se genera solo en `certs/` al primer arranque.
      El navegador avisará una vez; es aceptable para una red interna.
- [ ] Opción recomendada: copia `fullchain.pem` y `privkey.pem` de la CA
      corporativa en `certs/` antes de arrancar.
- [ ] `TLS_CN`/`TLS_SAN` cubren el nombre o IP real.
- [ ] Con certificado de confianza: `ACC_HSTS=1`.
- [ ] `certs/privkey.pem` con permisos restringidos (0600).

## 7. Arranque y verificación

```bash
docker compose --env-file .env.docker up -d --no-build
./herramientas/verificar-despliegue.sh
```

- [ ] `docker compose ps` muestra `app` y `nginx` en `healthy`.
- [ ] `https://<host>/healthz` devuelve `{"status":"ok","database":"ok"}`.
- [ ] `https://<host>/login` carga y el admin local entra.
- [ ] `/sistema?tab=acerca-de` muestra la versión y el commit esperados.
- [ ] `/inventario` tiene los grupos y hosts (o se crean).
- [ ] `/sistema?tab=auditoria` registra el acceso.
- [ ] `ACC_DRY_RUN=1` está activo en la barra superior.
- [ ] Una vista previa real contra un host de prueba funciona.
- [ ] Una ejecución real en **un host de prueba** funciona y se audita.
- [ ] Después de validar: desactiva el modo Simulación desde la interfaz.

## 8. Red y firewall de la PC de producción

- [ ] Solo se publican 80/443 (o los puertos elegidos). La app no expone 8000.
- [ ] El firewall permite 80/443 desde la red de administración.
- [ ] El firewall permite salida 22, 5986 y 636 hacia los destinos.
- [ ] No se publica Docker API ni el puerto 8000.
- [ ] Si hay proxy inverso externo, se conserva `ACC_TRUST_PROXY=1` y
      `ACC_COOKIE_SECURE=1`.

## 9. Copias de seguridad y operación

- [ ] Copia programada de `respaldo-datos.sh` (diaria o semanal).
- [ ] Las copias se guardan cifradas y fuera de la PC.
- [ ] Se prueba una restauración en una máquina de laboratorio.
- [ ] Retención definida: `ACC_HISTORY_DAYS`, `ACC_AUTH_EVENT_DAYS`,
      `ACC_JOB_RETENTION_HOURS`, `ACC_MAX_TASK_STATE_ROWS`.
- [ ] Logs revisados: `docker compose --env-file .env.docker logs -f app`.
- [ ] Alertas o revisión periódica de `/sistema?tab=auditoria`.

## 10. Actualización y rollback

Actualizar:

```bash
./herramientas/respaldo-datos.sh
docker compose --env-file .env.docker down
# copia el paquete nuevo sobre esta carpeta (conserva .env.docker y volúmenes)
docker load -i imagenes/ansible-control-center-images.tar
# ajusta ACC_IMAGE_TAG a la nueva VERSION
docker compose --env-file .env.docker up -d --no-build
./herramientas/verificar-despliegue.sh
```

- [ ] Respaldo previo hecho.
- [ ] `ACC_IMAGE_TAG` actualizado.
- [ ] Migraciones SQLite aplicadas solas al arrancar.
- [ ] Rollback probado: imágenes y `VERSION` anteriores disponibles.

## 11. Errores típicos

| Síntoma | Causa | Solución |
|---|---|---|
| `RuntimeError: Configuración insegura` | `ACC_ENV=production` sin clave/allowlist | Define `ACC_SECRET_KEY` y `ACC_ALLOWED_NETWORKS` |
| El contenedor reinicia en bucle | Compose sin `--env-file .env.docker` | Usa siempre `--env-file .env.docker` |
| `image not found` | No se cargó el tar | `docker load -i imagenes/...tar` |
| `port is already allocated` | 80/443 ocupados | Cambia `ACC_HTTP_PORT`/`ACC_HTTPS_PORT` |
| Nginx no arranca | Certificado corrupto | Borra `certs/*.pem` y reinicia nginx |
| Aviso de certificado | Autofirmado | Acepta una vez o instala uno real |
| `permission denied` en `/data` | UID incorrecto tras restaurar | `restaurar-datos.sh --limpiar` y revisa UID 10001 |
| LDAPS no conecta | CA no montada o 636 bloqueado | Copia la CA en `certs-ca/` y revisa firewall |
| Login LDAPS falla pero admin local entra | Bind/URL/CA mal | Revisa `docker compose logs app` y `/usuarios` |
| WinRM no conecta | 5985/HTTP o certificado | Usa 5986/HTTPS y `validate` |
| Lote queda `running` | Reinicio del proceso | `docker compose exec app flask --app app reconcile-batches` |
| `latest` en producción | Etiqueta incorrecta | Fija `ACC_IMAGE_TAG=<VERSION>` |

---

## Firma de aceptación

| Comprobación | Responsable | Fecha | Resultado |
|---|---|---|---|
| Paquete verificado (SHA-256) | | | |
| Configuración de producción revisada | | | |
| Datos migrados/restaurados | | | |
| TLS y acceso HTTPS | | | |
| Login y roles | | | |
| Vista previa y ejecución en host de prueba | | | |
| Copia de seguridad programada | | | |
| Firewall y red | | | |
