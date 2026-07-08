# RemoteKeyboard (MicroPython)

MicroPython port of the AVR [RemoteKeyboard](../RemoteKeyboard) firmware:
monitor and control a matrix-scanned keyboard or keypad. The device sits
on the keyboard matrix of an appliance (originally a Brother P-touch
Home & Hobby label printer), reports key presses/releases over serial,
and can simulate key presses on command.

## How it works

The appliance's own keyboard controller strobes the matrix column lines.
This firmware watches those strobes with pin interrupts; while exactly
one column is active it samples the row lines and reports debounced key
transitions. To simulate a key press it drives the key's row line to the
active level whenever the key's column is strobed. Idle-line polarity
(active-high vs. active-low matrices) is auto-detected.

## Hardware

Default target: **Raspberry Pi Pico (RP2040)**. Default pins (edit
`firmware/config.py` to change):

| Function      | GPIOs        | AVR original       |
|---------------|--------------|--------------------|
| Rows (8)      | GP2–GP9      | PB1:0 + PD7:2      |
| Columns (6)   | GP10–GP15    | PC5:0              |
| Aux switch    | GP16         | PB2                |
| UART (option) | GP0/GP1      | PD1:0              |

Row pins are high-impedance inputs except while forcing a key; column
pins are inputs with change interrupts; no internal pulls are used.

Also supported: **RP2350** (Pico 2) and **ESP32-S2/S3**. The firmware
auto-detects the chip and uses the fast register path on any of them;
pin numbers in `config.py` are still plain GPIO numbers. On ESP32-S2/S3
keep every configured pin below GPIO 32 and off the strapping,
flash/PSRAM, and native-USB pins — see the example in `config.py`.

**These are 3.3 V parts.** The original AVR ran at 5 V; if the keyboard
being tapped runs at 5 V you need level shifting on every matrix line.

On RP2040, RP2350, and ESP32-S2/S3 the time-critical paths use direct
GPIO register access (`machine.mem32`) and the column IRQ runs as a
hard interrupt with allocation-free code. Register layouts differ by
chip (the two RP2 chips even differ from each other, and RP2350
interleaves its high-bank registers), so the firmware detects which one
it is running on from the machine name and uses the matching register
addresses. The register path reaches only GPIOs 0–31; a chip it can't
identify, a config using a pin ≥ 32, or any other port, falls back to
`machine.Pin` calls and soft IRQs (slower; whether the scan keeps up
depends on how fast the appliance strobes its columns).

## Installing

Copy the firmware to the board (it starts automatically via `main.py`):

```bash
mpremote cp firmware/core.py firmware/config.py firmware/remotekeyboard.py firmware/main.py :
mpremote reset
```

By default the serial link is the Pico's USB serial port. To use a
hardware UART instead (38400 8N1, like the AVR original), set
`USE_UART = True` in `config.py`.

## Serial protocol

Unchanged from the (fixed) AVR firmware. ASCII; rows and columns are
single digits; column 6 addresses the aux (non-matrix) switches.

From device: `pRC\r\n` (key pressed), `rRC\r\n` (key released).

To device:
- `pRC\r` — simulate pressing row R, column C
- `rRC\r` — release the simulated press
- `R\r` — reset the microcontroller
- `\r` — dump debug state (observed levels, forced/active/reported
  switches per column, strobe counts, event queue overflows)

Simulated (forced) keys are never echoed back as events, and debounced
glitches never produce unmatched press/release pairs.

## Host scripts

Python replacements for the original Ruby scripts, in `host/`
(require `pyserial`: `pip install pyserial`):

- `terminal.py` — interactive terminal for the Brother P-touch: keys you
  type are pressed on the printer, printer key presses are echoed here.
  Arrows, Ctrl-arrows (home/end), backspace/DEL, Ctrl-U (clear), and
  Ctrl-P (print) are translated; exit with Ctrl-C or Ctrl-D.
- `demo.py` — types a demo message every 5 seconds, then clears it.

Both take optional `port [baud]` arguments and otherwise use the first
`/dev/cu.usb*`, `/dev/ttyACM*`, or `/dev/ttyUSB*` device.

```bash
python3 host/terminal.py
python3 host/demo.py
```

## Tests

The debounce/queue core and the host keyboard logic are pure Python and
tested on the host:

```bash
python3 -m unittest discover -s tests
```

`firmware/core.py` also runs unmodified under the MicroPython unix port.

## Layout

```
firmware/   device code: main.py, config.py, remotekeyboard.py, core.py
host/       host scripts: terminal.py, demo.py, remotekeyboard_host.py
tests/      CPython unit tests for core.py and remotekeyboard_host.py
```
