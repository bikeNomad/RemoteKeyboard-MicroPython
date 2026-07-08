# Integration tests for the matrix scanner, run under CPython with a
# fake `machine` module simulating the keyboard matrix lines. Uses the
# generic (Pin-object) GPIO path, exercising the same ISR logic that
# runs on hardware.
# Run from the repository root: python3 -m unittest discover -s tests

import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "firmware"))


class FakePin:
    """Simulated GPIO pin: external level plus our optional drive."""

    IN = 0
    OUT = 1
    IRQ_RISING = 1
    IRQ_FALLING = 2

    external = {}  # gpio -> level presented by the outside world

    def __init__(self, gpio, mode=IN, **_kwargs):
        self.gpio = gpio
        self.driven = None  # None = tristated

    def init(self, mode):
        if mode == FakePin.IN:
            self.driven = None
        elif self.driven is None:
            self.driven = 0

    def value(self, v=None):
        if v is None:
            if self.driven is not None:
                return self.driven
            return FakePin.external.get(self.gpio, 0)
        self.driven = v

    def irq(self, handler=None, trigger=0, hard=False):
        self.handler = handler


class FakeTimer:
    PERIODIC = 0

    def __init__(self, mode=0, period=0, callback=None):
        self.callback = callback


class _FakeMem32:
    def __getitem__(self, addr):
        return 0

    def __setitem__(self, addr, value):
        pass


def _install_fake_modules():
    machine = types.ModuleType("machine")
    machine.Pin = FakePin
    machine.Timer = FakeTimer
    machine.mem32 = _FakeMem32()
    machine.reset = lambda: None
    machine.disable_irq = lambda: 0
    machine.enable_irq = lambda _state: None
    machine.UART = None
    sys.modules["machine"] = machine

    micropython = types.ModuleType("micropython")
    micropython.const = lambda x: x
    sys.modules["micropython"] = micropython
    # no fake rp2 module: the import fails, selecting the generic path


_install_fake_modules()
from remotekeyboard import (  # noqa: E402
    RemoteKeyboard,
    esp32_gpio_addresses_for,
    sio_addresses_for,
)
from core import EVENT_PRESS  # noqa: E402


class TestSioAddresses(unittest.TestCase):
    # Offsets are verified against pico-sdk hardware/regs/sio.h. RP2350
    # interleaves the high-bank GPIO registers, so only GPIO_IN shares
    # an offset with RP2040.
    RP2040 = (0xD0000004, 0xD0000014, 0xD0000018, 0xD0000024, 0xD0000028)
    RP2350 = (0xD0000004, 0xD0000018, 0xD0000020, 0xD0000038, 0xD0000040)

    def test_rp2040(self):
        self.assertEqual(
            sio_addresses_for("Raspberry Pi Pico with RP2040"), self.RP2040
        )

    def test_rp2350_arm(self):
        self.assertEqual(
            sio_addresses_for("Raspberry Pi Pico 2 with RP2350"), self.RP2350
        )

    def test_rp2350_riscv_uses_rp2350_map(self):
        # "RP2350-RISCV" must select the RP2350 layout, not fall through
        self.assertEqual(
            sio_addresses_for("Pico 2 with RP2350-RISCV"), self.RP2350
        )

    def test_out_clr_differs_between_chips(self):
        # the dangerous case: RP2040's OUT_CLR offset is RP2350's OUT_SET
        self.assertNotEqual(self.RP2040[2], self.RP2350[2])
        self.assertEqual(self.RP2040[2], self.RP2350[1])

    def test_unknown_chip_returns_none(self):
        for name in ("", None, "ESP32 module", "Some board with RP9999"):
            self.assertIsNone(sio_addresses_for(name))


