import csv, json, os, tempfile, time, unittest
from datetime import datetime

import serial

from lm35.calibration import CalibrationStore
from lm35.demo import FakeSerial
from lm35.recorder import SessionRecorder, CSV_COLUMNS
from lm35.worker import SerialWorker, SessionSettings, PlotBuffer


def run_worker(tmp, factory, n=2, rate=100.0, secs=1.5, store=None, mid=None, demo=True):
    store = store or CalibrationStore(os.path.join(tmp, "cal.json"))
    st = SessionSettings(port="DEMO" if demo else "COMX", baud=921600, n_sensors=n, rate_hz=rate, oversample=1)
    w = SerialWorker(factory, st, store, os.path.join(tmp, "out"), demo=demo)
    w.start()
    t0 = time.monotonic()
    done_mid = False
    while time.monotonic() - t0 < secs and w.is_alive():
        if mid and not done_mid and time.monotonic() - t0 > secs / 2:
            mid(store)
            done_mid = True
        time.sleep(0.05)
    w.request_stop()
    w.join(10)
    assert not w.is_alive()
    return w, store


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class TestWorkerDemo(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.d.cleanup()

    def test_clean_run_csv_long_rows_and_timestamps(self):
        w, _ = run_worker(self.d.name, lambda: FakeSerial(), n=3)
        self.assertEqual(w.state, "detenido")
        rows = read_rows(w.csv_path)
        self.assertGreater(len(rows), 3 * 80)
        self.assertEqual(list(rows[0].keys()), CSV_COLUMNS)
        self.assertIn("DEMO_SIMULADO", os.path.basename(w.csv_path))
        self.assertTrue(all(r["demo"] == "SIMULADO" for r in rows))
        self.assertEqual({r["sensor_id"] for r in rows}, {"1", "2", "3"})
        # un frame -> n filas con mismo seq
        first_seq = rows[0]["seq"]
        self.assertEqual(sum(1 for r in rows[:3] if r["seq"] == first_seq), 3)
        for r in rows[:5]:
            u = datetime.fromisoformat(r["pc_time_utc"])
            l = datetime.fromisoformat(r["pc_time_local"])
            self.assertIsNotNone(u.utcoffset())
            self.assertEqual(u.utcoffset().total_seconds(), 0)
            self.assertIsNotNone(l.utcoffset())
            self.assertEqual(u, l)
            self.assertNotEqual(r["pc_time_utc"], r["device_time_us"])
        seqs = [int(r["seq"]) for r in rows[::3]]
        self.assertEqual(seqs, sorted(seqs))
        s = w.stats()
        self.assertEqual(s["lost"], 0)
        self.assertEqual(s["malformed"], 0)
        self.assertEqual(s["rows_written"], len(rows))
        meta = json.load(open(w.json_path, encoding="utf-8"))
        self.assertEqual(meta["mode"], "SIMULADO")
        self.assertEqual(meta["settings"]["n_sensors"], 3)
        self.assertIn("calibration_at_start", meta)
        self.assertIn("stats", meta)
        self.assertGreater(len(w.plot.window(0, 10.0, 1000)[0]), 10)

    def test_faults_detected(self):
        w, _ = run_worker(self.d.name, lambda: FakeSerial(faults=True), n=2, secs=2.2)
        s = w.stats()
        self.assertGreaterEqual(s["lost"], 1)
        self.assertGreaterEqual(s["malformed"], 2)
        self.assertGreaterEqual(s["reboots"], 1)
        self.assertGreaterEqual(s["rollovers"], 1)
        rows = read_rows(w.csv_path)
        self.assertEqual({r["boot_epoch"] for r in rows}, {"0", "1"})
        ev = open(w.log_path, encoding="utf-8").read().lower()
        for word in ("pérdida", "reinicio", "desborde", "mal formada"):
            self.assertIn(word, ev)

    def test_calibration_change_logged_and_in_rows(self):
        def mid(store):
            for a, r in [(500, 10.0), (1500, 40.0), (2500, 70.0)]:
                store.add_point(1, a, r)
            store.apply_fit(1)
        w, store = run_worker(self.d.name, lambda: FakeSerial(), n=2, secs=1.6, mid=mid)
        rows = [r for r in read_rows(w.csv_path) if r["sensor_id"] == "1"]
        revs = [r["calib_revision"] for r in rows]
        self.assertEqual(revs[0], "0")
        self.assertEqual(revs[-1], "1")
        self.assertEqual(rows[0]["calib_mode"], "nominal")
        self.assertEqual(rows[-1]["calib_mode"], "calibrado")
        r = rows[-1]
        t = float(r["slope_c_per_count"]) * float(r["adc_raw"]) + float(r["intercept_c"])
        self.assertAlmostEqual(t, float(r["temp_c"]), places=2)
        meta = json.load(open(w.json_path, encoding="utf-8"))
        self.assertEqual(len(meta["calibration_changes"]), 1)
        self.assertEqual(meta["calibration_changes"][0]["snapshot"]["sensors"]["1"]["revision"], 1)

    def test_disconnect(self):
        w, _ = run_worker(self.d.name, lambda: FakeSerial(disconnect_after_frames=40), n=1, secs=1.5)
        self.assertEqual(w.state, "error")
        self.assertIn("desconex", w.end_reason.lower())
        rows = read_rows(w.csv_path)     # archivo cerrado y legible
        self.assertGreaterEqual(len(rows), 30)

    def test_open_failure(self):
        def boom():
            raise serial.SerialException("no existe")
        w, _ = run_worker(self.d.name, boom, n=1, secs=0.3, demo=False)
        self.assertEqual(w.state, "error")

    def test_invalid_config_refused(self):
        store = CalibrationStore(os.path.join(self.d.name, "c.json"))
        st = SessionSettings(port="DEMO", baud=9600, n_sensors=6, rate_hz=100, oversample=1)
        with self.assertRaises(Exception):
            SerialWorker(lambda: FakeSerial(), st, store, self.d.name, demo=True)

    def test_sparse_s2_s6_only_and_real_join(self):
        store = CalibrationStore(os.path.join(self.d.name, "cal.json"))
        st = SessionSettings(port="DEMO", channels=(2, 6), n_sensors=2, rate_hz=80,
                             experience_label="prueba térmica")
        fake = FakeSerial()
        w = SerialWorker(lambda: fake, st, store, os.path.join(self.d.name, "out"), demo=True)
        w.start(); time.sleep(0.5); w.request_stop(); w.join(5)
        self.assertFalse(w.is_alive())
        self.assertEqual(fake.cfg[0], 34)
        rows = read_rows(w.csv_path)
        self.assertEqual({r["sensor_id"] for r in rows}, {"2", "6"})
        self.assertEqual({r["gpio"] for r in rows}, {"33", "39"})
        self.assertEqual({r["experience_label"] for r in rows}, {"prueba térmica"})

    def test_label_photo_copy_and_new_session_after_mask_change(self):
        png = os.path.join(self.d.name, "lugar.png")
        jpg = os.path.join(self.d.name, "lugar.jpg")
        with open(png, "wb") as f: f.write(b"\x89PNG\r\n\x1a\nresto")
        with open(jpg, "wb") as f: f.write(b"\xff\xd8\xff\xe0resto")
        store = CalibrationStore(os.path.join(self.d.name, "cal.json"))
        out = os.path.join(self.d.name, "out")
        def one(channels, photos):
            st = SessionSettings(port="DEMO", channels=channels, n_sensors=len(channels), rate_hz=50,
                                 experience_label="Prueba / calibración", photo_sources=photos)
            w = SerialWorker(lambda: FakeSerial(), st, store, out, demo=True)
            w.start(); time.sleep(0.25); w.request_stop(); w.join(5)
            self.assertFalse(w.is_alive())
            return w
        w1 = one((2, 6), {2: png, 6: jpg})
        w2 = one((1,), {1: png})
        self.assertNotEqual(os.path.dirname(w1.csv_path), os.path.dirname(w2.csv_path))
        self.assertNotIn("/", os.path.basename(w1.csv_path))
        rows = read_rows(w1.csv_path)
        self.assertEqual({r["photo_path"] for r in rows}, {"photos/S2.png", "photos/S6.jpg"})
        for rel in {r["photo_path"] for r in rows}:
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(w1.csv_path), *rel.split("/"))))
        meta = json.load(open(w1.json_path, encoding="utf-8"))
        self.assertEqual(meta["experience_label"], "Prueba / calibración")
        with open(png, "wb") as f: f.write(b"changed")
        self.assertTrue(open(os.path.join(os.path.dirname(w1.csv_path), "photos", "S2.png"), "rb").read().startswith(b"\x89PNG"))

    def test_invalid_photo_rejected(self):
        bad = os.path.join(self.d.name, "fake.png")
        with open(bad, "wb") as f: f.write(b"not an image")
        with self.assertRaises(ValueError):
            SessionRecorder(self.d.name, "t_", {"photo_sources": {1: bad}})


