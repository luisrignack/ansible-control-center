# services/rate_limiter.py
# Limitador de peticiones en memoria, por proceso.
#
# Suficiente para despliegues de un solo worker. Con varios workers/procesos
# cada uno mantiene su propia ventana; si se escala horizontalmente, sustituir
# por un backend compartido (Redis) manteniendo esta interfaz.
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import DefaultDict, Deque, Optional, Tuple


class RateLimiter:
    """Ventana deslizante de intentos por clave."""

    def __init__(self) -> None:
        self._events: DefaultDict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._checks = 0

    def check(self, key: str, limit: int, window: int) -> Tuple[bool, int]:
        """Registra un intento y decide si está permitido.

        Args:
            key: identificador del cubo (p. ej. ``login:1.2.3.4``).
            limit: número máximo de intentos en la ventana.
            window: tamaño de la ventana en segundos.

        Returns:
            ``(permitido, segundos_de_espera)``. Si no se permite, no se
            registra el intento y se indica cuándo se libera el cupo.
        """
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            self._checks += 1
            # Limpieza periódica de cubos vacíos para que el diccionario no
            # crezca sin límite con muchas IPs distintas.
            if self._checks % 1000 == 0:
                for stale in [k for k, v in self._events.items() if not v]:
                    del self._events[stale]

            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= limit:
                retry_after = max(1, int(events[0] + window - now) + 1)
                return False, retry_after

            events.append(now)
            return True, 0

    def reset(self, key: Optional[str] = None) -> None:
        """Limpia una clave o todas (útil al autenticarse o en pruebas)."""
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)


# Instancia compartida por la aplicación.
LIMITER = RateLimiter()
