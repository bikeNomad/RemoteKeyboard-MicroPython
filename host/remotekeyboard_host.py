#!/usr/bin/env python3
# Host-side library for the RemoteKeyboard firmware.
#
# Python port of the Ruby scripts from the original AVR project
# (ruby/terminal.rb and ruby/test.rb), for the "Brother P-touch Home &
# Hobby" label printer. Requires pyserial.
#
# Ned Konz, ned@bike-nomad.com

import glob
import os
import re
import sys
import time


class Key(str):
    """Marker type for named (non-printing) keys, e.g. Key("shift").

    Subclasses str so a Key hashes and compares equal to its name; the
    type only distinguishes named keys from literal text in key maps.
    """


class SerialInterface:
    """Paced serial connection to the RemoteKeyboard device."""

    def __init__(self, port, baud, write_delay=0.1):
        import serial  # pyserial; imported here so tests don't need it

        self.port = serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1)
        self.baud = baud
        self.write_delay = write_delay
        self.last_read = self.next_write = time.monotonic()

    def close(self):
        if self.port.is_open:
            self.port.close()

    def read_available(self, timeout=0.0, max_bytes=1000):
        """Return whatever is available within timeout ('' if nothing)."""
        self.port.timeout = timeout
        first = self.port.read(1)
        if not first:
            return ""
        self.last_read = time.monotonic()
        data = first + self.port.read(min(self.port.in_waiting, max_bytes - 1))
        return data.decode("ascii", "replace")

    def write(self, data):
        now = time.monotonic()
        if now < self.next_write:
            time.sleep(self.next_write - now)
        self.port.write(data.encode("ascii"))
        # 10 bits per byte on the wire (8N1: start + 8 data + stop)
        self.next_write = time.monotonic() + len(data) * 10.0 / self.baud + self.write_delay


class WebSocketInterface:
    """Connection to the RemoteKeyboard firmware's WiFi WebSocket.

    Same read_available()/write() shape as SerialInterface, so Keyboard
    works over WiFi unchanged. Requires the `websocket-client` package
    (`pip install websocket-client`), imported lazily.
    """

    def __init__(self, url, token=None, write_delay=0.02):
        import websocket  # websocket-client; lazy so serial use needs nothing

        if token:
            sep = "&" if "?" in url else "?"
            url = "{}{}token={}".format(url, sep, token)
        # enable_multithread: terminal.py reads in one thread, writes in another
        self._ws = websocket.create_connection(url, enable_multithread=True)
        self.write_delay = write_delay

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass

    def read_available(self, timeout=0.0, max_bytes=1000):
        import websocket

        self._ws.settimeout(timeout if timeout and timeout > 0 else 0.001)
        try:
            data = self._ws.recv()
        except (websocket.WebSocketTimeoutException, OSError):
            return ""
        except Exception:
            return ""
        if not data:
            return ""
        if isinstance(data, bytes):
            data = data.decode("ascii", "replace")
        return data

    def write(self, data):
        self._ws.send(data)
        if self.write_delay:
            time.sleep(self.write_delay)


