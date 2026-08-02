"""
Offline tests for pbbs.py's parsing and KAMXL's list_pbbs_messages()/
read_pbbs_message() methods.

IMPORTANT: unlike most fixtures in this suite, these are NOT taken
from a captured real-hardware session (see pbbs.py's module
docstring) -- they're built from the manual's documented output
format. This is a best-effort first draft; expect it to need
adjustment once actually tested against a real KAM-XL PBBS session,
the same way packet.py's HEADER_RE did.
"""

import unittest

from fakes import CannedSerial, make_kam

from kamxl import KAMTimeoutError
from pbbs import parse_message, parse_message_list


# Verbatim from the manual's PBBS message-list example.
LIST_TEXT = (
    "MSG# ST SIZE TO      FROM   DATE                SUBJECT\r\n"
    "6    B  45   KEPS    W3IWI  10/19/01 09:37:11 2  Line Element set\r\n"
    "4    B  26   HELP    WB5BBW 10/19/01 09:34:05    Xerox 820\r\n"
    "102120 BYTES AVAILABLE\r\n"
    "NEXT MESSAGE NUMBER 7\r\n"
    "ENTER COMMAND: B,J,K,L,R,S, or Help >"
)

# Verbatim from the manual's PBBS message-read example (header line),
# with a synthetic body -- the manual doesn't show a full body
# example.
READ_TEXT = (
    "MSG#2 02/10/92 10:30:58 FROM KB0NYK TO HELP @WA4EWV.#STX.TX.USA.NOAM\r\n"
    "This is the message body.\r\n"
    "Second line of body.\r\n"
    "ENTER COMMAND: B,J,K,L,R,S, or Help >"
)


class ParseMessageListTests(unittest.TestCase):
    def test_parses_both_rows(self):
        messages = parse_message_list(LIST_TEXT)

        self.assertEqual(len(messages), 2)

    def test_row_with_page_count(self):
        messages = parse_message_list(LIST_TEXT)
        first = messages[0]

        self.assertEqual(first.number, 6)
        self.assertEqual(first.msg_type, "B")
        self.assertIsNone(first.status)
        self.assertEqual(first.size, 45)
        self.assertEqual(first.to, "KEPS")
        self.assertEqual(first.from_call, "W3IWI")
        self.assertEqual(first.date, "10/19/01 09:37:11")
        self.assertEqual(first.pages, 2)
        self.assertEqual(first.subject, "Line Element set")

    def test_row_without_page_count(self):
        messages = parse_message_list(LIST_TEXT)
        second = messages[1]

        self.assertEqual(second.number, 4)
        self.assertIsNone(second.pages)
        self.assertEqual(second.subject, "Xerox 820")

    def test_header_and_footer_lines_skipped_not_fabricated(self):
        # "MSG# ST SIZE..." (column header), "NNN BYTES AVAILABLE",
        # "NEXT MESSAGE NUMBER N", and the command prompt should never
        # turn into a bogus message row.
        messages = parse_message_list(LIST_TEXT)

        numbers = {m.number for m in messages}
        self.assertEqual(numbers, {6, 4})

    def test_empty_mailbox(self):
        text = (
            "0 BYTES AVAILABLE\r\n"
            "NEXT MESSAGE NUMBER 1\r\n"
            "ENTER COMMAND: B,J,K,L,R,S, or Help >"
        )

        self.assertEqual(parse_message_list(text), [])


class ParseMessageTests(unittest.TestCase):
    def test_parses_header_and_body(self):
        message = parse_message(READ_TEXT)

        self.assertIsNotNone(message)
        self.assertEqual(message.number, 2)
        self.assertEqual(message.date, "02/10/92 10:30:58")
        self.assertEqual(message.from_call, "KB0NYK")
        self.assertEqual(message.to, "HELP")
        self.assertEqual(message.routing, "@WA4EWV.#STX.TX.USA.NOAM")
        self.assertEqual(
            message.body,
            "This is the message body.\nSecond line of body."
        )

    def test_no_routing_address(self):
        text = (
            "MSG#5 01/01/24 12:00:00 FROM N0CALL TO N0CALL\r\n"
            "Just a local message.\r\n"
            "ENTER COMMAND: B,J,K,L,R,S, or Help >"
        )

        message = parse_message(text)

        self.assertIsNotNone(message)
        self.assertIsNone(message.routing)
        self.assertEqual(message.body, "Just a local message.")

    def test_unrecognized_response_returns_none(self):
        # e.g. the KAM-XL rejected the message number.
        message = parse_message(
            "Message not found.\r\nENTER COMMAND: B,J,K,L,R,S, or Help >"
        )

        self.assertIsNone(message)


