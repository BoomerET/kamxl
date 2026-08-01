# KAM-XL Python Library — Project Overview

## Purpose

Not another Winlink client. This is a Python library for controlling the
Kantronics KAM-XL in Terminal Mode, exposing a clean Pythonic API instead of
requiring users to type terminal commands.

Goal: another developer should be able to `pip install` this and:

```python
from kamxl import KAMXL
```

and never need to know how the KAM's terminal protocol works.

## Design Philosophy

Hide the terminal interface behind normal Python objects/methods.

| Terminal command | Pythonic equivalent |
| --- | --- |
| `MONITOR ON/OFF` | `kam.set_typed("MONITOR", (True, False))` |
| `PORT 2` | `kam.set_default_port(2)` |
| `DISPLAY` | `kam.get_configuration()` |

**Core principle:** never write code because the manual says the KAM
behaves a certain way — write code based on observed behavior from the
real hardware whenever the two differ. This is what got us through the
`ECHO ON` issue, prompt timing, `DISPLAY` parsing, and connected-mode
experiments, and should stay a guiding rule.

## Coding Style

Prefer: dataclasses, small methods, lots of docstrings, descriptive
exceptions, type hints (future), readability over cleverness.

Avoid: giant functions, global state.

## Test Hardware

- Kantronics KAM-XL, firmware 1.24160
- Callsign AI6K, COM8, Port 2
- Live testing on 1200 baud packet
- Everything should be validated against the real unit, not just mocked

## Current Status

Implemented and working:

- Open/close serial port
- Send terminal commands, read responses
- Automatic timeout handling
- Automatic error detection (`EH?`)
- Typed getters/setters
- Multi-port parameters
- Boolean / integer / string conversion
- Automatic handling of `ECHO ON`
- Passive monitoring (`MONITOR`)
- Listening for unsolicited packet traffic (`listen()`)
- Command metadata via `CommandInfo` dataclass
- Read-only protection
- Custom exceptions (`KAMError`, `KAMCommandError`, `KAMTimeoutError`,
  `KAMConnectionError`)
- AX.25 connected mode: `connect_station()` (direct + VIA digipeat),
  `send_connected()`, `read_connected()`, `disconnect_station()`,
  hardened against real hardware races (see milestone 1 below)
- `Packet` dataclass + `PacketParser` (`packet.py`) reassembling
  chunked, non-line-aligned MONITOR text into structured packets
- `KAMXL.monitor()` -- callback and generator-based structured
  monitoring, built on `PacketParser`

## Immediate Next Milestones

1. ~~Make `connect_station()` robust~~ -- done. Fixed a real race
   condition (`_drain_input()`/`_strip_leading_prompt()`) where a
   stale "cmd:" prompt leaked into CONNECT/DISCONNECT banners, and a
   truncation bug (`require_line_end` on `_read_until_any()`) where a
   marker match returned before the rest of the line -- e.g. a VIA
   digipeat path -- had fully arrived. Validated against a direct
   connect (KD5EOC-10), a digipeated connect (via RSSTN), and a hard
   failure (RETRY COUNT EXCEEDED against N0CALL-15).
2. ~~Implement proper Convers Mode~~ -- done. `send_connected()` /
   `read_connected()` validated with a real live interactive session
   against a DigiPi AX.25 Node (URONode), including playing Zork
   over the link.
3. ~~Add packet parser classes~~ -- done. See `packet.py`.
4. ~~Build a `Packet` dataclass~~ -- done. See `packet.py`.
5. ~~Implement monitoring callbacks~~ -- done. `KAMXL.monitor()`
   supports both `callback=` and `for packet in kam.monitor():`.
6. Increase test coverage
7. Add type hints throughout
8. Package for PyPI
9. Write documentation
10. Build examples

## Long-Term Vision

### Terminal Mode
Complete support: every command, every parameter, typed conversions,
validation, documentation.

### Connected Mode
Behave like a socket:

```python
kam.connect_station("KD5EOC-10", via="RSSTN")
kam.send_connected(...)
kam.read_connected(...)
kam.disconnect_station()
```

### Monitoring
Callbacks and iteration, with packet decoding and filtering:

```python
kam.monitor(callback=my_function)
# or
for packet in kam.monitor():
    ...
```

### Packet Objects
Instead of returning raw text like:

```
KD5EOC-10>WB5NZV,RSSTN*/2:
```

return a structured object:

```python
Packet(
    source="KD5EOC-10",
    destination="WB5NZV",
    digipeaters=["RSSTN*"],
    port=2,
    payload="...",
)
```

Eventually parse UI frames, connected frames, APRS, Winlink, telemetry.

### Streams
Support multiple streams (A/B/C/D) exposed as objects.

### Async Support
Eventually:

```python
async with KAMXL(...) as kam:
    ...

async for packet in kam.monitor():
    ...
```

### Event Callbacks
`on_connect()`, `on_disconnect()`, `on_packet()`, `on_monitor()`,
`on_timeout()`, `on_retry()`.

### Configuration Objects
`kam.get_configuration()` should eventually return a `Configuration`
object with properties instead of a plain dict.

## Future Stretch Goals

APRS helper library, Winlink helper, BBS helper, GPS helper, Morse
helper, remote command helper, NET/ROM support, KISS mode support,
binary protocol support.
