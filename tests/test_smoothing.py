import unittest

import numpy as np

from lm35.smoothing import moving_average


class TestMovingAverage(unittest.TestCase):
    def test_causal_partial_prefix(self):
        np.testing.assert_allclose(
            moving_average([2.0, 4.0, 8.0, 10.0], 3),
            [2.0, 3.0, 14.0 / 3.0, 22.0 / 3.0],
        )

    def test_channels_are_independent_and_one_is_unchanged(self):
        a = np.array([1.0, 10.0, 100.0])
        b = np.array([7.0, 7.0, 7.0])
        np.testing.assert_array_equal(moving_average(a, 1), a)
        np.testing.assert_allclose(moving_average(a, 2), [1.0, 5.5, 55.0])
        np.testing.assert_allclose(moving_average(b, 2), b)

    def test_rejects_invalid_count(self):
        with self.assertRaises(ValueError):
            moving_average([1.0], 0)

    def test_frozen_average_excludes_samples_received_after_stop(self):
        from lm35.worker import PlotBuffer
        buffer = PlotBuffer()
        for t, value in [(1, 10), (2, 20), (3, 300)]:
            buffer.append(0, t, value, value)
        t, values = buffer.window(0, 10, 100, now=2, smoothing_count=2)
        self.assertEqual(t, [-1, 0])
        self.assertEqual(values, [10, 15])


if __name__ == "__main__":
    unittest.main()
