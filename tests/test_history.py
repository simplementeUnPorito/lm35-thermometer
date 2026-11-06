import csv
import json
import os
import queue
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from lm35.history import HistoryError, load_history
from lm35.recorder import CSV_COLUMNS

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def write_csv(path, n, hz=100.0, sensors=(1,), reboot_at=None, spike_at=None):
    """CSV crudo sintético; device_time_s empieza en 5 s (no en 0). reboot_at: índice donde el ESP32 reinicia."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        epoch, base = 0, 5.0
        for k in range(n):
            if reboot_at is not None and k == reboot_at:
                epoch, base = 1, 0.5 - k / hz          # tiempo del dispositivo vuelve casi a 0
            for sid in sensors:
                temp = 20.0 + sid + (50.0 if k == spike_at else 0.0)
                w.writerow({"pc_time_utc": (T0 + timedelta(seconds=k / hz)).isoformat(),
                            "device_time_s": f"{base + k / hz:.6f}", "boot_epoch": epoch,
                            "sensor_id": sid, "adc_raw": 1, "temp_c": f"{temp:.3f}"})


class TestHistory(unittest.TestCase):
    def test_full_duration_beyond_plotbuffer_and_earliest_samples(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.csv")
            write_csv(p, 20000, sensors=(2, 5), spike_at=7)          # 200 s >> 6000 puntos del PlotBuffer
            h = load_history(p)
            self.assertAlmostEqual(h.duration, 199.99, places=2)
            self.assertEqual(h.channels, [1, 4])
            self.assertEqual(h.t[1][0], 0.0)                         # primera muestra normalizada a 0
            self.assertEqual(len(h.t[1]), 20000)
            t, y = h.view(1, 0.0, 200.0, max_points=1000)
            self.assertLessEqual(len(t), 1100)
            self.assertAlmostEqual(float(y.max()), 72.0)             # el pico temprano sobrevive al diezmado
            self.assertEqual(float(t[0]), 0.0)

    def test_reboot_epoch_keeps_monotonic_time(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.csv")
            write_csv(p, 1000, reboot_at=500)
            h = load_history(p)
            self.assertGreater(h.duration, 9.9)
            self.assertTrue((h.t[0][1:] >= h.t[0][:-1]).all())

    def test_bad_csv(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.csv")
            open(p, "w").close()
            with self.assertRaises(HistoryError):
                load_history(p)
            with self.assertRaises(HistoryError):
                load_history(os.path.join(d, "no_existe.csv"))

    def test_smoothing_is_consistent_across_views_and_changes_width(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.csv")
            write_csv(p, 10, hz=1.0, spike_at=4)
            h = load_history(p)
            _, full = h.view(0, 0.0, 9.0, max_points=100, smoothing_count=3)
            t_zoom, zoom = h.view(0, 4.0, 7.0, max_points=100, smoothing_count=3)
            for t, value in zip(t_zoom, zoom):
                idx = int((h.t[0] == t).nonzero()[0][0])
                self.assertEqual(value, full[idx])
            self.assertNotEqual(float(h.view(0, 0, 9, 100, smoothing_count=1)[1][4]), float(full[4]))


class FakeWorker:
    """Hilo ya terminado con un CSV real, para ejercitar la GUI sin esperas."""
    demo = True
    state = "detenido"
    end_reason = ""

    def __init__(self, csv_path, channels=(1,)):
        self.csv_path, self.channels = csv_path, channels
        self.events = queue.Queue()
        self.settings = SimpleNamespace(experience_label="")

    def is_alive(self): return False
    def request_stop(self): pass
    def join(self, *_): pass

    def stats(self):
        keys = "frames_ok lost malformed duplicates reboots rollovers late_samples rows_written".split()
        return {**{k: 0 for k in keys}, "state": "detenido", "last_frame_age_s": 0, "elapsed_s": 0}


def mouse(app, name, x, y, button=1):
    from matplotlib.backend_bases import MouseEvent
    ev = MouseEvent(name, app.canvas, x, y, button=button)
    app.canvas.callbacks.process(name, ev)


class TestGuiHistory(unittest.TestCase):
    def setUp(self):
        from lm35.gui import App
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.tmp.name, demo=True)
        self.app.v_pc_average.set(1)
        self.app.geometry("1340x700+0+0")
        self.app.update()

    def tearDown(self):
        w = self.app.worker
        if w is not None and hasattr(w, "join") and w.is_alive():
            w.request_stop(); w.join(5)
        self.app.destroy()
        self.tmp.cleanup()

    def load(self, n=20000, spike_at=None):
        p = os.path.join(self.tmp.name, "raw.csv")
        write_csv(p, n, spike_at=spike_at)
        self.app.worker = FakeWorker(p)
        self.app._mode = "live"
        self.app.update_ui()                      # detecta fin -> congela -> inicia carga única
        self.assertEqual(self.app._mode, "frozen")
        self.app._loader.join(10)
        self.app.update_ui()
        self.assertEqual(self.app._mode, "history")

    def test_stop_freezes_immediately_and_survives_ticks(self):
        app = self.app
        app.start_acquisition()
        end = time.monotonic() + 3
        while app.plot.size(0) < 50 and time.monotonic() < end:
            app.update(); time.sleep(0.02)
        app.update_ui()
        app.stop_acquisition()
        self.assertEqual(app._mode, "frozen")
        snap = (list(app.lines[0].get_xdata()), list(app.lines[0].get_ydata()), app.ax.get_xlim(), app.ax.get_ylim())
        self.assertGreater(len(snap[0]), 0)
        # Hold the loader here: a fast stop may otherwise legitimately switch to
        # the complete CSV view while we are checking the temporary frozen view.
        with patch.object(app, "_begin_history_load"):
            for _ in range(3):
                app.plot.append(0, time.monotonic(), 4000.0, 400.0)
                app.update_ui()
                self.assertEqual((list(app.lines[0].get_xdata()), list(app.lines[0].get_ydata()),
                                  app.ax.get_xlim(), app.ax.get_ylim()), snap)
            app.ax.set_xlim(-7.0, -2.0)
            app.v_pc_average.set(5)
            frozen = (list(app.lines[0].get_xdata()), list(app.lines[0].get_ydata()))
            frozen_raw = (list(app.raw_lines[0].get_xdata()), list(app.raw_lines[0].get_ydata()))
            self.assertTrue(app.raw_lines[0].get_visible())
            self.assertEqual(frozen_raw[0], frozen[0])
            self.assertEqual(app.ax.get_xlim(), (-7.0, -2.0))
            app.plot.append(0, time.monotonic(), 8000.0, 800.0)
            app.update_ui()
            self.assertEqual((list(app.lines[0].get_xdata()), list(app.lines[0].get_ydata())), frozen)
            self.assertEqual((list(app.raw_lines[0].get_xdata()), list(app.raw_lines[0].get_ydata())), frozen_raw)
            self.assertEqual(app.ax.get_xlim(), (-7.0, -2.0))
        app.worker.join(5)
        app._begin_history_load(app.worker.csv_path)
        app._loader.join(10)
        app.update_ui()
        self.assertEqual(app._mode, "history")
        self.assertGreater(app._history.duration, 0.1)

    def test_history_pan_zoom_survive_ticks_and_restart_resets(self):
        app = self.app
        self.load()
        dur = app._history.duration
        self.assertAlmostEqual(dur, 199.99, places=2)
        self.assertAlmostEqual(app.ax.get_xlim()[1] - app.ax.get_xlim()[0], 10.0, places=6)
        self.assertAlmostEqual(app.ax.get_xlim()[1], dur, places=6)
        app.canvas.draw()
        bb = app.ax.bbox
        x, y = bb.x0 + bb.width / 2, bb.y0 + bb.height / 2
        before = app.ax.get_xlim()
        mouse(app, "button_press_event", x, y)
        mouse(app, "motion_notify_event", x + bb.width / 2, y)               # arrastrar a la derecha = ir atrás
        mouse(app, "button_release_event", x + bb.width / 2, y)
        panned = app.ax.get_xlim()
        self.assertAlmostEqual(panned[0], before[0] - 5.0, delta=0.05)
        for _ in range(3):
            app.update_ui()
        self.assertEqual(app.ax.get_xlim(), panned)
        app.pan_view(-1e6)                                                   # límite inferior: 0
        self.assertEqual(app.ax.get_xlim()[0], 0.0)
        span = app.ax.get_xlim()[1]
        mouse(app, "scroll_event", x, y, button="down")                       # alejar
        self.assertGreater(app.ax.get_xlim()[1] - app.ax.get_xlim()[0], span)
        app.view_all()
        self.assertEqual(app.ax.get_xlim(), (0.0, app._history.duration))
        self.assertLessEqual(len(app.lines[0].get_xdata()), 1600)
        self.assertEqual(float(app.lines[0].get_xdata()[0]), 0.0)
        app.zoom_view(0.1, 100.0)
        self.assertLess(app.ax.get_xlim()[1] - app.ax.get_xlim()[0], dur)
        # Nueva sesión: vuelve a la ventana deslizante y descarta el historial
        app.worker = None
        app.start_acquisition()
        self.assertEqual((app._mode, app.ax.get_xlim(), app._history), ("live", (-10.0, 0.0), None))
        self.assertFalse(hasattr(app, "btn_fit"))
        app.worker.request_stop(); app.worker.join(5)

    def test_old_oversample_and_nominal_vfs_ignored(self):
        from lm35.calibration import DEFAULT_VFS
        from lm35.gui import App
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "ajustes.json"), "w", encoding="utf-8") as f:
                json.dump({"os": 8, "rate": "50"}, f)
            with open(os.path.join(d, "DEMO_SIMULADO_calibracion.json"), "w", encoding="utf-8") as f:
                json.dump({"format": 1, "nominal_vfs": 3.3, "sensors": {}}, f)
            app = App(d, demo=True)
            try:
                self.assertEqual(app._read_cfg().oversample, 1)
                self.assertEqual(app.store.nominal_vfs, DEFAULT_VFS)
                self.assertFalse(hasattr(app, "v_os") or hasattr(app, "v_vfs"))
                self.assertEqual(app.v_pc_average.get(), 20)
                app._save_settings()
                with open(os.path.join(d, "ajustes.json"), encoding="utf-8") as f:
                    saved = json.load(f)
                    self.assertNotIn("os", saved)
                    self.assertEqual(saved["pc_average_samples"], 20)
            finally:
                app.destroy()

    def test_average_change_redraws_history_without_resetting_pan(self):
        self.load(spike_at=19950)
        self.app.pan_view(-0.2)
        before = self.app.ax.get_xlim()
        raw = list(self.app.lines[0].get_ydata())
        self.app.v_pc_average.set(5)
        self.app.update_idletasks()
        self.assertEqual(self.app.ax.get_xlim(), before)
        self.assertNotEqual(list(self.app.lines[0].get_ydata()), raw)
        self.assertTrue(self.app.raw_lines[0].get_visible())
        self.assertEqual(max(self.app.raw_lines[0].get_ydata()), 71.0)
        self.assertEqual(max(self.app.lines[0].get_ydata()), 31.0)
        self.assertEqual(self.app.raw_lines[0].get_zorder(), 1)
        self.assertEqual(self.app.lines[0].get_zorder(), 2)
        self.assertEqual(self.app.ax.get_xlim(), before)
        stable = list(self.app.lines[0].get_ydata())
        self.app.update_ui()
        self.assertEqual(list(self.app.lines[0].get_ydata()), stable)
        self.app.v_pc_average.set(1)
        self.app.update_idletasks()
        self.assertFalse(self.app.raw_lines[0].get_visible())


class TestFirmwareSource(unittest.TestCase):
    def test_single_analogread_no_oversample(self):
        path = os.path.join(os.path.dirname(__file__), "..", "firmware", "lm35_stream", "lm35_stream.ino")
        src = open(path, encoding="utf-8").read()
        self.assertEqual(src.count("analogRead("), 1)
        self.assertNotIn("oversample", src)
        self.assertIn("os != 1", src)


if __name__ == "__main__":
    unittest.main()
