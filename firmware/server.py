# Networked orchestration for RemoteKeyboard (MicroPython + asyncio).
#
# Runs the matrix scanner alongside any number of transports at once:
# the local USB-CDC (or hardware UART) link *and* WebSocket clients over
# WiFi, served with a small browser terminal by microdot. Key events are
# broadcast to every connected transport; commands from any transport are
# handled independently, with replies (debug dump, error echoes) going
# back to the sender.
#
# Design notes:
# - The time-critical matrix scanning is unchanged: the hard column IRQ
#   and the aux Timer still only push bytes into core.EventQueue. This
#   module is the (single) consumer, so swapping in WiFi never touches the
#   allocation-free ISR paths and can't disturb scan timing.
# - Everything here is cooperative asyncio, so forced[] keeps its "written
#   only from the main context" invariant and the SPSC queue keeps its
#   single consumer (the broadcast task).
# - This file is MicroPython-only (asyncio/network/microdot); it is not
#   imported by the CPython unit tests.

import sys
import asyncio

from remotekeyboard import RemoteKeyboard, BANNER
from core import CommandBuffer

# How often (ms) the cooperative tasks poll the event queue and outgoing
# buffers. Key presses are human-paced, so a few ms of added latency is
# imperceptible while keeping the loop cheap.
POLL_MS = 10
WIFI_TIMEOUT_MS = 20000


class _CallableSink:
    """Broadcast target wrapping a synchronous write(str) (USB/UART)."""

    def __init__(self, write_fn):
        self._write = write_fn

    def write(self, s):
        try:
            self._write(s)
        except Exception:
            # a wedged local link must not stall the broadcast task
            pass


class _WsClient:
    """Broadcast target for one WebSocket client.

    write() only appends to an outgoing buffer (safe to call from the
    broadcast task); a dedicated sender coroutine awaits the socket. This
    keeps the synchronous protocol code (_send_events, _handle_command,
    _dump) unchanged while the actual network I/O stays async.
    """

    def __init__(self, ws):
        self.ws = ws
        self._out = []
        self.closed = False

    def write(self, s):
        if not self.closed:
            self._out.append(s)

    async def run_sender(self):
        try:
            while not self.closed:
                if self._out:
                    data = "".join(self._out)
                    self._out = []
                    await self.ws.send(data)
                else:
                    await asyncio.sleep_ms(POLL_MS)
        except Exception:
            self.closed = True


async def _broadcast(kbd, sinks):
    """Drain the event queue and fan each event out to every transport."""
    while True:
        # rebuild each pass so clients that (dis)connect are picked up
        writers = [s.write for s in sinks]
        kbd._send_events(writers)
        await asyncio.sleep_ms(POLL_MS)


def _feeder(kbd, sink):
    """Return (command_buffer, on_command, on_overflow) for one transport."""
    cmds = CommandBuffer()

    def on_command(cmd):
        kbd._handle_command(cmd, sink)

    def on_overflow(line):
        sink.write(line + "?\r\n")

    return cmds, on_command, on_overflow


async def _usb_task(kbd, sink):
    import select

    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    cmds, on_command, on_overflow = _feeder(kbd, sink)
    while True:
        while poll.poll(0):
            ch = sys.stdin.read(1)
            if not ch:
                break
            cmds.feed(ch, on_command, on_overflow)
        await asyncio.sleep_ms(POLL_MS)


async def _uart_task(kbd, sink, uart):
    import select

    poll = select.poll()
    poll.register(uart, select.POLLIN)
    cmds, on_command, on_overflow = _feeder(kbd, sink)
    while True:
        while poll.poll(0):
            b = uart.read(1)
            if not b:
                break
            cmds.feed(chr(b[0]), on_command, on_overflow)
        await asyncio.sleep_ms(POLL_MS)


def _connect_wifi(cfg):
    import network

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    hostname = getattr(cfg, "WIFI_HOSTNAME", "")
    if hostname:
        try:
            network.hostname(hostname)
        except Exception:
            pass
    if not wlan.isconnected():
        wlan.connect(cfg.WIFI_SSID, cfg.WIFI_PASSWORD)
    return wlan


async def _wait_connected(wlan, timeout_ms):
    t = 0
    while t < timeout_ms:
        if wlan.isconnected():
            return True
        await asyncio.sleep_ms(250)
        t += 250
    return wlan.isconnected()


def _build_app(cfg, kbd, sinks):
    from microdot import Microdot, send_file
    from microdot.websocket import with_websocket

    app = Microdot()
    token = getattr(cfg, "AUTH_TOKEN", "")

    @app.route("/")
    async def index(request):
        return send_file("index.html")

    @app.route("/ws")
    @with_websocket
    async def ws_route(request, ws):
        # token auth: an empty configured token disables the WebSocket
        # entirely (a network keyboard injector must not be open).
        if not token or request.args.get("token") != token:
            await ws.send("unauthorized\r\n")
            return
        client = _WsClient(ws)
        sinks.append(client)
        sender = asyncio.create_task(client.run_sender())
        cmds, on_command, on_overflow = _feeder(kbd, client)
        client.write(BANNER)
        try:
            while True:
                msg = await ws.receive()
                if msg is None:
                    break
                cmds.feed(msg, on_command, on_overflow)
        finally:
            client.closed = True
            try:
                sinks.remove(client)
            except ValueError:
                pass
            sender.cancel()

    return app


async def serve(cfg):
    kbd = RemoteKeyboard(cfg)
    kbd._install_irqs()

    # The local link is always registered so the event queue is drained
    # even when no WebSocket client is connected.
    sinks = []
    tasks = []
    if getattr(cfg, "USE_UART", False):
        uart = kbd._io._uart
        local = _CallableSink(uart.write)
        sinks.append(local)
        tasks.append(asyncio.create_task(_uart_task(kbd, local, uart)))
    else:
        local = _CallableSink(sys.stdout.write)
        sinks.append(local)
        tasks.append(asyncio.create_task(_usb_task(kbd, local)))

    local.write(BANNER)
    tasks.append(asyncio.create_task(_broadcast(kbd, sinks)))

    if getattr(cfg, "WIFI_SSID", ""):
        try:
            wlan = _connect_wifi(cfg)
            if await _wait_connected(wlan, WIFI_TIMEOUT_MS):
                ip = wlan.ifconfig()[0]
                port = getattr(cfg, "WEB_PORT", 80)
                local.write("WiFi connected: http://%s:%d/\r\n" % (ip, port))
                if not getattr(cfg, "AUTH_TOKEN", ""):
                    local.write(
                        "WARNING: AUTH_TOKEN is empty; WebSocket disabled.\r\n"
                    )
                app = _build_app(cfg, kbd, sinks)
                tasks.append(asyncio.create_task(app.start_server(port=port)))
            else:
                local.write("WiFi connect timed out; USB/UART only.\r\n")
        except Exception as e:
            local.write("WiFi error: %r; USB/UART only.\r\n" % (e,))

    await asyncio.gather(*tasks)
