"""
Demonstrates PacketParser turning raw KAM-XL MONITOR text into
structured Packet objects -- no KAM-XL hardware required.

Real MONITOR output doesn't arrive in neat, line-aligned chunks; a
single line can be split across multiple serial reads, and a packet
isn't known to be "complete" until the next header line shows up (or
the session ends). This feeds the parser text in small, deliberately
awkward chunks to show it handles that correctly, then flush()es at
the end to recover the last packet.
"""

import sys
from pathlib import Path

# packet.py lives one directory up from examples/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packet import PacketParser


# Two packets' worth of realistic MONITOR text, chopped up mid-line the
# way it can actually arrive over serial (see docs/troubleshooting.md).
CHUNKS = [
    "KD5EOC-10>WB5NZV,RSSTN*/2:\r\nCMS via KD5EOC",
    " > \r\n",
    "KR8X-7>BEACON,K5LRK*/2:\r\nWelcome to the D",
    "enton County Texas EOC\r\n",
]


def main():
    parser = PacketParser()

    for chunk in CHUNKS:
        for packet in parser.feed(chunk):
            show(packet)

    # The last packet never saw a following header line, so it's still
    # buffered -- flush() finalizes it.
    for packet in parser.flush():
        show(packet)


def show(packet):
    print(f"{packet.source} -> {packet.destination} (port {packet.port})")

    if packet.digipeaters:
        print(f"  via: {', '.join(packet.digipeaters)}"
              f" (digipeated: {packet.digipeated})")

    print(f"  payload: {packet.payload!r}")
    print()


if __name__ == "__main__":
    main()
