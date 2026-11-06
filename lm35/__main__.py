"""CLI: python -m lm35 [gui | demo | smoke | recalc | ports]"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import tempfile
import time

from . import protocol as P
from .calibration import CalibrationStore
from .demo import FakeSerial, DEMO_TRUE_VFS, true_temp
from .recalc import recalc_csv, RecalcError
from .worker import SerialWorker, SessionSettings


def _data_dir(a):
    from .gui import default_data_dir
    return a.data_dir or default_data_dir()


def demo_adc(i: int, temp_c: float) -> int:
    """ADC (sin ruido) que produce el dispositivo simulado para el sensor i (0-based) a temp_c."""
    return round(0.01 * temp_c * (1 + 0.01 * (i - 2)) / DEMO_TRUE_VFS * P.ADC_MAX + 8)


def cmd_demo(a) -> int:
    out = os.path.join(a.data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datos"),
                       "demo_verificacion")
    os.makedirs(out, exist_ok=True)
    store = CalibrationStore(os.path.join(out, f"DEMO_SIMULADO_calibracion_{int(time.time())}.json"))
    st = SessionSettings(port="DEMO", baud=a.baud, n_sensors=a.sensors, rate_hz=a.rate, oversample=1,
                         experience_label="verificacion_demo")
    w = SerialWorker(lambda: FakeSerial(faults=a.inject_faults), st, store, out, demo=True)
    print(f"[DEMO SIMULADO] {a.sensors} sensores, {a.rate:g} Hz, {a.seconds:g} s, fallos={'sí' if a.inject_faults else 'no'}")
    w.start()
    t0, changed = time.monotonic(), False
    while time.monotonic() - t0 < a.seconds and w.is_alive():
        if not changed and time.monotonic() - t0 > a.seconds / 2:
            for T in (15.0, 25.0, 35.0, 45.0):                      # calibración sintética del sensor 1 a mitad de sesión
                store.add_point(1, demo_adc(0, T), T, "demo")
            store.apply_fit(1)
            changed = True
        time.sleep(0.05)
    w.request_stop()
    w.join(10)
    s = w.stats()
    print("estado:", w.state, "|", w.end_reason)
    print({k: s[k] for k in ("frames_ok", "lost", "malformed", "duplicates", "reboots", "rollovers", "rows_written")})
    print("CSV:", w.csv_path)
    problems = []
    with open(w.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if w.state != "detenido":
        problems.append("la sesión no terminó limpia")
    if len(rows) != s["rows_written"] or len(rows) == 0:
        problems.append("filas CSV != contador del hilo")
    if "DEMO_SIMULADO" not in os.path.basename(w.csv_path) or any(r["demo"] != "SIMULADO" for r in rows):
        problems.append("falta marca SIMULADO")
    revs = {r["calib_revision"] for r in rows if r["sensor_id"] == "1"}
    if revs != {"0", "1"}:
        problems.append(f"revisiones de calibración vistas en sensor 1: {sorted(revs)} (esperado 0 y 1)")
    exp_rows = s["frames_ok"] * a.sensors
    if exp_rows != len(rows):
        problems.append("filas != tramas × sensores")
    if a.inject_faults:
        if s["lost"] < 1 or s["malformed"] < 2 or s["reboots"] < 1 or s["rollovers"] < 1:
            problems.append("no se detectaron todos los fallos inyectados")
    elif s["lost"] or s["malformed"]:
        problems.append("pérdidas/mal formadas sin fallos inyectados")
    if not problems:   # recálculo offline: sensor 1 con ajuste, original intacto
        before = hashlib.sha256(open(w.csv_path, "rb").read()).hexdigest()
        outcsv = os.path.splitext(w.csv_path)[0] + "_recalc.csv"
        try:
            for p_ in (outcsv, outcsv + ".recalc.json"):
                if os.path.exists(p_):
                    os.remove(p_)
            info = recalc_csv(w.csv_path, outcsv, store.path)
            print("recalculado ->", outcsv, f"({info['rows']} filas)")
            if hashlib.sha256(open(w.csv_path, "rb").read()).hexdigest() != before:
                problems.append("el CSV original cambió")
            r1 = [r for r in csv.DictReader(open(outcsv, newline="", encoding="utf-8")) if r["sensor_id"] == "1"][-1]
            err = abs(float(r1["temp_c_recalc"]) - float(r1["temp_c"]))
            print(f"último sensor 1: temp_c(registrada)={r1['temp_c']} recalc={r1['temp_c_recalc']}")
            if err > 0.5:
                problems.append("recálculo con calibración final difiere de lo registrado tras el cambio")
        except RecalcError as e:
            problems.append(f"recalc: {e}")
    print("RESULTADO:", "OK" if not problems else "FALLÓ -> " + "; ".join(problems))
    return 0 if not problems else 1


def cmd_smoke(a) -> int:
    from .gui import App
    d = a.data_dir or tempfile.mkdtemp(prefix="lm35_smoke_")
    app = App(d, demo=True)
    for i, v in enumerate(app.v_enabled): v.set(i < 3)
    app.update()
    app.start_acquisition()
    t0 = time.monotonic()
    while time.monotonic() - t0 < a.seconds:
        app.update_idletasks(); app.update(); time.sleep(0.03)
        app.update_ui()
    n_pts = len(app.lines[0].get_xdata())
    app.fig.savefig(os.path.join(d, "captura_gui_demo.png"), dpi=80)
    try:
        import PIL  # noqa
    except ImportError:
        pass
    app.stop_acquisition()
    t1 = time.monotonic()
    while app.worker.is_alive() and time.monotonic() - t1 < 5:
        app.update(); time.sleep(0.03)
    app.update_ui()
    ok = n_pts > 20 and app.worker.state == "detenido"
    print(f"GUI smoke: puntos graficados={n_pts}, estado={app.worker.state}, PNG={os.path.join(d, 'captura_gui_demo.png')}")
    app.destroy()
    print("RESULTADO:", "OK" if ok else "FALLÓ")
    return 0 if ok else 1


def cmd_recalc(a) -> int:
    try:
        r = recalc_csv(a.input, a.output, a.calib, a.source)
    except RecalcError as e:
        print("Error:", e, file=sys.stderr)
        return 2
    print(f"{r['rows']} filas ({r['rows_invalid']} inválidas) -> {r['output']}")
    return 0


def cmd_ports(_a) -> int:
    import serial.tools.list_ports
    for p in serial.tools.list_ports.comports():
        print(p.device, "-", p.description)
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="lm35")
    ap.add_argument("--data-dir")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gui"); g.add_argument("--demo", action="store_true")
    d = sub.add_parser("demo", help="verificación headless con dispositivo SIMULADO")
    d.add_argument("--seconds", type=float, default=4.0); d.add_argument("--sensors", type=int, default=3)
    d.add_argument("--rate", type=float, default=100.0); d.add_argument("--baud", type=int, default=921600)
    d.add_argument("--inject-faults", action="store_true")
    s = sub.add_parser("smoke", help="prueba de humo de la GUI en modo demo"); s.add_argument("--seconds", type=float, default=3.0)
    r = sub.add_parser("recalc", help="recalcula un CSV crudo a un CSV nuevo")
    r.add_argument("input"); r.add_argument("output"); r.add_argument("--calib", required=True)
    r.add_argument("--source", choices=["active", "refit", "nominal"], default="active")
    sub.add_parser("ports")
    a = ap.parse_args(argv)
    if a.cmd in (None, "gui"):
        from .gui import main as gm
        gm(a.data_dir, getattr(a, "demo", False))
        return 0
    return {"demo": cmd_demo, "smoke": cmd_smoke, "recalc": cmd_recalc, "ports": cmd_ports}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
