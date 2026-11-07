"""Métricas de rendimiento en proceso, sin dependencias externas.

El proyecto evita Prometheus/StatsD para mantener el despliegue portable. Este
registro guarda una ventana acotada de duraciones por ruta y calcula p50/p95
con una muestra reciente, suficiente para detectar regresiones y comparar
contra los SLO definidos en ``docs/RENDIMIENTO.md``.

No se persiste nada: las métricas se reinician con el proceso. Para series
históricas se usan los lotes de ``execution_hosts`` (que sí están en SQLite).
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any, Deque, Dict, Iterable, Mapping


def percentile(values: Iterable[float], pct: float) -> float:
    """Percentil por rango más cercano sobre una lista ya ordenada o no.

    Con muestras pequeñas devuelve el valor observado más próximo, que es más
    honesto que interpolar. ``pct`` se expresa en 0–100.
    """
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    pct = min(100.0, max(0.0, float(pct)))
    if pct <= 0:
        return round(ordered[0], 2)
    index = max(0, math.ceil(pct / 100.0 * len(ordered)) - 1)
    return round(ordered[min(index, len(ordered) - 1)], 2)


class RequestMetrics:
    """Registro thread-safe de duraciones por endpoint."""

    def __init__(self, window: int = 500) -> None:
        self._window = max(10, int(window))
        self._lock = threading.Lock()
        self._samples: Dict[str, Deque[float]] = {}
        self._counts: Dict[str, int] = {}
        self._errors: Dict[str, int] = {}

    def record(self, endpoint: str, duration_ms: float, status: int = 200) -> None:
        """Añade una muestra. ``endpoint`` vacío se agrupa como ``desconocido``."""
        key = endpoint or "desconocido"
        try:
            value = max(0.0, float(duration_ms))
        except (TypeError, ValueError):
            return
        with self._lock:
            samples = self._samples.get(key)
            if samples is None:
                samples = deque(maxlen=self._window)
                self._samples[key] = samples
            samples.append(value)
            self._counts[key] = self._counts.get(key, 0) + 1
            if int(status or 0) >= 500:
                self._errors[key] = self._errors.get(key, 0) + 1

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Devuelve un resumen estable ordenado por nombre de endpoint."""
        with self._lock:
            keys = sorted(self._samples)
            raw = {
                key: (
                    list(self._samples[key]),
                    self._counts.get(key, 0),
                    self._errors.get(key, 0),
                )
                for key in keys
            }

        result: Dict[str, Dict[str, Any]] = {}
        for key, (samples, count, errors) in raw.items():
            if not samples:
                continue
            result[key] = {
                "count": count,
                "samples": len(samples),
                "errors": errors,
                "avg_ms": round(sum(samples) / len(samples), 2),
                "p50_ms": percentile(samples, 50),
                "p95_ms": percentile(samples, 95),
                "max_ms": round(max(samples), 2),
            }
        return result

    def reset(self) -> None:
        """Vacía la ventana; útil en pruebas y tras un cambio de configuración."""
        with self._lock:
            self._samples.clear()
            self._counts.clear()
            self._errors.clear()

    @property
    def window(self) -> int:
        return self._window


def summarize(snapshot: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """Agrega el snapshot por endpoint en totales globales."""
    total = sum(int(item.get("count", 0)) for item in snapshot.values())
    errors = sum(int(item.get("errors", 0)) for item in snapshot.values())
    slowest = None
    if snapshot:
        slowest = max(
            snapshot,
            key=lambda key: float(snapshot[key].get("p95_ms", 0.0)),
        )
    return {
        "endpoints": len(snapshot),
        "requests": total,
        "errors": errors,
        "slowest_endpoint": slowest,
    }