class TestEsp32Addresses(unittest.TestCase):
    # Offsets verified against esp-idf soc/gpio_reg.h (same on every
    # variant); only the peripheral base differs between S2 and S3.
    # (IN=+0x3C, OUT_W1TS=+0x08, OUT_W1TC=+0x0C, ENABLE_W1TS=+0x24,
    #  ENABLE_W1TC=+0x28)
    ESP32S3 = (0x6000403C, 0x60004008, 0x6000400C, 0x60004024, 0x60004028)
    ESP32S2 = (0x3F40403C, 0x3F404008, 0x3F40400C, 0x3F404024, 0x3F404028)

    def test_s3(self):
        self.assertEqual(
            esp32_gpio_addresses_for("ESP32S3 module with ESP32S3"), self.ESP32S3
        )

    def test_s2(self):
        self.assertEqual(
            esp32_gpio_addresses_for("ESP32S2 module with ESP32S2"), self.ESP32S2
        )

    def test_s2_and_s3_use_different_bases(self):
        # the dangerous case: the same offset but a wrong base would
        # drive an unrelated peripheral
        self.assertNotEqual(self.ESP32S2[0], self.ESP32S3[0])

    def test_unknown_or_plain_esp32_returns_none(self):
        # only S2/S3 are supported; the original ESP32 and unknown
        # names fall back to the portable Pin path
        for name in ("", None, "ESP32 module with ESP32", "Pico with RP2040"):
            self.assertIsNone(esp32_gpio_addresses_for(name))

    def test_sio_lookup_ignores_esp32_names(self):
        # the two families' lookups stay disjoint
        self.assertIsNone(sio_addresses_for("ESP32S3 module with ESP32S3"))
        self.assertIsNone(esp32_gpio_addresses_for("Raspberry Pi Pico with RP2040"))


class FakeIO:
    def __init__(self):
        self.written = []

    def write(self, s):
        self.written.append(s)

    def read_char(self, _timeout_ms):
        return None


def make_config(**overrides):
    cfg = types.SimpleNamespace(
        ROW_PINS=(2, 3, 4, 5, 6, 7, 8, 9),
        COL_PINS=(10, 11, 12, 13, 14, 15),
        AUX_PINS=(16,),
        AUX_ON_STATE=0,
        AUX_SCAN_HZ=30,
        USE_UART=False,
        UART_ID=0,
        UART_TX=0,
        UART_RX=1,
        BAUD=38400,
        EVENT_QUEUE_SIZE=64,
    )
    for name, value in overrides.items():
        setattr(cfg, name, value)
    return cfg


class ScannerTestCase(unittest.TestCase):
    def setUp(self):
        FakePin.external = {}
        self.cfg = make_config()
        self.scanner = RemoteKeyboard(self.cfg)
        self.scanner._io = FakeIO()

    def set_idle(self, level):
        for g in self.cfg.ROW_PINS + self.cfg.COL_PINS:
            FakePin.external[g] = level

    def strobe(self, col_index, active, pressed_rows=()):
        """Simulate the keyboard controller strobing one column."""
        idle = 0 if active else 1
        for i, g in enumerate(self.cfg.COL_PINS):
            FakePin.external[g] = active if i == col_index else idle
        for i, g in enumerate(self.cfg.ROW_PINS):
            FakePin.external[g] = active if i in pressed_rows else idle
        self.scanner._col_isr(None)
        # strobe ends: all columns and rows back to idle
        for g in self.cfg.COL_PINS + self.cfg.ROW_PINS:
            FakePin.external[g] = idle
        self.scanner._col_isr(None)

    def events(self):
        out = []
        while True:
            ev = self.scanner.queue.get()
            if ev < 0:
                return out
            out.append(ev)

    def driven_row_levels(self):
        return {
            g: pin.driven
            for bit, pin in self.scanner._io_pins
            for g in self.cfg.ROW_PINS
            if bit == 1 << g and pin.driven is not None
        }


