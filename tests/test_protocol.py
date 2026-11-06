import unittest
from lm35 import protocol as P


class TestFraming(unittest.TestCase):
    def test_roundtrip(self):
        raw = P.encode("D,1,2,0,3,1,100,2,200")
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(P.decode(raw), "D,1,2,0,3,1,100,2,200")

    def test_bad_checksum(self):
        with self.assertRaises(P.ProtocolError):
            P.decode(b"D,1,2,0,100,200*00\n")

    def test_malformed_lines(self):
        for bad in [b"", b"\n", b"hola\n", b"D,1*Z9\n", b"D,1*1\n", b"*41\n", b"\xff\xfe*00\n",
                    b"A" * 300 + b"*00\n"]:
            with self.assertRaises(P.ProtocolError, msg=repr(bad)):
                P.decode(bad)


class TestData(unittest.TestCase):
    def test_parse_ok(self):
        m = P.parse_line(P.encode("D,10,5000,0,35,1,1,2,2.5,6,4095"), expected_mask=35)
        self.assertEqual(m.kind, "D")
        self.assertEqual(m.frame.seq, 10)
        self.assertEqual(m.frame.t_us, 5000)
        self.assertEqual(m.frame.channels, (1, 2, 6))
        self.assertEqual(m.frame.adc, (1.0, 2.5, 4095.0))

    def test_wrong_field_count(self):
        with self.assertRaises(P.ProtocolError):
            P.parse_line(P.encode("D,10,5000,0,3,1,1"), expected_mask=3)

    def test_adc_limits(self):
        for v in ["4096", "-1", "nan", "inf", "", "abc", "4095.5"]:
            with self.assertRaises(P.ProtocolError, msg=v):
                P.parse_line(P.encode(f"D,1,1,0,1,1,{v}"), expected_mask=1)

    def test_bad_header_fields(self):
        for p in ["D,-1,1,0,1,1,5", "D,x,1,0,1,1,5", "D,1,4294967296,0,1,1,5", "D,1,1,-2,1,1,5"]:
            with self.assertRaises(P.ProtocolError, msg=p):
                P.parse_line(P.encode(p), expected_mask=1)

    def test_other_kinds(self):
        self.assertEqual(P.parse_line(P.encode("ACK,CFG,3,10000,1")).kind, "ACK")
        self.assertEqual(P.parse_line(P.encode("NAK,CFG,RANGO")).kind, "NAK")
        self.assertEqual(P.parse_line(P.encode("HELLO,fw1,77,6")).fields[2], "77")


class TestPlan(unittest.TestCase):
    def test_default_ok(self):
        p = P.plan_config(6, 100, 1, 921600)
        self.assertEqual(p.interval_us, 10000)
        self.assertLess(p.utilisation, 0.2)

    def test_sensor_count_limits(self):
        for n in [0, 7, -1, "x", None, 2.5]:
            with self.assertRaises(P.ConfigError, msg=str(n)):
                P.plan_config(n, 100, 1, 921600)
        for n in range(1, 7):
            P.plan_config(n, 100, 1, 921600)

    def test_rate_limits(self):
        for r in [0, -5, float("nan"), float("inf"), 1001, "abc", 0.05]:
            with self.assertRaises(P.ConfigError, msg=str(r)):
                P.plan_config(1, r, 1, 921600)

    def test_oversample_limits(self):
        for o in [0, 2, 16, 17, -1, 1.5]:
            with self.assertRaises(P.ConfigError):
                P.plan_config(1, 10, o, 921600)

    def test_bandwidth_rejected_at_low_baud(self):
        with self.assertRaises(P.ConfigError):
            P.plan_config(6, 100, 1, 9600)

    def test_adc_time_rejected(self):
        with self.assertRaises(P.ConfigError):
            P.plan_config(6, 1000, 1, 921600)

    def test_gpio_map(self):
        self.assertEqual(P.GPIOS, (32, 33, 34, 35, 36, 39))

    def test_sparse_mask_and_stable_map(self):
        self.assertEqual(P.mask_from_channels((2, 6)), 34)
        p = P.plan_config((2, 6), 100, 1, 921600)
        self.assertEqual((p.mask, p.n), (34, 2))
        f = P.parse_line(P.encode("D,1,2,0,34,2,111,6,666"), 34).frame
        self.assertEqual((f.channels, f.adc), ((2, 6), (111.0, 666.0)))
        with self.assertRaises(P.ProtocolError):
            P.parse_line(P.encode("D,1,2,0,34,1,111,6,666"), 34)


class TestSeqTracker(unittest.TestCase):
    def test_sequence_and_gap(self):
        t = P.SeqTracker()
        self.assertEqual(t.update(0, 0).status, "first")
        self.assertEqual(t.update(1, 10).status, "ok")
        r = t.update(5, 50)
        self.assertEqual((r.status, r.lost), ("gap", 3))

    def test_duplicate(self):
        t = P.SeqTracker()
        t.update(0, 0); t.update(1, 10)
        self.assertEqual(t.update(1, 10).status, "duplicate")

    def test_time_rollover(self):
        t = P.SeqTracker()
        t.update(100, 2**32 - 5000)
        r = t.update(101, 5000)
        self.assertEqual(r.status, "ok")
        self.assertTrue(r.rollover)
        self.assertEqual(r.t_us_unwrapped, 2**32 + 5000)

    def test_reboot_with_hello(self):
        t = P.SeqTracker()
        t.update(100, 1000000); t.update(101, 1010000)
        t.note_hello()
        r = t.update(0, 50)
        self.assertEqual(r.status, "reboot")
        self.assertEqual(r.boot_epoch, 1)
        self.assertEqual(r.t_us_unwrapped, 50)

    def test_reboot_inferred(self):
        t = P.SeqTracker()
        t.update(100, 1000000); t.update(101, 1010000)
        r = t.update(0, 50)
        self.assertEqual(r.status, "reboot")

    def test_seq_wrap_32(self):
        t = P.SeqTracker()
        t.update(2**32 - 1, 100)
        self.assertEqual(t.update(0, 200).status, "ok")


if __name__ == "__main__":
    unittest.main()
