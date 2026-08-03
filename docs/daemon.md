# Background Daemon

Milestone 2. Only one process can hold the KAM-XL's serial port open
at a time, so `kamxl_daemon.py` becomes the single owner of the
connection -- everything downstream (REST API, web terminal, live
monitor, BBS, ...) is meant to talk to *it*, not directly to the TNC.

Single KAM-XL / serial port per daemon instance for now -- see
"Scope" below.

## Running it

```
python3 kamxl_daemon.py --port COM8 --socket /tmp/kamxl.sock
```

Both flags are optional and fall back to environment variables, then
defaults:

| Flag | Env var | Default |
| --- | --- | --- |
| `--port` | `KAMXL_PORT` | *(required -- no fallback)* |
| `--socket` | `KAMXL_SOCKET` | `/tmp/kamxl.sock` |
| `--baud` | `KAMXL_BAUD` | `19200` |

`--port` has no hardcoded default on purpose: a wrong or missing
serial device should fail immediately with a clear error, not
silently try to open a port that isn't there and hang every
subsequent command until its timeout (a real bug found the hard way
-- see PROJECT.md's milestone 3 notes).

`--baud` must match the KAM-XL's own host baud rate (`HBAUD`) or
every command will time out instead of failing fast with a clear
mismatch error -- another real gap found the hard way: a firmware
flash can leave the KAM-XL at a different host baud than before (e.g.
38400 instead of the usual 19200), and this daemon previously had no
way to be told about that short of editing code. If unsure what the
KAM-XL is actually set to right now, check with a plain serial
terminal (e.g. `minicom`) first -- see
[troubleshooting.md](troubleshooting.md#every-command-times-out-after-a-firmware-flash)
for how to reset it to a known rate with `ABAUD`.

Runs in the foreground; `Ctrl-C` (or `SIGTERM`) closes the KAM-XL
connection, removes the socket file, and exits cleanly.

Logs connections, disconnections, and each request's outcome to the
terminal by default (`INFO` level). Add `-v`/`--verbose` to also log
individual packet broadcasts, and the raw text pulled off the wire
before it's even parsed (`DEBUG` level) -- both off by default since
a busy MONITOR session could otherwise flood the terminal. The raw
line is useful for telling "nothing arrived" apart from "something
arrived but didn't look like a packet header" -- e.g. a line
`packet.py`'s `HEADER_RE` doesn't recognize for some other reason.
(A real, since-fixed version of that exact failure mode: an earlier
`HEADER_RE` had no allowance at all for the KAM-XL's own
`<C>`/`<UA>`/`<UI>`-style control-packet annotations, shown by default
via `MCOM`/`MRESP` -- meaning *no* real-hardware MONITOR line ever
matched, silently. See PROJECT.md's milestone 5 notes.)

```
20:29:03 conn-5344: connected
20:29:03 conn-5344: ping -> ok
20:29:03 conn-5344: disconnected
```

## Protocol

Newline-delimited JSON over a Unix domain socket, in both directions.

**Request** (client -> daemon):

```json
{"id": "1", "method": "get_typed", "params": {"command": "MYCALL"}}
```

**Response** (daemon -> client, matches the request's `id`):

```json
{"id": "1", "ok": true, "result": ["AI6K-10", "AI6K-10"]}
```

or, on failure:

```json
{"id": "1", "ok": false, "error": {"type": "KAMError", "message": "..."}}
```

**Event** (daemon -> client, no `id` -- only sent to connections that
have called `monitor.subscribe`):

```json
{"event": "packet", "data": {"source": "KD5EOC-10", "destination": "BEACON", "digipeaters": [], "port": 2, "payload": "...", "raw": "..."}}
```

A single connection can send multiple requests over its lifetime and,
if subscribed, will see `packet` events interleaved with its request
responses -- match on `id` (present only on responses) to tell them
apart, the way the test client in `tests/test_daemon.py` does.

Tuples (multi-port values, `Packet.digipeaters`, etc.) cross the wire
as JSON arrays; send them back as JSON arrays too (the daemon converts
list -> tuple before handing values to `kamxl.py`).

## Methods

Mirrors the `KAMXL` API from [api_reference.md](api_reference.md) --
see that page for what each one actually does. `params` keys match
the corresponding Python method's arguments; optional arguments may be
omitted to use their default.

| Method | `params` | 
| --- | --- |
| `ping` | *(none)* |
| `status` | *(none)* -- returns `{"connected": bool, "port": str, "monitor_subscribers": int}` |
| `get` | `command` |
| `set` | `command`, `value` |
| `send_command` | `command`, `timeout` (optional) -- raw pass-through: sends ``command`` verbatim, returns whatever text comes back before the next `cmd:` prompt, no assumption about response shape. Powers the web terminal (milestone 4); `get`/`get_typed` are usually the better fit for anything with known typed metadata |
| `get_typed` | `command` |
| `set_typed` | `command`, `value` |
| `get_configuration` | *(none)* |
| `connect_station` | `callsign`, `via` (optional, string or array), `timeout` (optional) |
| `send_connected` | `text`, `add_cr` (optional) |
| `read_connected` | `timeout` (optional) |
| `disconnect_station` | `timeout` (optional), `command_mode_timeout` (optional, default 5) -- separately bounds the initial Ctrl-C-back-to-Command-mode step, which runs before the DISCONNECT confirmation itself |
| `monitor.subscribe` | *(none)* -- this connection starts receiving `packet` events (the background monitor thread itself is always running -- see "Concurrency model" below -- subscribing only affects whether *this connection* gets events pushed to it) |
| `monitor.unsubscribe` | *(none)* -- stops them |
| `pbbs.list_messages` | `mypbbs` (optional), `connect_timeout` (optional, default 15), `read_timeout` (optional, default 10, worst-case ceiling -- see below) -- drives a full connect/`L`/disconnect cycle against the KAM-XL's own firmware PBBS; returns a list of `PBBSMessageSummary` dicts. Verified against real hardware for both an empty and a populated mailbox. |
| `pbbs.read_message` | `number`, `mypbbs`/`connect_timeout`/`read_timeout` (all optional, same as above) -- connect/`R n`/disconnect; returns a `PBBSMessage` dict, or `null` if the number didn't resolve to a message |
| `stations.list` | *(none, milestone 7)* -- returns every known `Station` (see `stations.py`) as a list of dicts, sorted by callsign. Built passively by the always-on monitor thread decoding APRS position reports -- no `monitor.subscribe` needed. |
| `stations.get` | `callsign` -- one `Station` dict, or `null` if never heard |

`read_timeout` is a worst-case ceiling, not a fixed wait: the KAM-XL
library polls the connected-mode response in short slices and returns
as soon as the PBBS's `ENTER COMMAND:` prompt reappears, rather than
always waiting out the full duration. This fixed a real bug where a
message that took slightly longer than the old fixed 5s window to
arrive had its last line silently truncated.

Unknown methods and missing required params come back as an
`ok: false` response (`error.type` of `KAMError` or `MissingParam`
respectively) rather than closing the connection.

## Concurrency model

Every direct call into the wrapped `KAMXL` instance -- including the
background monitor loop -- goes through a single lock, since only one
Terminal Mode transaction can be in flight on the wire at a time.
Multiple client connections are handled concurrently (one thread per
connection), but their actual `KAMXL` calls are serialized behind that
lock.

Monitoring runs in its own background thread. Rather than holding the
lock for the whole loop, it polls in short bursts (~50ms), briefly
acquiring the lock each time -- so ordinary `get`/`set` requests can
still interleave with it instead of blocking for its entire duration.

**Always on, since milestone 7.** Through milestone 6, this thread
started on the first `monitor.subscribe` and stopped once the last
subscriber disconnected or unsubscribed -- fine when broadcasting
`packet` events to a live monitor pane was its only job. Milestone 7
added a station database (`stations.py`) that needs to build up
passively from whatever MONITOR traffic the KAM-XL sees, whether or
not anyone has the map page (or any monitor client) open -- so the
thread now starts the moment a `KAMDaemon` is constructed and only
stops on `shutdown()`. `monitor.subscribe`/`unsubscribe` still control
whether *that connection* receives `packet` events, but no longer have
any bearing on whether the thread itself runs.

### Known limitation

Brief lock acquisition keeps *this process's* commands and monitor
polling from stepping on each other, but it can't change what's
already true of the KAM-XL itself: unsolicited MONITOR traffic and a
command's response share one physical serial stream on real hardware.
If a monitored packet happens to arrive on the wire in the gap between
sending a command and its response finishing, no amount of
client-side locking prevents it from showing up interleaved with (or
folded into) that response -- this is a pre-existing characteristic of
`kamxl.py`'s Terminal Mode handling, not something introduced by the
daemon. Not yet hit in testing; noted here per this project's
practice of writing down real constraints rather than assuming a
locking scheme has fully solved them.

Milestone 7's always-on monitor thread makes this limitation more
likely to actually surface in practice than it was through milestone
6 (when it only ran while a monitor pane happened to be open) --
there's now effectively always a background reader polling between
commands. It doesn't introduce any *new* risk beyond what's described
above (the lock still fully serializes each individual command against
the polling thread), just more opportunities for real MONITOR traffic
to legitimately be waiting in the gap right before a command starts.
Worth watching for if PBBS/connect responses ever look corrupted or
interleaved with unrelated MONITOR text on real hardware.

## Testing

`tests/test_daemon.py` runs a real `KAMDaemon` on a real (throwaway)
Unix socket against the same scripted fake serial connections used
elsewhere in the offline suite, talked to by a real socket client --
the socket and threading layers are exercised for real; only the
serial connection underneath is faked. Included in `python3
run_tests.py`.

## Scope

Single KAM-XL / serial port per daemon instance. Multi-device support
(more than one TNC managed by one daemon) isn't planned until there's
an actual need for it -- see PROJECT.md's roadmap notes for the
reasoning.
