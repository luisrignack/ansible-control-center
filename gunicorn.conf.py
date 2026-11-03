"""Configuración de Gunicorn para Hora Admin.

Se usa un solo worker por defecto porque el progreso de las tareas vive en
memoria (``app._tasks``). Para escalar a varios workers habrá que mover ese
estado a Redis/SQLite (fase posterior).
"""
import os

bind = os.environ.get("HORA_ADMIN_BIND", "127.0.0.1:5000")
workers = int(os.environ.get("HORA_ADMIN_WORKERS", "1"))
worker_class = "sync"
timeout = int(os.environ.get("HORA_ADMIN_GUNICORN_TIMEOUT", "120"))
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("HORA_ADMIN_LOG_LEVEL", "info").lower()
