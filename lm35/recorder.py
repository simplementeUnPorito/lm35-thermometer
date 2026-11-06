"""Registro de sesión: CSV largo (una fila por sensor y trama), bitácora de eventos y JSON de sesión.

Lo usa únicamente el hilo del puerto serie (la GUI nunca es el único registrador).
Se vacía a disco cada ~1 s y al cerrar.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone

CSV_COLUMNS = [
    "pc_time_local", "pc_time_utc", "session_id", "demo", "seq", "device_time_us", "device_time_s",
    "boot_epoch", "ovr", "sensor_id", "sensor_name", "gpio", "adc_raw", "temp_c", "calib_mode",
    "calib_revision", "slope_c_per_count", "intercept_c", "calib_quality", "calib_r2", "calib_n_points",
    "nominal_vfs", "experience_label", "photo_path",
]
FLUSH_INTERVAL_S = 1.0
MAX_EVENTS_IN_JSON = 2000


def safe_label(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text).strip(), flags=re.UNICODE).strip("_")
    return value[:48] or "sin_etiqueta"


def image_kind(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    raise ValueError("La foto debe ser un PNG o JPEG válido.")


def iso_pair(dt_utc: datetime | None = None):
    """(local con offset, UTC con offset) en ISO 8601 con milisegundos, del mismo instante."""
    u = dt_utc or datetime.now(timezone.utc)
    return u.astimezone().isoformat(timespec="milliseconds"), u.isoformat(timespec="milliseconds")


class SessionRecorder:
    def __init__(self, out_dir, prefix: str, meta: dict, clock=time.monotonic):
        self._clock = clock
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        label_part = safe_label(meta.get("experience_label", ""))
        self.session_id = f"{prefix}{label_part}_{stamp}"
        self.dir = os.path.join(str(out_dir), self.session_id)
        k = 1
        while os.path.exists(self.dir):
            k += 1
            self.session_id = f"{prefix}{label_part}_{stamp}_{k}"
            self.dir = os.path.join(str(out_dir), self.session_id)
        os.makedirs(self.dir)
        self.csv_path = os.path.join(self.dir, f"{self.session_id}_raw.csv")
        self.json_path = os.path.join(self.dir, f"{self.session_id}_session.json")
        self.log_path = os.path.join(self.dir, f"{self.session_id}_eventos.log")
        self.meta = dict(meta)
        self.photo_paths = {}
        photos_dir = os.path.join(self.dir, "photos")
        for sid, source in sorted((self.meta.pop("photo_sources", {}) or {}).items(), key=lambda x: int(x[0])):
            if not source:
                continue
            ext = image_kind(source)
            os.makedirs(photos_dir, exist_ok=True)
            rel = f"photos/S{int(sid)}{ext}"
            shutil.copy2(source, os.path.join(self.dir, *rel.split("/")))
            self.photo_paths[int(sid)] = rel
        self.meta["sensor_photos"] = {str(k): v for k, v in self.photo_paths.items()}
        self.meta["session_id"] = self.session_id
        self.meta["started_local"], self.meta["started_utc"] = iso_pair()
        self.meta["calibration_changes"] = []
        self.events = []
        self.rows = 0
        self._csv = open(self.csv_path, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._csv, fieldnames=CSV_COLUMNS)
        self._w.writeheader()
        self._log = open(self.log_path, "w", encoding="utf-8")
        self._last_flush = clock()
        self._closed = False
        self._csv.flush()
        self._write_json()

    def write_row(self, row: dict):
        self._w.writerow(row)
        self.rows += 1

    def log_event(self, level: str, text: str):
        local, utc = iso_pair()
        self._log.write(f"{utc}\t{level}\t{text}\n")
        if len(self.events) < MAX_EVENTS_IN_JSON:
            self.events.append({"utc": utc, "level": level, "text": text})

    def log_calibration_change(self, snapshot: dict):
        local, utc = iso_pair()
        self.meta["calibration_changes"].append({"utc": utc, "local": local, "snapshot": snapshot})
        self.log_event("INFO", "Cambio de calibración activa (snapshot guardado en el JSON de sesión)")
        self._write_json()

    def flush_if_due(self):
        if self._clock() - self._last_flush >= FLUSH_INTERVAL_S:
            self._flush()

    def _flush(self):
        self._csv.flush()
        self._log.flush()
        self._last_flush = self._clock()

    def _write_json(self, extra=None):
        d = dict(self.meta)
        d["events"] = self.events
        if extra:
            d.update(extra)
        tmp = self.json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1, ensure_ascii=False, default=str)
        os.replace(tmp, self.json_path)

    def close(self, stats: dict, reason: str):
        if self._closed:
            return
        self._closed = True
        local, utc = iso_pair()
        self.log_event("INFO", f"Fin de sesión: {reason}")
        self._flush()
        self._csv.close()
        self._log.close()
        self._write_json({"ended_utc": utc, "ended_local": local, "end_reason": reason,
                          "rows_written": self.rows, "stats": stats})