class Keyboard:
    """Keyboard with shift key, caps and num lock, and editing keys.

    Subclasses define KEY_DEFS: (keycode, unshifted, shifted) triples,
    where keycode is the two-digit "RC" row/column string used by the
    firmware and the meanings are literal text or Key(...) names.
    """

    KEY_DEFS = ()

    # firmware banner and key event pattern
    EVENT_RE = re.compile(r"\s*([pr])(\d\d)\s*")

    def __init__(self, transport):
        self.transport = transport
        self.unshifted = {}  # keycode <-> unshifted meaning
        self.shifted = {}  # keycode <-> shifted meaning
        self.power_state = False
        self.shift_state = False
        self.caps_lock_state = False
        self.num_lock_state = False
        self._rx_buf = ""
        for keycode, unshifted, shifted in self.KEY_DEFS:
            self._add_key(keycode, unshifted, shifted)

    def _add_key(self, keycode, unshifted, shifted):
        self.unshifted[keycode] = unshifted
        self.unshifted[unshifted] = keycode
        self.shifted[keycode] = shifted
        self.shifted[shifted] = keycode

    # ---- device -> host: interpreting reported key events ------------

    def poll_events(self, timeout=0.0):
        """Read pending device output; return printable interpretation."""
        data = self.transport.read_available(timeout)
        if not data:
            return ""
        self._rx_buf += data.replace("\x00", "")
        text, self._rx_buf = self.interpret(self._rx_buf)
        # drop complete non-event lines (banner, debug dump output); a
        # partial trailing event has no \r\n yet and is preserved
        if "\r\n" in self._rx_buf:
            self._rx_buf = self._rx_buf.rsplit("\r\n", 1)[1]
        return text

    def interpret(self, data):
        """Consume key events from data; return (text, remainder)."""
        out = []

        def handle(match):
            if match.group(1) == "p":
                text = self.key_pressed(match.group(2))
                if text:
                    out.append(text)
            else:
                self.key_released(match.group(2))
            return ""

        remainder = self.EVENT_RE.sub(handle, data)
        return "".join(out), remainder

    def _meaning(self, keycode, pressed):
        shifted = self.shifted.get(keycode)
        use_shifted = self.shift_state or (
            self.num_lock_state
            and isinstance(shifted, str)
            and not isinstance(shifted, Key)
            and shifted.isdigit()
        )
        key = shifted if use_shifted else self.unshifted.get(keycode)
        if key is None:
            return None
        if isinstance(key, Key):
            handler = getattr(self, str(key), None)
            if callable(handler):
                result = handler(pressed)
                return result if result is not None else f"<{key}>"
            return f"<U {key}>"
        if len(key) == 1 and key.isalpha():
            return key.upper() if self.caps_lock_state else key.lower()
        return key

    def key_pressed(self, keycode):
        """Handle a reported press; returns its text representation."""
        text = self._meaning(keycode, True)
        if text is None:
            print(f"pressed unknown key {keycode!r}", file=sys.stderr)
            return ""
        return text

    def key_released(self, keycode):
        text = self._meaning(keycode, False)
        if text is None:
            print(f"released unknown key {keycode!r}", file=sys.stderr)

    # ---- host -> device: simulating key presses ----------------------

    def press(self, keycode):
        if keycode is None:
            print("press of unmapped key skipped", file=sys.stderr)
            return
        self.transport.write(f"p{keycode}\r")

    def release(self, keycode):
        if keycode is None:
            print("release of unmapped key skipped", file=sys.stderr)
            return
        self.transport.write(f"r{keycode}\r")

    def tap(self, meaning):
        """Press and release the key with the given meaning."""
        code = self.unshifted.get(meaning) or self.shifted.get(meaning)
        self.press(code)
        self.release(code)

    def drive(self, key_or_text):
        """Type a named Key or a string of literal text on the device."""
        if isinstance(key_or_text, Key):
            self.tap(key_or_text)
        else:
            self.drive_text(key_or_text)

    def drive_text(self, text):
        for ch in text.rstrip("\r\n"):
            shift0 = self.shift_state
            caps0 = self.caps_lock_state
            num0 = self.num_lock_state
            if ch.isdigit():
                code = self.shifted.get(ch)
                # digits need shift unless num lock or shift is already on
                need_shift = not (num0 or shift0)
                if need_shift:
                    self.press(self.unshifted[Key("shift")])
                self.press(code)
                self.release(code)
                if need_shift:
                    self.release(self.unshifted[Key("shift")])
            elif ch.isascii() and ch.isalpha():
                code = self.unshifted.get(ch.upper())
                if num0:
                    self.tap(Key("num_lock"))
                    self.num_lock(True)
                if (not caps0 and ch.isupper()) or (caps0 and ch.islower()):
                    self.tap(Key("caps_lock"))
                    self.caps_lock(True)
                self.press(code)
                self.release(code)
            elif ch == "\x15":  # ctrl-U: clear
                if not shift0:
                    self.press(self.unshifted[Key("shift")])
                code = self.shifted[Key("clear")]
                self.press(code)
                self.release(code)
                if not shift0:
                    self.release(self.unshifted[Key("shift")])
            else:
                code = self.unshifted.get(ch) or self.shifted.get(ch)
                self.press(code)
                self.release(code)

    # ---- named key handlers (called when the device reports them) ----

    def shift(self, pressed):
        self.shift_state = pressed
        return ""

    def caps_lock(self, pressed):
        if pressed:
            self.caps_lock_state = not self.caps_lock_state
        return ""

    def num_lock(self, pressed):
        if pressed:
            self.num_lock_state = not self.num_lock_state
        return ""

    def power(self, pressed):
        if pressed:
            self.power_state = not self.power_state
        return None  # renders as <power>

    def backspace(self, pressed):
        return "\x08" if pressed else ""

    def clear(self, pressed):
        return "\r" if pressed else ""


