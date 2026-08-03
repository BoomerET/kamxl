"""
Offline tests for winlink.py's parsing/building helpers and
KAMXL.check_winlink_mail()'s composition of the connected-mode
primitives.

IMPORTANT: unlike most of this project, winlink.py was NOT built from
observed real-hardware behavior -- there's no KAM-XL manual involved
at all here. It's built from the public B2F/FBB protocol specs and,
for the one genuinely security-sensitive piece (the secure-login
response algorithm), a real open-source reference implementation
(wl2k-go) -- see winlink.py's module docstring for the sources and
caveats. SECURE_LOGIN_TEST_VECTORS below are wl2k-go's own published
test vectors, so that piece is confirmed correct independent of
real-hardware testing. Everything else here is a best-effort first
draft against the spec, unverified against a real RMS gateway --
expect adjustment the same way pbbs.py's parsing needed it.
"""

import unittest

from fakes import CannedSerial, make_kam  # noqa: F401 (path + fixture reuse)

from kamxl import KAMTimeoutError
import winlink as w


class SecureLoginResponseTests(unittest.TestCase):
    def test_matches_reference_implementation_vectors(self):
        for challenge, password, expect in w.SECURE_LOGIN_TEST_VECTORS:
            got = w.secure_login_response(challenge, password)
            self.assertEqual(got, expect)

    def test_is_case_sensitive_like_the_reference(self):
        # Not a design choice of ours -- matches wl2k-go's own
        # behavior exactly (see the two test vectors above, which
        # differ only in password case and produce different
        # responses). Documented here explicitly since it's a real
        # gotcha: a stored password with the wrong case will silently
        # fail login rather than erroring obviously.
        upper = w.secure_login_response("23753528", "FOOBAR")
        mixed = w.secure_login_response("23753528", "FooBar")

        self.assertNotEqual(upper, mixed)

    def test_response_is_always_eight_digits(self):
        response = w.secure_login_response("00000001", "X")

        self.assertEqual(len(response), 8)
        self.assertTrue(response.isdigit())


class ParseSecureChallengeTests(unittest.TestCase):
    def test_parses_challenge_line(self):
        self.assertEqual(w.parse_secure_challenge(";PQ: 425"), "425")

    def test_case_insensitive_prefix(self):
        self.assertEqual(w.parse_secure_challenge(";pq: 425"), "425")

    def test_non_challenge_line_returns_none(self):
        self.assertIsNone(w.parse_secure_challenge("Welcome to WL2K"))
        self.assertIsNone(w.parse_secure_challenge(";FW: AI6K-10"))


class SIDTests(unittest.TestCase):
    def test_parses_real_looking_sid(self):
        sid = w.parse_sid("[WL2K-5.0-B2FIHM$]")

        self.assertIsNotNone(sid)
        self.assertEqual(sid.app_name, "WL2K")
        self.assertEqual(sid.app_version, "5.0")
        self.assertEqual(sid.codes, "B2FIHM$")

    def test_sid_has_code(self):
        sid = w.parse_sid("[WL2K-5.0-B2FIHM$]")

        self.assertTrue(w.sid_has_code(sid, "F"))
        self.assertTrue(w.sid_has_code(sid, "B2"))
        self.assertFalse(w.sid_has_code(sid, "X"))

    def test_non_sid_line_returns_none(self):
        self.assertIsNone(w.parse_sid("Welcome to WL2K"))
        self.assertIsNone(w.parse_sid("FB P F6FBB FC1GHV FC1MVP 1_A 10"))

    def test_build_sid_only_claims_ascii_basic_and_bid(self):
        # Deliberately NOT claiming B/B1/B2 (compressed protocol) --
        # see winlink.py's module docstring for why. "$" must be last.
        sid = w.build_sid("kamxl", "0.1")

        self.assertEqual(sid, "[kamxl-0.1-F$]")


