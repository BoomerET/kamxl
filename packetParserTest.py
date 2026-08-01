"""
Offline test for PacketParser, using real chunked text captured from
the KAM-XL over the air during earlier live testing (see PROJECT.md).
No radio hardware needed -- this only exercises the text reassembly
logic, feeding it in the same non-line-aligned pieces actually
observed on real hardware, not clean synthetic lines.
"""

from packet import PacketParser


def check(label, condition):
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {label}")

    if not condition:
        raise SystemExit(1)


# --- Case 1: a single beacon, delivered as one clean read_available()
# chunk (this did happen sometimes -- not every read was split).
parser = PacketParser()

packets = parser.feed(
    "KD5EOC-10>BEACON/2:\r\n"
    "Winlink 2000 RMS Packet Server\r\n"
)
packets += parser.flush()

check("single clean packet: one packet parsed", len(packets) == 1)
p = packets[0]
check("single clean packet: source", p.source == "KD5EOC-10")
check("single clean packet: destination", p.destination == "BEACON")
check("single clean packet: no digipeaters", p.digipeaters == ())
check("single clean packet: not digipeated", not p.digipeated)
check("single clean packet: port", p.port == 2)
check(
    "single clean packet: payload",
    p.payload == "Winlink 2000 RMS Packet Server"
)


# --- Case 2: a multi-line beacon with a digipeater path, split across
# reads exactly like real hardware did:
#   "KR8X-7>BEACON,K5LRK*/2:\r\nKR8X-7 Node alias TXDBSN:\r\nKR8X-10 Winlink RMS GW,\r\n-11 BBS,\r\n-12 CHAT"
# fed in arbitrary-width chunks.
parser = PacketParser()

raw = (
    "KR8X-7>BEACON,K5LRK*/2:\r\n"
    "KR8X-7 Node alias TXDBSN:\r\n"
    "KR8X-10 Winlink RMS GW,\r\n"
    "-11 BBS,\r\n"
    "-12 CHAT"
)

# Feed it in small, deliberately misaligned pieces (not on line
# boundaries) to mirror what read_available() actually handed back
# during live testing.
chunk_size = 7
packets = []

for i in range(0, len(raw), chunk_size):
    packets += parser.feed(raw[i:i + chunk_size])

packets += parser.flush()

check("multi-line + digipeat: one packet parsed", len(packets) == 1)
p = packets[0]
check("multi-line + digipeat: source", p.source == "KR8X-7")
check("multi-line + digipeat: destination", p.destination == "BEACON")
check(
    "multi-line + digipeat: digipeaters",
    p.digipeaters == ("K5LRK*",)
)
check("multi-line + digipeat: digipeated", p.digipeated)
check("multi-line + digipeat: port", p.port == 2)
check(
    "multi-line + digipeat: payload",
    p.payload == (
        "KR8X-7 Node alias TXDBSN:\n"
        "KR8X-10 Winlink RMS GW,\n"
        "-11 BBS,\n"
        "-12 CHAT"
    )
)


# --- Case 3: two back-to-back packets in one listen() session, like
# hearing the AI6K-4 beacon shortly after another station's beacon.
parser = PacketParser()

packets = parser.feed(
    "KD5EOC-10>BEACON/2:\r\n"
    "Winlink 2000 RMS Packet Server\r\n"
    "AI6K-4>BEACON/2:\r\n"
    "AI6K-4 Linux Node http://digipi.org/\r\n"
)
packets += parser.flush()

check("two packets: count", len(packets) == 2)
check("two packets: first source", packets[0].source == "KD5EOC-10")
check(
    "two packets: first payload",
    packets[0].payload == "Winlink 2000 RMS Packet Server"
)
check("two packets: second source", packets[1].source == "AI6K-4")
check(
    "two packets: second payload",
    packets[1].payload == "AI6K-4 Linux Node http://digipi.org/"
)


# --- Case 4: no trailing newline before the session ends (KAM-XL cuts
# off mid-payload-line when the listen() window closes) -- flush()
# needs to catch this, not silently drop it.
parser = PacketParser()

packets = parser.feed(
    "AI6K-4>BEACON/2:\r\n"
    "AI6K-4 Linux Node http://digipi.org/"  # no trailing \r\n
)
packets += parser.flush()

check("no trailing newline: one packet parsed", len(packets) == 1)
check(
    "no trailing newline: payload preserved",
    packets[0].payload == "AI6K-4 Linux Node http://digipi.org/"
)


# --- Case 5: Convers-mode / non-MONITOR text (the Winlink handshake
# captured earlier) should never be mistaken for MONITOR packets --
# there's no header line, so it should just be silently ignored
# rather than raising or fabricating a bogus packet.
parser = PacketParser()

packets = parser.feed(
    "Welcome to the Denton County Texas EOC\r\n"
    "[WL2K-5.0-B2FWIHJM$]\r\n"
    ";PQ: 78358301\r\n"
    "CMS via KD5EOC >\r\n"
)
packets += parser.flush()

check("non-MONITOR text: no packets fabricated", len(packets) == 0)


print()
print("All packet parser checks passed.")
