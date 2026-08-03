# API Reference

Covers the public surface of `kamxl.py`, `packet.py`, `pbbs.py`,
`aprs.py`, and `stations.py`. Anything prefixed with `_` is internal
and not covered here (see source comments instead).

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

**Verified against real hardware** -- confirmed against a real KAM-XL
PBBS for both an empty mailbox and a populated one (see `pbbs.py`'s
module docstring and `tests/test_pbbs.py`'s `RealHardwareEmptyMailboxTests`).

`read_timeout` is a worst-case ceiling, not a fixed wait: both methods
collect the connected-mode response through a private
`_collect_pbbs_response()` helper (a thin wrapper around the more
general `_poll_until()`, added for milestone 8's Winlink support --
see below) that polls in short slices and returns as soon as the
PBBS's `ENTER COMMAND:` prompt reappears (meaning it's done sending),
rather than always waiting out the full duration. This replaced an
earlier design that did a single `read_connected(timeout=read_timeout)`
call -- on real hardware, a message that took slightly longer than
that fixed window to fully arrive had its last line silently
truncated.

| Method | Description |
| --- | --- |
| `list_pbbs_messages(mypbbs=None, connect_timeout=15, read_timeout=10)` | Connect to the PBBS and list its messages. `mypbbs` defaults to the KAM-XL's current `MYPBBS` setting. Returns a list of `PBBSMessageSummary`. |
| `read_pbbs_message(number, mypbbs=None, connect_timeout=15, read_timeout=10)` | Connect to the PBBS and read one message. Returns a `PBBSMessage`, or `None` if the response didn't look like a message (e.g. the number doesn't exist). |

### Winlink (Milestone 8)

Connects to a real Winlink RMS Packet gateway over AX.25 and
downloads whatever mail is waiting, using the FBB/B2F forwarding
protocol (`winlink.py`) -- a real, separately-documented protocol
(<https://winlink.org/B2F>, <http://www.f6fbb.org/protocole.html>),
not something derived from the KAM-XL manual.

**Scope, chosen deliberately** (see PROJECT.md's milestone 8
writeup): plain ASCII FBB tier only (no LZHUF compression, no binary
framing -- `winlink.py` never claims `B`/`B1`/`B2` in its own SID, so
a real gateway will never propose a compressed message to us) and
receive-only (never proposes an outbound message of its own). One
practical consequence of the ASCII-only choice: per the B2F spec,
messages received this way carry only a plain title + body, not
Winlink's richer structured address header (Mid/Date/From/To/Subject/
attachments) -- that header is only transmitted to B2-capable clients.

**Partially verified against a real RMS gateway.** Built from the
spec and cross-checked against a trusted open-source reference
implementation (`wl2k-go`) for the one genuinely security-sensitive
piece -- the secure-login response algorithm, verified against
`wl2k-go`'s own published test vectors
(`winlink.SECURE_LOGIN_TEST_VECTORS`, `tests/test_winlink.py`). A
live test against a real gateway (KD5EOC-10) confirmed the SID
exchange, the `;PQ:`/`;PR:` secure-login challenge-response, and
handshake-prompt detection all work end-to-end. That same test also
found a real bug (since fixed): the KAM-XL echoes our own
connected-mode transmission back to us -- the same behavior already
known for PBBS's `L` command -- and `has_end_of_block_marker()` used
to treat a bare `FF` as "the gateway has nothing to propose," so our
own echoed `FF` (always sent, since this module never proposes
anything of its own) could be mistaken for the gateway's reply. It
happened to still produce the right answer that day (there really
was no mail), but would have silently missed real waiting mail.
Fixed by only matching the gateway-only `F>`/`FQ` markers -- see
`has_end_of_block_marker()`'s docstring and
`tests/test_winlink.py`'s
`test_own_echoed_transmission_not_mistaken_for_gateways_reply`.
Proposal parsing and message-body extraction against an actual
populated mailbox remain unverified -- that needs an account with
real mail waiting, which hasn't happened yet. Expect the same kind
of further correction `pbbs.py` and `packet.py`'s `HEADER_RE` both
needed after their own first real tests.

