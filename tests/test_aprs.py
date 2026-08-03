import unittest

from fakes import make_kam  # noqa: F401 (ensures kamxl/ is on sys.path)

from aprs import parse_position


class ParsePositionUncompressedTests(unittest.TestCase):
    """
    APRS position-report decoding. Fixtures are the APRS Protocol
    Reference spec's own canonical example
    ("!4903.50N/07201.75W-Test") rather than real captured traffic --
    see aprs.py's module docstring for why this is treated as
    unverified against real hardware until checked against an actual
    live APRS capture, the same caution applied to pbbs.py.
    """

    def test_no_timestamp(self):
        position = parse_position("!4903.50N/07201.75W-Test comment")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.latitude, 49 + 3.50 / 60, places=6)
        self.assertAlmostEqual(
            position.longitude, -(72 + 1.75 / 60), places=6
        )
        self.assertEqual(position.symbol_table, "/")
        self.assertEqual(position.symbol_code, "-")
        self.assertEqual(position.comment, "Test comment")
        self.assertIsNone(position.timestamp)
        self.assertEqual(position.raw, "!4903.50N/07201.75W-Test comment")

    def test_equals_data_type_same_as_bang(self):
        # '=' is '!' with the APRS messaging-capable flag set -- same
        # position shape, no bearing on parsing.
        position = parse_position("=4903.50N/07201.75W-")

        self.assertIsNotNone(position)
        self.assertEqual(position.comment, "")

    def test_with_timestamp(self):
        position = parse_position(
            "/092345z4903.50N/07201.75W-Test comment"
        )

        self.assertIsNotNone(position)
        self.assertEqual(position.timestamp, "092345z")
        self.assertAlmostEqual(position.latitude, 49 + 3.50 / 60, places=6)

    def test_at_data_type_same_as_slash(self):
        position = parse_position("@092345h4903.50N/07201.75W-")

        self.assertIsNotNone(position)
        self.assertEqual(position.timestamp, "092345h")

    def test_southern_and_western_hemisphere_negative(self):
        position = parse_position("!4903.50S/07201.75W-")

        self.assertIsNotNone(position)
        self.assertLess(position.latitude, 0)
        self.assertLess(position.longitude, 0)

    def test_northern_and_eastern_hemisphere_positive(self):
        position = parse_position("!4903.50N/07201.75E-")

        self.assertIsNotNone(position)
        self.assertGreater(position.latitude, 0)
        self.assertGreater(position.longitude, 0)

    def test_alternate_symbol_table(self):
        # Backslash table, different symbol code -- shouldn't affect
        # position decoding at all, just carried through verbatim.
        position = parse_position(r"!4903.50N\07201.75W>")

        self.assertIsNotNone(position)
        self.assertEqual(position.symbol_table, "\\")
        self.assertEqual(position.symbol_code, ">")

    def test_position_ambiguity_treated_as_zero(self):
        # Trailing minute digits replaced with spaces -- see module
        # docstring's "POSITION AMBIGUITY NOT FULLY MODELED" note.
        position = parse_position("!49  .  N/072  .  W-")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.latitude, 49.0, places=6)
        self.assertAlmostEqual(position.longitude, -72.0, places=6)


class ParsePositionNonPositionTests(unittest.TestCase):
    def test_empty_payload(self):
        self.assertIsNone(parse_position(""))

    def test_status_packet_not_a_position(self):
        self.assertIsNone(parse_position(">Status text here"))

    def test_message_packet_not_a_position(self):
        self.assertIsNone(
            parse_position(":N0CALL   :Hello there{001")
        )

    def test_object_packet_not_a_position(self):
        self.assertIsNone(
            parse_position(";LEADER   *111111z4903.50N/07201.75W-")
        )

    def test_plain_text_not_a_position(self):
        self.assertIsNone(parse_position("Just some plain text"))

    def test_compressed_position_not_supported_yet(self):
        # Compressed form: symbol table char immediately follows the
        # data type identifier, then base-91 chars rather than
        # digits -- deliberately unsupported for now (see module
        # docstring), should degrade to None, not raise or misparse.
        self.assertIsNone(parse_position("!/5L!!<*e7>7P["))


if __name__ == "__main__":
    unittest.main()