class BrotherPTouchHomeAndHobby(Keyboard):
    # keycode ("RC" row/column), unshifted meaning, shifted meaning
    KEY_DEFS = (
        # printing characters
        ("72", " ", Key("feed")),
        ("71", ",", ":"),
        ("42", ".", "?"),
        ("53", "'", "/"),
        ("02", '"', "!"),
        ("54", "&", Key("size")),
        ("64", "A", Key("style")),
        ("13", "B", "<cent>"),
        ("73", "C", "@"),
        ("74", "D", Key("frame")),
        ("35", "E", "3"),
        ("24", "F", Key("accent")),
        ("14", "G", Key("repeat")),
        ("44", "H", Key("check")),
        ("05", "I", "8"),
        ("04", "J", Key("preset")),
        ("01", "K", "("),
        ("41", "L", ")"),
        ("03", "M", "."),
        ("43", "N", "<telephone>"),
        ("11", "O", "9"),
        ("21", "P", "0"),
        ("55", "Q", "1"),
        ("75", "R", "4"),
        ("34", "S", Key("underline")),
        ("25", "T", "5"),
        ("45", "U", "7"),
        ("23", "V", "$"),
        ("65", "W", "2"),
        ("33", "X", "~"),
        ("15", "Y", "6"),
        ("63", "Z", "-"),
        ("22", "\n", "\n"),
        # control keys
        ("31", Key("backspace"), Key("clear")),
        ("51", Key("l_arrow"), Key("home")),
        ("61", Key("r_arrow"), Key("end")),
        ("60", Key("symbol"), Key("symbol")),
        # shift keys
        ("52", Key("shift"), Key("shift")),
        ("12", Key("shift"), Key("shift")),
        # sticky mod keys
        ("62", Key("caps_lock"), Key("caps_lock")),
        ("32", Key("num_lock"), Key("num_lock")),
        # special keys
        ("06", Key("power"), Key("power")),
        ("50", Key("print"), Key("print")),
    )


DEFAULT_BAUD = 38400
PORT_PATTERNS = ("/dev/cu.usb*", "/dev/ttyACM*", "/dev/ttyUSB*")


def find_port():
    """Return the first likely serial port, or exit with an error."""
    for pattern in PORT_PATTERNS:
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    print("Can't find serial port!", file=sys.stderr)
    sys.exit(1)


_WS_SCHEMES = ("ws://", "wss://", "http://", "https://")


def _to_ws_url(arg):
    """Normalize a user-supplied URL to a ws(s):// URL with a path.

    http(s):// becomes ws(s)://, and a bare host (no path) gets /ws.
    """
    url = arg
    if url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    elif url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    scheme, rest = url.split("://", 1)
    if "/" not in rest:
        rest += "/ws"
    return scheme + "://" + rest


def open_keyboard(argv):
    """Open a BrotherPTouchHomeAndHobby from command-line arguments.

    First argument may be a serial port (default), or a WebSocket/HTTP URL
    to reach the firmware over WiFi (e.g. ws://192.168.1.50/ws or
    http://remotekeyboard.local). For WiFi, the second argument (or the
    RK_TOKEN environment variable) supplies the AUTH_TOKEN.
    """
    args = argv[1:]
    if args and args[0].startswith(_WS_SCHEMES):
        url = _to_ws_url(args[0])
        token = args[1] if len(args) > 1 else os.environ.get("RK_TOKEN")
        print(f"Using WebSocket {url}", file=sys.stderr)
        return BrotherPTouchHomeAndHobby(WebSocketInterface(url, token))
    port = args[0] if args else find_port()
    baud = int(args[1]) if len(args) > 1 else DEFAULT_BAUD
    print(f"Using serial port {port}", file=sys.stderr)
    return BrotherPTouchHomeAndHobby(SerialInterface(port, baud))
