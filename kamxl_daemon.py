"""
Background daemon that owns the KAM-XL's serial connection and
exposes its capabilities to other local processes over a Unix domain
socket, using a newline-delimited JSON protocol.

Why a daemon: only one process can hold a serial port open at a time.
Everything downstream of this milestone (REST API, web terminal, live
monitor, BBS, ...) is meant to talk to *this* process, not directly to
the TNC.

Protocol (newline-delimited JSON in both directions):

    Client -> Daemon (request):
        {"id": <string>, "method": <string>, "params": {...}}

    Daemon -> Client (response, matches the request's id):
        {"id": <string>, "ok": true, "result": <any>}
        {"id": <string>, "ok": false,
         "error": {"type": <string>, "message": <string>}}

    Daemon -> Client (event -- no "id", only sent to connections that
    have called "monitor.subscribe"):
        {"event": "packet", "data": {...}}

See docs/daemon.md for the full method list, usage, and a known
limitation around monitor traffic and command responses sharing one
physical serial stream.

Run directly:

    python3 kamxl_daemon.py --port COM8 --socket /tmp/kamxl.sock

Single KAM-XL / serial port per daemon instance -- see PROJECT.md for
why multi-device support isn't in scope yet.
"""

import argparse
import dataclasses
import json
import logging
import os
import signal
import socketserver
import threading
import time

from typing import Any, Callable, Dict, Optional, Set

from kamxl import KAMXL, KAMError
from packet import Packet, PacketParser


DEFAULT_SOCKET_PATH = "/tmp/kamxl.sock"

# Not configured here -- main() calls logging.basicConfig() so a real
# daemon run prints activity to its terminal. A NullHandler is added
# directly (rather than leaving this unconfigured) so importing this
# as a library -- e.g. tests/test_daemon.py -- doesn't trigger the
# logging module's "no handlers configured" fallback, which would
# otherwise print WARNING-and-above messages (missing params, command
# errors) straight to stderr during the offline test run.
logger = logging.getLogger("kamxl_daemon")
logger.addHandler(logging.NullHandler())


