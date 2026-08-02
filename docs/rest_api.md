# REST API

Milestone 3. `kamxl_rest.py` is a thin HTTP front end for
[the daemon](daemon.md) -- it translates HTTP requests into the
daemon's Unix socket protocol and proxies its `packet` events out as
Server-Sent Events. It does not talk to the KAM-XL directly; the
daemon remains the single owner of the serial connection. Start the
daemon first.

## Running it

```
python3 kamxl_daemon.py --port COM8 &
python3 kamxl_rest.py --daemon-socket /tmp/kamxl.sock --port 8080
```

| Flag | Env var | Default |
| --- | --- | --- |
| `--daemon-socket` | `KAMXL_SOCKET` | `/tmp/kamxl.sock` |
| `--host` | `KAMXL_REST_HOST` | `0.0.0.0` (all interfaces) |
| `--port` | `KAMXL_REST_PORT` | `8080` |
| `--api-key` | `KAMXL_REST_API_KEY` | random, generated and printed at startup |

`Ctrl-C`/`SIGTERM` shuts down cleanly, same as the daemon.

## Authentication

Binds to all interfaces by default so it's reachable from other
devices on your LAN (a phone, another PC), which means every request
needs a bearer token:

```
Authorization: Bearer <token>
```

If you don't pass `--api-key`, one is generated randomly and printed
to the terminal at startup -- save it. A request with a missing or
wrong token gets `401 Unauthorized`.

To skip auth entirely, pass `--no-auth` -- refused unless `--host` is
also `127.0.0.1`, since running without auth while reachable from the
whole LAN would defeat the point.

