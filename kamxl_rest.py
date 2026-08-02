"""
REST API in front of kamxl_daemon.py -- translates HTTP requests into
the daemon's newline-delimited JSON protocol over its Unix socket, and
proxies its "packet" events out as Server-Sent Events for live
monitoring over plain HTTP (no websockets needed).

This process does NOT talk to the KAM-XL directly -- it's a thin HTTP
front end for the daemon, which remains the single owner of the
serial connection. Run kamxl_daemon.py first.

Run directly:

    python3 kamxl_rest.py --daemon-socket /tmp/kamxl.sock --port 8080

See docs/rest_api.md for the full endpoint list, authentication, and
usage examples.

Security note: binds to 0.0.0.0 (all interfaces) by default, since
the point is reaching it from other devices on the LAN (a phone,
another PC). That means it needs authentication -- a bearer token,
checked on every request. The token still travels in cleartext over
plain HTTP, though, which is an acceptable bar for a trusted home LAN
but NOT safe to expose over the open internet (e.g. via port
forwarding) without putting TLS in front of it first.
"""

import argparse
import json
import logging
import os
import re
import secrets
import signal
import socket
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs


DEFAULT_DAEMON_SOCKET = "/tmp/kamxl.sock"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080

# Daemon error types that mean "the request was well-formed, but the
# KAM-XL (or the daemon itself) couldn't fulfill it" -- as opposed to
# a malformed request, which is a client-side (4xx) problem.
_DAEMON_ERROR_STATUS = 502

logger = logging.getLogger("kamxl_rest")
logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Daemon client
# ---------------------------------------------------------------------------

class DaemonUnavailable(Exception):
    """Couldn't reach the daemon's Unix socket at all."""
    pass


class DaemonTimeout(Exception):
    """
    The daemon didn't send a response within the expected time.

    Distinct from DaemonUnavailable: the daemon is reachable, it's just
    not answering yet -- most often because the underlying KAM-XL
    command (or AX.25 connect/disconnect) is still legitimately in
    progress and hasn't hit its own timeout yet.
    """
    pass


