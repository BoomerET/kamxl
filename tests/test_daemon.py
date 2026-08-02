"""
Offline, end-to-end tests for kamxl_daemon.py: a real KAMDaemon,
wired to the same scripted fake serial connections used elsewhere in
this suite, listening on a real (throwaway) Unix socket, talked to by
a real socket client -- no KAM-XL hardware, no mocking of the socket
layer itself.
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest

from fakes import CannedSerial, ChunkSerial, ScriptedSerial, make_kam

from kamxl_daemon import KAMDaemon


class _Client:
    """
    Minimal newline-delimited JSON client for exercising a KAMDaemon
    over its Unix socket.
    """

    def __init__(self, socket_path, timeout=2):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(socket_path)
        self.rfile = self.sock.makefile("r")
        self._next_id = 0

    def call(self, method, **params):
        self._next_id += 1
        request_id = str(self._next_id)

        self.sock.sendall((json.dumps({
            "id": request_id,
            "method": method,
            "params": params,
        }) + "\n").encode("ascii"))

        # Skip past any monitor "packet" events (no "id") that might
        # be interleaved before our matching response arrives.
        while True:
            response = json.loads(self._readline())

            if response.get("id") == request_id:
                return response

    def read_event(self, timeout=2):
        self.sock.settimeout(timeout)
        return json.loads(self._readline())

    def _readline(self):
        line = self.rfile.readline()

        if not line:
            raise ConnectionError("Daemon closed the connection")

        return line

    def close(self):
        # socket.makefile() keeps its own reference to the underlying
        # fd -- closing just self.sock without also closing self.rfile
        # leaves the connection technically still open at the OS
        # level (refcounted), so the server never sees EOF/reset.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        self.rfile.close()
        self.sock.close()


class DaemonTestCase(unittest.TestCase):
    """
    Starts a KAMDaemon wired to a caller-supplied fake serial
    connection, on a throwaway Unix socket, in a background thread.
    """

    def start_daemon(self, serial):
        kam = make_kam(serial)

        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "kamxl.sock")

        daemon = KAMDaemon(kam, socket_path)

        thread = threading.Thread(
            target=daemon.serve_forever,
            daemon=True
        )
        thread.start()

        deadline = time.monotonic() + 2

        while (
            not os.path.exists(socket_path)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        # addCleanup runs LIFO -- register thread.join() *before*
        # shutdown() so shutdown() (which unblocks serve_forever()'s
        # loop) actually runs first. Registered the other way around,
        # join(timeout) just burns its whole timeout waiting on a
        # thread nothing has told to stop yet.
        self.addCleanup(thread.join, 2)
        self.addCleanup(daemon.shutdown)

        return daemon, socket_path

    def connect(self, socket_path):
        # The socket file existing (checked in start_daemon) doesn't
        # strictly guarantee the server is done with bind()+listen()
        # by the time a client tries to connect -- under load (this
        # suite spins up a fresh daemon thread per test), an early
        # connect() attempt can still occasionally see
        # ConnectionRefusedError. Retry briefly rather than assume
        # the first attempt lands.
        deadline = time.monotonic() + 2
        last_error = None

        while time.monotonic() < deadline:
            try:
                client = _Client(socket_path)
                break
            except ConnectionRefusedError as exc:
                last_error = exc
                time.sleep(0.02)
        else:
            raise last_error

        self.addCleanup(client.close)

        return client


class PingStatusTests(DaemonTestCase):
    def test_ping(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("ping")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "pong")

    def test_status(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("status")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["connected"], True)
        self.assertEqual(response["result"]["port"], "COM_FAKE")
        self.assertEqual(response["result"]["monitor_subscribers"], 0)


class TypedCommandTests(DaemonTestCase):
    def test_get_typed_multiport_int(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "HBAUD": "HBAUD    0/1200",
        }))
        client = self.connect(socket_path)

        response = client.call("get_typed", command="HBAUD")

        self.assertTrue(response["ok"])
        # Tuples cross the wire as JSON arrays.
        self.assertEqual(response["result"], [0, 1200])

    def test_set_typed_multiport_bool_round_trip(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "MONITOR": "MONITOR  ON/OFF",
        }))
        client = self.connect(socket_path)

        # A JSON array in -> converted back to a tuple before it
        # reaches kamxl.py's isinstance(value, tuple) check.
        response = client.call(
            "set_typed",
            command="MONITOR",
            value=[True, False]
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], [True, False])

    def test_get_configuration(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "DISPLAY": "MYCALL   AI6K-10/AI6K-10\r\nHBAUD    0/1200",
        }))
        client = self.connect(socket_path)

        response = client.call("get_configuration")

        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"],
            {"MYCALL": "AI6K-10/AI6K-10", "HBAUD": "0/1200"}
        )


class SendCommandTests(DaemonTestCase):
    """
    The raw send_command passthrough added for the web terminal
    (milestone 4) -- unlike get/get_typed, it doesn't assume a
    "COMMAND value" response shape, so it works for commands kamxl.py
    has no typed metadata for at all.
    """

    def test_send_arbitrary_command_returns_raw_text(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "BEACON": "BEACON EVERY 0",
        }))
        client = self.connect(socket_path)

        response = client.call("send_command", command="BEACON")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "BEACON EVERY 0")

    def test_unknown_command_still_returns_whatever_came_back(self):
        # No typed metadata, no scripted response -- just the echo
        # and prompt, same as an unrecognized command would look on
        # real hardware.
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("send_command", command="NOTAREALCOMMAND")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "")


class ErrorHandlingTests(DaemonTestCase):
    def test_unknown_method(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("not_a_real_method")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")

    def test_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("get")  # missing "command"

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_readonly_command_error_reaches_client(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "VERSION": "KAM-XL VERSION 1.24160",
        }))
        client = self.connect(socket_path)

        response = client.call(
            "set_typed",
            command="VERSION",
            value="TEST"
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")
        self.assertIn("read-only", response["error"]["message"])


class ConnectStationTests(DaemonTestCase):
    def test_successful_connect(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["*** CONNECTED to KD5EOC-10\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call(
            "connect_station",
            callsign="KD5EOC-10",
            timeout=1
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "*** CONNECTED to KD5EOC-10\r\n")

    def test_busy_raises_connection_error(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["***(N0CALL) busy\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call(
            "connect_station",
            callsign="N0CALL",
            timeout=1
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMConnectionError")


class ConnectedModePassthroughTests(DaemonTestCase):
    def test_send_and_read_connected(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["hello back!\r\n"])
        )
        client = self.connect(socket_path)

        send_response = client.call("send_connected", text="hi there")
        self.assertTrue(send_response["ok"])
        self.assertIsNone(send_response["result"])

        read_response = client.call("read_connected", timeout=0.3)
        self.assertTrue(read_response["ok"])
        self.assertEqual(read_response["result"], "hello back!\r\n")

    def test_disconnect_station(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["cmd:", "cmd:*** DISCONNECTED\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call("disconnect_station", timeout=1)

        self.assertTrue(response["ok"])
        self.assertIn("DISCONNECTED", response["result"])


class MonitorSubscribeTests(DaemonTestCase):
    # A packet is only known to be "complete" once the *next* header
    # line arrives (see packet.py's PacketParser docstring), so this
    # includes a third header purely to complete the second packet --
    # otherwise it would just sit pending, waiting for the monitor
    # loop to stop and flush it (see test_unsubscribe_flushes_pending_packet
    # below for that path instead).
    CHUNKS = [
        "KD5EOC-10>BEACON/2:\r\n",
        "Winlink 2000 RMS Packet Server\r\n",
        "AI6K-4>BEACON/2:\r\n",
        "AI6K-4 Linux Node http://digipi.org/\r\n",
        "N0CALL>BEACON/2:\r\n",
    ]

    def test_subscribe_receives_packet_events(self):
        _, socket_path = self.start_daemon(ChunkSerial(self.CHUNKS))
        client = self.connect(socket_path)

        subscribe_response = client.call("monitor.subscribe")
        self.assertTrue(subscribe_response["ok"])

        first = client.read_event(timeout=3)
        self.assertEqual(first["event"], "packet")
        self.assertEqual(first["data"]["source"], "KD5EOC-10")
        self.assertEqual(first["data"]["destination"], "BEACON")
        self.assertEqual(first["data"]["port"], 2)

        second = client.read_event(timeout=3)
        self.assertEqual(second["data"]["source"], "AI6K-4")
        self.assertEqual(
            second["data"]["payload"],
            "AI6K-4 Linux Node http://digipi.org/"
        )

    def test_status_reflects_subscriber_count(self):
        _, socket_path = self.start_daemon(ChunkSerial([]))
        client = self.connect(socket_path)

        before = client.call("status")
        self.assertEqual(before["result"]["monitor_subscribers"], 0)

        client.call("monitor.subscribe")

        after = client.call("status")
        self.assertEqual(after["result"]["monitor_subscribers"], 1)

        client.call("monitor.unsubscribe")

        final = client.call("status")
        self.assertEqual(final["result"]["monitor_subscribers"], 0)

    def test_disconnect_stops_monitor_thread(self):
        # A background monitor thread that outlives its last
        # subscriber would be a real resource leak -- confirm it
        # actually stops (and flushes/discards cleanly, rather than
        # raising) once the only client goes away.
        daemon, socket_path = self.start_daemon(ChunkSerial([
            "N0CALL>BEACON/2:\r\n",
            "final packet, never followed by another header\r\n",
        ]))
        client = self.connect(socket_path)

        client.call("monitor.subscribe")
        time.sleep(0.3)  # let the monitor loop pick up the chunks

        client.close()

        deadline = time.monotonic() + 2
        while (
            daemon._monitor_thread is not None
            and daemon._monitor_thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        self.assertIsNotNone(daemon._monitor_thread)
        self.assertFalse(daemon._monitor_thread.is_alive())

        with daemon._subscribers_lock:
            self.assertEqual(len(daemon._subscribers), 0)


if __name__ == "__main__":
    unittest.main()
