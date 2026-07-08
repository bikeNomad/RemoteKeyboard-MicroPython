# Pin and serial configuration for the RemoteKeyboard firmware.
#
# Defaults target a Raspberry Pi Pico (RP2040). All pin numbers are GPIO
# numbers. The matrix wiring mirrors the original AVR firmware: rows are
# normally high-impedance inputs (driven only to force a key while its
# column is strobed by the host keyboard controller), columns are inputs
# watched with pin-change interrupts.
#
# NOTE: the RP2040 is a 3.3 V part. If the keyboard being monitored runs
# at 5 V, level shifting is required.

# Row pins (matrix rows, read while a column is strobed; max 8)
ROW_PINS = (2, 3, 4, 5, 6, 7, 8, 9)

# Column pins (strobe inputs from the host keyboard controller; max 6,
# so that the aux pseudo-column index still fits in 3 bits)
COL_PINS = (10, 11, 12, 13, 14, 15)

# Auxiliary (non-matrix) switch pins, reported as rows of the pseudo-
# column len(COL_PINS); max 8
AUX_PINS = (16,)

# Level an aux switch line reads when the switch is ON (0 = active low)
AUX_ON_STATE = 0

# Aux switch scan rate (the AVR used its 30.5 Hz timer overflow)
AUX_SCAN_HZ = 30

# Serial link to the host. False: USB CDC (sys.stdin/stdout, the Pico's
# USB serial port; BAUD is ignored). True: hardware UART.
USE_UART = False
UART_ID = 0
UART_TX = 0
UART_RX = 1
BAUD = 38400

# Key event queue size (power of 2)
EVENT_QUEUE_SIZE = 64
