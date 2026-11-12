FROM python:3.12-slim

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
    ACC_GUNICORN_TIMEOUT=330

# sshpass y openssh-client son necesarios para la autenticación por contraseña
# que usa Ansible. tini actúa como PID 1 y propaga las señales.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        openssh-client \
        sshpass \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 acc \
    && useradd --uid 10001 --gid acc --create-home --shell /bin/bash acc

WORKDIR /app

COPY requirements.txt ./
COPY vendor/wheels/ /wheels/
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt --find-links /wheels

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
