"""
Offline, end-to-end tests for kamxl_rest.py: a real KAMDaemon (wired
to the scripted fakes used elsewhere in this suite) plus a real
kamxl_rest HTTP server pointed at it, talked to with real HTTP
requests -- no KAM-XL hardware, no mocking of the socket/HTTP layers
themselves.
"""

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest

from fakes import CannedSerial, ChunkSerial, ScriptedSerial, make_kam

from kamxl_daemon import KAMDaemon
import kamxl_rest


class RestTestCase(unittest.TestCase):
    """
    Starts a KAMDaemon (wired to a caller-supplied fake serial
    connection) and a kamxl_rest server pointed at it, both on
    throwaway addresses, in background threads.
    """

    def start_stack(self, serial, api_key="test-token"):
        kam = make_kam(serial)

        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "kamxl.sock")

        daemon = KAMDaemon(kam, socket_path)
        daemon_thread = threading.Thread(
            target=daemon.serve_forever,
            daemon=True
        )
        daemon_thread.start()

        deadline = time.monotonic() + 2

        while (
            not os.path.exists(socket_path)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        # addCleanup runs LIFO -- register thread.join() *before*
        # shutdown() so that, in execution order, shutdown() (which
        # unblocks serve_forever()'s loop) runs first and join() runs
        # second. Registered the other way around, join(timeout) just
        # burns its whole timeout waiting on a thread that shutdown()
        # hasn't been told to stop yet -- cost every single test in
        # this suite a wasted 2+ seconds before this was caught.
        self.addCleanup(daemon_thread.join, 2)
        self.addCleanup(daemon.shutdown)

        server = kamxl_rest.serve(
            socket_path,
            "127.0.0.1",
            0,  # let the OS pick a free port
            api_key
        )
        server_thread = threading.Thread(
            target=server.serve_forever,
            daemon=True
        )
        server_thread.start()

        self.addCleanup(server_thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        port = server.server_address[1]

        return daemon, port

    def request(
        self,
        port,
        method,
        path,
        body=None,
        token="test-token"
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        headers = {}

        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        data = None

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = json.loads(response.read())

        return response.status, payload


class AuthTests(RestTestCase):
    def test_missing_token_rejected(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping", token=None)

        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["type"], "Unauthorized")

    def test_wrong_token_rejected(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping", token="wrong")

        self.assertEqual(status, 401)

    def test_correct_token_accepted(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_no_auth_mode_requires_no_token(self):
        _, port = self.start_stack(ScriptedSerial({}), api_key=None)

        status, payload = self.request(port, "GET", "/ping", token=None)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])


class EndpointTests(RestTestCase):
    def test_ping(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "pong")

    def test_status(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/status")

        self.assertEqual(status, 200)
        self.assertTrue(payload["result"]["connected"])

    def test_get_typed(self):
        _, port = self.start_stack(ScriptedSerial({
            "HBAUD": "HBAUD    0/1200",
        }))

        status, payload = self.request(port, "GET", "/params/HBAUD")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], [0, 1200])

    def test_set_typed_round_trip(self):
        _, port = self.start_stack(ScriptedSerial({
            "MONITOR": "MONITOR  ON/OFF",
        }))

        status, payload = self.request(
            port, "PUT", "/params/MONITOR",
            body={"value": [True, False]}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], [True, False])

    def test_set_typed_missing_value_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "PUT", "/params/MONITOR",
            body={}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_get_configuration(self):
        _, port = self.start_stack(ScriptedSerial({
            "DISPLAY": "MYCALL   AI6K-10/AI6K-10\r\nHBAUD    0/1200",
        }))

        status, payload = self.request(port, "GET", "/configuration")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["result"],
            {"MYCALL": "AI6K-10/AI6K-10", "HBAUD": "0/1200"}
        )

    def test_get_and_set_raw(self):
        _, port = self.start_stack(ScriptedSerial({
            "MYCALL": "MYCALL   AI6K-10/AI6K-10",
        }))

        status, payload = self.request(port, "GET", "/params/MYCALL/raw")
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "AI6K-10/AI6K-10")

        status, payload = self.request(
            port, "PUT", "/params/MYCALL/raw",
            body={"value": "AI6K-11/AI6K-11"}
        )
        self.assertEqual(status, 200)

    def test_readonly_param_maps_to_502(self):
        _, port = self.start_stack(ScriptedSerial({
            "VERSION": "KAM-XL VERSION 1.24160",
        }))

        status, payload = self.request(
            port, "PUT", "/params/VERSION",
            body={"value": "TEST"}
        )

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["type"], "KAMError")


