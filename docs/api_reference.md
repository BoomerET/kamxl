# API Reference

Covers the public surface of `kamxl.py` and `packet.py`. Anything
prefixed with `_` is internal and not covered here (see source
comments instead).

## `KAMXL`

```python
KAMXL(port: str, baudrate: int = 19200, timeout: float = 0.25)
```

`port` is passed straight to `pyserial` (e.g. `"COM8"`,
`"/dev/ttyUSB0"`). Nothing touches the wire until `connect()`.

### Connection

| Method | Description |
| --- | --- |
| `connect()` | Open the serial port. No-op if already open. |
| `disconnect()` | Close the serial port if open. Safe to call even if never connected. |
| `is_connected` (property) | `True` if the port is open. |

### Raw commands

| Method | Description |
| --- | --- |
| `send_command(command, command_timeout=10)` | Send a Terminal Mode command, wait for `cmd:`, return the response text (echo and prompt stripped). Raises `KAMCommandError` on `EH?`. |
| `get(command)` | Query a parameter, return its raw string value. |
| `set(command, value)` | Set a parameter to a raw value. |

### Typed commands

| Method | Description |
| --- | --- |
| `get_typed(command)` | Query a parameter known to `COMMANDS`, converted to a Python type (see [Commands](#commands) below). Unknown commands fall back to `get()`. |
| `set_typed(command, value)` | Set a parameter using a normal Python value, validated against its type/choices before writing. Read-only parameters raise `KAMError`. |

### Boolean parameters

| Method | Description |
| --- | --- |
| `get_bool(command)` | Query a single-value `ON`/`OFF` parameter as `bool`. |
| `set_bool(command, value)` | Set a single-value `ON`/`OFF` parameter from a `bool`. |

### Restricted-choice parameters

For parameters with more than two legal values (e.g. `DIGIPEAT`'s
`ON`/`UIONLY`/`OFF`).

| Method | Description |
| --- | --- |
| `get_choice(command)` | Query a single-value choice parameter, upper-cased. |
| `set_choice(command, value, choices=None)` | Set it, validating against `choices` if supplied. |

### Multi-port parameters

KAM-XL parameters that apply separately to port 1 and port 2 use a
`value1/value2` wire format. These helpers return/accept
`(port1, port2)` tuples.

| Method | Description |
| --- | --- |
| `get_multiport(command)` | Raw `(port1_str, port2_str)`. |
| `get_multiport_bool(command)` / `set_multiport_bool(command, port, value)` | Both ports as `bool`, or set just one port (leaving the other unchanged on the wire). |
| `get_multiport_choice(command)` / `set_multiport_choice(command, port, value, choices=None)` | Same, for restricted-choice multi-port parameters like `FULLDUP`. |

### Configuration / default port

| Method | Description |
| --- | --- |
| `get_configuration()` | Run `DISPLAY`, return every one-line setting as a `dict`. Multi-line `DISPLAY` entries aren't parsed here. |
| `get_default_port()` / `set_default_port(port)` | Read/write the `PORT` setting (1 or 2). Note this is the *default* port, not necessarily the active stream -- see `STATUS`/`STREAMSW` in the manual. |

### AX.25 connected mode

| Method | Description |
| --- | --- |
| `connect_station(callsign, via=None, timeout=60)` | Attempt a CONNECT, optionally via one or more digipeaters. Returns the connect banner text. Raises `KAMConnectionError` on a hard failure (retry exceeded, busy, disconnected) or `KAMTimeoutError` if nothing matched in time. |
| `send_connected(text, add_cr=True)` | Send text while in Convers mode. |
| `read_connected(timeout=5)` | Collect connected-mode data for up to `timeout` seconds. |
| `enter_command_mode(timeout=5)` | Send Ctrl-C to leave Convers mode. |
| `disconnect_station(timeout=30)` | Return to Command mode and disconnect the current link. |

### Monitoring

| Method | Description |
| --- | --- |
| `read_available()` | Return whatever bytes are currently waiting, decoded. Non-blocking. |
| `listen(seconds=60, callback=None)` | Collect raw unsolicited text for a fixed window. Returns the text, or streams it to `callback` if given. |
| `monitor(seconds=None, callback=None)` | Like `listen()`, but yields parsed `Packet` objects instead of raw text. Returns a generator if `callback` is omitted (`for packet in kam.monitor(): ...`); otherwise blocks and calls `callback(packet)` for each one. Defaults to running until interrupted (`seconds=None`), unlike `listen()`. |

### PBBS (Milestone 6)

Not a BBS this library implements -- the KAM-XL's own firmware PBBS
(mailbox) already handles message storage, forwarding, and SYSOP
access. These two methods drive it through the connected-mode methods
above (a connect to `MYPBBS`, one command, a disconnect) and hand the
raw text to `pbbs.py` for parsing. A connect from the local serial
terminal gets automatic SYSOP privilege per the manual -- no password
exchange needed.

**Unverified against real hardware** -- `pbbs.py`'s parsing is built
from the manual's documented output format, not a captured live
session (see `pbbs.py`'s module docstring). Treat it as a first draft
pending an actual PBBS test.

| Method | Description |
| --- | --- |
| `list_pbbs_messages(mypbbs=None, connect_timeout=15, read_timeout=5)` | Connect to the PBBS and list its messages. `mypbbs` defaults to the KAM-XL's current `MYPBBS` setting. Returns a list of `PBBSMessageSummary`. |
| `read_pbbs_message(number, mypbbs=None, connect_timeout=15, read_timeout=5)` | Connect to the PBBS and read one message. Returns a `PBBSMessage`, or `None` if the response didn't look like a message (e.g. the number doesn't exist). |

### Exceptions

All inherit from `KAMError`.

| Exception | Raised when |
| --- | --- |
| `KAMError` | Base class; also raised directly for "not connected" and read-only-parameter errors. |
| `KAMCommandError` | The KAM-XL responded with `EH?`. |
| `KAMTimeoutError` | No expected response arrived in time. |
| `KAMConnectionError` | An AX.25 CONNECT failed (retry exceeded, busy, or an immediate disconnect). |

## Commands

`COMMANDS` (a `Dict[str, CommandInfo]`) drives `get_typed`/`set_typed`.
Each entry is:

```python
CommandInfo(type: str, writable: bool = True, choices: Optional[Tuple[str, ...]] = None)
```

| Command | Type | Python shape | Notes |
| --- | --- | --- | --- |
| `MONITOR` | `multiport_bool` | `(bool, bool)` | |
| `MCON` | `multiport_bool` | `(bool, bool)` | |
| `DIGIPEAT` | `choice` | `str` | Single value, not per-port. One of `ON`, `UIONLY`, `OFF`. |
| `FULLDUP` | `multiport_choice` | `(str, str)` | Per port. One of `ON`, `OFF`, `LOOPBACK` each. |
| `MYCALL` | `multiport_string` | `(str, str)` | |
| `HBAUD` | `multiport_int` | `(int, int)` | |
| `PORT` | `int` | `int` | Default radio port, 1 or 2. |
| `VERSION` | `string` | `str` | Read-only. |

Unknown commands passed to `get_typed`/`set_typed` fall back to the
raw `get`/`set` string API rather than raising -- useful for
parameters not yet in `COMMANDS`.

To add a new one, add an entry to `COMMANDS` with the right `type`
(and `choices` if it's a `choice`/`multiport_choice`) -- no other code
changes needed for `get_typed`/`set_typed` to pick it up.

## `Packet`

*(from `packet.py`)*

```python
@dataclass(frozen=True)
class Packet:
    source: str
    destination: str
    digipeaters: Tuple[str, ...]
    port: int
    payload: str
    raw: str
    frame_type: Optional[str] = None
```

Produced by `PacketParser`/`KAMXL.monitor()` from one MONITOR header
line (`SOURCE>DESTINATION[,DIGI1,DIGI2*]/PORT: <TAG>:`, the `<TAG>:`
part present whenever `MCOM`/`MRESP` are `ON` -- the factory default)
plus the payload lines that follow until the next header.
`digipeaters` keeps the `*` suffix KAM-XL appends to any digipeater
that's already repeated the packet; `digipeated` (a `bool` property)
is `True` if any of them have.

`frame_type` is that bracketed tag (`"UI"`, `"C"`, `"UA"`, `"D"`,
`"DM"`, `"I00"`, `"rr1"`, ...) when present, `None` otherwise. `"UI"`
is an ordinary unconnected/beacon frame; most other values are AX.25
connect-session control/supervisory traffic between two *other*
stations (connect/disconnect requests and acks, numbered info frames,
receiver-ready acks, ...) and typically carry no payload of their
own -- see the manual's `MCOM`/`MRESP` entries for the full list.

`raw` is the full original text (header + payload) in case the parsed
fields are ever wrong and you need to fall back to the source text.

## `PacketParser`

*(from `packet.py`)*

Incrementally reassembles MONITOR text -- which arrives in arbitrary,
not-line-aligned chunks -- into `Packet` objects. `KAMXL.monitor()`
uses one internally; use it directly if you're feeding it text from
somewhere other than `listen()`/`read_available()` (see
[`examples/offline_packet_parsing.py`](../examples/offline_packet_parsing.py)).

| Method | Description |
| --- | --- |
| `feed(text)` | Feed newly-received text in. Returns a list of `Packet`s completed as a result (usually 0 or 1). |
| `flush()` | Finalize whatever's left buffered -- call at the end of a session to not lose the last packet, which is only known to be "done" once a following header line arrives. |

## `PBBSMessageSummary` / `PBBSMessage`

*(from `pbbs.py`)*

```python
@dataclass(frozen=True)
class PBBSMessageSummary:
    number: int
    msg_type: str            # 'B' bulletin, 'T' traffic, 'P' private
    status: Optional[str]
    size: int
    to: str
    from_call: str
    date: str
    pages: Optional[int]
    subject: str


@dataclass(frozen=True)
class PBBSMessage:
    number: int
    date: str
    from_call: str
    to: str
    routing: Optional[str]   # "@..." hierarchical routing, if present
    body: str
```

`PBBSMessageSummary` is one row of `list_pbbs_messages()`'s result;
`PBBSMessage` is `read_pbbs_message()`'s result. Both are parsed from
the KAM-XL's own PBBS command output by `pbbs.parse_message_list()`
and `pbbs.parse_message()` respectively -- **unverified against real
hardware**, see the PBBS methods section above.
