FROM python:3.12-slim

ARG APP_VERSION=0.0.0
ARG APP_COMMIT=

# Mirror opcional para redes sin salida a deb.debian.org ni pypi.org.
# ``APT_MIRROR`` es la base de los repositorios APT (p. ej.
# http://nexus.local:8081/repository) y ``PIP_INDEX_URL`` el índice de PyPI.
# Vacíos, se usan los repositorios oficiales de la imagen base.
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=

LABEL org.opencontainers.image.title="Ansible Control Center" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${APP_COMMIT}" \
      org.opencontainers.image.source="https://github.com/luisrignack/ansible-control-center"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ACC_SECRETS_DIR=/secrets \
    ACC_DATA_DIR=/data \
    ACC_LOG_DIR=/logs \
    ACC_BIND=0.0.0.0:8000 \
    ACC_WORKERS=1 \
    ACC_GUNICORN_WORKER_CLASS=gthread \
    ACC_GUNICORN_THREADS=8 \
    ACC_GUNICORN_TIMEOUT=330 \
    ACC_BUILD_COMMIT="${APP_COMMIT}"

# sshpass y openssh-client son necesarios para la autenticación por contraseña
# que usa Ansible. tini actúa como PID 1 y propaga las señales.
#
# Con ``APT_MIRROR`` se reescriben las fuentes de Debian para el proxy Nexus
# (una entrada por suite, porque cada una vive en un repositorio distinto).
# La firma PGP de Debian se sigue validando con el keyring de la imagen base.
RUN if [ -n "$APT_MIRROR" ]; then \
        . /etc/os-release; \
        codename="${VERSION_CODENAME:-trixie}"; \
        { \
            echo "Types: deb"; \
            echo "URIs: ${APT_MIRROR}/${codename}"; \
            echo "Suites: ${codename}"; \
            echo "Components: main"; \
            echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp"; \
            echo; \
            echo "Types: deb"; \
            echo "URIs: ${APT_MIRROR}/${codename}-updates"; \
            echo "Suites: ${codename}-updates"; \
            echo "Components: main"; \
            echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp"; \
            echo; \
            echo "Types: deb"; \
            echo "URIs: ${APT_MIRROR}/${codename}-security"; \
            echo "Suites: ${codename}-security"; \
            echo "Components: main"; \
            echo "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp"; \
        } > /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        openssh-client \
        sshpass \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 acc \
    && useradd --uid 10001 --gid acc --create-home --shell /bin/bash acc

WORKDIR /app

# Se instala el lock, no el rango de requirements.txt: la imagen debe
# reproducirse con las mismas versiones en cualquier equipo. vendor/wheels
# permite resolver ldap3/pyasn1 sin red cuando el índice no responde.
COPY requirements.lock ./
COPY vendor/wheels/ /wheels/
RUN pip_args=""; \
    if [ -n "$PIP_INDEX_URL" ]; then pip_args="--index-url $PIP_INDEX_URL"; fi; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then pip_args="$pip_args --trusted-host $PIP_TRUSTED_HOST"; fi; \
    python -m pip install --upgrade pip $pip_args \
    && python -m pip install -r requirements.lock --find-links /wheels $pip_args

COPY --chown=acc:acc . .

RUN install -d -m 0750 /data /logs \
    && install -d -m 0700 /secrets /home/acc/.ssh \
    && chown -R acc:acc /data /logs /secrets /home/acc /app \
    && chmod +x /app/docker/entrypoint.sh

USER acc

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