class TestActiveLowMatrix(ScannerTestCase):
    # column lines idle high, strobes and pressed rows read low

    def test_press_and_release_events(self):
        self.set_idle(1)
        self.strobe(0, active=0, pressed_rows=(2,))
        self.assertEqual(self.events(), [])  # first sample: debouncing
        self.strobe(0, active=0, pressed_rows=(2,))
        self.assertEqual(self.events(), [EVENT_PRESS | (2 << 3) | 0])
        self.strobe(0, active=0)
        self.strobe(0, active=0)
        self.assertEqual(self.events(), [(2 << 3) | 0])
        self.assertTrue(self.scanner.idle_high)

    def test_one_scan_glitch_reports_nothing(self):
        self.set_idle(1)
        self.strobe(3, active=0, pressed_rows=(5,))
        for _ in range(3):
            self.strobe(3, active=0)
        self.assertEqual(self.events(), [])

    def test_mid_transition_ignored(self):
        self.set_idle(1)
        # two columns low at once: no column is sampled
        FakePin.external[self.cfg.COL_PINS[0]] = 0
        FakePin.external[self.cfg.COL_PINS[1]] = 0
        self.scanner._col_isr(None)
        self.assertEqual(self.events(), [])
        self.assertEqual(bytes(self.scanner.debouncer.active), bytes(7))

    def test_forced_row_driven_low_then_released(self):
        self.set_idle(1)
        self.scanner._handle_command("p20")  # force row 2, column 0
        self.assertEqual(self.scanner.forced[0], 0x04)
        # while column 0 is strobed, row 2 must be driven to active (low)
        for i, g in enumerate(self.cfg.COL_PINS):
            FakePin.external[g] = 0 if i == 0 else 1
        self.scanner._col_isr(None)
        self.assertEqual(self.driven_row_levels(), {4: 0})  # row 2 = GP4
        # strobe ends: all rows tristated again
        FakePin.external[self.cfg.COL_PINS[0]] = 1
        self.scanner._col_isr(None)
        self.assertEqual(self.driven_row_levels(), {})
        # forcing generated no reported events
        self.assertEqual(self.events(), [])


class TestActiveHighMatrix(ScannerTestCase):
    # column lines idle low, strobes and pressed rows read high

    def test_polarity_flip_on_idle_lines(self):
        self.set_idle(0)
        self.scanner._col_isr(None)  # all "active" under wrong polarity
        self.assertFalse(self.scanner.idle_high)

    def test_polarity_flip_mid_strobe_still_samples(self):
        # first observation is a strobe: N-1 columns look active under
        # the wrong assumed polarity; the ISR flips and retries
        self.set_idle(0)
        self.strobe(2, active=1, pressed_rows=(1,))
        self.assertFalse(self.scanner.idle_high)
        self.strobe(2, active=1, pressed_rows=(1,))
        self.assertEqual(self.events(), [EVENT_PRESS | (1 << 3) | 2])

    def test_forced_row_driven_high(self):
        self.set_idle(0)
        self.scanner._col_isr(None)  # learn polarity
        self.scanner._handle_command("p03")
        for i, g in enumerate(self.cfg.COL_PINS):
            FakePin.external[g] = 1 if i == 3 else 0
        self.scanner._col_isr(None)
        self.assertEqual(self.driven_row_levels(), {2: 1})  # row 0 = GP2


class TestAuxSwitches(ScannerTestCase):
    def test_aux_press_reported_on_pseudo_column(self):
        FakePin.external[16] = 1  # aux idle (AUX_ON_STATE = 0)
        self.scanner._aux_tick(None)
        FakePin.external[16] = 0  # switch closes
        self.scanner._aux_tick(None)
        self.scanner._aux_tick(None)
        self.assertEqual(self.events(), [EVENT_PRESS | (0 << 3) | 6])

    def test_forced_aux_driven_and_not_echoed(self):
        self.scanner._handle_command("p06")
        self.scanner._aux_tick(None)
        pins = {bit: pin for bit, pin in self.scanner._io_pins}
        self.assertEqual(pins[1 << 16].driven, 0)  # ON level (active low)
        self.scanner._aux_tick(None)
        self.assertEqual(self.events(), [])


class RecordingMem32:
    """mem32 stand-in that records writes and serves programmed reads."""

    def __init__(self):
        self.reads = {}     # addr -> value returned by reads
        self.writes = []    # (addr, value) in order

    def __getitem__(self, addr):
        return self.reads.get(addr, 0)

    def __setitem__(self, addr, value):
        self.writes.append((addr, value))