Browser contexts that can't set a custom header -- `EventSource`
(used by `/monitor/stream`), or just pasting a URL into the address
bar to load the [web terminal](#web-terminal) -- can instead pass the
same token as a query parameter: `?token=<token>`. Checked as a
fallback only; the header is still preferred everywhere a client can
set one, since a token in a URL is more likely to end up logged
somewhere (browser history, server access logs) than one in a header.

**Security note:** the token travels in cleartext over plain HTTP.
That's an acceptable bar for a trusted home LAN, but this is *not*
safe to expose over the open internet (e.g. via port forwarding)
without putting TLS in front of it first (a reverse proxy, for
example).

## Response format

Mirrors the daemon's own protocol for consistency:

```json
{"ok": true, "result": <any>}
```
```json
{"ok": false, "error": {"type": "<string>", "message": "<string>"}}
```

## Endpoints

All bodies and responses are JSON. See
[api_reference.md](api_reference.md) for what each underlying `KAMXL`
call actually does.

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| GET | `/ping` | | |
| GET | `/status` | | `{"connected", "port", "monitor_subscribers"}` |
| GET | `/configuration` | | Full `DISPLAY` dump |
| GET | `/params/<COMMAND>` | | Typed get (`get_typed`) |
| PUT | `/params/<COMMAND>` | `{"value": ...}` | Typed set (`set_typed`) |
| GET | `/params/<COMMAND>/raw` | | Raw string get |
| PUT | `/params/<COMMAND>/raw` | `{"value": "..."}` | Raw string set |
| POST | `/connect` | `{"callsign", "via"?, "timeout"?}` | AX.25 CONNECT |
| POST | `/disconnect` | `{"timeout"?}` | AX.25 DISCONNECT |
| POST | `/connected/send` | `{"text", "add_cr"?}` | Send while connected |
| GET | `/connected/read?timeout=5` | | Read while connected |
| GET | `/monitor/stream` | | Server-Sent Events, see below |
| POST | `/terminal/exec` | `{"command", "timeout"?}` | Raw command passthrough, see [Web terminal](#web-terminal) |
| GET | `/` | | Serves the web terminal page |
| GET | `/pbbs/messages` | | List PBBS messages, see [PBBS](#pbbs) |
| GET | `/pbbs/messages/<N>` | | Read PBBS message `N`; `result` is `null` if not found |
| GET | `/pbbs` | | Serves the PBBS web page |

Multi-port values (`MYCALL`, `HBAUD`, `MONITOR`, ...) cross the wire
as JSON arrays: `{"value": [true, false]}`.

`/connect`, `/disconnect`, and `/connected/read` accept a `timeout`
(seconds) that's passed straight through to the underlying KAM-XL
operation -- e.g. `{"timeout": 90}` for a slow digipeated connect.
`/disconnect` also accepts `command_mode_timeout` (default `5`),
which separately bounds the initial Ctrl-C step that returns to
Command mode before the DISCONNECT itself is attempted -- the two run
sequentially, so a slow/unresponsive Ctrl-C step won't be masked by a
generous `timeout` on the DISCONNECT step alone.
This layer automatically waits a bit longer than that on its own end
(so it doesn't give up on you before the daemon could possibly have
an answer); a `504` means even that extended wait wasn't enough,
which usually means the KAM-XL operation is still genuinely in
progress rather than actually broken -- retry, or check status/logs
before assuming something's wrong.

### Status codes

| Status | Meaning |
| --- | --- |
| 200 | Success |
| 400 | Malformed request (missing required field) |
| 401 | Missing/invalid `Authorization` header (or `?token=` query parameter) |
| 404 | No such endpoint |
| 405 | Endpoint exists, wrong HTTP method |
| 502 | Request reached the daemon, but the KAM-XL/daemon rejected or failed it (`error.type` is the underlying exception, e.g. `KAMConnectionError`, `KAMTimeoutError`, `KAMError`) |
| 503 | Can't reach the daemon at all -- probably not running |
| 504 | Reached the daemon, but it hasn't answered yet within the time this layer is willing to wait. Distinct from 502: the operation may still be in progress on the KAM-XL side (most likely on a slow/failed AX.25 connect) rather than having actually failed |

## Examples

```
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/status

curl -H "Authorization: Bearer $TOKEN" \
     -X PUT -d '{"value": [true, false]}' \
     http://kam-host:8080/params/MONITOR

curl -H "Authorization: Bearer $TOKEN" \
     -X POST -d '{"callsign": "KD5EOC-10", "via": "RSSTN"}' \
     http://kam-host:8080/connect
```

## Live monitoring (`/monitor/stream`)

A [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
stream -- plain HTTP, no websockets needed, works directly with a
browser's `EventSource`:

```javascript
const source = new EventSource("http://kam-host:8080/monitor/stream");
// Note: EventSource can't set an Authorization header. See below.

source.onmessage = (e) => {
    const event = JSON.parse(e.data);
    console.log(event.data.source, "->", event.data.destination, event.data.payload);
};
```

Sends a `: keepalive` comment roughly every request timeout period of
silence to keep the connection alive through proxies -- `EventSource`
clients can ignore these (they don't fire `onmessage`).

Internally, waiting for the next event uses `select.select()` rather
than relying on the socket's own read timeout -- a real bug, found
live on hardware: once a `socket.timeout` fires once on a
`socket.makefile()` object, CPython sets a sticky flag that makes
every *later* read on that same file object fail immediately with
`OSError: cannot read from timed out object` instead of trying again.
That silently killed the stream after the very first idle period
(EventSource would then reconnect and repeat, discarding whatever
packet arrived during the gap). `select()` only lets a read happen
once data is actually waiting, so that path is never triggered.

**`EventSource` and auth:** the browser `EventSource` API can't set
custom headers, so authenticate it with the `?token=` query parameter
described under [Authentication](#authentication) instead:

```javascript
const source = new EventSource(
    `http://kam-host:8080/monitor/stream?token=${token}`
);
```

```
curl -N -H "Authorization: Bearer $TOKEN" http://kam-host:8080/monitor/stream
```

## Web terminal

`GET /` serves a single self-contained HTML page (no build step, no
external dependency) with two panes: a live packet monitor on top, a
terminal-like command box below.

The terminal pane: type any raw Terminal Mode command (`VERSION`,
`DISPLAY`, `BEACON`, `MHEARD`, ...) and see the KAM-XL's raw
response, same as a serial terminal program would show. It POSTs to
`/terminal/exec`, which passes the command straight through to
`KAMXL.send_command()` -- no assumption about response shape, so it
works for anything, not just commands `kamxl.py` has typed metadata
for.

The monitor pane (milestone 5): connects to `/monitor/stream` via
`EventSource` and prints each packet as it arrives -- time, port, a
`<TAG>` if the KAM-XL included one (see `frame_type` in
[api_reference.md](api_reference.md) -- `<UI>` is an ordinary beacon,
most others are AX.25 control/supervisory traffic between other
stations), source -> destination (with any digipeat path), payload.
A status indicator (`live` / `reconnecting...`) reflects the
connection state; `EventSource` reconnects on its own if the
connection drops. This only shows traffic if `MONITOR` is actually
`ON` for the relevant port(s) on the KAM-XL -- the page doesn't
enable it for you, so if the feed stays empty, type `MONITOR ON/ON`
(multi-port values are slash-separated, not space-separated) into
the terminal pane first, or `PUT /params/MONITOR`.

```
curl -H "Authorization: Bearer $TOKEN" \
     -X POST -d '{"command": "VERSION"}' \
     http://kam-host:8080/terminal/exec
```

Open it in a browser at `http://kam-host:8080/?token=<token>` -- the
page reads `token` from its own URL and carries it forward on every
request it makes (see the query-string auth fallback above). Without
a valid token, `GET /` itself returns `401` just like any other
endpoint when authentication is enabled.

## PBBS

`GET /pbbs` serves a second self-contained page: a **read-only**
browser for the KAM-XL's own built-in firmware PBBS (mailbox). Not a
BBS this project implements -- the firmware handles message storage,
forwarding, and SYSOP access; this is just a list-and-read view over
it. Linked from the web terminal's header, and back again.

Behind it, `GET /pbbs/messages` and `GET /pbbs/messages/<N>` each
drive a full AX.25 connect/command/disconnect cycle against the
KAM-XL's `MYPBBS` on every call -- there's no persistent PBBS
session, so expect each request to take a few seconds, not be
instant. A connect from the local serial terminal gets automatic
SYSOP privilege per the manual, no password needed.

```
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/pbbs/messages
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/pbbs/messages/6
```

`/pbbs/messages/<N>` returns `{"ok": true, "result": null}` (not a
404) if `N` doesn't resolve to a message -- distinguishing "asked for
something that doesn't exist" from "the endpoint itself doesn't
exist."

**Unverified against real hardware.** Unlike everything else in this
document, the PBBS message-list and message-read parsing (`pbbs.py`)
was built from the manual's documented output format, not a captured
live session -- it's a first draft, expected to need adjustment the
same way `packet.py`'s `HEADER_RE` did once actually tested against a
real PBBS mailbox. If messages come back empty, missing, or
misparsed, that's the first place to look.

## Testing

`tests/test_rest.py` runs a real `KAMDaemon` (wired to the same
scripted fakes used elsewhere) plus a real `kamxl_rest` HTTP server
pointed at it, talked to with real HTTP requests -- the socket and
HTTP layers are exercised for real; only the serial connection
underneath is faked. Included in `python3 run_tests.py`.
