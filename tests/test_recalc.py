import csv, hashlib, json, os, tempfile, unittest

from lm35.calibration import CalibrationStore
from lm35.recorder import CSV_COLUMNS
from lm35.recalc import recalc_csv, RecalcError


def make_raw(path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for k, (sid, adc) in enumerate([(1, 100), (1, 200), (2, 300), (1, "nan"), (2, 400)]):
            row = {c: "" for c in CSV_COLUMNS}
            row.update(seq=k, sensor_id=sid, adc_raw=adc, temp_c="9.99", calib_mode="nominal",
                       calib_revision=0, pc_time_utc="2026-01-01T00:00:00.000+00:00")
            w.writerow(row)


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


class TestRecalc(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.raw = os.path.join(self.d.name, "raw.csv")
        make_raw(self.raw)
        self.store = CalibrationStore(os.path.join(self.d.name, "cal.json"))
        for a, r in [(100, 10), (300, 30), (500, 50)]:
            self.store.add_point(1, a, r)
        self.store.apply_fit(1)    # T = 0.1*adc

    def tearDown(self):
        self.d.cleanup()

    def test_recalc_preserves_original(self):
        before = sha(self.raw)
        out = os.path.join(self.d.name, "new.csv")
        res = recalc_csv(self.raw, out, os.path.join(self.d.name, "cal.json"))
        self.assertEqual(sha(self.raw), before)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["temp_c"], "9.99")          # original intacto
        self.assertAlmostEqual(float(rows[0]["temp_c_recalc"]), 10.0, 3)
        self.assertEqual(rows[0]["recalc_revision"], "1")
        self.assertEqual(rows[0]["recalc_mode"], "calibrado")
        self.assertEqual(rows[2]["recalc_mode"], "nominal")     # sensor 2 sin calibrar
        self.assertEqual(rows[3]["temp_c_recalc"], "")           # ADC inválido
        self.assertEqual(res["rows_invalid"], 1)
        meta = json.load(open(out + ".recalc.json", encoding="utf-8"))
        self.assertEqual(meta["source_sha256"], before)

    def test_refuses_overwrite(self):
        with self.assertRaises(RecalcError):
            recalc_csv(self.raw, self.raw, os.path.join(self.d.name, "cal.json"))
        out = os.path.join(self.d.name, "x.csv")
        recalc_csv(self.raw, out, os.path.join(self.d.name, "cal.json"))
        with self.assertRaises(RecalcError):
            recalc_csv(self.raw, out, os.path.join(self.d.name, "cal.json"))

    def test_nominal_forced_and_bad_input(self):
        out = os.path.join(self.d.name, "n.csv")
        recalc_csv(self.raw, out, os.path.join(self.d.name, "cal.json"), source="nominal")
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        self.assertEqual(rows[0]["recalc_mode"], "nominal")
        junk = os.path.join(self.d.name, "junk.csv")
        open(junk, "w").write("a,b\n1,2\n")
        with self.assertRaises(RecalcError):
            recalc_csv(junk, os.path.join(self.d.name, "j2.csv"), os.path.join(self.d.name, "cal.json"))


if __name__ == "__main__":
    unittest.main()
