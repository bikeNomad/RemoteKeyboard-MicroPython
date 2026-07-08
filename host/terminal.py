#!/usr/bin/env python3
# Interactive terminal for the RemoteKeyboard firmware (Python port of
# the original ruby/terminal.rb).
#
# Keys typed here are simulated on the device's keyboard; key presses
# on the device's keyboard are interpreted and printed here.
# Exit with Ctrl-C or Ctrl-D.
#
# Usage: python3 terminal.py [port [baud]]
#        python3 terminal.py ws://<board-ip>/ws [token]   (over WiFi)
#        python3 terminal.py http://remotekeyboard.local [token]
# For WiFi the token may also come from the RK_TOKEN environment variable.

import select
import sys
import termios
import threading
import tty

from remotekeyboard_host import Key, open_keyboard

# host key (or escape sequence) -> device key; anything else is typed
# as literal text
HOST_KEYS = {
    "\x1b[D": Key("l_arrow"),
    "\x1b[C": Key("r_arrow"),
    "\x7f": Key("backspace"),  # DEL
    "\x08": Key("backspace"),
    "\x1b[1;5D": Key("home"),  # ctrl-left
    "\x1b[1;5C": Key("end"),  # ctrl-right
    "\x10": Key("print"),  # ctrl-P
    # ctrl-U maps to itself: drive_text knows it as shift+clear
}

ESCAPE_TIMEOUT = 0.02  # window to assemble an escape sequence
EXIT_KEYS = ("\x03", "\x04")  # ctrl-C, ctrl-D


def read_key(fd):
    """Read one key, assembling escape sequences; None on timeout."""
    if not select.select([fd], [], [], 0.1)[0]:
        return None
    key = sys.stdin.read(1)
    if key != "\x1b":
        return key
    while len(key) < 8 and select.select([fd], [], [], ESCAPE_TIMEOUT)[0]:
        key += sys.stdin.read(1)
    return key


def device_reader(kbd, stop):
    """Print the interpretation of key events reported by the device."""
    while not stop.is_set():
        text = kbd.poll_events(0.05)
        if text:
            sys.stdout.write(text.replace("\n", "\r\n"))
            sys.stdout.flush()


def main():
    kbd = open_keyboard(sys.argv)
    stop = threading.Event()
    threading.Thread(target=device_reader, args=(kbd, stop), daemon=True).start()

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)  # unbuffered, no echo; keeps Ctrl-C as interrupt
    try:
        while True:
            key = read_key(fd)
            if key is None:
                continue
            if key in EXIT_KEYS:
                break
            kbd.drive(HOST_KEYS.get(key, key))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        kbd.transport.close()
        print()


if __name__ == "__main__":
    main()