class BuildHandshakeResponseTests(unittest.TestCase):
    def test_without_challenge_no_pr_line(self):
        response = w.build_handshake_response("AI6K-10")

        self.assertEqual(response, ";FW: AI6K-10\r[kamxl-0.1-F$]")

    def test_with_challenge_includes_pr_line(self):
        response = w.build_handshake_response(
            "AI6K-10", secure_challenge="23753528", password="FOOBAR"
        )

        self.assertEqual(
            response,
            ";FW: AI6K-10\r[kamxl-0.1-F$]\r;PR: 72768415"
        )

    def test_challenge_without_password_raises(self):
        with self.assertRaises(ValueError):
            w.build_handshake_response("AI6K-10", secure_challenge="425")


class ProposalTests(unittest.TestCase):
    """
    Fixtures verbatim from f6fbb.org's ascii-basic protocol example --
    see winlink.py's module docstring for the source.
    """

    def test_parses_private_message_proposal(self):
        proposal = w.parse_proposal(
            "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345"
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.msg_type, "P")
        self.assertEqual(proposal.sender, "F6FBB")
        self.assertEqual(proposal.via, "FC1GHV")
        self.assertEqual(proposal.recipient, "FC1MVP")
        self.assertEqual(proposal.mid, "24657_F6FBB")
        self.assertEqual(proposal.size, 1345)

    def test_parses_bulletin_proposal(self):
        proposal = w.parse_proposal("FB B F6FBB FRA FBB 22_456_F6FBB 8548")

        self.assertEqual(proposal.msg_type, "B")

    def test_non_proposal_line_returns_none(self):
        self.assertIsNone(w.parse_proposal("F>"))
        self.assertIsNone(w.parse_proposal("FQ"))
        self.assertIsNone(w.parse_proposal(""))

    def test_parse_proposals_skips_non_matching_lines(self):
        text = (
            "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345\r\n"
            "FB B F6FBB FRA FBB 22_456_F6FBB 8548\r\n"
            "F>\r\n"
        )

        proposals = w.parse_proposals(text)

        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[1].mid, "22_456_F6FBB")

    def test_build_fs_line_accepts_all(self):
        self.assertEqual(w.build_fs_line(3), "FS +++")

    def test_build_fs_line_rejects_all(self):
        self.assertEqual(w.build_fs_line(2, accept=False), "FS --")

    def test_has_end_of_block_marker(self):
        self.assertTrue(w.has_end_of_block_marker("FB P a b c d 5\r\nF>\r\n"))
        self.assertFalse(w.has_end_of_block_marker("FB P a b c d 5\r\n"))

    def test_has_end_of_block_marker_does_not_match_bare_ff(self):
        # Real bug, found live against a real gateway (KD5EOC-10):
        # KAMXL.check_winlink_mail() always sends its own "FF" right
        # after login (receive-only MVP, never proposes an outbound
        # message) -- and the KAM-XL echoes connected-mode
        # transmissions back to us (same behavior already known for
        # PBBS). A bare "FF" is deliberately NOT treated as an
        # end-of-block marker here, so our own echoed "FF" can never
        # be mistaken for the gateway's reply -- see
        # has_end_of_block_marker()'s docstring for the full story.
        self.assertFalse(w.has_end_of_block_marker("FF\r\n"))

    def test_has_fq_marker(self):
        self.assertTrue(w.has_fq_marker("FQ\r\n"))
        self.assertFalse(w.has_fq_marker("FF\r\n"))


