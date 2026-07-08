#!/usr/bin/env python3
# Demo mode for the RemoteKeyboard firmware (Python port of the
# original ruby/test.rb): types a message on the device's keyboard
# every few seconds, then clears it.
#
# Usage: python3 demo.py [port [baud]]
#        python3 demo.py ws://<board-ip>/ws [token]   (over WiFi)

import sys
import time

from remotekeyboard_host import open_keyboard

MESSAGE = "Control any Matrix Keyboard"


def main():
    kbd = open_keyboard(sys.argv)
    try:
        while True:
            kbd.drive_text(MESSAGE)
            time.sleep(5)
            kbd.drive_text("\x15")  # ctrl-U: clear
    except KeyboardInterrupt:
        kbd.transport.close()


if __name__ == "__main__":
    main()
