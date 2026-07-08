# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MicroPython port of the AVR firmware in the sibling `../RemoteKeyboard`
repository: monitors and controls matrix-scanned keyboards. Targets the
Raspberry Pi Pico (RP2040) by default; the serial protocol is identical
to the (fixed) AVR version. See README.md for wiring and usage.

## Commands

```bash
# run all unit tests (CPython; no hardware or pyserial needed)
python3 -m unittest discover -s tests

# deploy firmware to a board
mpremote cp firmware/core.py firmware/config.py firmware/remotekeyboard.py firmware/main.py :
mpremote reset
```

A MicroPython unix-port binary is at `~/bin/micropython`; `firmware/core.py`
runs under it, and all firmware files should stay compilable with it:
`micropython -c "compile(open('firmware/remotekeyboard.py').read(), 'f', 'exec')"`.

## Architecture and invariants

- `firmware/core.py` is hardware-independent and must stay importable
  under both CPython (for tests) and MicroPython. Do not import
  `machine`/`micropython` there.
- `firmware/remotekeyboard.py` holds all hardware access. The column pin
  IRQ runs **hard** on RP2040/RP2350: its call paths (`_col_isr`,
  `_read_rows`, `_assert_rows`, `_queue_reports`, the `core.py` methods,
  and the RP2 GPIO helpers) must not allocate — no f-strings, floats, or
  `for i in range(...)`; they use `while` loops, preallocated
  bytearray/array tables, and `mem32` SIO register access.
- The SIO register map differs between RP2040 and RP2350 (RP2350
  interleaves the high-bank GPIO registers), so `sio_addresses_for()`
  picks the right addresses from the machine name; unidentified RP2
  chips fall back to the portable Pin path. If you touch those offsets,
  verify against pico-sdk `hardware/regs/sio.h` — a wrong table silently
  drives the wrong registers. `sio_addresses_for()` is a pure function
  covered by tests/test_scanner.py.
- ISRs never write to the serial port; key events go through
  `core.EventQueue` (SPSC ring: ISRs write head, main loop writes tail)
  and the main loop transmits them. The aux `Timer` callback wraps its
  queue access in `machine.disable_irq()` to serialize with the column
  IRQ.
- `forced[]` is written only by the main loop and read by ISRs.
- `_read_rows()` leaves row pins tristated with pulls off; the ISR
  re-asserts forced rows afterwards (same rationale as the AVR fix:
  restoring drive state would force the previous column's keys onto the
  newly strobed column).
- Debounce semantics (in `core.MatrixDebouncer`): vertical debounce plus
  reported-state gating; forced switches are never reported. Behavior is
  pinned by tests/test_core.py — the AVR version's unmatched-event
  glitch bug must not be reintroduced.
- Event encoding (1 byte): bit 6 press, bits 5:3 row, bits 2:0 column.
  This caps the matrix at 8 rows × 6 columns + aux pseudo-column 6.

## Host scripts

`host/remotekeyboard_host.py` is the shared library (Brother P-touch key
map, shift/caps/num-lock state, event interpretation); `terminal.py` and
`demo.py` are the entry points. `Keyboard` takes any transport object
with `write(str)` and `read_available(timeout)` so tests use a fake;
pyserial is imported lazily inside `SerialInterface` only. Named keys
are `Key("name")` (a str subclass marking non-printing keys).

## Testing conventions

Logic changes need a test in `tests/`; the tests run pure CPython with
fake transports and no MicroPython dependencies. Behavior worth pinning:
debounce edge cases, queue overflow, digit/caps/num-lock typing
sequences, and event-stream parsing across split reads.
