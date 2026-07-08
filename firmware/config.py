# Pin and serial configuration for the RemoteKeyboard firmware.
#
# Defaults target a Raspberry Pi Pico (RP2040). All pin numbers are GPIO
# numbers. The matrix wiring mirrors the original AVR firmware: rows are
# normally high-impedance inputs (driven only to force a key while its
# column is strobed by the host keyboard controller), columns are inputs
# watched with pin-change interrupts.
#
# Supported fast-path targets: RP2040, RP2350, and ESP32-S2/S3 (the
# firmware auto-detects the chip and uses direct GPIO register access).
# Any other MicroPython port still works through the portable Pin path.
#
# On ESP32-S2/S3, keep every pin below GPIO 32 (the fast register path
# only reaches the low GPIO bank; higher pins force the slower Pin path)
# and avoid the strapping, flash/PSRAM, and native-USB pins. An example:
#     ROW_PINS = (1, 2, 4, 5, 6, 7, 15, 16)
#     COL_PINS = (17, 18, 8, 9, 10, 11)
#     AUX_PINS = (12,)
#
# NOTE: these are 3.3 V parts. If the keyboard being monitored runs at
# 5 V, level shifting is required.

# Row pins (matrix rows, read while a column is strobed; max 8)
ROW_PINS = (2, 3, 4, 5, 6, 7, 8, 9)

# Column pins (strobe inputs from the host keyboard controller; max 7,
# so that the aux pseudo-column index len(COL_PINS) still fits in the
# 3-bit column field of the event byte)
COL_PINS = (10, 11, 12, 13, 14, 15, 16)

# Auxiliary (non-matrix) switch pins, reported as rows of the pseudo-
# column len(COL_PINS); max 8
AUX_PINS = (17,)

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

# ---- WiFi web terminal (Pico 2 W / Pico W / ESP32-S2/S3) ------------
# When WIFI_SSID is non-empty and the port has asyncio, the firmware also
# serves a browser terminal and a WebSocket alongside the USB/UART link
# (all transports run at once). Leave WIFI_SSID empty to disable
# networking entirely (USB/UART only). Station mode: the board joins your
# existing WiFi and you connect to the IP it prints on the serial link.
WIFI_SSID = ""
WIFI_PASSWORD = ""

# DHCP hostname to request; where mDNS is available you can then reach the
# board at http://<name>.local/. Empty to leave the port default.
WIFI_HOSTNAME = "remotekeyboard"

# TCP port for the web terminal and WebSocket.
WEB_PORT = 80

# Shared secret required to open the WebSocket (browser connects to
# ws://<board>/ws?token=...). REQUIRED for networking: an empty token
# disables the WebSocket, because a network-reachable keyboard injector
# must not be open to anyone on the LAN. Set this before enabling WiFi.
AUTH_TOKEN = ""
