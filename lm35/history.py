"""Historial completo de una sesión detenida, leído del CSV crudo (fuente autoritativa).

Durante la adquisición la GUI sólo usa el PlotBuffer acotado; al terminar el hilo, el CSV se carga UNA vez
(en un hilo aparte) y la GUI navega todo el tiempo de la experiencia: arrastrar = desplazar, rueda = zoom.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime

import numpy as np

from . import protocol as P
from .smoothing import moving_average


class HistoryError(RuntimeError):
    pass


def _pc_seconds(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


class History:
    """t[ch] (s desde la primera muestra, creciente) y temp[ch] (°C) por canal 0..5; duration en s."""

    def __init__(self, t, temp, duration):
        self.t, self.temp, self.duration = t, temp, duration
        self._smooth_count = None
        self._smooth_temp = {}

    @property
    def channels(self):
        return [i for i in range(P.MAX_SENSORS) if len(self.t[i])]

    def _series(self, ch, smoothing_count):
        smoothing_count = max(1, int(smoothing_count))
        if smoothing_count == 1:
            return self.temp[ch]
        if smoothing_count != self._smooth_count:
            self._smooth_count = smoothing_count
            self._smooth_temp.clear()
        if ch not in self._smooth_temp:
            self._smooth_temp[ch] = moving_average(self.temp[ch], smoothing_count)
        return self._smooth_temp[ch]

    def view(self, ch, t0, t1, max_points=1500, smoothing_count=1):
        """Datos de [t0, t1] reducidos a <= ~max_points conservando picos (mín. y máx. por tramo)."""
        t, y = self.t[ch], self._series(ch, smoothing_count)
        lo = max(0, int(np.searchsorted(t, t0, side="left")) - 1)       # un punto extra a cada lado
        hi = min(len(t), int(np.searchsorted(t, t1, side="right")) + 1)
        t, y = t[lo:hi], y[lo:hi]
        n = len(t)
        if n <= max_points:
            return t, y
        buckets = max(1, max_points // 2)
        edges = np.linspace(0, n, buckets + 1).astype(int)
        keep = []
        for a, b in zip(edges[:-1], edges[1:]):
            if b <= a:
                continue
            seg = y[a:b]
            i_min, i_max = a + int(np.argmin(seg)), a + int(np.argmax(seg))
            keep.extend((i_min, i_max) if i_min <= i_max else (i_max, i_min))
        idx = np.unique(keep)
        return t[idx], y[idx]

    def ylim(self, chans, t0, t1, smoothing_count=1):
        lo, hi = math.inf, -math.inf
        for ch in chans:
            t, y = self.view(ch, t0, t1, max_points=1500, smoothing_count=smoothing_count)
            if len(y):
                lo, hi = min(lo, float(np.min(y))), max(hi, float(np.max(y)))
        return (lo, hi) if lo <= hi else None


def load_history(csv_path) -> History:
    """Lee el CSV crudo. Reinicios del ESP32 (época nueva o tiempo hacia atrás) se encadenan con el reloj de la PC."""
    ts = [[] for _ in range(P.MAX_SENSORS)]
    ys = [[] for _ in range(P.MAX_SENSORS)]
    offset, prev_dev, prev_epoch, prev_out, prev_pc, prev_pc_text = 0.0, None, None, 0.0, None, None
    first = None
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            rd = csv.reader(f)
            head = next(rd, None)
            if not head:
                raise HistoryError("CSV vacío.")
            col = {name: k for k, name in enumerate(head)}
            try:
                c_dev, c_sid, c_temp = col["device_time_s"], col["sensor_id"], col["temp_c"]
            except KeyError as e:
                raise HistoryError(f"Falta la columna {e} en el CSV.")
            c_epoch, c_pc = col.get("boot_epoch"), col.get("pc_time_utc")
            need = max(c_dev, c_sid, c_temp)
            for row in rd:
                if len(row) <= need:
                    continue
                try:
                    dev, temp, sid = float(row[c_dev]), float(row[c_temp]), int(row[c_sid])
                except ValueError:
                    continue
                if not (math.isfinite(dev) and math.isfinite(temp)) or not 1 <= sid <= P.MAX_SENSORS:
                    continue
                epoch = row[c_epoch] if c_epoch is not None else None
                pc_text = row[c_pc] if c_pc is not None else None
                if prev_dev is not None and (epoch != prev_epoch or dev < prev_dev):
                    # Reinicio: la nueva época continúa tras el último instante, separada por el tiempo real de la PC.
                    a, b = _pc_seconds(prev_pc_text), _pc_seconds(pc_text)
                    gap = max(0.0, b - a) if a is not None and b is not None else 0.0
                    offset = prev_out + gap - dev
                out = dev + offset
                if first is None:
                    first = out
                if out < prev_out:       # sólo salto de redondeo: mantener el orden monótono
                    out = prev_out
                ts[sid - 1].append(out)
                ys[sid - 1].append(temp)
                prev_dev, prev_epoch, prev_out, prev_pc_text = dev, epoch, out, pc_text
    except OSError as e:
        raise HistoryError(f"No se pudo leer el CSV: {e}")
    if first is None:
        raise HistoryError("El CSV no contiene muestras válidas.")
    t = [np.asarray(a, dtype=np.float64) - first for a in ts]
    temp = [np.asarray(a, dtype=np.float64) for a in ys]
    for i, a in enumerate(t):
        if len(a) > 1 and np.any(np.diff(a) < 0):           # frames de distintos canales llegan ordenados; por seguridad
            order = np.argsort(a, kind="stable")
            t[i], temp[i] = a[order], temp[i][order]
    duration = max((float(a[-1]) for a in t if len(a)), default=0.0)
    return History(t, temp, duration)