class KAMXLPbbsIntegrationTests(unittest.TestCase):
    """
    Exercises list_pbbs_messages()/read_pbbs_message()'s composition
    of connect_station()/send_connected()/read_connected()/
    disconnect_station() -- call order, argument flow, and that
    disconnect_station() always runs even if a step in between
    raises. Each of those four primitives is already independently
    covered elsewhere in this suite (test_connect.py,
    test_typed_commands.py), so they're stubbed here directly rather
    than driven through a full CannedSerial round trip -- chaining
    connect + L/R + read + disconnect through one shared canned-chunk
    queue doesn't work cleanly, since read_connected()'s "collect for
    N seconds" semantics gobbles up every remaining queued chunk in a
    single call regardless of which logical step they were meant for.
    """

    def _kam_with_stubs(self, read_connected_result, mypbbs_lookup="AI6K-1"):
        kam = make_kam(CannedSerial([]))
        calls = []

        kam.get = lambda command: calls.append(("get", command)) or mypbbs_lookup
        kam.connect_station = lambda callsign, **kwargs: calls.append(
            ("connect_station", callsign)
        )
        kam.send_connected = lambda text, **kwargs: calls.append(
            ("send_connected", text)
        )
        kam.read_connected = lambda **kwargs: (
            calls.append(("read_connected",)) or read_connected_result
        )
        kam.disconnect_station = lambda **kwargs: calls.append(
            ("disconnect_station",)
        )

        return kam, calls

    def test_list_pbbs_messages_calls_in_order(self):
        kam, calls = self._kam_with_stubs(LIST_TEXT)

        messages = kam.list_pbbs_messages(mypbbs="AI6K-1")

        self.assertEqual(
            [call[0] for call in calls],
            [
                "connect_station",
                "send_connected",
                "read_connected",
                "disconnect_station",
            ]
        )
        self.assertEqual(calls[0], ("connect_station", "AI6K-1"))
        self.assertEqual(calls[1], ("send_connected", "L"))
        self.assertEqual(len(messages), 2)

    def test_read_pbbs_message_sends_correct_command(self):
        kam, calls = self._kam_with_stubs(READ_TEXT)

        message = kam.read_pbbs_message(2, mypbbs="AI6K-1")

        self.assertEqual(calls[1], ("send_connected", "R 2"))
        self.assertIsNotNone(message)
        self.assertEqual(message.from_call, "KB0NYK")

    def test_mypbbs_defaults_to_configured_value(self):
        # No explicit mypbbs= -- should read MYPBBS off the KAM-XL
        # first, then connect to whatever that says, rather than
        # requiring every caller to already know/pass it.
        kam, calls = self._kam_with_stubs(LIST_TEXT, mypbbs_lookup="AI6K-1")

        kam.list_pbbs_messages()

        self.assertEqual(calls[0], ("get", "MYPBBS"))
        self.assertEqual(calls[1], ("connect_station", "AI6K-1"))

    def test_explicit_mypbbs_skips_lookup(self):
        kam, calls = self._kam_with_stubs(LIST_TEXT)

        kam.list_pbbs_messages(mypbbs="AI6K-2")

        self.assertNotIn("get", [call[0] for call in calls])
        self.assertEqual(calls[0], ("connect_station", "AI6K-2"))

    def test_disconnect_always_runs_even_if_read_fails(self):
        kam, calls = self._kam_with_stubs(LIST_TEXT)

        def failing_read(**kwargs):
            calls.append(("read_connected",))
            raise KAMTimeoutError("boom")

        kam.read_connected = failing_read

        with self.assertRaises(KAMTimeoutError):
            kam.list_pbbs_messages(mypbbs="AI6K-1")

        self.assertIn(("disconnect_station",), calls)


if __name__ == "__main__":
    unittest.main()