class MessageBlockTests(unittest.TestCase):
    def test_splits_multiple_ctrl_z_terminated_blocks(self):
        text = "Title one\r\nBody one\r\n\x1aTitle two\r\nBody two\x1a"

        blocks = w.split_message_blocks(text, 2)

        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0].startswith("Title one"))
        self.assertTrue(blocks[1].startswith("Title two"))

    def test_trailing_remainder_after_last_ctrl_z_is_dropped(self):
        # Whatever follows the final ^Z (next proposal batch, FQ,
        # etc.) isn't a message body.
        text = "Title\r\nBody\x1aFQ\r\n"

        blocks = w.split_message_blocks(text, 1)

        self.assertEqual(blocks, ["Title\r\nBody"])

    def test_returns_fewer_than_count_if_not_all_arrived_yet(self):
        text = "Title one\r\nBody one\x1a"

        blocks = w.split_message_blocks(text, 2)

        self.assertEqual(len(blocks), 1)

    def test_parse_message_block_splits_title_and_body(self):
        proposal = w.parse_proposal(
            "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345"
        )

        message = w.parse_message_block(
            "Test Subject\r\nLine one\r\nLine two\r\n", proposal
        )

        self.assertEqual(message.title, "Test Subject")
        self.assertEqual(message.body, "Line one\nLine two")
        self.assertEqual(message.proposal, proposal)