class ConnectDisconnectTests(RestTestCase):
    def test_connect_success(self):
        _, port = self.start_stack(
            CannedSerial(["*** CONNECTED to KD5EOC-10\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/connect",
            body={"callsign": "KD5EOC-10", "timeout": 1}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "*** CONNECTED to KD5EOC-10\r\n")

    def test_connect_missing_callsign_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "POST", "/connect", body={})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_connect_busy_maps_to_502(self):
        _, port = self.start_stack(
            CannedSerial(["***(N0CALL) busy\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/connect",
            body={"callsign": "N0CALL", "timeout": 1}
        )

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["type"], "KAMConnectionError")

    def test_send_and_read_connected(self):
        _, port = self.start_stack(
            CannedSerial(["hello back!\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/connected/send",
            body={"text": "hi there"}
        )
        self.assertEqual(status, 200)

        status, payload = self.request(
            port, "GET", "/connected/read?timeout=0.3"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "hello back!\r\n")

    def test_disconnect(self):
        _, port = self.start_stack(
            CannedSerial(["cmd:", "cmd:*** DISCONNECTED\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/disconnect",
            body={"timeout": 1}
        )

        self.assertEqual(status, 200)
        self.assertIn("DISCONNECTED", payload["result"])


class RoutingErrorTests(RestTestCase):
    def test_unknown_route_is_404(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/nope")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "NotFound")

    def test_wrong_method_is_405(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "POST", "/ping")

        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["type"], "MethodNotAllowed")


class MonitorStreamTests(RestTestCase):
    def test_stream_delivers_a_packet_event(self):
        _, port = self.start_stack(ChunkSerial([
            "KD5EOC-10>BEACON/2:\r\n",
            "Winlink 2000 RMS Packet Server\r\n",
            "AI6K-4>BEACON/2:\r\n",
        ]))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request(
            "GET", "/monitor/stream",
            headers={"Authorization": "Bearer test-token"}
        )
        response = conn.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/event-stream"
        )

        buf = b""
        deadline = time.monotonic() + 3
        frame = None

        while time.monotonic() < deadline:
            chunk = response.fp.read1(4096)

            if not chunk:
                break

            buf += chunk

            if b"\n\n" in buf:
                frame, _, buf = buf.partition(b"\n\n")

                if frame.startswith(b"data:"):
                    break

                frame = None

        self.assertIsNotNone(frame, "No SSE data frame received in time")

        event = json.loads(frame[len(b"data:"):].decode("utf-8"))

        self.assertEqual(event["event"], "packet")
        self.assertEqual(event["data"]["source"], "KD5EOC-10")
        self.assertEqual(event["data"]["destination"], "BEACON")


class DaemonClientTimeoutTests(unittest.TestCase):
    """
    Directly exercises DaemonClient's socket-timeout handling against a
    minimal fake "daemon" that delays its response by a controlled
    amount -- no KAMDaemon/KAMXL involved, just the socket layer.

    Regression coverage for a real bug: DaemonClient's own socket read
    timeout could fire before the daemon had a chance to answer, since
    both defaulted to the same 10s window. Most visible on /connect
    and /disconnect, whose default timeouts (60s/30s) are relayed to
    the daemon but were *not* reflected in how long the REST layer
    itself waited -- so a legitimately-in-progress connect attempt
    surfaced as an unhandled socket.timeout / 500 instead of either
    succeeding or getting a clean answer from the daemon.
    """

    def start_slow_daemon(self, delay):
        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "slow.sock")

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(socket_path)
        server_sock.listen(1)
        server_sock.settimeout(0.1)

        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = server_sock.accept()
                except socket.timeout:
                    continue

                try:
                    conn.recv(65536)  # the request line; contents unused
                    time.sleep(delay)
                    conn.sendall(
                        (json.dumps(
                            {"id": "1", "ok": True, "result": "slow"}
                        ) + "\n").encode("ascii")
                    )
                finally:
                    conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        self.addCleanup(stop.set)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server_sock.close)

        return socket_path

    def test_default_timeout_too_short_raises_daemon_timeout(self):
        socket_path = self.start_slow_daemon(delay=0.3)
        client = kamxl_rest.DaemonClient(socket_path, timeout=0.1)

        with self.assertRaises(kamxl_rest.DaemonTimeout):
            client.call("ping")

    def test_per_call_socket_timeout_override_waits_long_enough(self):
        # Same slow daemon and the same short default -- but this call
        # asks for a longer wait, exactly like _h_connect/_h_disconnect
        # do when a caller passes a large "timeout" body field.
        socket_path = self.start_slow_daemon(delay=0.3)
        client = kamxl_rest.DaemonClient(socket_path, timeout=0.1)

        response = client.call("ping", _socket_timeout=1)

        self.assertEqual(response["result"], "slow")


if __name__ == "__main__":
    unittest.main()
