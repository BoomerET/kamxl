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
   against the same scripted fakes used elsewhere, and now also
   verified end-to-end against Dave's real KAM-XL (see milestone 3's
   notes below for how that hardware pass went). Single KAM-XL /
   serial port per daemon instance; multi-device support isn't in
   scope yet.
3. **REST API** exposing the daemon's capabilities over HTTP -- done.
   `kamxl_rest.py`: stdlib `http.server` only (no new dependency),
   proxies HTTP requests to the daemon's Unix socket and translates
   its JSON protocol into REST responses (see
   [docs/rest_api.md](docs/rest_api.md) for the endpoint list).
   Binds `0.0.0.0` by default (LAN-accessible, per Dave's choice),
   which meant it needed authentication before that was safe -- a
   bearer token, generated and printed at startup if not supplied,
   checked on every request; `--no-auth` is refused unless bound to
   localhost only. Live monitoring exposed as Server-Sent Events
   (`/monitor/stream`) rather than websockets, so a plain HTTP client
   (or eventually milestone 4's web terminal) can consume it directly
   -- though browser `EventSource` can't set the Authorization header
   this needs, so real browser support for that specifically waits on
   milestone 4. Covered by `tests/test_rest.py`, the same
   fakes-underneath/real-sockets-on-top approach as the daemon's own
   tests. Along the way, found and fixed a real bug in the *test
   harness itself* (not the daemon or REST code): `addCleanup`'s LIFO
   ordering meant `thread.join(2)` was firing before the matching
   `shutdown()` in both `test_daemon.py` and `test_rest.py`, silently
   wasting up to 2 seconds per test on a join that couldn't succeed
   yet -- fixing the registration order (plus tightening
   `serve_forever()`'s default poll interval from 0.5s to 0.1s) took
   the full offline suite from ~35s for 54 tests down to ~7.5s for 74.
   Now running against real hardware -- and found two real bugs doing
   it. First, `kamxl_daemon.py --port` defaulted to `COM8`, so
   starting the daemon without explicitly passing the real device
   (`/dev/ttyUSB2` on Dave's Linux box) silently tried to open a port
   that didn't exist, and every command hung for its full timeout,
   looking exactly like a genuine hardware fault. `--port` is now
   required (or `$KAMXL_PORT`), failing fast with a clear error
   instead. Second, and more subtly: `DaemonClient`'s own socket read
   timeout and the KAM-XL-side command timeout it was waiting on both
   defaulted to 10s, so the REST layer could give up (raising a raw,
   unhandled `socket.timeout` -> 500) at essentially the same moment
   the daemon was still legitimately waiting -- and for `/connect`
   (default 60s) and `/disconnect` (default 30s) specifically, the
   REST socket's fixed 10s timeout meant it would *always* give up
   long before those operations could realistically finish, every
   time. Fixed with a `DaemonTimeout` exception mapped to a proper
   `504`, and per-call socket timeout overrides on `/connect`,
   `/disconnect`, and `/connected/read` that track the caller-supplied
   `timeout` with margin, instead of relying on a single fixed
   default. Covered by `DaemonClientTimeoutTests` in
   `tests/test_rest.py`, exercising the timeout race directly with a
   deliberately slow fake Unix-socket server (no KAM-XL/daemon
   internals needed to prove the fix).

   A third real bug turned up chasing the same hardware session:
   `disconnect_station()`'s internal Ctrl-C-back-to-Command-mode step
   always ran with its own hardcoded 5s timeout, completely ignoring
   whatever `timeout` the caller passed to `disconnect_station()`
   itself. Added a separate `command_mode_timeout` param (default 5,
   unchanged for existing callers), threaded through the daemon and
   `/disconnect`'s REST body, with the REST socket-timeout budget
   updated to cover both sequential steps.

   Both `kamxl_daemon.py` and `kamxl_rest.py` are now confirmed
   working end-to-end against the real KAM-XL (`GET /params/VERSION`
   returning `KAM XL -1.24160- SERIAL NUMBER - 00001D6C2798` over a
   live `curl` call, daemon and REST as separate processes talking
   over the Unix socket exactly as designed). Getting there took a
   genuine debugging odyssey, worth recording since none of it turned
   out to be the code: after ruling out the three real bugs above,
   `/params/VERSION` and `/disconnect` were *still* timing out --
   even a bare, daemon-free `KAMXL` connection in a standalone script
   couldn't get a `cmd:` prompt back, which pointed at the TNC itself
   rather than anything in this codebase. A machine reboot didn't fix
   it either (expected, in hindsight -- that only resets the
   computer, not the separate KAM-XL unit on the other end of the USB
   cable). The leading theory became `INTFACE` being saved as `KISS`
   from earlier Winlink/APRS use, which -- per the manual -- boots
   the unit straight back into KISS mode on every power-up regardless
   of a power cycle, and would explain silence to both plain commands
   and Ctrl-C. Wrote `exitKissMode.py`, a small standalone script that
   sends the raw 3-byte KISS-exit frame (`FEND FF FEND`) outside
   `kamxl.py`'s normal protocol, as a way to test that theory directly
   against hardware. In the end, the actual cause was simpler: the
   post-reboot serial device had shifted (`/dev/ttyUSB2` before the
   reboot, briefly assumed to be `/dev/ttyUSB1` after, actually
   `/dev/ttyUSB0`), and the daemon had been correctly, faithfully
   timing out talking to a port the KAM-XL wasn't even on. `--port`
   being required now (see above) at least turns a *missing* port into
   an immediate error -- a *wrong-but-existing* one still has to fail
   the slow way, which is what happened here.
4. **Web terminal** -- browser-based Terminal Mode session -- done.
   `kamxl_rest.py` now serves it directly: `GET /` returns a single
   self-contained HTML/JS page (no build step, no external
   dependency, consistent with the REST API's own stdlib-only
   philosophy), with a terminal-like input box that POSTs to a new
   `/terminal/exec` endpoint. That endpoint -- and the daemon's new
   `send_command` method underneath it -- is a raw pass-through
   (`KAMXL.send_command()`, no assumption about response shape),
   deliberately distinct from `get`/`get_typed`: it works for any
   command, including ones `kamxl.py` has no typed metadata for at
   all (`BEACON`, `MHEARD`, ...), which is the point of a *terminal*
   as opposed to a structured params API.

   This surfaced the same browser-auth gap already flagged when the
   REST API shipped: `EventSource` (needed for milestone 5's live
   monitor) and a bare `GET /` page load can't set a custom
   `Authorization` header. Solved generally rather than just for the
   terminal page -- `_check_auth()` now also accepts the same token
   as a `?token=` query parameter, checked as a fallback after the
   header. The web terminal page reads `token` from its own URL and
   carries it forward on every request it makes. This also means
   milestone 5 shouldn't need any further backend auth changes when
   it wires up `/monitor/stream` from a browser.

   Covered by `SendCommandTests` in `tests/test_daemon.py` and
   `TerminalTests` plus new query-string-token auth tests in
   `tests/test_rest.py`.
5. **Live packet monitor** -- browser view of `kam.monitor()` traffic
   in real time -- done. Added to the *same* page as the web terminal
   (per Dave's call -- one URL, one token, rather than a separate
   route) as a scrolling feed pane above the command box: time, port,
   source -> destination (with any digipeat path), payload, driven by
   an `EventSource` against the `/monitor/stream` endpoint milestone
   3 already built. This is what the query-string `?token=` auth
   fallback added in milestone 4 was actually for -- no further
   backend auth work was needed to wire it up, as expected. A status
   indicator reflects `live` vs `reconnecting...` (`EventSource`
   retries on its own). Doesn't turn `MONITOR` on for you -- the page
   just displays whatever's already flowing, same as the daemon's
   monitor thread always has; documented in
   [docs/rest_api.md](docs/rest_api.md) so an empty feed isn't a
   surprise. Covered by new `test_page_served_at_root` assertions and
   `test_stream_accepts_query_string_token` in `tests/test_rest.py`
   (real SSE response, query-string auth specifically -- the actual
   in-browser rendering isn't unit-testable offline, same limitation
   as the terminal page's JS).

   Testing this against real RF traffic (tuning to 144.39 MHz, the
   North American APRS frequency, to get a reliable stream of real
   packets rather than relying on a single weak DigiPi link) turned
   up two more real, previously-invisible bugs -- the live monitor
   pane had actually never worked against real hardware until both
   were fixed:

   - `DaemonClient.stream_events()` crashed with
     `OSError: cannot read from timed out object` the moment it hit
     its first idle keepalive window. CPython's `socket.makefile()`
     sets a sticky flag the first time a read times out; every later
     read on that same file object then fails immediately instead of
     trying again. The SSE stream would run for one keepalive window,
     die, and get silently replaced by `EventSource`'s own
     auto-reconnect -- discarding anything that arrived during the
     reconnect gap. Fixed with `select.select()`-based polling so
     `readline()` only ever runs once data is already known to be
     waiting, never touching the socket-level timeout path (and that
     sticky flag) at all. `StreamEventsKeepaliveTests` in
     `tests/test_rest.py` reproduces multiple keepalive cycles against
     a real (idle) fake Unix-socket daemon.
   - Bigger: `packet.py`'s `HEADER_RE` never actually matched a
     real-hardware MONITOR line at all, for *any* packet, ever. `MCOM`
     and `MRESP` -- both `ON/ON` by factory default -- append a
     bracketed frame-type tag to every header
     (`K5LRK>BEACON/2: <UI>:`, `WB5NZV>KD5EOC-10,RSSTN*/2: <<C>>:`,
     `KD5EOC-10>WB5NZV,RSSTN/2: <<I00>>:`, and so on for `UA`, `D`,
     `DM`, and numbered/lowercase supervisory frames like `rr1`) that
     the original regex's strict end-of-line anchor had no allowance
     for. Since `PacketParser._process_line()` only appends
     non-matching lines to an *already-pending* packet, and nothing
     had ever matched to start one, every single line -- going all
     the way back to the daemon's original design -- was silently
     dropped. This is exactly why the monitor pane looked empty even
     with `MONITOR` confirmed on, hardware confirmed receiving
     traffic (per DCD and the DigiPi's own log), and the SSE
     connection confirmed `live`: nothing was ever wrong with any of
     those layers, `packet.py` just never recognized a single real
     header line. Found this way -- live, on hardware -- rather than
     from the manual; the manual's tag-format description was only
     actually confirmed against a real captured log once this bug
     surfaced. Fixed by extending `HEADER_RE` with an optional
     `<TAG>:`/`<<TAG>>:` suffix group, and added a new `frame_type`
     field to `Packet` (`None` for untagged/synthetic fixtures, so
     every existing offline test stayed backward compatible
     unchanged) so `"UI"` (an ordinary beacon) can be told apart from
     AX.25 control/supervisory chatter like `"C"`/`"UA"`/`"D"`/`"DM"`/
     `"I00"`/`"rr1"` at a glance -- now shown as a `<TAG>` in the web
     terminal's monitor pane. `RealHardwareFrameTypeTests` in
     `tests/test_packet_parser.py` uses fixtures taken verbatim from a
     live daemon log (single- and double-bracket tags, digipeated and
     plain, connect/disconnect/info/supervisory frames, back-to-back
     header lines with zero payload between them) to lock this in.
6. **BBS with a modern web UI** -- done, scoped down from the
   original idea after research turned up a much better path: the
   KAM-XL already has a full BBS in firmware (PBBS -- mail, bulletins,
   forwarding, SYSOP access), and neither `kamxl.py` nor the daemon
   had any support at all for incoming AX.25 connects or multiple
   simultaneous sessions, which a custom-built BBS would have needed
   from scratch. Given the choice (asked directly rather than
   assumed), the call was to build a **read-only web UI on top of the
   existing firmware PBBS** instead of a new BBS engine -- list
   messages, read one, done. Much smaller, and the actual AX.25
   session handling stays where it's already proven: the firmware.

   Turns out this fits the existing connected-mode primitives
   perfectly. Per the manual, accessing PBBS -- even your own, even
   locally -- is just an ordinary `CONNECT` to `MYPBBS`, and a local
   serial connect gets automatic SYSOP privilege (no password
   exchange). So `KAMXL.list_pbbs_messages()`/`read_pbbs_message()`
   (new in `kamxl.py`) are thin compositions of
   `connect_station()`/`send_connected()`/`read_connected()`/
   `disconnect_station()` -- all four already hardened by earlier
   milestones -- sending `L` or `R <n>` at PBBS's own
   `ENTER COMMAND:` prompt and handing the raw text to a new `pbbs.py`
   (parsing/dataclasses, the same relationship `packet.py` has to raw
   MONITOR text: `PBBSMessageSummary` for a list row, `PBBSMessage`
   for a read message). `kamxl_daemon.py` gained
   `pbbs.list_messages`/`pbbs.read_message`; `kamxl_rest.py` gained
   `GET /pbbs/messages`, `GET /pbbs/messages/<N>`, and a second
   self-contained page at `GET /pbbs` (list + click-to-read, linked
   from the terminal page's header and back).

   **Partially verified against real hardware.** The empty-mailbox
   case is now confirmed -- Dave's KAM-XL has PBBS enabled with no
   messages, and `parse_message_list()` correctly returned `[]`
   against the real raw text (added as `RealHardwareEmptyMailboxTests`
   in `tests/test_pbbs.py`). That text also revealed two real
   formatting details the manual didn't show -- the sign-on banner
   reads `NNN BYTES AVAILABLE IN NN BLOCKS` (not the manual's plain
   `NNN BYTES AVAILABLE`), and an empty mailbox prints
   `THERE ARE NO MESSAGES` -- neither needed a parser change, since
   `parse_message_list()` already skips any line that doesn't look
   like a numbered message row (worked correctly by design, not
   luck). The populated-list-row format and message-read format are
   still unverified -- that needs an actual message in the mailbox to
   test, which hasn't happened yet. Treat those two as a first draft;
   expect them to need the same kind of real-hardware correction
   `packet.py`'s `HEADER_RE` needed after milestone 5. Offline coverage
   (`tests/test_pbbs.py`, plus daemon/REST tests) uses the manual's
   own example lines as fixtures and stubs the connected-mode
   primitives directly for the call-order/argument-flow/error-handling
   behavior, rather than trying to chain a full connect+command+
   disconnect exchange through one fake serial queue (`read_connected()`'s
   "collect for N seconds" semantics doesn't compose cleanly that
   way -- found this the hard way while writing these tests, not
   while testing hardware).
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
