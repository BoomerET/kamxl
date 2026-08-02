import time
import unittest

from fakes import CannedSerial, SilentSerial, make_kam

from kamxl import (
    KAMConnectionError,
    KAMTimeoutError,
)


class ReadUntilAnyLineEndTests(unittest.TestCase):
    """
    Direct tests of the require_line_end fix: a marker regex can
    match on the *start* of a line (e.g. "*** connected to") before
    the rest of that line -- like a VIA digipeat path -- has actually
    arrived. Observed on real hardware with RSSTN: the banner came
    back as "*** CONNECTED to KD5EOC-10 VIA RS" with "STN\\r\\n"
    trickling in a moment later as if it were ordinary traffic.
    """

    def test_without_require_line_end_returns_truncated(self):
        kam = make_kam(
            CannedSerial([
                "*** CONNECTED to KD5EOC-10 VIA RS",
                "STN\r\n",
            ])
        )

        text, marker = kam._read_until_any(
            kam.CONNECT_MARKERS,
            timeout=1
        )

        self.assertEqual(marker, "connected")
        self.assertEqual(
            text,
            "*** CONNECTED to KD5EOC-10 VIA RS"
        )

    def test_with_require_line_end_waits_for_full_line(self):
        kam = make_kam(
            CannedSerial([
                "*** CONNECTED to KD5EOC-10 VIA RS",
                "STN\r\n",
            ])
        )

        text, marker = kam._read_until_any(
            kam.CONNECT_MARKERS,
            timeout=1,
            require_line_end=True,
            line_end_grace=0.3
        )

        self.assertEqual(marker, "connected")
        self.assertEqual(
            text,
            "*** CONNECTED to KD5EOC-10 VIA RSSTN\r\n"
        )

    def test_require_line_end_gives_up_after_grace_period(self):
        # If the rest of the line never actually arrives, this
        # should still return the match (not hang forever).
        kam = make_kam(
            CannedSerial([
                "*** CONNECTED to KD5EOC-10 VIA RS",
            ])
        )

        text, marker = kam._read_until_any(
            kam.CONNECT_MARKERS,
            timeout=1,
            require_line_end=True,
            line_end_grace=0.1
        )

        self.assertEqual(marker, "connected")
        self.assertEqual(
            text,
            "*** CONNECTED to KD5EOC-10 VIA RS"
        )


class ConnectStationTests(unittest.TestCase):
    def test_direct_connect_strips_stray_leading_prompt(self):
        # Regression test for the real bug: a stale "cmd:" prompt,
        # still in flight when the buffer was cleared right before
        # CONNECT was sent, ended up glued to the front of the
        # returned banner.
        kam = make_kam(
            CannedSerial(["cmd:*** CONNECTED to KD5EOC-10\r\n"])
        )

        banner = kam.connect_station("KD5EOC-10")

        self.assertEqual(
            banner,
            "*** CONNECTED to KD5EOC-10\r\n"
        )

    def test_via_digipeat_connect_is_not_truncated(self):
        kam = make_kam(
            CannedSerial([
                "*** CONNECTED to KD5EOC-10 VIA RS",
                "STN\r\n",
            ])
        )

        banner = kam.connect_station(
            "KD5EOC-10",
            via="RSSTN"
        )

        self.assertEqual(
            banner,
            "*** CONNECTED to KD5EOC-10 VIA RSSTN\r\n"
        )
        # Confirm the actual command sent included the VIA clause.
        self.assertIn(
            b"CONNECT KD5EOC-10 VIA RSSTN\r",
            kam.serial.written
        )

    def test_via_accepts_a_list_of_digipeaters(self):
        kam = make_kam(
            CannedSerial(["*** CONNECTED to TARGET\r\n"])
        )

        kam.connect_station(
            "TARGET",
            via=["DIGI1", "DIGI2"]
        )

        self.assertIn(
            b"CONNECT TARGET VIA DIGI1,DIGI2\r",
            kam.serial.written
        )

    def test_retry_count_exceeded_raises_connection_error(self):
        kam = make_kam(
            CannedSerial([
                "*** retry count exceeded\r\n*** DISCONNECTED\r\n",
            ])
        )

        with self.assertRaises(KAMConnectionError) as ctx:
            kam.connect_station("N0CALL-15")

        self.assertIn("retry count exceeded", str(ctx.exception))

    def test_busy_raises_connection_error(self):
        kam = make_kam(
            CannedSerial(["***(N0CALL) busy\r\n"])
        )

        with self.assertRaises(KAMConnectionError):
            kam.connect_station("N0CALL")

    def test_no_response_raises_timeout_error(self):
        kam = make_kam(CannedSerial([]))

        with self.assertRaises(KAMTimeoutError):
            kam.connect_station("N0CALL", timeout=0.1)


class DisconnectStationTests(unittest.TestCase):
    def test_disconnect_strips_stray_leading_prompt(self):
        # Same regression as the connect-side fix, but for
        # disconnect_station(): enter_command_mode() consumes the
        # first "cmd:" itself, then this queues a second one glued to
        # the front of the DISCONNECTED confirmation, exactly as
        # observed on real hardware.
        kam = make_kam(
            CannedSerial([
                "cmd:",
                "cmd:*** DISCONNECTED\r\n",
            ])
        )

        confirmation = kam.disconnect_station()

        self.assertEqual(
            confirmation,
            "*** DISCONNECTED\r\n"
        )

    def test_no_response_raises_timeout_error(self):
        kam = make_kam(CannedSerial(["cmd:"]))

        with self.assertRaises(KAMTimeoutError):
            kam.disconnect_station(timeout=0.1)

    def test_command_mode_timeout_is_honored_independently(self):
        # Regression test: enter_command_mode() used to always run with
        # its own hardcoded 5s default inside disconnect_station(),
        # completely ignoring whatever the caller passed -- so a
        # caller-supplied command_mode_timeout has to actually bound
        # *this* step, not the overall call, and a large `timeout`
        # must not paper over a short command_mode_timeout.
        kam = make_kam(SilentSerial())

        start = time.monotonic()

        with self.assertRaises(KAMTimeoutError) as ctx:
            kam.disconnect_station(timeout=10, command_mode_timeout=0.1)

        elapsed = time.monotonic() - start

        self.assertIn("Command mode", str(ctx.exception))
        self.assertLess(
            elapsed, 2,
            "disconnect_station() waited on the outer `timeout` "
            "instead of the shorter command_mode_timeout"
        )


if __name__ == "__main__":
    unittest.main()
