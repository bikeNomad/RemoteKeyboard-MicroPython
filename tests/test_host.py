# Unit tests for the host-side keyboard library.
# Run from the repository root: python3 -m unittest discover -s tests

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

from remotekeyboard_host import BrotherPTouchHomeAndHobby, Key  # noqa: E402


class FakeTransport:
    def __init__(self):
        self.written = []
        self.incoming = ""

    def write(self, data):
        self.written.append(data)

    def read_available(self, timeout=0.0, max_bytes=1000):
        data, self.incoming = self.incoming, ""
        return data


class TestDriveKeyboard(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.kbd = BrotherPTouchHomeAndHobby(self.transport)

    def test_digit_wrapped_in_shift(self):
        self.kbd.drive_text("5")  # '5' is shift-T, keycode 25
        self.assertEqual(
            self.transport.written, ["p12\r", "p25\r", "r25\r", "r12\r"]
        )

    def test_digit_with_num_lock_needs_no_shift(self):
        self.kbd.num_lock_state = True
        self.kbd.drive_text("5")
        self.assertEqual(self.transport.written, ["p25\r", "r25\r"])

    def test_lowercase_letter_without_caps(self):
        self.kbd.drive_text("a")  # 'A' key is keycode 64
        self.assertEqual(self.transport.written, ["p64\r", "r64\r"])

    def test_uppercase_letter_taps_caps_lock_first(self):
        self.kbd.drive_text("A")  # caps_lock is keycode 62
        self.assertEqual(
            self.transport.written, ["p62\r", "r62\r", "p64\r", "r64\r"]
        )
        self.assertTrue(self.kbd.caps_lock_state)

    def test_ctrl_u_sends_shift_clear(self):
        self.kbd.drive_text("\x15")  # clear is shift of keycode 31
        self.assertEqual(
            self.transport.written, ["p12\r", "p31\r", "r31\r", "r12\r"]
        )

    def test_drive_named_key(self):
        self.kbd.drive(Key("l_arrow"))  # keycode 51
        self.assertEqual(self.transport.written, ["p51\r", "r51\r"])


class TestInterpretEvents(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.kbd = BrotherPTouchHomeAndHobby(self.transport)

    def poll(self, data):
        self.transport.incoming = data
        return self.kbd.poll_events()

    def test_press_prints_lowercase_without_caps(self):
        self.assertEqual(self.poll("p64\r\nr64\r\n"), "a")

    def test_event_split_across_reads(self):
        self.assertEqual(self.poll("p6"), "")
        self.assertEqual(self.poll("4\r\n"), "a")

    def test_caps_lock_event_toggles_state(self):
        text = self.poll("p62\r\nr62\r\np64\r\nr64\r\n")
        self.assertEqual(text, "A")
        self.assertTrue(self.kbd.caps_lock_state)

    def test_shift_makes_digit(self):
        # shift (52) held while T (25) pressed yields '5'
        self.assertEqual(self.poll("p52\r\np25\r\nr25\r\nr52\r\n"), "5")

    def test_non_event_lines_are_discarded(self):
        self.assertEqual(self.poll("RemoteKeyboard v2.0 (MicroPython)\r\n"), "")
        self.assertEqual(self.kbd._rx_buf, "")
        self.assertEqual(self.poll("p64\r\n"), "a")

    def test_unknown_keycode_warns_and_prints_nothing(self):
        self.assertEqual(self.poll("p77\r\n"), "")


if __name__ == "__main__":
    unittest.main()
