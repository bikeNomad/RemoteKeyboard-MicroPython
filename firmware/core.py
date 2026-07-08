# Hardware-independent core logic for RemoteKeyboard.
#
# This module must stay importable under both MicroPython and CPython:
# the debounce and queue logic is unit-tested on the host (see tests/).
# Everything here is safe to call from a MicroPython hard IRQ handler:
# no heap allocation (bytearray element access and small-int arithmetic
# only).

# Event byte encoding (same as the AVR firmware):
# bit 6: 1 = press, 0 = release; bits 5:3 = row; bits 2:0 = column
EVENT_PRESS = 0x40


def event_row(ev):
    return (ev >> 3) & 0x07


def event_column(ev):
    return ev & 0x07


class MatrixDebouncer:
    """Per-column vertical debouncer with reported-state gating.

    Each column keeps its last two row samples. A row change is valid
    when it differed between the two previous samples but matches the
    newest one (i.e. stable for one sample after a change). Valid
    changes are reported only when they differ from the last *reported*
    state; without that gate, a one-sample glitch is suppressed on
    entry but emits an unmatched press/release event when the input
    returns to its old state. Rows being forced by us are never
    reported: their inputs can read back our own drive.
    """

    def __init__(self, n_columns, row_mask=0xFF):
        self.row_mask = row_mask
        self.active = bytearray(n_columns)    # newest accepted sample
        self.prior = bytearray(n_columns)     # sample before that
        self.reported = bytearray(n_columns)  # last state sent to host

    def sample(self, column, row_inputs, forced):
        """Feed one row sample for a column.

        Returns a bitmask of rows whose debounced state changed; the new
        state of each is the corresponding bit of row_inputs. Callable
        from a hard IRQ.
        """
        row_inputs &= self.row_mask
        active = self.active[column]
        changed = active ^ row_inputs
        changed2 = active ^ self.prior[column]
        valid = changed2 & ~changed
        report = (valid
                  & (self.reported[column] ^ row_inputs)
                  & ~forced
                  & self.row_mask)
        self.reported[column] = ((self.reported[column] & ~report)
                                 | (row_inputs & report))
        self.prior[column] = active
        self.active[column] = row_inputs
        return report


class CommandBuffer:
    """Assembles carriage-return-terminated commands from a byte stream.

    One instance per transport (USB, UART, each WebSocket client) so each
    connection has its own partial line. feed() splits on '\\r' exactly
    like the original main loop: '\\n' is ignored, a command is emitted on
    '\\r', and a line longer than max_len is flushed to on_overflow (which
    the AVR/serial protocol echoes back as ``line?``). Pure logic, unit
    tested on the host.
    """

    def __init__(self, max_len=7):
        self.max_len = max_len
        self.buf = ""

    def feed(self, chars, on_command, on_overflow):
        for ch in chars:
            if ch == "\n" or ch == "":
                continue
            if ch == "\r":
                on_command(self.buf)
                self.buf = ""
            elif len(self.buf) < self.max_len:
                self.buf += ch
            else:
                on_overflow(self.buf)
                self.buf = ""


class EventQueue:
    """Lock-free single-producer/single-consumer byte ring buffer.

    put() may be called from an ISR (the producer); get() only from the
    main loop (the consumer). head is written only by the producer,
    tail only by the consumer. When two ISR contexts can produce (the
    column IRQ and the aux timer), the lower-priority one must hold
    machine.disable_irq() around put().
    """

    def __init__(self, size=64):
        if size & (size - 1):
            raise ValueError("size must be a power of 2")
        self._buf = bytearray(size)
        self._mask = size - 1
        self.head = 0
        self.tail = 0
        self.overflows = 0

    def put(self, ev):
        nxt = (self.head + 1) & self._mask
        if nxt == self.tail:
            self.overflows = (self.overflows + 1) & 0xFF  # full: drop
            return False
        self._buf[self.head] = ev
        self.head = nxt
        return True

    def get(self):
        """Return the next event, or -1 if the queue is empty."""
        if self.tail == self.head:
            return -1
        ev = self._buf[self.tail]
        self.tail = (self.tail + 1) & self._mask
        return ev
