# KAM-XL Python Library — Project Overview

## Roadmap (updated)

The original milestone list below (Terminal Mode robustness, Convers
Mode, packet parsing, monitoring, tests, type hints) is the
foundation -- Milestone 1 here. Direction as of now:

1. **Python library** -- done (this file's original scope, below).
2. **Background daemon** that owns the serial connection to the
   KAM-XL and manages it -- done. `kamxl_daemon.py`: newline-delimited
   JSON over a Unix domain socket, one lock serializing all KAMXL
   access (including a background monitor-broadcast thread, polled in
   short bursts so it doesn't starve ordinary commands), pub/sub
   `monitor.subscribe`/`unsubscribe` for live `Packet` events to any
   number of connected clients. See
   [docs/daemon.md](docs/daemon.md) for the protocol and a documented
   known limitation (monitor traffic and command responses still
   share one physical serial stream on real hardware -- no amount of
   client-side locking changes that). Covered by
   `tests/test_daemon.py`, a real socket/threading integration test
   against the same scripted fakes used elsewhere -- not yet run
   against a real KAM-XL end-to-end (no hardware available in the
   sandbox this was built in); worth a hardware smoke test before
   relying on it. Single KAM-XL / serial port per daemon instance;
   multi-device support isn't in scope yet.
3. **REST API** exposing the daemon's capabilities over HTTP.
4. **Web terminal** -- browser-based Terminal Mode session.
5. **Live packet monitor** -- browser view of `kam.monitor()` traffic
   in real time.
6. **BBS with a modern web UI**.
7. **APRS mapping and station database**.
8. **Plugins for Wavelog, Winlink, and Home Assistant**.

---


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
6. ~~Increase test coverage~~ -- done for now. 39 offline unit tests
   in `tests/` (standard-library `unittest`, no third-party packages
   needed -- run with `python3 run_tests.py`), using a scripted fake
   serial connection to exercise typed getters/setters, echo/`EH?`
   handling, and connect/disconnect edge cases. Includes explicit
   regression tests for the two real bugs found on hardware (stray
   prompt leaking into banners, truncated VIA digipeat lines) so
   they can't silently come back. Still no substitute for real
   hardware testing per the design philosophy above -- this is a
   safety net for regressions, not a replacement.
7. ~~Add type hints throughout~~ -- done for `kamxl.py` and
   `packet.py`. Uses the `typing` module (`Optional`, `Union`, etc.)
   rather than newer `X | None` syntax, for broader Python version
   compatibility. No `mypy` available to statically verify (no
   network access in the sandbox this was built in) -- the offline
   test suite is still what actually proves behavior; re-ran it
   after adding hints and confirmed nothing regressed.
8. Package for PyPI -- scaffolding done. `pyproject.toml` added
   (PEP 621 metadata, flat layout via `py-modules = ["kamxl",
   "packet"]` so the existing `from kamxl import KAMXL` imports used
   by every `*Test.py` script keep working unchanged). Verified with
   `pip install --no-build-isolation -e .` that `kamxl` and `packet`
   import correctly from outside the repo. Not yet published --
   that needs a PyPI account/API token, and this sandbox has no
   network access to PyPI to do a full `python -m build` dry run
   (only got as far as confirming the TOML parses and the modules
   install/import correctly under local setuptools 59.6, which
   predates PEP 621 support -- worth a real `pip install build &&
   python -m build` check on a machine with a current toolchain
   before actually publishing).
9. ~~Write documentation~~ -- done. `docs/quickstart.md`,
   `docs/api_reference.md`, and `docs/troubleshooting.md` (the last
   consolidating the real-hardware quirks found so far: marker
   casing, truncated VIA banners, the stale-prompt race, DIGIPEAT/
   FULLDUP's non-obvious choice types, and MONITOR's sub-filters).
10. ~~Build examples~~ -- done. `examples/`: two `offline_*.py`
    scripts that need no hardware (packet parsing off canned text,
    typed commands off the same scripted fake serial the test suite
    uses) plus three `hardware_*.py` scripts mirroring the
    root-level `*Test.py` pattern but trimmed for reading.

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
