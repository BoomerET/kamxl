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

Multi-port values (`MYCALL`, `HBAUD`, `MONITOR`, ...) cross the wire
as JSON arrays: `{"value": [true, false]}`.

### Status codes

| Status | Meaning |
| --- | --- |
| 200 | Success |
| 400 | Malformed request (missing required field) |
| 401 | Missing/invalid `Authorization` header |
| 404 | No such endpoint |
| 405 | Endpoint exists, wrong HTTP method |
| 502 | Request reached the daemon, but the KAM-XL/daemon rejected or failed it (`error.type` is the underlying exception, e.g. `KAMConnectionError`, `KAMTimeoutError`, `KAMError`) |
| 503 | Can't reach the daemon at all -- probably not running |

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

**`EventSource` and auth:** the browser `EventSource` API can't set
custom headers, so it can't send the bearer token this endpoint
requires. Until milestone 4 (web terminal) adds a proper browser-side
solution for this, test the stream with `curl` (which can set
headers) or a Python client, not a bare `EventSource` -- noted here
rather than left as a surprise.

```
curl -N -H "Authorization: Bearer $TOKEN" http://kam-host:8080/monitor/stream
```

## Testing

`tests/test_rest.py` runs a real `KAMDaemon` (wired to the same
scripted fakes used elsewhere) plus a real `kamxl_rest` HTTP server
pointed at it, talked to with real HTTP requests -- the socket and
HTTP layers are exercised for real; only the serial connection
underneath is faked. Included in `python3 run_tests.py`.
