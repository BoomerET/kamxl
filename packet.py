import re

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# Matches a KAM-XL MONITOR header line, e.g.:
#
#   KD5EOC-10>BEACON/2:
#   KR8X-7>BEACON,K5LRK*/2:
#
# Format observed on real hardware:
#
#   SOURCE>DESTINATION[,DIGI1,DIGI2*]/PORT:
#
# Digipeaters that have already repeated the packet are suffixed with
# "*". This is scoped to MONITOR/unsolicited traffic only -- plain
# Convers-mode session text (e.g. a Winlink handshake or a node chat
# session) never matches this shape and is intentionally left alone.
HEADER_RE: re.Pattern = re.compile(
    r"^(?P<source>[A-Za-z0-9\-]+)"
    r">(?P<destination>[A-Za-z0-9\-]+)"
    r"(?:,(?P<digipeaters>[A-Za-z0-9\-\*,]+))?"
    r"/(?P<port>\d+):$"
)


@dataclass(frozen=True)
class Packet:
    """
    A single unsolicited packet, as reported by KAM-XL MONITOR output.

    ``digipeaters`` preserves the "*" suffix on any that have already
    repeated the packet (e.g. "K5LRK*") so callers can tell where in
    the path the packet actually is.

    ``payload`` is the packet's info text with the trailing newlines
    from each physical line stripped and rejoined with "\\n" -- i.e.
    it doesn't include the header line itself. ``raw`` keeps the full
    original text (header + payload) for debugging or in case the
    parsed fields are ever wrong.
    """

    source: str
    destination: str
    digipeaters: Tuple[str, ...]
    port: int
    payload: str
    raw: str

    @property
    def digipeated(self) -> bool:
        """
        True if at least one digipeater in the path has already
        repeated this packet.
        """
        return any(
            digi.endswith("*")
            for digi in self.digipeaters
        )


class PacketParser:
    """
    Incrementally reassembles KAM-XL MONITOR output into Packet
    objects.

    MONITOR text arrives from listen()/read_available() in arbitrary,
    not-line-aligned chunks -- observed on real hardware: a single
    line like "Welcome to the Denton County Texas EOC" split across
    reads as "Welcome to the D" / "enton County Texas EOC". Packets
    aren't explicitly delimited either: a packet is only known to be
    complete once the *next* packet's header line arrives (or the
    session ends), so this has to buffer across an unknown number of
    feed() calls.

    Usage:

        parser = PacketParser()

        def on_chunk(text):
            for packet in parser.feed(text):
                handle(packet)

        kam.listen(seconds=600, callback=on_chunk)

        # At the end of the session, flush() to emit anything left
        # over that never saw a following header line.
        for packet in parser.flush():
            handle(packet)
    """

    def __init__(self) -> None:
        self._buffer: str = ""
        self._pending: Optional[Dict[str, Any]] = None

    def feed(self, text: str) -> List[Packet]:
        """
        Feed newly-received text into the parser.

        Returns a list of Packet objects completed as a result of
        this call. Usually 0 or 1, but could be more if a single
        chunk happens to contain several complete lines.
        """
        self._buffer += text

        completed: List[Packet] = []

        while True:
            newline_index = self._buffer.find("\n")

            if newline_index == -1:
                break

            line = self._buffer[:newline_index]
            self._buffer = self._buffer[newline_index + 1:]

            packet = self._process_line(
                line.rstrip("\r")
            )

            if packet is not None:
                completed.append(packet)

        return completed

    def flush(self) -> List[Packet]:
        """
        Finalize whatever is still buffered -- e.g. because the
        session ended before a trailing newline or another header
        line arrived.

        Returns a list of any Packet objects this produces (usually
        0 or 1).
        """
        completed: List[Packet] = []

        if self._buffer:
            line = self._buffer.rstrip("\r")
            self._buffer = ""

            packet = self._process_line(line)

            if packet is not None:
                completed.append(packet)

        packet = self._finalize_pending()

        if packet is not None:
            completed.append(packet)

        return completed

    def _process_line(self, line: str) -> Optional[Packet]:
        """
        Handle one complete, decoded line.

        If it's a header line, whatever was previously pending is
        now complete and gets returned. Otherwise, the line is folded
        into the currently pending packet's payload (or silently
        dropped if nothing is pending -- e.g. leading noise before
        the first header, or non-MONITOR text like Convers-mode
        session traffic).
        """
        match = HEADER_RE.match(line)

        if match is None:
            if self._pending is not None:
                self._pending["payload_lines"].append(line)

            return None

        completed = self._finalize_pending()

        digipeaters = match.group("digipeaters")

        self._pending = {
            "source": match.group("source"),
            "destination": match.group("destination"),
            "digipeaters": (
                tuple(digipeaters.split(","))
                if digipeaters
                else ()
            ),
            "port": int(match.group("port")),
            "header_line": line,
            "payload_lines": [],
        }

        return completed

    def _finalize_pending(self) -> Optional[Packet]:
        if self._pending is None:
            return None

        payload = "\n".join(
            self._pending["payload_lines"]
        )

        raw = "\n".join(
            [self._pending["header_line"]]
            + self._pending["payload_lines"]
        )

        packet = Packet(
            source=self._pending["source"],
            destination=self._pending["destination"],
            digipeaters=self._pending["digipeaters"],
            port=self._pending["port"],
            payload=payload,
            raw=raw,
        )

        self._pending = None

        return packet
