# RemoteKeyboard: monitor and control a matrix-scanned keyboard.
#
# MicroPython port of the AVR firmware in ../RemoteKeyboard. The host
# keyboard controller strobes the column lines; we watch the strobes
# with pin interrupts, read the row lines while exactly one column is
# active, and report debounced key transitions over serial. Forced
# (simulated) key presses drive the row lines to the active level while
# their column is strobed.
#
# Serial protocol (ASCII, unchanged from the AVR version):
#   device -> host:  pRC\r\n  key at row R, column C pressed
#                    rRC\r\n  key at row R, column C released
#   host -> device:  pRC\r    force (press) row R, column C
#                    rRC\r    release forced row R, column C
#                    R\r      reset
#                    \r       dump debug state
# Column len(COL_PINS) addresses the aux (non-matrix) switches.
#
# Concurrency rules (same shape as the fixed AVR code):
# - The column pin IRQ runs hard where the port supports it: it must
#   not allocate memory, so those paths use while loops, preallocated
#   tables, and direct register access. It never writes to the serial
#   port; key events go into an EventQueue drained by the main loop.
# - The aux Timer callback also produces events, so it wraps its queue
#   access in machine.disable_irq() to serialize with the column IRQ.
# - forced[] is written only by the main loop and read by the ISRs.

import sys
import select

import machine
from machine import Pin, Timer, mem32

from core import MatrixDebouncer, EventQueue, EVENT_PRESS, event_row, event_column

try:
    import rp2  # noqa: F401

    _RP2 = True
except ImportError:
    _RP2 = False

# SIO single-cycle GPIO registers: fast, allocation-free access from a
# hard IRQ. The SIO base is 0xD0000000 on both RP2 chips, but RP2350
# interleaves the high-bank (GPIO 32+) registers between the low-bank
# ones, so every offset except GPIO_IN differs from RP2040. Guessing is
# unsafe: applying the RP2040 offsets on an RP2350 puts GPIO_OUT_CLR
# where GPIO_OUT_SET lives, so a pin *clear* would *set* the pin.
# Offsets verified against pico-sdk hardware/regs/sio.h for each chip.
_SIO_BASE = 0xD0000000
# (GPIO_IN, GPIO_OUT_SET, GPIO_OUT_CLR, GPIO_OE_SET, GPIO_OE_CLR)
_SIO_OFFSETS = {
    "RP2040": (0x004, 0x014, 0x018, 0x024, 0x028),
    "RP2350": (0x004, 0x018, 0x020, 0x038, 0x040),
}


def sio_addresses_for(machine_name):
    """Absolute (IN, OUT_SET, OUT_CLR, OE_SET, OE_CLR) SIO addresses for
    the RP2 chip named in machine_name (as from os.uname().machine or
    sys.implementation._machine), or None if it names no known RP2 chip.
    An unrecognized chip must fall back to the portable Pin path rather
    than drive registers at guessed offsets."""
    if machine_name:
        # test RP2350 first: "RP2350-RISCV" also contains it, and has
        # the same SIO layout (same chip, different core)
        for chip in ("RP2350", "RP2040"):
            if chip in machine_name:
                return tuple(_SIO_BASE + off for off in _SIO_OFFSETS[chip])
    return None


def _detect_sio_addresses():
    """Identify the running RP2 chip from the machine name and return
    its SIO addresses, or None if it can't be positively identified."""
    names = []
    try:
        names.append(sys.implementation._machine)
    except AttributeError:
        pass
    try:
        import os

        names.append(os.uname().machine)
    except Exception:
        pass
    for name in names:
        addrs = sio_addresses_for(name)
        if addrs is not None:
            return addrs
    return None


BANNER = "RemoteKeyboard v2.0 (MicroPython) by Ned Konz\r\n"


class _UsbIO:
    """Serial link over USB CDC (sys.stdin/stdout)."""

    def __init__(self):
        self._poll = select.poll()
        self._poll.register(sys.stdin, select.POLLIN)

    def read_char(self, timeout_ms):
        if self._poll.poll(timeout_ms):
            return sys.stdin.read(1)
        return None

    def write(self, s):
        sys.stdout.write(s)