class KAMDaemon:
    """
    Owns a connected KAMXL instance and dispatches JSON requests to
    it, serialized behind a single lock -- only one Terminal Mode
    transaction can be in flight on the wire at a time.
    """

    def __init__(
        self,
        kam: KAMXL,
        socket_path: str = DEFAULT_SOCKET_PATH
    ) -> None:
        self.kam = kam
        self.socket_path = socket_path

        self._kam_lock = threading.RLock()

        self._subscribers: Set["DaemonRequestHandler"] = set()
        self._subscribers_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()

        self._methods: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "ping": self._m_ping,
            "status": self._m_status,
            "get": self._m_get,
            "set": self._m_set,
            "get_typed": self._m_get_typed,
            "set_typed": self._m_set_typed,
            "get_configuration": self._m_get_configuration,
            "connect_station": self._m_connect_station,
            "send_connected": self._m_send_connected,
            "read_connected": self._m_read_connected,
            "disconnect_station": self._m_disconnect_station,
            "monitor.subscribe": self._m_monitor_subscribe,
            "monitor.unsubscribe": self._m_monitor_unsubscribe,
        }

        self._server: Optional["_ThreadingUnixStreamServer"] = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    # -----------------------------------------------------------------------
    # Method dispatch
    # -----------------------------------------------------------------------

    def dispatch(self, method: Optional[str], params: Dict[str, Any]) -> Any:
        handler = self._methods.get(method) if method else None

        if handler is None:
            raise KAMError(f"Unknown method: {method!r}")

        return handler(params or {})

    def _m_ping(self, params: Dict[str, Any]) -> str:
        return "pong"

    def _m_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._subscribers_lock:
            subscriber_count = len(self._subscribers)

        with self._kam_lock:
            connected = self.kam.is_connected

        return {
            "connected": connected,
            "port": self.kam.port,
            "monitor_subscribers": subscriber_count,
        }

    def _m_get(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            return self.kam.get(params["command"])

    def _m_set(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            return self.kam.set(params["command"], params["value"])

    def _m_get_typed(self, params: Dict[str, Any]) -> Any:
        with self._kam_lock:
            return self.kam.get_typed(params["command"])

    def _m_set_typed(self, params: Dict[str, Any]) -> Any:
        # JSON has no tuple type -- a multi-port value arrives as a
        # list and needs converting back before it reaches kamxl.py's
        # isinstance(value, tuple) checks.
        value = params["value"]

        if isinstance(value, list):
            value = tuple(value)

        with self._kam_lock:
            return self.kam.set_typed(params["command"], value)

    def _m_get_configuration(self, params: Dict[str, Any]) -> Dict[str, str]:
        with self._kam_lock:
            return self.kam.get_configuration()

    def _m_connect_station(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            return self.kam.connect_station(
                params["callsign"],
                via=params.get("via"),
                timeout=params.get("timeout", 60),
            )

    def _m_send_connected(self, params: Dict[str, Any]) -> None:
        with self._kam_lock:
            self.kam.send_connected(
                params["text"],
                add_cr=params.get("add_cr", True),
            )

        return None

    def _m_read_connected(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            return self.kam.read_connected(params.get("timeout", 5))

    def _m_disconnect_station(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            return self.kam.disconnect_station(params.get("timeout", 30))

    def _m_monitor_subscribe(self, params: Dict[str, Any]) -> None:
        # Actual subscriber-set membership is handled by the request
        # handler itself (it needs to add *itself*) -- this just
        # guarantees the background broadcast thread is running.
        self._ensure_monitor_thread()
        return None

    def _m_monitor_unsubscribe(self, params: Dict[str, Any]) -> None:
        return None

    # -----------------------------------------------------------------------
    # Monitor broadcast
    # -----------------------------------------------------------------------

    def _ensure_monitor_thread(self) -> None:
        with self._subscribers_lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return

            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
            )
            self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        parser = PacketParser()

        while not self._monitor_stop.is_set():
            with self._subscribers_lock:
                has_subscribers = bool(self._subscribers)

            if not has_subscribers:
                break

            # Brief lock acquisition per poll (rather than holding it
            # for the whole loop) so ordinary get/set requests can
            # still interleave while monitoring is active. This does
            # NOT fully prevent unsolicited MONITOR traffic and a
            # command's response from arriving interleaved on the
            # wire itself -- see docs/daemon.md's "Known limitation".
            with self._kam_lock:
                text = self.kam.read_available()

            if text:
                for packet in parser.feed(text):
                    self._broadcast_packet(packet)

            time.sleep(0.05)

        # PacketParser only knows a packet is complete once the
        # *next* header line arrives -- so whatever was still being
        # assembled when the last subscriber left (or the daemon is
        # shutting down) would otherwise be silently lost. Flush it
        # out now rather than dropping it.
        for packet in parser.flush():
            self._broadcast_packet(packet)

    def _broadcast_packet(self, packet: Packet) -> None:
        event = {
            "event": "packet",
            "data": dataclasses.asdict(packet),
        }

        with self._subscribers_lock:
            subscribers = list(self._subscribers)

        logger.debug(
            "packet: %s -> %s (port %s) to %d subscriber(s)",
            packet.source, packet.destination, packet.port,
            len(subscribers)
        )

        for handler in subscribers:
            handler.send_event(event)

    def add_subscriber(self, handler: "DaemonRequestHandler") -> None:
        with self._subscribers_lock:
            self._subscribers.add(handler)

        self._ensure_monitor_thread()

    def remove_subscriber(self, handler: "DaemonRequestHandler") -> None:
        with self._subscribers_lock:
            self._subscribers.discard(handler)

    # -----------------------------------------------------------------------
    # Server lifecycle
    # -----------------------------------------------------------------------

    def serve_forever(self) -> None:
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        self._server = _ThreadingUnixStreamServer(
            self.socket_path,
            DaemonRequestHandler
        )
        self._server.daemon_instance = self  # type: ignore[attr-defined]

        try:
            self._server.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        # serve_forever() calls this again itself, from a different
        # thread, once its own serve_forever() loop unblocks as a
        # *result* of the self._server.shutdown() call below -- so
        # this can legitimately be entered concurrently by two
        # threads. Without this guard, one thread can see
        # self._server as not-None, then have the other thread set it
        # to None out from under it before it calls server_close().
        with self._shutdown_lock:
            if self._shutdown_done:
                return

            self._shutdown_done = True

            self._monitor_stop.set()

            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._server = None

            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)

            self.kam.disconnect()


class _ThreadingUnixStreamServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer
):
    daemon_threads = True
    allow_reuse_address = True


class DaemonRequestHandler(socketserver.StreamRequestHandler):
    """
    One instance per connected client. Handles both directions on
    that client's socket: synchronous request/response, and (if
    subscribed) asynchronous "packet" events pushed by the daemon's
    monitor broadcast thread -- hence the write lock, since both can
    happen concurrently from different threads.
    """

    def setup(self) -> None:
        super().setup()
        self._write_lock = threading.Lock()
        self._subscribed = False
        # Unix domain sockets don't have a meaningful client_address
        # (it's typically empty) -- use the handling thread's id as a
        # short, unique-enough label to tell concurrent connections
        # apart in the log.
        self._client_id = f"conn-{threading.get_ident() % 10000}"
        logger.info("%s: connected", self._client_id)

    def handle(self) -> None:
        daemon: KAMDaemon = self.server.daemon_instance  # type: ignore[attr-defined]

        for raw_line in self.rfile:
            line = raw_line.strip()

            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send({
                    "id": None,
                    "ok": False,
                    "error": {
                        "type": "ProtocolError",
                        "message": f"Invalid JSON: {exc}",
                    },
                })
                continue

            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}

            try:
                if method == "monitor.subscribe":
                    self._subscribed = True
                    daemon.add_subscriber(self)
                elif method == "monitor.unsubscribe":
                    self._subscribed = False
                    daemon.remove_subscriber(self)

                result = daemon.dispatch(method, params)

                logger.info("%s: %s -> ok", self._client_id, method)

                self._send({
                    "id": request_id,
                    "ok": True,
                    "result": result,
                })
            except KeyError as exc:
                logger.warning(
                    "%s: %s -> missing param %s",
                    self._client_id, method, exc
                )

                self._send({
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required param: {exc}",
                    },
                })
            except Exception as exc:
                logger.warning(
                    "%s: %s -> %s: %s",
                    self._client_id, method, type(exc).__name__, exc
                )

                self._send({
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                })

    def finish(self) -> None:
        daemon: KAMDaemon = self.server.daemon_instance  # type: ignore[attr-defined]

        if self._subscribed:
            daemon.remove_subscriber(self)

        logger.info("%s: disconnected", self._client_id)

        super().finish()

    def send_event(self, event: Dict[str, Any]) -> None:
        self._send(event)

    def _send(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj) + "\n").encode("ascii")

        with self._write_lock:
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except OSError:
                # Client went away mid-write (e.g. a monitor event
                # racing a disconnect) -- nothing useful to do.
                pass


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        description="KAM-XL background daemon"
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("KAMXL_PORT", "COM8"),
        help="Serial port the KAM-XL is on (default: $KAMXL_PORT or COM8)",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("KAMXL_SOCKET", DEFAULT_SOCKET_PATH),
        help=f"Unix socket path to listen on (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Also log individual packet broadcasts (DEBUG level)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    kam = KAMXL(args.port)
    kam.connect()

    daemon = KAMDaemon(kam, args.socket)

    # socketserver.BaseServer.shutdown() blocks until serve_forever()'s
    # own loop notices and exits -- and it deadlocks if called from the
    # same thread that's running serve_forever(), since that thread is
    # exactly what's suspended (mid-loop) to run this signal handler in
    # the first place. It can only ever resume once the handler
    # returns, and the handler can't return until it does. So
    # serve_forever() runs on a background thread instead, and the
    # signal handler just flags a stop and lets the main thread do the
    # actual daemon.shutdown() call, safely, from outside that thread.
    shutdown_requested = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        logger.info("Shutting down (signal %s)...", signum)
        shutdown_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    server_thread = threading.Thread(
        target=daemon.serve_forever,
        daemon=True
    )
    server_thread.start()

    logger.info("KAM-XL daemon: %s -> %s", args.port, args.socket)

    try:
        while not shutdown_requested.is_set():
            shutdown_requested.wait(timeout=1)
    finally:
        daemon.shutdown()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
