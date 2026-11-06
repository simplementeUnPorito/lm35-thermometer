"""Validación manual con placa real SIN sensores: no valida temperaturas.

Ejecutar desde la carpeta del proyecto:
  python -m tests.validar_hardware --port COM8
"""
import argparse
import csv
import json
import os
import time
from pathlib import Path

from lm35.calibration import CalibrationStore
from lm35.gui import App
from lm35.worker import SerialWorker, SessionSettings, open_serial


def check_record(worker, channels):
    if worker.state != "detenido":
        raise RuntimeError(worker.end_reason)
    with open(worker.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    stats = worker.stats()
    assert rows and len(rows) == stats["frames_ok"] * len(channels)
    assert {int(r["sensor_id"]) for r in rows} == set(channels)
    assert all(0 <= float(r["adc_raw"]) <= 4095 for r in rows)
    assert stats["lost"] == 0 and stats["malformed"] == 0, stats
    return {"csv": worker.csv_path, "channels": list(channels), "stats": stats}


def run(port, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    results = []
    app = App(os.path.join(out_dir, "interfaz"), demo=False)
    def pump(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.update()
            time.sleep(0.03)
    try:
        app.v_port.set(port)
        app.v_experience.set("VALIDACION_SIN_SENSORES_COM8")
        for v in app.v_enabled:
            v.set(True)
        for i, v in enumerate(app.v_sensor_names):
            v.set(f"Entrada flotante S{i+1}")
        app.start_acquisition()
        pump(13)
        app.stop_acquisition()
        assert app._mode == "frozen"
        app.worker.join(5)
        pump(1)
        assert app._mode == "history", app._mode
        assert app._history.duration > 10
        initial = app.ax.get_xlim()
        app.pan_view(-3)
        moved = app.ax.get_xlim()
        assert moved[0] < initial[0]
        pump(0.5)
        assert app.ax.get_xlim() == moved
        app.view_all()
        assert app.ax.get_xlim()[0] == 0
        app.fig.savefig(os.path.join(out_dir, "grafico_sin_sensores.png"))
        results.append(check_record(app.worker, (1, 2, 3, 4, 5, 6)))
    finally:
        if app.worker:
            app.worker.request_stop()
            app.worker.join(5)
        app.destroy()

    store = CalibrationStore(os.path.join(out_dir, "calibracion_solo_validacion.json"))
    for channels, rate in [((2, 6), 100), ((1, 2, 3, 4, 5, 6), 400)]:
        settings = SessionSettings(port=port, channels=channels, rate_hz=rate,
                                   experience_label=f"SIN_SENSORES_{rate}Hz")
        worker = SerialWorker(lambda: open_serial(port, 921600), settings, store, out_dir)
        worker.start()
        deadline = time.monotonic() + 10
        while worker.state != "adquiriendo" and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(2)
        worker.request_stop()
        worker.join(5)
        assert not worker.is_alive(), "El hilo no cerró"
        result = check_record(worker, channels)
        result["requested_rate_hz"] = rate
        results.append(result)
    report = {"port": port, "warning": "SIN SENSORES: entradas flotantes; no valida temperaturas",
              "results": results}
    Path(out_dir, "resultado_hardware.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM8")
    parser.add_argument("--out", default="datos/validacion_hardware_sin_sensores")
    args = parser.parse_args()
    run(args.port, args.out)
