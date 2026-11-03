"""Configuración de Gunicorn para Hora Admin.

Se usa un solo worker por defecto porque el progreso de las tareas vive en
memoria (``app._tasks``). El worker es ``gthread`` para que una conexión SSE
abierta no bloquee el resto de peticiones. Para escalar a varios workers habrá
que mover ese estado a Redis/SQLite (fase posterior).
"""
import os

bind = os.environ.get("HORA_ADMIN_BIND", "127.0.0.1:5000")
workers = int(os.environ.get("HORA_ADMIN_WORKERS", "1"))
worker_class = os.environ.get("HORA_ADMIN_GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.environ.get("HORA_ADMIN_GUNICORN_THREADS", "4"))
# El SSE espera como máximo 300 s; el timeout debe superarlo para no matar al
# worker en mitad de un stream o de un lote largo.
timeout = int(os.environ.get("HORA_ADMIN_GUNICORN_TIMEOUT", "330"))
graceful_timeout = int(
    os.environ.get("HORA_ADMIN_GUNICORN_GRACEFUL_TIMEOUT", "30")
)
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("HORA_ADMIN_LOG_LEVEL", "info").lower()
