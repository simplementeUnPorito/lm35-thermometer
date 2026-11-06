import json, os, tempfile, unittest
from lm35 import calibration as C


def pts(pairs):
    return [C.Point(adc=a, ref_c=r, timestamp="2026-01-01T00:00:00+00:00", source="manual") for a, r in pairs]


class TestFit(unittest.TestCase):
    def test_exact_line(self):
        f = C.fit_linear(pts([(100, 10), (200, 20), (400, 40)]))
        self.assertAlmostEqual(f.slope, 0.1, 9)
        self.assertAlmostEqual(f.intercept, 0.0, 7)
        self.assertAlmostEqual(f.r2, 1.0, 9)
        self.assertEqual(f.n, 3)

    def test_two_points_minimal(self):
        f = C.fit_linear(pts([(100, 10), (500, 50)]))
        self.assertIn("mínima", f.quality.lower())
        self.assertAlmostEqual(f.slope, 0.1)

    def test_degenerate_cases(self):
        bad = {
            "vacio": [],
            "uno": [(100, 10)],
            "adc iguales": [(100, 10), (100, 30), (100, 50)],
            "adc casi iguales": [(100, 10), (100.5, 30)],
            "ref iguales": [(100, 20), (200, 20), (300, 20)],
            "pendiente negativa": [(100, 50), (400, 10)],
            "nan adc": [(float("nan"), 10), (200, 20)],
            "nan ref": [(100, float("nan")), (200, 20)],
            "inf": [(100, 10), (float("inf"), 20)],
            "adc fuera": [(100, 10), (5000, 20)],
            "adc negativo": [(-3, 10), (200, 20)],
            "ref fuera": [(100, 10), (200, 900)],
        }
        for name, p in bad.items():
            with self.assertRaises(C.CalibrationError, msg=name):
                C.fit_linear(pts(p))

    def test_noisy_quality_lower(self):
        good = C.fit_linear(pts([(100, 10), (300, 30), (500, 50), (700, 70)]))
        noisy = C.fit_linear(pts([(100, 10), (300, 36), (500, 44), (700, 70)]))
        self.assertLess(noisy.r2, good.r2)
        self.assertGreater(noisy.rmse, good.rmse)
        self.assertGreater(noisy.max_abs_resid, 0)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.d.name, "cal.json")
        self.s = C.CalibrationStore(self.path, nominal_vfs=2.2)

    def tearDown(self):
        self.d.cleanup()

    def test_nominal(self):
        c = self.s.coeffs(1)
        self.assertEqual(c.mode, "nominal")
        self.assertEqual(c.revision, 0)
        self.assertAlmostEqual(c.apply(4095), 2.2 * 100, 6)

    def test_apply_revision_history_persist(self):
        for a, r in [(100, 10), (300, 30), (500, 50)]:
            self.s.add_point(2, a, r, source="captura")
        c = self.s.apply_fit(2)
        self.assertEqual((c.mode, c.revision), ("calibrado", 1))
        self.assertEqual(self.s.version, 1)
        self.s.add_point(2, 700, 70)
        c2 = self.s.apply_fit(2)
        self.assertEqual(c2.revision, 2)
        self.s.save()
        s2 = C.CalibrationStore(self.path)
        self.assertEqual(s2.coeffs(2).revision, 2)
        self.assertEqual(len(s2.points(2)), 4)
        self.assertAlmostEqual(s2.coeffs(2).apply(250), 25.0, 6)
        hist = os.path.join(self.d.name, "cal_history.jsonl")
        lines = open(hist, encoding="utf-8").read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        h = json.loads(lines[-1])
        self.assertEqual(h["sensor_id"], 2)
        self.assertEqual(h["revision"], 2)
        self.assertIn("points", h)

    def test_failed_fit_does_not_change(self):
        self.s.add_point(1, 100, 10)
        with self.assertRaises(C.CalibrationError):
            self.s.apply_fit(1)
        self.assertEqual(self.s.coeffs(1).revision, 0)
        self.assertEqual(self.s.version, 0)

    def test_revert_and_remove(self):
        self.s.add_point(1, 100, 10); self.s.add_point(1, 300, 30)
        self.s.apply_fit(1)
        self.s.remove_points(1, [0])
        self.assertEqual(len(self.s.points(1)), 1)
        self.assertEqual((self.s.coeffs(1).mode, self.s.coeffs(1).revision), ("nominal", 2))
        self.assertEqual(len(C.CalibrationStore(self.path).points(1)), 1)
        c = self.s.revert_nominal(1)
        self.assertEqual((c.mode, c.revision), ("nominal", 3))
        with self.assertRaises(C.CalibrationError):
            self.s.remove_points(1, [5])

    def test_reset_only_selected_sensor_persists_nominal_revision(self):
        for sid in (2, 3):
            self.s.add_point(sid, 100, 10)
            self.s.add_point(sid, 300, 30)
            self.s.apply_fit(sid)
        before_other = self.s.snapshot()["sensors"]["3"]
        c = self.s.reset_sensor(2)
        self.assertEqual((c.mode, c.revision), ("nominal", 2))
        self.assertEqual(self.s.points(2), [])
        self.assertEqual(self.s.snapshot()["sensors"]["3"], before_other)
        loaded = C.CalibrationStore(self.path)
        self.assertEqual(loaded.points(2), [])
        self.assertEqual((loaded.coeffs(2).mode, loaded.coeffs(2).revision), ("nominal", 2))
        self.assertEqual(len(loaded.points(3)), 2)
        with open(self.s.history_path, encoding="utf-8") as f:
            history = [json.loads(line) for line in f]
        reset = history[-1]
        self.assertEqual(reset["event"], "reset_sensor")
        self.assertEqual(len(reset["prior_points"]), 2)

    def test_add_point_validation(self):
        for a, r in [(float("nan"), 1), (1, float("inf")), (-1, 5), (5000, 5), ("x", 5)]:
            with self.assertRaises(C.CalibrationError):
                self.s.add_point(1, a, r)
        with self.assertRaises(C.CalibrationError):
            self.s.add_point(7, 1, 1)

    def test_csv_roundtrip_and_bad_rows(self):
        self.s.add_point(1, 100, 10, timestamp="2026-02-03T04:05:06+00:00")
        self.s.add_point(3, 200.5, 20)
        out = os.path.join(self.d.name, "p.csv")
        self.s.export_points_csv(out)
        s2 = C.CalibrationStore(os.path.join(self.d.name, "o.json"))
        added, errs = s2.import_points_csv(out)
        self.assertEqual((added, errs), (2, []))
        self.assertEqual(s2.points(1)[0].timestamp, "2026-02-03T04:05:06+00:00")
        self.assertEqual(s2.points(3)[0].adc, 200.5)
        bad = os.path.join(self.d.name, "bad.csv")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("sensor_id,timestamp_utc,adc,ref_c,source,note\n"
                    "1,2026-01-01T00:00:00+00:00,100,10,manual,\n"
                    "9,,100,10,manual,\n1,,nan,10,manual,\n1,,abc,1,manual,\n")
        s3 = C.CalibrationStore(os.path.join(self.d.name, "o3.json"))
        added, errs = s3.import_points_csv(bad)
        self.assertEqual(added, 1)
        self.assertEqual(len(errs), 3)

    def test_corrupt_json_raises(self):
        with open(self.path, "w") as f:
            f.write("{no json")
        with self.assertRaises(C.CalibrationError):
            C.CalibrationStore(self.path)


if __name__ == "__main__":
    unittest.main()
