"""Operaciones puras de suavizado usadas sólo por la presentación."""
from __future__ import annotations

import numpy as np


def moving_average(values, count: int):
    """Promedio móvil causal con prefijo parcial para las primeras muestras."""
    count = int(count)
    if count < 1:
        raise ValueError("count debe ser al menos 1")
    data = np.asarray(values, dtype=np.float64)
    if data.ndim != 1:
        raise ValueError("values debe ser un vector")
    if count == 1 or not len(data):
        return data.copy()
    cumulative = np.cumsum(data, dtype=np.float64)
    totals = cumulative.copy()
    if len(data) > count:
        totals[count:] -= cumulative[:-count]
    divisors = np.minimum(np.arange(1, len(data) + 1), count)
    return totals / divisors
