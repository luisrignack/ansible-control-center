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


class RateLimiter:
    """Ventana deslizante de intentos por clave.

    El diccionario de claves se purga periódicamente por antigüedad y tiene un
    límite duro de tamaño. Sin esas dos salvaguardas, un cliente capaz de variar
    la clave (por ejemplo, falsificando la IP de origen) podría hacer crecer la
    memoria sin límite.
    """

    # Cada cuántas comprobaciones se revisan las claves obsoletas.
    CLEANUP_EVERY = 1000
    # Número máximo de claves simultáneas; al superarlo se desalojan las más
    # antiguas para que el limitador no se convierta en un vector de DoS.
    MAX_KEYS = 50_000

    def __init__(self) -> None:
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._checks = 0
        # Ventana más amplia observada. Las claves solo se consideran obsoletas
        # cuando su último evento queda fuera de esa ventana, de modo que una
        # limpieza con una ventana corta no borre cubos de ventanas largas.
        self._max_window = 0

    def check(self, key: str, limit: int, window: int) -> tuple[bool, int]:
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
            self._max_window = max(self._max_window, window)

            # Limpieza periódica de cubos obsoletos. Antes solo se borraban los
            # deques vacíos, de modo que las claves con eventos antiguos pero
            # no reutilizadas quedaban en memoria para siempre.
            if self._checks % self.CLEANUP_EVERY == 0:
                self._purge_stale(now)

            if key not in self._events and len(self._events) >= self.MAX_KEYS:
                self._evict_oldest()

            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= limit:
                retry_after = max(1, int(events[0] + window - now) + 1)
                return False, retry_after

            events.append(now)
            return True, 0

    def _purge_stale(self, now: float) -> None:
        """Elimina los cubos cuyo último intento ya no cuenta en ninguna ventana."""
        stale_after = max(self._max_window, 60)
        for key in [
            k
            for k, events in self._events.items()
            if not events or events[-1] <= now - stale_after
        ]:
            del self._events[key]

    def _evict_oldest(self) -> None:
        """Desaloja el 10 % de las claves con el último intento más antiguo."""
        overflow = max(1, len(self._events) // 10)
        oldest = sorted(
            self._events,
            key=lambda k: self._events[k][-1] if self._events[k] else 0.0,
        )[:overflow]
        for key in oldest:
            del self._events[key]

    def reset(self, key: str | None = None) -> None:
        """Limpia una clave o todas (útil al autenticarse o en pruebas)."""
        with self._lock:
            if key is None:
                self._events.clear()
                self._checks = 0
                self._max_window = 0
            else:
                self._events.pop(key, None)


# Instancia compartida por la aplicación.
LIMITER = RateLimiter()
