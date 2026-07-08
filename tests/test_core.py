# Unit tests for the hardware-independent firmware core.
# Run from the repository root: python3 -m unittest discover -s tests

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "firmware"))

from core import (  # noqa: E402
    EVENT_PRESS,
    EventQueue,
    MatrixDebouncer,
    event_column,
    event_row,
)


class TestMatrixDebouncer(unittest.TestCase):
    def setUp(self):
        self.d = MatrixDebouncer(7)

    def test_clean_press_reports_on_second_stable_sample(self):
        self.assertEqual(self.d.sample(0, 0x01, 0), 0)
        self.assertEqual(self.d.sample(0, 0x01, 0), 0x01)
        self.assertEqual(self.d.reported[0], 0x01)

    def test_press_then_release(self):
        self.d.sample(0, 0x01, 0)
        self.d.sample(0, 0x01, 0)  # press reported
        self.assertEqual(self.d.sample(0, 0x00, 0), 0)
        self.assertEqual(self.d.sample(0, 0x00, 0), 0x01)  # release
        self.assertEqual(self.d.reported[0], 0x00)

    def test_one_sample_glitch_reports_nothing(self):
        # released -> one noise sample high -> released again: the old
        # AVR logic emitted an unmatched release here
        self.assertEqual(self.d.sample(0, 0x01, 0), 0)
        self.assertEqual(self.d.sample(0, 0x00, 0), 0)
        self.assertEqual(self.d.sample(0, 0x00, 0), 0)
        self.assertEqual(self.d.sample(0, 0x00, 0), 0)
        self.assertEqual(self.d.reported[0], 0x00)

    def test_dropout_glitch_while_held_reports_nothing(self):
        self.d.sample(0, 0x01, 0)
        self.d.sample(0, 0x01, 0)  # press reported
        for rows in (0x00, 0x01, 0x01, 0x01):
            self.assertEqual(self.d.sample(0, rows, 0), 0)
        self.assertEqual(self.d.reported[0], 0x01)

    def test_forced_rows_never_reported(self):
        # a forced (simulated) key press can read back as active but
        # must not be echoed to the host
        self.assertEqual(self.d.sample(0, 0x01, 0x01), 0)
        self.assertEqual(self.d.sample(0, 0x01, 0x01), 0)
        self.assertEqual(self.d.reported[0], 0x00)
        # after unforcing, the return to the real state is silent too
        self.assertEqual(self.d.sample(0, 0x00, 0), 0)
        self.assertEqual(self.d.sample(0, 0x00, 0), 0)
        self.assertEqual(self.d.reported[0], 0x00)

    def test_real_key_reported_alongside_forced_key(self):
        self.d.sample(0, 0x05, 0x04)  # row 2 forced, row 0 real
        self.assertEqual(self.d.sample(0, 0x05, 0x04), 0x01)

    def test_multiple_rows_report_together(self):
        self.d.sample(0, 0x05, 0)
        self.assertEqual(self.d.sample(0, 0x05, 0), 0x05)

    def test_columns_are_independent(self):
        self.d.sample(0, 0x01, 0)
        self.d.sample(1, 0x02, 0)
        self.assertEqual(self.d.sample(0, 0x01, 0), 0x01)
        self.assertEqual(self.d.sample(1, 0x02, 0), 0x02)

    def test_row_mask_applied(self):
        d = MatrixDebouncer(1, row_mask=0x0F)
        d.sample(0, 0xF1, 0)
        self.assertEqual(d.sample(0, 0xF1, 0), 0x01)


class TestEventQueue(unittest.TestCase):
    def test_fifo_order(self):
        q = EventQueue(8)
        for ev in (1, 2, 3):
            self.assertTrue(q.put(ev))
        self.assertEqual([q.get(), q.get(), q.get()], [1, 2, 3])
        self.assertEqual(q.get(), -1)

    def test_overflow_drops_and_counts(self):
        q = EventQueue(8)  # holds size-1 = 7 events
        results = [q.put(i) for i in range(9)]
        self.assertEqual(results, [True] * 7 + [False] * 2)
        self.assertEqual(q.overflows, 2)
        self.assertEqual([q.get() for _ in range(7)], list(range(7)))
        self.assertEqual(q.get(), -1)

    def test_size_must_be_power_of_two(self):
        with self.assertRaises(ValueError):
            EventQueue(10)

    def test_event_encoding_round_trip(self):
        for row in range(8):
            for col in range(7):
                ev = EVENT_PRESS | (row << 3) | col
                self.assertEqual(event_row(ev), row)
                self.assertEqual(event_column(ev), col)
                self.assertTrue(ev & EVENT_PRESS)


if __name__ == "__main__":
    unittest.main()
