# kamxl

A Python library for controlling the Kantronics KAM-XL packet TNC in
Terminal Mode -- a clean, Pythonic API instead of hand-typed terminal
commands.

```python
from kamxl import KAMXL

kam = KAMXL("COM8")
kam.connect()

kam.set_typed("MONITOR", (True, False))
kam.connect_station("KD5EOC-10", via="RSSTN")

for packet in kam.monitor():
    print(packet.source, "->", packet.destination, ":", packet.payload)
```

This is not a Winlink client -- it's meant to be the general-purpose
layer underneath one (or a BBS, a chat tool, an APRS decoder, ...).

## Status

Actively developed and tested against real hardware (Kantronics
KAM-XL, firmware 1.24160, over live 1200-baud packet). Not yet on
PyPI.

Implemented so far:

- Serial connection management, command/response handling, automatic
  `EH?` error detection and `ECHO ON` handling
- Typed getters/setters for KAM-XL parameters (booleans, integers,
  strings, multi-port and restricted-choice values)
- AX.25 connected mode: `connect_station()` (direct and VIA
  digipeat), `send_connected()`, `read_connected()`,
  `disconnect_station()`
- Passive monitoring: `listen()` for raw text, `monitor()` for
  structured `Packet` objects, callback- or generator-style

See [PROJECT.md](PROJECT.md) for the full design philosophy, roadmap,
and what's coming next (test coverage, type hints, packaging, docs,
examples, async support, APRS/Winlink/BBS helpers).

## Design philosophy

Never write code because the manual says the KAM-XL behaves a
certain way -- write code based on observed behavior from real
hardware whenever the two differ. Several bugs were only ever found
by testing against a live KAM-XL over the air, not by reading the
manual: a stale command prompt leaking into connect/disconnect
banners, a VIA digipeat path getting truncated because a marker
matched before the rest of the line arrived, and a race between an
async link-failure notice and an unrelated command's response.

## License

MIT -- see [LICENSE](LICENSE).