class _UartIO:
    """Serial link over a hardware UART."""

    def __init__(self, cfg):
        self._uart = machine.UART(
            cfg.UART_ID, baudrate=cfg.BAUD, tx=Pin(cfg.UART_TX), rx=Pin(cfg.UART_RX)
        )
        self._poll = select.poll()
        self._poll.register(self._uart, select.POLLIN)

    def read_char(self, timeout_ms):
        if self._poll.poll(timeout_ms):
            b = self._uart.read(1)
            if b:
                return chr(b[0])
        return None

    def write(self, s):
        self._uart.write(s)


class RemoteKeyboard:
    def __init__(self, cfg):
        self.cfg = cfg
        self.n_rows = len(cfg.ROW_PINS)
        self.n_cols = len(cfg.COL_PINS)
        self.n_aux = len(cfg.AUX_PINS)
        if self.n_rows > 8:
            raise ValueError("at most 8 rows (3-bit row in event encoding)")
        if self.n_cols > 6:
            raise ValueError("at most 6 columns (aux column must fit 3 bits)")
        if self.n_aux > 8:
            raise ValueError("at most 8 aux switches")

        self.row_mask = (1 << self.n_rows) - 1
        self.col_mask = (1 << self.n_cols) - 1
        self.aux_mask = (1 << self.n_aux) - 1

        # GPIO bit positions for each logical row/column/aux index
        self.row_bits = tuple(1 << g for g in cfg.ROW_PINS)
        self.col_bits = tuple(1 << g for g in cfg.COL_PINS)
        self.aux_bits = tuple(1 << g for g in cfg.AUX_PINS)
        self.all_row_bits = 0
        for b in self.row_bits:
            self.all_row_bits |= b
        self.all_aux_bits = 0
        for b in self.aux_bits:
            self.all_aux_bits |= b

        # bit-count and highest-set-bit tables for logical column values
        n = 1 << self.n_cols
        self.pop = bytearray(n)
        self.high = bytearray(n)
        for v in range(1, n):
            self.pop[v] = self.pop[v >> 1] + (v & 1)
            self.high[v] = self.high[v >> 1] + 1 if v > 1 else 0

        # forced (simulated) switches per column; last entry is aux
        self.forced = bytearray(self.n_cols + 1)
        self.debouncer = MatrixDebouncer(self.n_cols + 1, self.row_mask)
        self.queue = EventQueue(cfg.EVENT_QUEUE_SIZE)

        # Assume the column lines idle high (strobe active low) until the
        # observed line states prove otherwise; auto-detected like the AVR
        # version by counting simultaneously-active columns.
        self.idle_high = True

        # debug observations (dumped by the empty command)
        self.seen_cols_high = 0
        self.seen_cols_low = self.col_mask
        self.seen_rows_high = 0
        self.seen_rows_low = self.row_mask
        from array import array

        self.strobes = array("H", [0] * (self.n_cols + 1))

        # configure all matrix pins as plain inputs, no pulls
        self.row_pins = [Pin(g, Pin.IN) for g in cfg.ROW_PINS]
        self.col_pins = [Pin(g, Pin.IN) for g in cfg.COL_PINS]
        self.aux_pins = [Pin(g, Pin.IN) for g in cfg.AUX_PINS]

        sio = _detect_sio_addresses() if _RP2 else None
        if sio is not None:
            # fast SIO-register path with chip-correct register addresses
            (self._sio_in, self._sio_out_set, self._sio_out_clr,
             self._sio_oe_set, self._sio_oe_clr) = sio
            self._read_raw = self._read_raw_rp2
            self._tristate = self._tristate_rp2
            self._drive = self._drive_rp2
        else:
            # portable Pin-object path: correct on any port, and the
            # fallback for RP2 chips whose SIO layout we can't identify
            # (gpio bit, Pin) pairs for the generic path
            self._io_pins = tuple(
                (1 << g, Pin(g, Pin.IN))
                for g in tuple(cfg.ROW_PINS) + tuple(cfg.COL_PINS) + tuple(cfg.AUX_PINS)
            )
            self._read_raw = self._read_raw_generic
            self._tristate = self._tristate_generic
            self._drive = self._drive_generic

        self._io = _UartIO(cfg) if cfg.USE_UART else _UsbIO()
        self._timer = None

    # ---- low-level GPIO helpers -------------------------------------
    # RP2 path: direct SIO register access (fast, allocation-free)

    def _read_raw_rp2(self):
        return mem32[self._sio_in]

    def _tristate_rp2(self, gpio_mask):
        mem32[self._sio_oe_clr] = gpio_mask
        mem32[self._sio_out_clr] = gpio_mask

    def _drive_rp2(self, gpio_mask, high):
        if high:
            mem32[self._sio_out_set] = gpio_mask
        else:
            mem32[self._sio_out_clr] = gpio_mask
        mem32[self._sio_oe_set] = gpio_mask

    # Generic path: Pin objects (slower; positional args only so the
    # calls stay allocation-free)

    def _read_raw_generic(self):
        raw = 0
        pins = self._io_pins
        i = 0
        n = len(pins)
        while i < n:
            bit, pin = pins[i]
            if pin.value():
                raw |= bit
            i += 1
        return raw

    def _tristate_generic(self, gpio_mask):
        pins = self._io_pins
        i = 0
        n = len(pins)
        while i < n:
            bit, pin = pins[i]
            if gpio_mask & bit:
                pin.init(Pin.IN)
            i += 1

    def _drive_generic(self, gpio_mask, high):
        pins = self._io_pins
        i = 0
        n = len(pins)
        while i < n:
            bit, pin = pins[i]
            if gpio_mask & bit:
                pin.init(Pin.OUT)
                pin.value(1 if high else 0)
            i += 1

    # ---- matrix scanning (hard IRQ context) -------------------------

    def _read_rows(self):
        # Tristate first: forced rows may be driven, and a leftover
        # output latch would bias the read. The interpreter overhead
        # between tristate and read far exceeds the 10 us settling
        # delay the AVR version needed.
        self._tristate(self.all_row_bits)
        raw = self._read_raw()
        rows = 0
        bits = self.row_bits
        i = 0
        n = self.n_rows
        while i < n:
            if raw & bits[i]:
                rows |= 1 << i
            i += 1
        self.seen_rows_high |= rows
        self.seen_rows_low &= rows
        return rows

    def _assert_rows(self, forced):
        # Drive forced rows to the active level. Rows were left
        # tristated by _read_rows, so nothing to do when unforced.
        if not forced:
            return
        mask = 0
        bits = self.row_bits
        i = 0
        n = self.n_rows
        while i < n:
            if forced & (1 << i):
                mask |= bits[i]
            i += 1
        self._drive(mask, not self.idle_high)

    def _queue_reports(self, column, report, rows):
        q = self.queue
        i = 0
        n = self.n_rows
        while i < n:
            b = 1 << i
            if report & b:
                q.put((EVENT_PRESS if rows & b else 0) | (i << 3) | column)
            i += 1

    def _col_isr(self, _pin):
        raw = self._read_raw()
        cols = 0
        bits = self.col_bits
        i = 0
        n_cols = self.n_cols
        while i < n_cols:
            if raw & bits[i]:
                cols |= 1 << i
            i += 1
        self.seen_cols_high |= cols
        self.seen_cols_low &= cols

        inv = self.col_mask if self.idle_high else 0
        retry = 2
        while retry:
            retry -= 1
            c = (cols ^ inv) & self.col_mask
            n = self.pop[c]
            if n == 1:  # exactly one active column: sample the rows
                col = self.high[c]
                self.strobes[col] = (self.strobes[col] + 1) & 0xFFFF
                rows = self._read_rows()
                if self.idle_high:
                    rows ^= self.row_mask
                report = self.debouncer.sample(col, rows, self.forced[col])
                if report:
                    self._queue_reports(col, report, rows)
                self._assert_rows(self.forced[col])
                return
            if n == 0:  # no active columns: leave all rows tristated
                self._tristate(self.all_row_bits)
                return
            if n == n_cols - 1:
                # assumed idle polarity is wrong; with it corrected,
                # exactly one column is active: flip and retry
                self.idle_high = not self.idle_high
                inv ^= self.col_mask
                continue
            if n == n_cols:
                # assumed idle polarity is wrong; nothing is active
                self.idle_high = not self.idle_high
                return
            return  # 2..n-2 columns active: mid-transition, ignore

    # ---- aux switches (Timer callback context) -----------------------

    def _read_aux(self):
        self._tristate(self.all_aux_bits)
        raw = self._read_raw()
        val = 0
        bits = self.aux_bits
        on_high = self.cfg.AUX_ON_STATE
        i = 0
        n = self.n_aux
        while i < n:
            level = 1 if raw & bits[i] else 0
            if level == (1 if on_high else 0):
                val |= 1 << i
            i += 1
        return val

    def _assert_aux(self, forced):
        on_mask = 0
        off_mask = 0
        bits = self.aux_bits
        i = 0
        n = self.n_aux
        while i < n:
            if forced & (1 << i):
                on_mask |= bits[i]
            else:
                off_mask |= bits[i]
            i += 1
        if off_mask:
            self._tristate(off_mask)
        if on_mask:
            self._drive(on_mask, bool(self.cfg.AUX_ON_STATE))

    def _aux_tick(self, _timer):
        rows = self._read_aux()
        aux_col = self.n_cols
        # the column IRQ also produces queue events; serialize with it
        state = machine.disable_irq()
        try:
            report = self.debouncer.sample(aux_col, rows, self.forced[aux_col])
            if report:
                self._queue_reports(aux_col, report, rows)
        finally:
            machine.enable_irq(state)
        self._assert_aux(self.forced[aux_col])

    # ---- serial protocol (main loop context) -------------------------

    def _send_events(self):
        io = self._io
        while True:
            ev = self.queue.get()
            if ev < 0:
                return
            state = "p" if ev & EVENT_PRESS else "r"
            io.write(f"{state}{event_row(ev)}{event_column(ev)}\r\n")

    def _dump(self):
        io = self._io
        io.write(f"\r\nchi: {self.seen_cols_high:02X} clo: {self.seen_cols_low:02X}\r\n")
        io.write(f"rhi: {self.seen_rows_high:02X} rlo: {self.seen_rows_low:02X}\r\n")
        io.write("Co Fo Ac Pr Re CSTR\r\n")
        d = self.debouncer
        for i in range(self.n_cols + 1):
            io.write(
                f"{i:02X} {self.forced[i]:02X} {d.active[i]:02X} "
                f"{d.prior[i]:02X} {d.reported[i]:02X} {self.strobes[i]:04X}\r\n"
            )
            self.strobes[i] = 0
        io.write(f"Ov: {self.queue.overflows:02X}\r\n")
        self.queue.overflows = 0

    def _handle_command(self, cmd):
        if cmd == "":
            self._dump()
            return
        if cmd == "R":
            machine.reset()
        if len(cmd) == 3 and cmd[0] in "pr":
            row = ord(cmd[1]) - 0x30
            col = ord(cmd[2]) - 0x30
            if (0 <= row < self.n_rows and 0 <= col <= self.n_cols
                    and not (col == self.n_cols and row >= self.n_aux)):
                # forced[] is written only here (main loop); ISRs only read
                if cmd[0] == "p":
                    self.forced[col] |= 1 << row
                else:
                    self.forced[col] &= 0xFF & ~(1 << row)
                return
        self._io.write(f"{cmd}?\r\n")

    # ---- setup and main loop -----------------------------------------

    def _install_irqs(self):
        handler = self._col_isr
        trigger = Pin.IRQ_RISING | Pin.IRQ_FALLING
        for p in self.col_pins:
            try:
                p.irq(handler=handler, trigger=trigger, hard=True)
            except (TypeError, ValueError):
                # port without hard IRQ support (e.g. ESP32): soft IRQ
                p.irq(handler=handler, trigger=trigger)
        if self.n_aux:
            self._timer = Timer(
                mode=Timer.PERIODIC,
                period=max(1, 1000 // self.cfg.AUX_SCAN_HZ),
                callback=self._aux_tick,
            )

    def run(self):
        self._install_irqs()
        io = self._io
        io.write(BANNER)
        buf = ""
        while True:
            self._send_events()
            ch = io.read_char(10)
            if ch is None or ch == "\n":
                continue
            if ch == "\r":
                self._handle_command(buf)
                buf = ""
            elif len(buf) < 7:
                buf += ch
            else:
                io.write(f"{buf}?\r\n")
                buf = ""
