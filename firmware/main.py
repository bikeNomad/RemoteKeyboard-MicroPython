# RemoteKeyboard firmware entry point (runs automatically at boot).

import micropython

# hard IRQ handlers can't allocate; reserve space for their tracebacks
micropython.alloc_emergency_exception_buf(100)

import config

try:
    import asyncio
except ImportError:  # port without asyncio: fall back to the blocking loop
    asyncio = None

if asyncio is not None:
    # networked build: USB/UART and (if configured) WiFi WebSocket at once
    import server

    asyncio.run(server.serve(config))
else:
    from remotekeyboard import RemoteKeyboard

    RemoteKeyboard(config).run()
