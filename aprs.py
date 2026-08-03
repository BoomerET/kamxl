"""
Parsing helpers for APRS position reports carried in AX.25 UI-frame
payloads (packet.py's Packet.payload).

APRS (Automatic Packet Reporting System) is an open, widely-documented
protocol layered on top of plain AX.25 UI frames -- unlike pbbs.py,
this isn't reverse-engineered from the KAM-XL manual, it's built from
the public APRS Protocol Reference spec. Still, per this project's
usual caution, treat it as unverified until checked against a real
captured live session (milestone 5's HEADER_RE bug is the reminder of
why that matters even for well-documented formats).

MVP SCOPE: position reports only. An APRS payload's first character
(the "data type identifier") says what kind of packet it is --
position, status, message, object, weather, telemetry, and more. Only
the position report identifiers ('!', '=', '/', '@') are handled here;
everything else returns None from parse_position(), same as a line
pbbs.py's parser doesn't recognize -- a deliberate "skip, don't guess"
choice.

COMPRESSED POSITIONS NOT SUPPORTED YET: APRS has two position
encodings -- human-readable "uncompressed" (degrees-minutes text, e.g.
"4903.50N") and a denser base-91 "compressed" form. Only uncompressed
is implemented here. A compressed position (recognizable by its symbol
table character appearing immediately after the data type identifier,
followed by 4 base-91 characters rather than digits) is intentionally
left unparsed for now -- parse_position() returns None for it rather
than guessing. Real-world impact: some trackers/apps default to
compressed, so not every position-report packet on the air will
decode yet.

POSITION AMBIGUITY NOT FULLY MODELED: APRS allows trailing digits of
the minutes fields to be replaced with spaces to indicate reduced
precision (e.g. a station only willing to report to the nearest
degree). This parser treats an ambiguous digit as '0' for the purpose
of computing a decimal coordinate, which is the conventional
"most likely" interpretation, but doesn't track or expose the
ambiguity level itself -- a caller has no way to tell "exact" from
"rounded to a full degree" apart from the returned latitude/longitude
alone. Documented here as a known simplification rather than silently
getting it wrong.
"""

import re

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AprsPosition:
    """
    A decoded APRS position report.

    ``symbol_table`` and ``symbol_code`` together select the map icon
    per the APRS spec's symbol tables (e.g. table "/" code ">" is a
    car, table "/" code "-" is a house) -- rendering that mapping is
    left to callers (the web map, milestone 7). ``timestamp`` is the
    raw APRS timestamp text (e.g. "092345z"), not decoded to a real
    date -- APRS timestamps carry no year and are ambiguous about
    UTC/local depending on their trailing letter, so turning them into
    an actual datetime needs a caller-supplied "as of" reference point
    this module doesn't have. ``comment`` is whatever free text
    followed the symbol code, verbatim. ``raw`` keeps the original
    payload text for debugging.
    """

    latitude: float
    longitude: float
    symbol_table: str
    symbol_code: str
    comment: str
    timestamp: Optional[str]
    raw: str


def _decode_latitude(digits: str, direction: str) -> float:
    # "DDMM.mm" -- 2-digit degrees, 2-digit minutes, '.', 2-digit
    # hundredths-of-a-minute. Ambiguous (space) digits are treated as
    # '0' -- see module docstring's "POSITION AMBIGUITY" note.
    digits = digits.replace(" ", "0")

    degrees = int(digits[0:2])
    minutes = float(digits[2:])

    decimal = degrees + minutes / 60.0

    return -decimal if direction == "S" else decimal


def _decode_longitude(digits: str, direction: str) -> float:
    # "DDDMM.mm" -- 3-digit degrees, same minutes shape as latitude.
    digits = digits.replace(" ", "0")

    degrees = int(digits[0:3])
    minutes = float(digits[3:])

    decimal = degrees + minutes / 60.0

    return -decimal if direction == "W" else decimal


# Uncompressed position, no timestamp: data type '!' or '='.
#
#   !4903.50N/07201.75W-Test comment
#
# Latitude "DDMM.mm" (with optional space-for-ambiguity digits),
# N/S, symbol table character, longitude "DDDMM.mm" (same shape),
# E/W, symbol code character, then a free-text comment.
_POSITION_RE = re.compile(
    r"^[!=]"
    r"(?P<lat>\d{2}[\d ]{2}\.[\d ]{2})(?P<lat_dir>[NS])"
    r"(?P<sym_table>.)"
    r"(?P<lon>\d{3}[\d ]{2}\.[\d ]{2})(?P<lon_dir>[EW])"
    r"(?P<sym_code>.)"
    r"(?P<comment>.*)$"
)

# Uncompressed position, with timestamp: data type '/' or '@'.
#
#   /092345z4903.50N/07201.75W-Test comment
#
# Same position shape as above, preceded by a 7-character APRS
# timestamp (6 digits + a type letter -- 'z'/'/' = day/hour/minute,
# 'h' = hour/minute/second, see spec).
_POSITION_WITH_TIMESTAMP_RE = re.compile(
    r"^[/@]"
    r"(?P<timestamp>\d{6}[zh/])"
    r"(?P<lat>\d{2}[\d ]{2}\.[\d ]{2})(?P<lat_dir>[NS])"
    r"(?P<sym_table>.)"
    r"(?P<lon>\d{3}[\d ]{2}\.[\d ]{2})(?P<lon_dir>[EW])"
    r"(?P<sym_code>.)"
    r"(?P<comment>.*)$"
)


def parse_position(payload: str) -> Optional[AprsPosition]:
    """
    Parse an AX.25 UI-frame payload as an APRS uncompressed position
    report.

    Returns None if the payload isn't a position report at all (any
    other APRS data type, or non-APRS traffic entirely), or is a
    position report in the compressed format this module doesn't
    support yet -- see the module docstring. Deliberately permissive
    like pbbs.py's parsers: a format surprise means "no position",
    not an exception.
    """
    if not payload:
        return None

    match = _POSITION_RE.match(payload)
    timestamp = None

    if match is None:
        match = _POSITION_WITH_TIMESTAMP_RE.match(payload)

        if match is not None:
            timestamp = match.group("timestamp")

    if match is None:
        return None

    try:
        latitude = _decode_latitude(
            match.group("lat"), match.group("lat_dir")
        )
        longitude = _decode_longitude(
            match.group("lon"), match.group("lon_dir")
        )
    except ValueError:
        # Shouldn't happen given the regex's own digit/space
        # constraints, but a malformed real-world packet degrading to
        # "no position" beats an unhandled exception taking down the
        # station tracker.
        return None

    return AprsPosition(
        latitude=latitude,
        longitude=longitude,
        symbol_table=match.group("sym_table"),
        symbol_code=match.group("sym_code"),
        comment=match.group("comment"),
        timestamp=timestamp,
        raw=payload,
    )
