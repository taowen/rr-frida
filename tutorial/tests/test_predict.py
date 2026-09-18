"""Tests for the logical-clock interpretation.

    python3 -m unittest tutorial.tests.test_predict
"""

from __future__ import annotations

import struct
import unittest

from tutorial.compare_predict import _f32, elapsed_values


class FakeTrace:
    def __init__(self, readings):
        self.metadata = {"clock_readings": readings}


def reading(ns: int) -> dict:
    return {"seconds": ns // 1_000_000_000, "nanos": ns % 1_000_000_000}


class TestElapsed(unittest.TestCase):
    def test_first_call_is_zero(self):
        trace = FakeTrace([reading(1_000_000), reading(1_030_000)])
        self.assertEqual(elapsed_values(trace)[0], 0.0)

    def test_gap_in_milliseconds(self):
        trace = FakeTrace([reading(1_000_000), reading(31_000_000)])
        values = elapsed_values(trace)
        self.assertEqual(len(values), 2)
        self.assertAlmostEqual(values[1], 30.0, places=4)

    def test_single_reading(self):
        trace = FakeTrace([reading(5_000_000)])
        self.assertEqual(elapsed_values(trace), [0.0])

    def test_no_readings_raises(self):
        with self.assertRaises(Exception):
            elapsed_values(FakeTrace([]))

    def test_float_boundary_matches_c(self):
        # The nanosecond difference is narrowed to float before dividing. Doing
        # it in double and narrowing at the end is off by one ULP for this gap,
        # which is why the adapter must use the C path.
        gap_ns = 57_087_246
        trace = FakeTrace([reading(0), reading(gap_ns)])
        value = elapsed_values(trace)[1]
        c_path = _f32(_f32(float(gap_ns)) / _f32(1000000.0))
        double_path = _f32(gap_ns / 1e6)
        self.assertEqual(struct.pack("<f", value), struct.pack("<f", c_path))
        self.assertNotEqual(struct.pack("<f", c_path), struct.pack("<f", double_path))


if __name__ == "__main__":
    unittest.main()
