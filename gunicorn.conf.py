"""Configuración de Gunicorn para Ansible Control Center.

Se usa un solo worker por defecto porque la ejecución y la cancelación de
trabajos viven en el proceso. Las lecturas de estado y el SSE ya refrescan
desde SQLite, así que subir ``ACC_WORKERS`` es seguro para tráfico de solo
lectura; la cancelación, en cambio, puede no alcanzar al worker que ejecuta.
Para varios workers de ejecución hace falta la cola compartida (fase
posterior). El worker es ``gthread`` para que una conexión SSE abierta no
bloquee el resto de peticiones.
"""
import os

bind = os.environ.get("ACC_BIND", "127.0.0.1:5000")
workers = int(os.environ.get("ACC_WORKERS", "1"))
worker_class = os.environ.get("ACC_GUNICORN_WORKER_CLASS", "gthread")
# Cada conexión SSE ocupa un hilo mientras dura el stream (90 s). Con 4
# usuarios mirando el progreso a la vez, 4 hilos dejaban la web sin margen
# para cargar páginas o cancelar; 8 da holgura sin multiplicar la memoria.
threads = int(os.environ.get("ACC_GUNICORN_THREADS", "8"))
# El SSE espera como máximo 300 s; el timeout debe superarlo para no matar al
# worker en mitad de un stream o de un lote largo.
timeout = int(os.environ.get("ACC_GUNICORN_TIMEOUT", "330"))
graceful_timeout = int(
    os.environ.get("ACC_GUNICORN_GRACEFUL_TIMEOUT", "30")
)
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("ACC_LOG_LEVEL", "info").lower()