class TestRecorder(unittest.TestCase):
    def test_flush_every_second_and_close(self):
        with tempfile.TemporaryDirectory() as d:
            now = [100.0]
            rec = SessionRecorder(d, "t_", {"n_sensors": 1}, clock=lambda: now[0])
            rec.write_row({c: "x" for c in CSV_COLUMNS})
            rec.flush_if_due()
            self.assertEqual(len(open(rec.csv_path, encoding="utf-8").read().splitlines()), 1)  # solo cabecera
            now[0] += 1.2
            rec.flush_if_due()
            self.assertEqual(len(open(rec.csv_path, encoding="utf-8").read().splitlines()), 2)
            rec.write_row({c: "y" for c in CSV_COLUMNS})
            rec.close({}, "fin")
            self.assertEqual(len(open(rec.csv_path, encoding="utf-8").read().splitlines()), 3)
            self.assertIn("ended_utc", json.load(open(rec.json_path, encoding="utf-8")))


class TestPlotBuffer(unittest.TestCase):
    def test_bounded(self):
        b = PlotBuffer(6, maxlen=500)
        for k in range(100000):
            b.append(0, k * 0.001, 1.0, 2.0)
        self.assertLessEqual(b.size(0), 500)
        t, y = b.window(0, 10.0, 200)
        self.assertLessEqual(len(t), 200)

    def test_smoothing_uses_prefix_before_window_and_smooths_latest_adc_temp(self):
        b = PlotBuffer(1, maxlen=20)
        for k, value in enumerate((0.0, 10.0, 20.0, 30.0)):
            b.append(0, float(k), value * 10.0, value)
        t, y = b.window(0, 1.0, 100, now=3.0, smoothing_count=3)
        self.assertEqual(t, [-1.0, 0.0])
        self.assertEqual(y, [10.0, 20.0])
        self.assertEqual(b.latest(0, smoothing_count=3), (3.0, 200.0, 20.0))

    def test_display_smoothing_does_not_modify_raw_csv(self):
        with tempfile.TemporaryDirectory() as d:
            w, _ = run_worker(d, lambda: FakeSerial(), n=1, secs=0.4)
            with open(w.csv_path, "rb") as f:
                before = f.read()
            w.plot.window(0, 10.0, 100, smoothing_count=20)
            w.plot.latest(0, smoothing_count=20)
            with open(w.csv_path, "rb") as f:
                self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