class KAMXLWinlinkIntegrationTests(unittest.TestCase):
    """
    Exercises check_winlink_mail()'s composition of connect_station()/
    send_connected()/read_connected()/disconnect_station() -- call
    order, argument flow, and that disconnect_station() always runs
    even if a step in between raises. Same reasoning and pattern as
    test_pbbs.py's KAMXLPbbsIntegrationTests: connected-mode primitives
    are stubbed directly rather than driven through a full CannedSerial
    round trip, since chaining a multi-stage exchange (handshake,
    proposals, messages) through one shared canned-chunk queue doesn't
    compose cleanly with read_connected()'s "collect for N seconds"
    semantics.
    """

    def _kam_with_stubs(self, read_connected_chunks, mycall_lookup="AI6K-10/AI6K-10"):
        kam = make_kam(CannedSerial([]))
        calls = []
        remaining = list(read_connected_chunks)

        kam.get = lambda command: calls.append(("get", command)) or mycall_lookup
        kam.connect_station = lambda callsign, **kwargs: calls.append(
            ("connect_station", callsign)
        )
        kam.send_connected = lambda text, **kwargs: calls.append(
            ("send_connected", text)
        )
        kam.read_connected = lambda **kwargs: (
            calls.append(("read_connected",))
            or (remaining.pop(0) if remaining else "")
        )
        kam.disconnect_station = lambda **kwargs: calls.append(
            ("disconnect_station",)
        )

        return kam, calls

    def test_no_mail_waiting_returns_empty_list(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\n;PQ: 425\r\n>\r\n",
            "FQ\r\n",
        ])

        messages = kam.check_winlink_mail("AI6K-10", "FOOBAR")

        self.assertEqual(messages, [])
        # get MYCALL, connect, read (handshake), send (login+FF),
        # read (proposals -> FQ), disconnect.
        self.assertEqual(
            [call[0] for call in calls],
            [
                "get", "connect_station", "read_connected",
                "send_connected", "read_connected", "disconnect_station",
            ]
        )
        self.assertEqual(calls[0], ("get", "MYCALL"))
        self.assertEqual(calls[1], ("connect_station", "AI6K-10"))

        # Login response computed from the real challenge + password.
        sent = calls[3]
        self.assertEqual(sent[0], "send_connected")
        self.assertIn(";PR: " + w.secure_login_response("425", "FOOBAR"), sent[1])
        self.assertIn("FF", sent[1])

    def test_no_login_challenge_skips_pr_line(self):
        # Some gateways (e.g. a private/local RMS Packet setup) might
        # not require secure login at all.
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\nWelcome\r\n>\r\n",
            "FQ\r\n",
        ])

        kam.check_winlink_mail("AI6K-10", "FOOBAR")

        sent = calls[3]
        self.assertEqual(sent[0], "send_connected")
        self.assertNotIn(";PR:", sent[1])

    def test_own_echoed_transmission_not_mistaken_for_gateways_reply(self):
        """
        Regression test for a real bug found live against a real
        gateway (KD5EOC-10, Denton County Texas EOC). Verbatim shape
        of what was actually observed via daemon -v logging:

            08:29:22 conn-6720: connected
            08:29:33 winlink handshake raw: 'Welcome to the Denton
                County Texas EOC\\r\\n[WL2K-5.0-B2FWIHJM$]\\r\\n
                ;PQ: 20914129\\r\\nCMS via KD5EOC >\\r\\n'
            08:29:34 winlink proposals raw: ';FW: AI6K\\r\\n
                [kamxl-0.1-F$]\\r\\n;PR: 14482272\\r\\nFF\\r\\n'
            08:29:37 conn-6720: winlink.check_mail -> ok

            (webapp: "No mail waiting.")

            That "proposals raw" text is EXACTLY what we ourselves
            sent (;FW:/SID/;PR:/FF) -- the KAM-XL echoes connected-
            mode transmissions back to us, same as PBBS's already-
            known echo of its own "L" command. The old
            has_end_of_block_marker() treated a bare "FF" as "the
            gateway has nothing", so it stopped and returned "no
            mail" on seeing our OWN echo -- before the real gateway
            had said anything at all. It happened to still be the
            right answer that day (there really was no mail), but
            would have silently under-reported real waiting mail.

        This test feeds exactly that echoed chunk first, followed by
        the gateway's real (delayed) reply in a later read_connected()
        call, and confirms the real reply is what gets used --
        proving the fix, not just changing behavior.
        """
        kam, calls = self._kam_with_stubs([
            "Welcome to the Denton County Texas EOC\r\n"
            "[WL2K-5.0-B2FWIHJM$]\r\n;PQ: 20914129\r\nCMS via KD5EOC >\r\n",
            # Our own echoed transmission -- NOT the gateway's reply.
            ";FW: AI6K\r\n[kamxl-0.1-F$]\r\n;PR: 14482272\r\nFF\r\n",
            # The gateway's real, delayed reply.
            "FB P N0CALL AI6K AI6K 12345_N0CALL 42\r\nF>\r\n",
            "Test Subject\r\nHello there.\r\n\x1a",
        ])

        messages = kam.check_winlink_mail(
            "KD5EOC-10", "REALPASSWORD", mycall="AI6K"
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].title, "Test Subject")
        self.assertEqual(messages[0].proposal.sender, "N0CALL")

    def test_pending_mail_downloaded_and_parsed(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\n;PQ: 425\r\n>\r\n",
            "FB P N0CALL AI6K-10 AI6K-10 12345_N0CALL 42\r\nF>\r\n",
            "Test Subject\r\nHello there.\r\n\x1a",
        ])

        messages = kam.check_winlink_mail("AI6K-10", "FOOBAR")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].title, "Test Subject")
        self.assertEqual(messages[0].body, "Hello there.")
        self.assertEqual(messages[0].proposal.sender, "N0CALL")

        # get, connect, read (handshake), send (login+FF),
        # read (proposals), send (FS), read (messages), disconnect.
        self.assertEqual(
            [call[0] for call in calls],
            [
                "get", "connect_station", "read_connected",
                "send_connected", "read_connected", "send_connected",
                "read_connected", "disconnect_station",
            ]
        )
        fs_call = calls[5]
        self.assertEqual(fs_call, ("send_connected", "FS +"))

    def test_explicit_mycall_skips_mycall_lookup(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\n>\r\n",
            "FQ\r\n",
        ])

        kam.check_winlink_mail("AI6K-10", "FOOBAR", mycall="AI6K-2")

        self.assertNotIn("get", [call[0] for call in calls])

        # calls: connect, read (handshake), send (login+FF), ...
        sent = calls[2]
        self.assertEqual(sent[0], "send_connected")
        self.assertIn("AI6K-2", sent[1])

    def test_disconnect_always_runs_even_if_read_fails(self):
        kam, calls = self._kam_with_stubs([])

        def failing_read(**kwargs):
            calls.append(("read_connected",))
            raise KAMTimeoutError("boom")

        kam.read_connected = failing_read

        with self.assertRaises(KAMTimeoutError):
            kam.check_winlink_mail("AI6K-10", "FOOBAR")

        self.assertIn(("disconnect_station",), calls)


if __name__ == "__main__":
    unittest.main()
