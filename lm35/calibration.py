"""Calibración lineal por sensor: T[°C] = pendiente * ADC + offset (mínimos cuadrados ADC vs referencia)."""
from __future__ import annotations

import csv
import json
import math
import os
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from .protocol import ADC_MAX, MAX_SENSORS

REF_MIN_C, REF_MAX_C = -55.0, 155.0      # rango nominal del LM35 (-55..150 °C) con margen
MIN_ADC_SPAN = 10.0                       # cuentas
MIN_REF_SPAN = 1.0                        # °C
DEFAULT_VFS = 2.2                         # V aproximados a fondo de escala con atenuación 6 dB (NO calibrado)
LM35_V_PER_C = 0.010
POINT_COLUMNS = ["sensor_id", "timestamp_utc", "adc", "ref_c", "source", "note"]


class CalibrationError(ValueError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Point:
    adc: float
    ref_c: float
    timestamp: str
    source: str = "manual"
    note: str = ""


@dataclass
class FitResult:
    slope: float
    intercept: float
    n: int
    r2: float
    rmse: float
    max_abs_resid: float
    adc_min: float
    adc_max: float
    ref_min: float
    ref_max: float
    quality: str
    warnings: list


@dataclass(frozen=True)
class Coeffs:
    mode: str            # 'nominal' | 'calibrado'
    slope: float
    intercept: float
    revision: int
    quality: str = ""
    r2: float = float("nan")
    n_points: int = 0

    def apply(self, adc: float) -> float:
        return self.slope * adc + self.intercept


def nominal_coeffs(vfs: float, revision: int = 0) -> Coeffs:
    return Coeffs("nominal", vfs / ADC_MAX / LM35_V_PER_C, 0.0, revision,
                  "aproximada (sin calibrar)", float("nan"), 0)


def _check_point(adc, ref_c):
    try:
        adc = float(adc)
        ref_c = float(ref_c)
    except (TypeError, ValueError):
        raise CalibrationError("ADC y temperatura de referencia deben ser numéricos.")
    if not (math.isfinite(adc) and math.isfinite(ref_c)):
        raise CalibrationError("ADC o referencia no finitos (NaN/inf).")
    if not 0 <= adc <= ADC_MAX:
        raise CalibrationError(f"ADC {adc:g} fuera de 0..{ADC_MAX}.")
    if not REF_MIN_C <= ref_c <= REF_MAX_C:
        raise CalibrationError(f"Referencia {ref_c:g} °C fuera de {REF_MIN_C:g}..{REF_MAX_C:g} °C.")
    return adc, ref_c


def fit_linear(points) -> FitResult:
    pts = list(points)
    if len(pts) < 2:
        raise CalibrationError("Se necesitan al menos 2 puntos (recomendado ≥3 repartidos en el rango).")
    xs, ys = [], []
    for p in pts:
        a, r = _check_point(p.adc, p.ref_c)
        xs.append(a)
        ys.append(r)
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    adc_span, ref_span = max(xs) - min(xs), max(ys) - min(ys)
    if adc_span < MIN_ADC_SPAN or sxx <= 0:
        raise CalibrationError(
            f"Puntos degenerados: el rango de ADC ({adc_span:g} cuentas) es menor que {MIN_ADC_SPAN:g}.")
    if ref_span < MIN_REF_SPAN or syy <= 0:
        raise CalibrationError(
            f"Puntos degenerados: la referencia varía menos de {MIN_REF_SPAN:g} °C (pendiente indeterminada).")
    slope = sxy / sxx
    intercept = my - slope * mx
    if not (math.isfinite(slope) and math.isfinite(intercept)):
        raise CalibrationError("Ajuste no finito.")
    if slope <= 0:
        raise CalibrationError(f"Pendiente no positiva ({slope:.5g}); el LM35 es creciente. Revise los puntos.")
    res = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    ss_res = sum(e * e for e in res)
    r2 = 1.0 - ss_res / syy
    rmse = math.sqrt(ss_res / n)
    mar = max(abs(e) for e in res)
    warns = []
    if n == 2:
        quality = "mínima (2 puntos, sin redundancia)"
        warns.append("Con 2 puntos el ajuste es exacto y no permite estimar el error.")
    else:
        if ref_span < 20:
            warns.append("Rango de referencia < 20 °C: extrapolar es poco fiable.")
        if r2 < 0.999 or mar > 1.0:
            warns.append("Residuos altos: revise estabilidad térmica o puntos atípicos.")
        quality = "buena" if not warns and n >= 3 else "dudosa" if r2 < 0.99 else "aceptable"
    if slope < 0.005 or slope > 0.2:
        warns.append(f"Pendiente {slope:.4g} °C/cuenta lejos de lo esperado (~0.05).")
    return FitResult(slope, intercept, n, r2, rmse, mar, min(xs), max(xs), min(ys), max(ys), quality, warns)


class CalibrationStore:
    """Calibración persistente (JSON) de hasta 6 sensores, con historial de revisiones (JSONL)."""

    def __init__(self, path, nominal_vfs: float = DEFAULT_VFS):
        self.path = str(path)
        self._lock = threading.RLock()
        self.version = 0           # sube en cada cambio de calibración activa
        self.nominal_vfs = float(nominal_vfs)
        self._s = {i: self._blank(i) for i in range(1, MAX_SENSORS + 1)}
        if os.path.exists(self.path):
            self._load()

    @staticmethod
    def _blank(i):
        return {"sensor_id": i, "name": f"S{i}", "mode": "nominal", "revision": 0, "active": None, "points": []}

    @property
    def history_path(self):
        base, _ = os.path.splitext(self.path)
        return base + "_history.jsonl"

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.nominal_vfs = float(d.get("nominal_vfs", self.nominal_vfs))
            for k, s in d["sensors"].items():
                i = int(k)
                if i in self._s:
                    base = self._blank(i)
                    base.update({kk: s[kk] for kk in ("name", "mode", "revision", "active") if kk in s})
                    base["points"] = [Point(**p) for p in s.get("points", [])]
                    self._s[i] = base
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise CalibrationError(f"No se pudo leer {self.path}: {e}")

    def _sid(self, i):
        if i not in self._s:
            raise CalibrationError(f"Sensor {i} inválido (1..{MAX_SENSORS}).")
        return self._s[i]

    def save(self):
        with self._lock:
            d = {"format": 1, "saved_utc": utc_now_iso(), "nominal_vfs": self.nominal_vfs,
                 "note": "pendiente [°C/cuenta], offset [°C]; T = pendiente*ADC + offset",
                 "sensors": {str(i): {**{k: v for k, v in s.items() if k != "points"},
                                      "points": [asdict(p) for p in s["points"]]}
                             for i, s in self._s.items()}}
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)

    # --- consulta -------------------------------------------------------
    def name(self, i):
        return self._sid(i)["name"]

    def set_name(self, i, name):
        with self._lock:
            self._sid(i)["name"] = (name or f"S{i}").strip()[:32]

    def points(self, i):
        with self._lock:
            return list(self._sid(i)["points"])

    def coeffs(self, i) -> Coeffs:
        with self._lock:
            s = self._sid(i)
            if s["mode"] == "calibrado" and s["active"]:
                a = s["active"]
                return Coeffs("calibrado", a["slope"], a["intercept"], s["revision"], a.get("quality", ""),
                              a.get("r2", float("nan")), a.get("n", 0))
            return nominal_coeffs(self.nominal_vfs, s["revision"])

    def all_coeffs(self):
        with self._lock:
            return {i: self.coeffs(i) for i in self._s}

    def set_nominal_vfs(self, vfs):
        vfs = float(vfs)
        if not math.isfinite(vfs) or not 0.5 <= vfs <= 3.6:
            raise CalibrationError("Tensión de fondo de escala nominal fuera de 0.5..3.6 V.")
        with self._lock:
            self.nominal_vfs = vfs
            self.version += 1

    # --- puntos ---------------------------------------------------------
    def add_point(self, i, adc, ref_c, source="manual", timestamp=None, note=""):
        adc, ref_c = _check_point(adc, ref_c)
        with self._lock:
            self._sid(i)["points"].append(Point(adc, ref_c, timestamp or utc_now_iso(), source, note))

    def remove_points(self, i, indices):
        """Elimina puntos y deja inmediatamente la conversión nominal activa."""
        with self._lock:
            s = self._sid(i)
            pts = s["points"]
            idx = sorted(set(indices))
            if any(k < 0 or k >= len(pts) for k in idx):
                raise CalibrationError("Índice de punto inexistente.")
            if not idx:
                return self.coeffs(i)
            prior_points = [asdict(p) for p in pts]
            for k in reversed(idx):
                del pts[k]
            s["revision"] += 1
            s["mode"] = "nominal"
            s["active"] = None
            self.version += 1
            self._log(i, "eliminar_puntos", {"deleted_indices": idx, "prior_points": prior_points})
            self.save()
            return self.coeffs(i)

    def reset_sensor(self, i) -> Coeffs:
        """Borra todos los puntos y activa nominal, sin modificar otros sensores."""
        with self._lock:
            s = self._sid(i)
            prior_points = [asdict(p) for p in s["points"]]
            s["points"].clear()
            s["revision"] += 1
            s["mode"] = "nominal"
            s["active"] = None
            self.version += 1
            self._log(i, "reset_sensor", {"prior_points": prior_points})
            self.save()
            return self.coeffs(i)

    # --- ajuste / revisiones -------------------------------------------
    def fit(self, i) -> FitResult:
        return fit_linear(self.points(i))

    def _log(self, i, event, extra):
        s = self._s[i]
        rec = {"timestamp_utc": utc_now_iso(), "sensor_id": i, "name": s["name"], "event": event,
               "revision": s["revision"], "mode": s["mode"], "nominal_vfs": self.nominal_vfs,
               "points": [asdict(p) for p in s["points"]], **extra}
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def apply_fit(self, i) -> Coeffs:
        with self._lock:
            f = self.fit(i)           # si falla no se modifica nada
            s = self._sid(i)
            s["revision"] += 1
            s["mode"] = "calibrado"
            s["active"] = {"slope": f.slope, "intercept": f.intercept, "n": f.n, "r2": f.r2, "rmse": f.rmse,
                           "max_abs_resid": f.max_abs_resid, "quality": f.quality, "warnings": f.warnings,
                           "adc_range": [f.adc_min, f.adc_max], "ref_range": [f.ref_min, f.ref_max],
                           "created_utc": utc_now_iso()}
            self.version += 1
            self._log(i, "aplicar_ajuste", {"active": s["active"]})
            self.save()
            return self.coeffs(i)

    def revert_nominal(self, i) -> Coeffs:
        with self._lock:
            s = self._sid(i)
            s["revision"] += 1
            s["mode"] = "nominal"
            s["active"] = None
            self.version += 1
            self._log(i, "volver_a_nominal", {})
            self.save()
            return self.coeffs(i)

    def snapshot(self) -> dict:
        """Estado activo de todos los sensores (para guardar con cada sesión)."""
        with self._lock:
            return {"nominal_vfs": self.nominal_vfs, "sensors": {
                str(i): {"name": s["name"], "mode": s["mode"], "revision": s["revision"],
                         "active": json.loads(json.dumps(s["active"])) if s["mode"] == "calibrado" else None,
                         "n_points": len(s["points"])} for i, s in self._s.items()}}

    # --- CSV de puntos --------------------------------------------------
    def export_points_csv(self, path, sensor=None):
        with self._lock, open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(POINT_COLUMNS)
            for i, s in self._s.items():
                if sensor in (None, i):
                    for p in s["points"]:
                        w.writerow([i, p.timestamp, repr(p.adc), repr(p.ref_c), p.source, p.note])

    def import_points_csv(self, path, sensor=None):
        """Importa puntos; devuelve (agregados, lista_de_errores). Filas inválidas se omiten."""
        added, errors = 0, []
        with open(path, newline="", encoding="utf-8-sig") as f:
            rd = csv.DictReader(f)
            missing = {"adc", "ref_c"} - set(rd.fieldnames or [])
            if missing or (sensor is None and "sensor_id" not in (rd.fieldnames or [])):
                raise CalibrationError("CSV sin columnas requeridas (sensor_id, adc, ref_c).")
            for ln, row in enumerate(rd, start=2):
                try:
                    sid = sensor if sensor is not None else int(row["sensor_id"])
                    self.add_point(sid, row["adc"], row["ref_c"], row.get("source") or "importado",
                                   row.get("timestamp_utc") or None, row.get("note") or "")
                    added += 1
                except (CalibrationError, ValueError, TypeError) as e:
                    errors.append(f"línea {ln}: {e}")
        return added, errors
