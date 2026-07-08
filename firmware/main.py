# RemoteKeyboard firmware entry point (runs automatically at boot).

import micropython

# hard IRQ handlers can't allocate; reserve space for their tracebacks
micropython.alloc_emergency_exception_buf(100)

import config
from remotekeyboard import RemoteKeyboard

RemoteKeyboard(config).run()
