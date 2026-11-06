"""Hilo del puerto serie: lee, valida, convierte a °C, registra en CSV y alimenta el buffer de la gráfica.

El hilo es el DUEÑO de la lectura y del registro CSV; la GUI sólo consulta el PlotBuffer y la cola de eventos.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import serial

from . import protocol as P
from .smoothing import moving_average
from .recorder import SessionRecorder, iso_pair

PREFIX_REAL = "lm35_"
PREFIX_DEMO = "DEMO_SIMULADO_lm35_"
MAX_EVENT_REPEAT = 200
ADC_ATTENUATION = "6 dB (rango nominal ~0.15-1.75 V, plena escala ~2.2 V; no calibrado de fábrica)"


class HandshakeError(RuntimeError):
    pass


@dataclass
class SessionSettings:
    port: str
    baud: int = 921600
    n_sensors: int = 1
    rate_hz: float = 100.0
    oversample: int = 1
    channels: tuple | None = None
    experience_label: str = ""
    photo_sources: dict | None = None

    def __post_init__(self):
        if self.channels is None:
            self.channels = tuple(range(1, self.n_sensors + 1))
        else:
            self.channels = tuple(int(x) for x in self.channels)
            self.n_sensors = len(self.channels)


def open_serial(port: str, baud: int):
    return serial.Serial(port, baud, timeout=0.05, write_timeout=1.0)


class PlotBuffer:
    """Buffer acotado e independiente del registro: (t_monotónico, adc, °C) por canal."""

    def __init__(self, n_ch: int = P.MAX_SENSORS, maxlen: int = 6000):
        self._lock = threading.Lock()
        self._d = [deque(maxlen=maxlen) for _ in range(n_ch)]

    def append(self, ch, t, adc, temp):
        with self._lock:
            self._d[ch].append((t, adc, temp))

    def size(self, ch):
        with self._lock:
            return len(self._d[ch])

    def clear(self):
        with self._lock:
            for d in self._d:
                d.clear()

    def latest(self, ch, smoothing_count=1):
        with self._lock:
            pts = list(self._d[ch])
        if not pts:
            return None
        count = max(1, int(smoothing_count))
        tail = pts[-count:]
        return pts[-1][0], sum(p[1] for p in tail) / len(tail), sum(p[2] for p in tail) / len(tail)

    def window(self, ch, seconds, max_points, now=None, smoothing_count=1):
        """(t relativo <= 0, °C) de los últimos 'seconds' s, diezmado a <= max_points."""
        now = time.monotonic() if now is None else now
        with self._lock:
            pts = list(self._d[ch])
        if not pts:
            return [], []
        times = [p[0] for p in pts]
        smooth = moving_average([p[2] for p in pts], smoothing_count)
        visible = [i for i, value in enumerate(times) if now - seconds <= value <= now]
        if len(visible) > max_points:
            step = -(-len(visible) // max_points)
            visible = visible[::step]
        return [times[i] - now for i in visible], [float(smooth[i]) for i in visible]

    def recent_adc(self, ch, seconds, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            return [p[1] for p in self._d[ch] if p[0] >= now - seconds]


def _fmt_adc(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


class SerialWorker(threading.Thread):
    def __init__(self, serial_factory, settings: SessionSettings, store, out_dir, demo=False, plot=None):
        super().__init__(daemon=True, name="serial-worker")
        self.plan = P.plan_config(settings.channels, settings.rate_hz, settings.oversample, settings.baud)
        self.factory, self.settings, self.store = serial_factory, settings, store
        self.out_dir, self.demo = out_dir, demo
        self.channels = tuple(settings.channels)
        self.mask = self.plan.mask
        self.n = len(self.channels)
        self.plot = plot or PlotBuffer()
        self.events: "queue.Queue[tuple]" = queue.Queue(maxsize=1000)
        self.state = "inactivo"
        self.end_reason = ""
        self.csv_path = self.json_path = self.log_path = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._stats = dict(frames_ok=0, malformed=0, lost=0, duplicates=0, reboots=0, rollovers=0,
                           late_samples=0, rows_written=0, silence_events=0)
        self._counts = {}
        self._buf = bytearray()
        self._last_rx = time.monotonic()
        self._silent = False
        self._acked = {}
        self._boot_id = None
        self._need_reconfig = False
        self.rec = None
        self.tracker = P.SeqTracker()
        self._ver = -1
        self._coeffs = {}
        self._last_ovr = 0
        self._t_start = None

    # ---- API pública ----------------------------------------------------
    def request_stop(self):
        self._stop_event.set()

    def stats(self):
        with self._lock:
            s = dict(self._stats)
        s["state"] = self.state
        s["last_frame_age_s"] = time.monotonic() - self._last_rx
        s["elapsed_s"] = (time.monotonic() - self._t_start) if self._t_start else 0.0
        return s

    # ---- utilidades -------------------------------------------------------
    def _inc(self, k, v=1):
        with self._lock:
            self._stats[k] += v

    def _event(self, level, text, key=None):
        if key:
            c = self._counts[key] = self._counts.get(key, 0) + 1
            if c == MAX_EVENT_REPEAT + 1:
                text += " (se suprimen avisos repetidos de este tipo)"
            elif c > MAX_EVENT_REPEAT:
                return
        if self.rec:
            self.rec.log_event(level, text)
        try:
            self.events.put_nowait((level, text))
        except queue.Full:
            pass

    def _read_lines(self):
        data = self.ser.read(max(1, getattr(self.ser, "in_waiting", 0) or 1))
        lines = []
        if data:
            self._buf += data
            while True:
                i = self._buf.find(b"\n")
                if i < 0:
                    break
                lines.append(bytes(self._buf[:i + 1]))
                del self._buf[:i + 1]
            if len(self._buf) > 512:
                self._buf.clear()
                lines.append(b"\x00overflow\n")
        return lines

    def _malformed(self, line, err):
        self._inc("malformed")
        self._event("WARN", f"Trama mal formada ({err}): {bytes(line)[:60]!r}", key="malformed")

    # ---- protocolo ---------------------------------------------------------
    def _cmd(self, payload, name, timeout=1.0, retries=3):
        for _ in range(retries):
            self.ser.write(P.encode(payload))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if self._stop_event.is_set() and name != "STOP":
                    raise HandshakeError("cancelado por el usuario")
                for ln in self._read_lines():
                    try:
                        m = P.parse_line(ln, self.mask)
                    except P.ProtocolError:
                        continue          # basura del arranque del ESP32 u otras tramas: se ignoran
                    if m.kind == "HELLO" and len(m.fields) > 2:
                        self._boot_id = m.fields[2]
                    elif m.kind == "ACK" and len(m.fields) > 1 and m.fields[1] == name:
                        if name == "PING" and len(m.fields) > 3:
                            self._boot_id = m.fields[3]
                        return m
                    elif m.kind == "NAK" and len(m.fields) > 1 and m.fields[1] == name:
                        raise HandshakeError(f"El ESP32 rechazó {name}: {','.join(m.fields[2:])}")
        raise HandshakeError(f"Sin respuesta del ESP32 a {name}")

    def _configure_and_start(self):
        self.state = "configurando"
        self._event("INFO", "Configurando ESP32...")
        try:
            self.ser.write(P.encode("STOP"))     # por si quedó adquiriendo de una sesión previa
            time.sleep(0.05)
        except serial.SerialTimeoutException:
            pass
        self._cmd("PING", "PING", timeout=0.5, retries=10)
        p = self.plan
        self._cmd(f"CFG,{p.mask},{p.interval_us},{p.oversample}", "CFG")
        self._cmd("START", "START")
        self._buf.clear()
        self._last_rx = time.monotonic()
        self.state = "adquiriendo"
        self._event("INFO", f"Adquiriendo: {p.n} sensor(es), {p.rate_hz:g} Hz/sensor, "
                            f"intervalo {p.interval_us} µs, {p.bytes_per_s:.0f} B/s ({p.utilisation*100:.1f} % del enlace)")

    # ---- calibración activa ------------------------------------------------
    def _refresh_coeffs(self):
        if self.store.version != self._ver:
            first = self._ver < 0
            self._ver = self.store.version
            self._coeffs = {i: self.store.coeffs(i) for i in range(1, P.MAX_SENSORS + 1)}
            self._names = {i: self.store.name(i) for i in range(1, P.MAX_SENSORS + 1)}
            self._vfs = self.store.nominal_vfs
            if not first and self.rec:
                self.rec.log_calibration_change(self.store.snapshot())
                self._event("INFO", "Calibración activa modificada durante la sesión")

    # ---- procesamiento ------------------------------------------------------
    def _process_line(self, line):
        try:
            m = P.parse_line(line, self.mask)
        except P.ProtocolError as e:
            if self.state == "adquiriendo" or self.state == "deteniendo":
                self._malformed(line, e)
            return
        if m.kind == "D":
            if self.state in ("adquiriendo", "deteniendo"):
                self._frame(m.frame)
        elif m.kind == "HELLO":
            bid = m.fields[2] if len(m.fields) > 2 else None
            if self.state == "adquiriendo" and bid != self._boot_id:
                self._boot_id = bid
                self.tracker.note_hello()
                self._need_reconfig = True
                self._event("WARN", f"Reinicio del ESP32 detectado (HELLO, boot_id={bid}); reconfigurando")
            elif self.state == "deteniendo":
                self._boot_id = bid
        elif m.kind == "ACK" and len(m.fields) > 1:
            self._acked[m.fields[1]] = True
        elif m.kind == "NAK":
            self._event("WARN", f"NAK del ESP32: {','.join(m.fields[1:])}")

    def _frame(self, fr):
        now_u = datetime.now(timezone.utc)
        mono = time.monotonic()
        self._last_rx = mono
        if self._silent:
            self._silent = False
            self._event("INFO", "Datos reanudados")
        r = self.tracker.update(fr.seq, fr.t_us)
        if r.status == "duplicate":
            self._inc("duplicates")
            self._event("WARN", f"Trama duplicada o fuera de orden (seq={fr.seq})", key="dup")
            return
        if r.status == "gap":
            self._inc("lost", r.lost)
            self._event("WARN", f"Pérdida de {r.lost} trama(s) antes de seq={fr.seq}", key="gap")
        if r.status == "reboot":
            self._inc("reboots")
            self._event("WARN", f"Reinicio del dispositivo: seq/tiempo retrocedieron (seq={fr.seq}); "
                                f"nueva época de arranque {r.boot_epoch}")
        if r.rollover:
            self._inc("rollovers")
            self._event("INFO", "Desborde de micros() del ESP32 (32 bits, ~71.6 min) compensado")
        if fr.ovr > self._last_ovr:
            self._inc("late_samples", fr.ovr - self._last_ovr)
            self._event("WARN", f"El ESP32 llegó tarde a {fr.ovr - self._last_ovr} muestreo(s) (ovr={fr.ovr})", key="ovr")
        self._last_ovr = fr.ovr
        self._refresh_coeffs()
        local, utc = iso_pair(now_u)
        for sid, adc in zip(fr.channels, fr.adc):
            c = self._coeffs[sid]
            temp = c.apply(adc)
            self.plot.append(sid - 1, mono, adc, temp)
            self.rec.write_row({
                "pc_time_local": local, "pc_time_utc": utc, "session_id": self.rec.session_id,
                "demo": "SIMULADO" if self.demo else "REAL", "seq": fr.seq, "device_time_us": fr.t_us,
                "device_time_s": f"{r.t_us_unwrapped / 1e6:.6f}", "boot_epoch": r.boot_epoch, "ovr": fr.ovr,
                "sensor_id": sid, "sensor_name": self._names[sid], "gpio": P.GPIOS[sid - 1],
                "adc_raw": _fmt_adc(adc), "temp_c": f"{temp:.3f}", "calib_mode": c.mode,
                "calib_revision": c.revision, "slope_c_per_count": f"{c.slope:.8g}",
                "intercept_c": f"{c.intercept:.6g}", "calib_quality": c.quality,
                "calib_r2": "" if c.r2 != c.r2 else f"{c.r2:.6f}", "calib_n_points": c.n_points,
                "nominal_vfs": f"{self._vfs:g}", "experience_label": self.settings.experience_label,
                "photo_path": self.rec.photo_paths.get(sid, "")})
        self._inc("frames_ok")
        self._inc("rows_written", len(fr.adc))

    # ---- hilo ---------------------------------------------------------------
    def run(self):
        self.state = "conectando"
        reason, ok = "detenido por el usuario", True
        try:
            meta = {"mode": "SIMULADO" if self.demo else "REAL",
                    "warning": "DATOS SIMULADOS: no provienen de hardware" if self.demo else "",
                    "settings": asdict(self.settings), "plan": asdict(self.plan),
                    "experience_label": self.settings.experience_label,
                    "selected_channels": list(self.channels),
                    "gpios": [P.GPIOS[i - 1] for i in self.channels], "adc_attenuation": ADC_ATTENUATION,
                    "calibration_at_start": self.store.snapshot(), "calibration_file": self.store.path,
                    "photo_sources": self.settings.photo_sources or {}}
            self.rec = SessionRecorder(self.out_dir, PREFIX_DEMO if self.demo else PREFIX_REAL, meta)
            self.csv_path, self.json_path, self.log_path = self.rec.csv_path, self.rec.json_path, self.rec.log_path
            self._refresh_coeffs()
            self._t_start = time.monotonic()
            self._event("INFO", f"Sesión {self.rec.session_id} ({'SIMULADA' if self.demo else 'REAL'}) en {self.settings.port}")
            try:
                self.ser = self.factory()
            except (serial.SerialException, OSError) as e:
                raise HandshakeError(f"No se pudo abrir {self.settings.port}: {e}")
            self._configure_and_start()
            self._stream()
        except serial.SerialException as e:
            ok, reason = False, f"Desconexión o error del puerto serie: {e}"
        except HandshakeError as e:
            ok, reason = (str(e) == "cancelado por el usuario"), str(e)
        except Exception as e:      # no dejar morir el hilo en silencio
            ok, reason = False, f"Error interno: {type(e).__name__}: {e}"
        finally:
            try:
                self.ser.close()
            except Exception:
                pass
            if self.rec:
                self._event("INFO" if ok else "ERROR", reason)
                self.rec.close(self.stats(), reason)
            self.end_reason = reason
            self.state = "detenido" if ok else "error"

    def _stream(self):
        stopping, deadline = False, 0.0
        interval = self.plan.interval_us / 1e6
        while True:
            now = time.monotonic()
            if self._stop_event.is_set() and not stopping:
                stopping, deadline = True, now + 1.0
                self.state = "deteniendo"
                self._acked.pop("STOP", None)
                self.ser.write(P.encode("STOP"))
            for ln in self._read_lines():
                self._process_line(ln)
            self.rec.flush_if_due()
            if stopping:
                if self._acked.get("STOP") or time.monotonic() > deadline:
                    if not self._acked.get("STOP"):
                        self._event("WARN", "El ESP32 no confirmó STOP")
                    return
                continue
            if self._need_reconfig:
                self._need_reconfig = False
                self._configure_and_start()
            if (not self._silent and self.state == "adquiriendo"
                    and now - self._last_rx > max(2.0, 20 * interval)):
                self._silent = True
                self._inc("silence_events")
                self._event("ERROR", f"Sin datos del ESP32 desde hace {now - self._last_rx:.1f} s")