class TestRegisterPath(unittest.TestCase):
    """Exercise the fast register path (used on RP2 and ESP32-S2/S3) with
    the ESP32-S3 addresses, confirming reads and drives hit the right
    registers rather than falling back to the Pin path."""

    ADDRS = TestEsp32Addresses.ESP32S3  # (IN, OUT_SET, OUT_CLR, OE_SET, OE_CLR)

    def setUp(self):
        import remotekeyboard
        self.mod = remotekeyboard
        self._orig_mem32 = remotekeyboard.mem32
        self._orig_detect = remotekeyboard._detect_register_addresses
        self.mem = RecordingMem32()
        remotekeyboard.mem32 = self.mem
        remotekeyboard._detect_register_addresses = lambda: self.ADDRS
        self.scanner = RemoteKeyboard(make_config())

    def tearDown(self):
        self.mod.mem32 = self._orig_mem32
        self.mod._detect_register_addresses = self._orig_detect

    def test_register_path_selected(self):
        self.assertEqual(self.scanner._read_raw, self.scanner._read_raw_reg)
        self.assertFalse(hasattr(self.scanner, "_io_pins"))

    def test_read_raw_reads_in_register(self):
        in_addr = self.ADDRS[0]
        self.mem.reads[in_addr] = 0x1234
        self.assertEqual(self.scanner._read_raw(), 0x1234)

    def test_tristate_clears_oe_and_out(self):
        self.mem.writes.clear()
        self.scanner._tristate(0xABC)
        # OE_CLR then OUT_CLR, both with the mask
        self.assertEqual(self.mem.writes, [(self.ADDRS[4], 0xABC), (self.ADDRS[2], 0xABC)])

    def test_drive_high_sets_out_then_oe(self):
        self.mem.writes.clear()
        self.scanner._drive(0x30, True)
        self.assertEqual(self.mem.writes, [(self.ADDRS[1], 0x30), (self.ADDRS[3], 0x30)])

    def test_drive_low_clears_out_then_sets_oe(self):
        self.mem.writes.clear()
        self.scanner._drive(0x30, False)
        self.assertEqual(self.mem.writes, [(self.ADDRS[2], 0x30), (self.ADDRS[3], 0x30)])

    def test_high_pin_forces_generic_path(self):
        # a pin >= 32 is out of the low-bank registers' reach: even with a
        # register map available, the scanner must use the Pin path
        scanner = RemoteKeyboard(make_config(AUX_PINS=(35,)))
        self.assertEqual(scanner._read_raw, scanner._read_raw_generic)
        self.assertTrue(hasattr(scanner, "_io_pins"))


class TestCommands(ScannerTestCase):
    def test_press_and_release_set_forced_bits(self):
        self.scanner._handle_command("p75")
        self.assertEqual(self.scanner.forced[5], 0x80)
        self.scanner._handle_command("r75")
        self.assertEqual(self.scanner.forced[5], 0x00)
        self.assertEqual(self.scanner._io.written, [])

    def test_invalid_commands_rejected(self):
        for cmd in ("p90", "p08", "p16", "x00", "p0", "hello"):
            self.scanner._io.written.clear()
            self.scanner._handle_command(cmd)
            self.assertEqual(self.scanner._io.written, [f"{cmd}?\r\n"], cmd)
        self.assertEqual(bytes(self.scanner.forced), bytes(7))

    def test_aux_row_bound(self):
        self.scanner._handle_command("p06")  # aux 0: valid
        self.assertEqual(self.scanner.forced[6], 0x01)
        self.scanner._handle_command("p16")  # aux 1 doesn't exist
        self.assertEqual(self.scanner._io.written, ["p16?\r\n"])

    def test_empty_command_dumps_state(self):
        self.scanner._handle_command("")
        text = "".join(self.scanner._io.written)
        self.assertIn("Co Fo Ac Pr Re CSTR", text)
        self.assertIn("Ov:", text)

    def test_send_events_formats_protocol(self):
        self.scanner.queue.put(EVENT_PRESS | (2 << 3) | 3)
        self.scanner.queue.put((7 << 3) | 6)
        self.scanner._send_events()
        self.assertEqual(self.scanner._io.written, ["p23\r\n", "r76\r\n"])


if __name__ == "__main__":
    unittest.main()