| Method | Description |
| --- | --- |
| `check_winlink_mail(gateway, password, mycall=None, connect_timeout=60, read_timeout=30)` | Connect to `gateway` (an RMS Packet station's callsign), complete secure login if challenged, and download up to one proposal block (5 messages) of waiting mail. `mycall` defaults to the KAM-XL's `MYCALL` (first port value). Returns a list of `WinlinkMessage`, empty if nothing's waiting. `password` is never logged. |

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
and `pbbs.parse_message()` respectively. The message-read format
(`PBBSMessage`) has now been verified against real hardware, including
a real truncation bug found and fixed (see the PBBS methods section
above). The message-*list* row format (`PBBSMessageSummary`) is still
only built from the manual's documented example -- unverified against
a real populated mailbox listing.

## `AprsPosition`

*(from `aprs.py`, milestone 7)*

```python
@dataclass(frozen=True)
class AprsPosition:
    latitude: float
    longitude: float
    symbol_table: str
    symbol_code: str
    comment: str
    timestamp: Optional[str]   # raw APRS timestamp text, not decoded
    raw: str
```

`aprs.parse_position(payload)` decodes an AX.25 UI-frame payload
(`Packet.payload`) as an APRS uncompressed position report, returning
`None` for anything else -- any other APRS data type (status, message,
object, weather, ...), a *compressed* position report (not supported
yet), or non-APRS traffic entirely. MVP scope, chosen deliberately:
position reports are what a map needs; everything else was scoped out
for now. See the module docstring for two other known simplifications
(compressed positions unsupported, position ambiguity not fully
modeled). Built from the public APRS Protocol Reference spec, not
reverse-engineered from the KAM-XL manual, but still treated with the
project's usual caution -- unverified against a real captured APRS
session.

## `Station` / `StationTracker`

*(from `stations.py`, milestone 7)*

```python
@dataclass(frozen=True)
class Station:
    callsign: str
    latitude: float
    longitude: float
    symbol_table: str
    symbol_code: str
    comment: str
    last_heard: float    # time.time() epoch seconds
    packet_count: int
```

`StationTracker` maintains an in-memory "who's where" database, one
`Station` per source callsign (SSIDs are distinct stations), keeping
only the latest known position -- no history, no persistence across
restarts (a deliberate MVP choice; see PROJECT.md). Fed by
`update(packet, now=None)`, which only even attempts
`aprs.parse_position()` for ordinary UI frames (`packet.frame_type` is
`None` or `"UI"`) -- AX.25 connect-session control/supervisory frames
never carry APRS payloads. `kamxl_daemon.py`'s always-on monitor
thread feeds every packet it sees to a shared `StationTracker`
instance, so the database builds passively over time, whether or not
a client is actually watching.

| Method | Description |
| --- | --- |
| `update(packet, now=None)` | Feed one `Packet` in. Returns the resulting `Station` if it decoded as a position report, `None` otherwise. |
| `list_stations()` | All known stations, sorted by callsign. |
| `get_station(callsign)` | One station, or `None` if never heard. |

## `Proposal` / `WinlinkMessage`

*(from `winlink.py`, milestone 8)*

```python
@dataclass(frozen=True)
class Proposal:
    msg_type: str   # 'P' private, 'B' bulletin
    sender: str
    via: str         # "BBS of recipient" (@) field
    recipient: str
    mid: str         # unique message ID, for dedup
    size: int
    raw: str


@dataclass(frozen=True)
class WinlinkMessage:
    title: str       # plain FBB title line -- NOT a structured
                      # Winlink address header, see below
    body: str
    proposal: Proposal
    raw: str
```

`Proposal` is one "FB ..." line -- the gateway offering a message.
`WinlinkMessage` pairs the downloaded body with the `Proposal` that
offered it. Because this module only claims the plain-ASCII FBB tier
(deliberately, see PROJECT.md milestone 8), `title` is just a plain
FBB title line, not Winlink's richer Mid/Date/From/To/Subject header
-- that's only sent to clients claiming B2 support.

| Function | Description |
| --- | --- |
| `secure_login_response(challenge, password)` | The 8-digit response to a `;PQ:` secure-login challenge. Ported from `wl2k-go` and verified against its own test vectors -- see `SECURE_LOGIN_TEST_VECTORS`. Case-sensitive on `password`, matching the reference exactly. |
| `parse_secure_challenge(line)` | Parse a `;PQ: <digits>` line, or `None`. |
| `parse_sid(line)` / `build_sid(app_name, app_version)` | Parse a remote SID line, or build our own (always `F$` -- ASCII-basic + BID only). |
| `sid_has_code(sid, code)` | Whether a parsed `RemoteSID` advertises a given feature code. |
| `build_handshake_response(mycall, app_name, app_version, secure_challenge=None, password=None)` | Build the `;FW:`/SID/`;PR:` block to send after reading the remote's handshake. Raises `ValueError` if a challenge was given with no password. |
| `parse_proposal(line)` / `parse_proposals(text)` | Parse one or all `FB ...` proposal lines. |
| `build_fs_line(count, accept=True)` | Build an `FS ...` line accepting (or rejecting) every proposed message -- MVP scope, no per-message selection yet. |
| `has_end_of_block_marker(text)` / `has_fq_marker(text)` | Whether text contains `F>`/`FF` (block done) or `FQ` (session ending). |
| `split_message_blocks(text, count)` / `parse_message_block(raw_text, proposal)` | Split Ctrl-Z-delimited message bodies out of raw text, and turn one into a `WinlinkMessage`. |