class DaemonClient:
    """
    Talks to a running kamxl_daemon.py over its Unix socket.

    Opens a fresh connection per call (and per SSE stream) rather than
    holding one open and sharing it across HTTP handler threads --
    simpler to reason about correctness-wise than synchronizing a
    single shared connection, and Unix socket connections are cheap
    enough that this isn't a meaningful cost for a LAN tool.
    """

    # Default socket-level read timeout for a single call(). Must stay
    # comfortably above kamxl.py's own default command_timeout (10s) --
    # otherwise this socket read gives up before the daemon has even
    # had a chance to hit *its* timeout and send back a clean JSON
    # error, and callers see a raw, unhandled socket.timeout instead.
    # Endpoints that relay a caller-supplied timeout (connect/
    # disconnect/read_connected, which can legitimately run well past
    # this) pass their own, larger _socket_timeout per call instead of
    # relying on this default -- see kamxl_rest.py's _h_connect etc.
    def __init__(self, socket_path: str, timeout: float = 15) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(
        self,
        method: str,
        _socket_timeout: Optional[float] = None,
        **params: Any
    ) -> Dict[str, Any]:
        sock = self._connect(_socket_timeout)

        try:
            sock.sendall(self._encode("1", method, params))

            rfile = sock.makefile("r")

            try:
                line = rfile.readline()
            except socket.timeout:
                raise DaemonTimeout(
                    f"Daemon didn't respond to {method!r} within "
                    f"{_socket_timeout if _socket_timeout is not None else self.timeout}s"
                )

            if not line:
                raise DaemonUnavailable(
                    "Daemon closed the connection unexpectedly"
                )

            return json.loads(line)
        finally:
            sock.close()

    def stream_events(self, method: str, **params: Any):
        """
        Subscribe via ``method`` (e.g. "monitor.subscribe") on a
        dedicated connection and yield each subsequent event dict.
        Yields None roughly every ``self.timeout`` seconds of silence
        instead of blocking forever, so callers (the SSE handler) can
        use that to send a keepalive and notice a closed HTTP client.

        The connection is closed (ending the daemon-side subscription
        too) once the caller stops iterating.
        """
        sock = self._connect()

        try:
            sock.sendall(self._encode("1", method, params))

            rfile = sock.makefile("r")
            ack = rfile.readline()

            if not ack:
                raise DaemonUnavailable(
                    "Daemon closed the connection unexpectedly"
                )

            while True:
                try:
                    line = rfile.readline()
                except socket.timeout:
                    yield None
                    continue

                if not line:
                    return

                line = line.strip()

                if line:
                    yield json.loads(line)
        finally:
            sock.close()

    def _connect(self, timeout: Optional[float] = None) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout if timeout is not None else self.timeout)

        try:
            sock.connect(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError) as exc:
            sock.close()
            raise DaemonUnavailable(
                f"Can't reach kamxl_daemon.py at {self.socket_path}: {exc}"
            )

        return sock

    @staticmethod
    def _encode(request_id: str, method: str, params: Dict[str, Any]) -> bytes:
        return (json.dumps({
            "id": request_id,
            "method": method,
            "params": params,
        }) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

# Each route: (HTTP method, compiled path regex, handler method name).
# Handlers are looked up by name on RESTRequestHandler so they can be
# plain instance methods with access to self.daemon / self._send_json
# / etc.
ROUTES: Tuple[Tuple[str, "re.Pattern", str], ...] = (
    ("GET", re.compile(r"^/ping$"), "_h_ping"),
    ("GET", re.compile(r"^/status$"), "_h_status"),
    ("GET", re.compile(r"^/configuration$"), "_h_get_configuration"),
    ("GET", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)$"), "_h_get_typed"),
    ("PUT", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)$"), "_h_set_typed"),
    ("GET", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)/raw$"), "_h_get_raw"),
    ("PUT", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)/raw$"), "_h_set_raw"),
    ("POST", re.compile(r"^/connect$"), "_h_connect"),
    ("POST", re.compile(r"^/disconnect$"), "_h_disconnect"),
    ("POST", re.compile(r"^/connected/send$"), "_h_send_connected"),
    ("GET", re.compile(r"^/connected/read$"), "_h_read_connected"),
    ("GET", re.compile(r"^/monitor/stream$"), "_h_monitor_stream"),
)


class RESTRequestHandler(BaseHTTPRequestHandler):
    # Set on the class by serve() below, so every handler instance
    # (one per request, per BaseHTTPRequestHandler's design) shares
    # the same DaemonClient and API key without needing a custom
    # server subclass to pass them through.
    daemon: DaemonClient
    api_key: Optional[str]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Route stdlib's own per-request access log through our
        # logger instead of straight to stderr, so -v/--verbose and
        # the rest of the logging setup applies to it too.
        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- Dispatch -----------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, http_method: str) -> None:
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        method_matched_path = False

        for route_method, pattern, handler_name in ROUTES:
            match = pattern.match(path)

            if match is None:
                continue

            method_matched_path = True

            if route_method != http_method:
                continue

            handler = getattr(self, handler_name)

            try:
                handler(match.groupdict(), query)
            except DaemonTimeout as exc:
                self._send_json(504, {
                    "ok": False,
                    "error": {"type": "DaemonTimeout", "message": str(exc)},
                })
            except DaemonUnavailable as exc:
                self._send_json(503, {
                    "ok": False,
                    "error": {"type": "DaemonUnavailable", "message": str(exc)},
                })
            except Exception as exc:
                logger.exception("Unhandled error in %s", handler_name)
                self._send_json(500, {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                })

            return

        if method_matched_path:
            self._send_json(405, {
                "ok": False,
                "error": {"type": "MethodNotAllowed", "message": f"{http_method} not allowed on {path}"},
            })
        else:
            self._send_json(404, {
                "ok": False,
                "error": {"type": "NotFound", "message": f"No such endpoint: {path}"},
            })

    def _check_auth(self) -> bool:
        if not self.api_key:
            return True

        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.api_key}"

        if not secrets.compare_digest(header, expected):
            self._send_json(401, {
                "ok": False,
                "error": {
                    "type": "Unauthorized",
                    "message": "Missing or invalid Authorization: Bearer <token> header",
                },
            })
            return False

        return True

    # -- Body / response helpers --------------------------------------

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))

        if length == 0:
            return {}

        raw = self.rfile.read(length)

        if not raw.strip():
            return {}

        return json.loads(raw)

    def _send_json(self, status: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay(
        self,
        method: str,
        _socket_timeout: Optional[float] = None,
        **params: Any
    ) -> None:
        """
        Call a daemon method and translate its {"ok": ...} response
        directly into the equivalent HTTP response.

        _socket_timeout overrides how long we wait on our end for the
        daemon's reply. It must stay above whatever "timeout" (if any)
        is being passed through in **params for the underlying KAM-XL
        operation -- otherwise this call gives up before the daemon
        could possibly have an answer yet, surfacing as a raw
        DaemonTimeout instead of the operation actually completing.
        """
        response = self.daemon.call(
            method,
            _socket_timeout=_socket_timeout,
            **params
        )

        if response.get("ok"):
            self._send_json(200, {"ok": True, "result": response.get("result")})
            return

        error = response.get("error") or {}

        status = (
            400
            if error.get("type") == "MissingParam"
            else _DAEMON_ERROR_STATUS
        )

        self._send_json(status, {"ok": False, "error": error})

    # -- Endpoint handlers ----------------------------------------------

    def _h_ping(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("ping")

    def _h_status(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("status")

    def _h_get_configuration(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get_configuration")

    def _h_get_typed(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get_typed", command=params["command"])

    def _h_set_typed(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "value" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'value'"},
            })
            return

        self._relay("set_typed", command=params["command"], value=body["value"])

    def _h_get_raw(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get", command=params["command"])

    def _h_set_raw(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "value" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'value'"},
            })
            return

        self._relay("set", command=params["command"], value=body["value"])

    def _h_connect(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "callsign" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'callsign'"},
            })
            return

        connect_timeout = body.get("timeout", 60)

        self._relay(
            "connect_station",
            callsign=body["callsign"],
            via=body.get("via"),
            timeout=connect_timeout,
            # +5s margin over the KAM-XL-side timeout we just passed
            # through, so this socket doesn't give up before the
            # daemon could possibly have an answer.
            _socket_timeout=connect_timeout + 5,
        )

    def _h_disconnect(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()
        disconnect_timeout = body.get("timeout", 30)

        self._relay(
            "disconnect_station",
            timeout=disconnect_timeout,
            _socket_timeout=disconnect_timeout + 5,
        )

    def _h_send_connected(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "text" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'text'"},
            })
            return

        self._relay(
            "send_connected",
            text=body["text"],
            add_cr=body.get("add_cr", True),
        )

    def _h_read_connected(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        timeout = float(query.get("timeout", ["5"])[0])
        self._relay(
            "read_connected",
            timeout=timeout,
            _socket_timeout=timeout + 5,
        )

    def _h_monitor_stream(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            for event in self.daemon.stream_events("monitor.subscribe"):
                if event is None:
                    chunk = b": keepalive\n\n"
                else:
                    chunk = f"data: {json.dumps(event)}\n\n".encode("utf-8")

                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client navigated away / closed the EventSource -- not an
            # error, just the end of this stream.
            pass


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

class _RestHTTPServer(ThreadingHTTPServer):
    # socketserver's default poll_interval (0.5s) is how often
    # serve_forever()'s loop wakes up to check for a pending
    # shutdown() -- overriding the default here (rather than passing
    # poll_interval at each serve_forever() call site) covers both
    # main() and anything else -- e.g. tests/test_rest.py -- that
    # calls serve_forever() directly on a server built by serve().
    def serve_forever(self, poll_interval: float = 0.1) -> None:
        super().serve_forever(poll_interval=poll_interval)


def serve(
    daemon_socket: str,
    host: str,
    port: int,
    api_key: Optional[str],
) -> ThreadingHTTPServer:
    RESTRequestHandler.daemon = DaemonClient(daemon_socket)
    RESTRequestHandler.api_key = api_key

    return _RestHTTPServer((host, port), RESTRequestHandler)


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        description="REST API for kamxl_daemon.py"
    )
    parser.add_argument(
        "--daemon-socket",
        default=os.environ.get("KAMXL_SOCKET", DEFAULT_DAEMON_SOCKET),
        help=f"Path to the daemon's Unix socket (default: {DEFAULT_DAEMON_SOCKET})",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("KAMXL_REST_HOST", DEFAULT_HOST),
        help=f"Address to bind to (default: {DEFAULT_HOST} -- all interfaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("KAMXL_REST_PORT", DEFAULT_PORT)),
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KAMXL_REST_API_KEY"),
        help="Bearer token required on every request. Random one generated "
             "and printed at startup if not given. Pass --no-auth to disable "
             "(only if --host is 127.0.0.1 / localhost).",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable authentication. Refused unless --host is localhost.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Also log each proxied daemon call (DEBUG level)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.no_auth:
        if args.host not in ("127.0.0.1", "localhost"):
            parser.error(
                "--no-auth is only allowed with --host 127.0.0.1 "
                "(this API is reachable from your whole LAN otherwise)"
            )
        api_key = None
        logger.warning("Authentication disabled (--no-auth, localhost only)")
    else:
        api_key = args.api_key or secrets.token_urlsafe(32)

        if not args.api_key:
            logger.info("Generated API key (save this): %s", api_key)

    server = serve(args.daemon_socket, args.host, args.port, api_key)

    # Same reasoning as kamxl_daemon.py's main(): serve_forever() runs
    # on a background thread so that shutdown() -- called from the
    # signal handler's thread -- doesn't deadlock waiting for a loop
    # it would otherwise be blocking itself.
    shutdown_requested = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        logger.info("Shutting down (signal %s)...", signum)
        shutdown_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    logger.info(
        "KAM-XL REST API: http://%s:%s -> %s",
        args.host, args.port, args.daemon_socket
    )

    try:
        while not shutdown_requested.is_set():
            shutdown_requested.wait(timeout=1)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
