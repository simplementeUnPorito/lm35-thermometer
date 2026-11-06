"""Recalcula °C de un CSV crudo con otra calibración, SIN tocar el original (escribe un CSV nuevo)."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os

from . import calibration as C
from .recorder import iso_pair

EXTRA = ["temp_c_recalc", "recalc_mode", "recalc_revision", "recalc_slope", "recalc_intercept",
         "recalc_quality", "recalc_source"]


class RecalcError(ValueError):
    pass


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def recalc_csv(in_path, out_path, calib_path, source="active", overwrite_output=False) -> dict:
    """source: 'active' (calibración activa guardada), 'refit' (reajusta con los puntos actuales) o 'nominal'."""
    if source not in ("active", "refit", "nominal"):
        raise RecalcError("source debe ser active, refit o nominal.")
    ip, op = os.path.realpath(in_path), os.path.realpath(out_path)
    if ip == op:
        raise RecalcError("La salida no puede ser el archivo original.")
    if os.path.exists(op) and not overwrite_output:
        raise RecalcError(f"Ya existe {out_path}; no se sobrescribe.")
    if os.path.exists(op + ".recalc.json") and not overwrite_output:
        raise RecalcError("Ya existe el JSON de metadatos de salida.")
    if not os.path.exists(calib_path):
        raise RecalcError(f"No existe el archivo de calibración {calib_path}.")
    try:
        store = C.CalibrationStore(calib_path)
    except C.CalibrationError as e:
        raise RecalcError(str(e))
    coeffs = {}
    for i in range(1, 7):
        if source == "nominal":
            coeffs[i] = C.nominal_coeffs(store.nominal_vfs)
        elif source == "refit":
            try:
                f = store.fit(i)
                coeffs[i] = C.Coeffs("calibrado", f.slope, f.intercept, store.coeffs(i).revision, f.quality, f.r2, f.n)
            except C.CalibrationError:
                coeffs[i] = C.nominal_coeffs(store.nominal_vfs)
        else:
            coeffs[i] = store.coeffs(i)
    src_hash = _sha256(in_path)
    n = bad = 0
    with open(in_path, newline="", encoding="utf-8-sig") as fin:
        rd = csv.DictReader(fin)
        cols = rd.fieldnames or []
        if "adc_raw" not in cols or "sensor_id" not in cols:
            raise RecalcError("El CSV no tiene columnas adc_raw y sensor_id (¿es un CSV crudo de esta aplicación?).")
        tmp = op + ".tmp"
        os.makedirs(os.path.dirname(op) or ".", exist_ok=True)
        with open(tmp, "w", newline="", encoding="utf-8") as fout:
            wr = csv.DictWriter(fout, fieldnames=list(cols) + EXTRA)
            wr.writeheader()
            for row in rd:
                n += 1
                out = dict(row)
                try:
                    c = coeffs[int(row["sensor_id"])]
                    adc = float(row["adc_raw"])
                    if not math.isfinite(adc) or not 0 <= adc <= 4095:
                        raise ValueError
                    out.update(temp_c_recalc=f"{c.apply(adc):.3f}", recalc_mode=c.mode, recalc_revision=c.revision,
                               recalc_slope=f"{c.slope:.8g}", recalc_intercept=f"{c.intercept:.6g}",
                               recalc_quality=c.quality, recalc_source=source)
                except (KeyError, ValueError):
                    bad += 1
                    out.update({k: "" for k in EXTRA})
                wr.writerow(out)
    os.replace(tmp, op)
    info = {"source_file": os.path.abspath(in_path), "source_sha256": src_hash, "output": os.path.abspath(out_path),
            "calibration_file": os.path.abspath(calib_path), "calibration_source": source,
            "calibration_snapshot": store.snapshot(), "rows": n, "rows_invalid": bad,
            "created_local": iso_pair()[0]}
    with open(op + ".recalc.json", "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1, ensure_ascii=False)
    return info
