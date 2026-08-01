"""
Offline test for KAMXL.monitor(), using a fake serial connection fed
real chunked text captured earlier. No radio hardware needed -- this
only exercises monitor()'s own wiring (PacketParser integration,
generator vs. callback modes, deadline handling). PacketParser's text
reassembly itself is already covered by packetParserTest.py.
"""

from kamxl import KAMXL


class FakeSerial:
    """
    Minimal stand-in for pyserial's Serial, fed a fixed queue of
    chunks to simulate arrival the same way read_available() sees it
    on real hardware -- one chunk becomes available per poll.
    """

    def __init__(self, chunks):
        self.is_open = True
        self._chunks = list(chunks)

    @property
    def in_waiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, n):
        if not self._chunks:
            return b""

        return self._chunks.pop(0).encode("ascii")


def check(label, condition):
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {label}")

    if not condition:
        raise SystemExit(1)


CHUNKS = [
    "KD5EOC-10>BEACON/2:\r\n",
    "Winlink 2000 RMS Packet Server\r\n",
    "AI6K-4>BEACON/2:\r\n",
    "AI6K-4 Linux Node http://digipi.org/\r\n",
]


# --- Generator mode: bounded by seconds, so the trailing flush()
# happens naturally once the deadline is reached.
kam = KAMXL("COM_FAKE")
kam.serial = FakeSerial(CHUNKS)

packets = list(kam.monitor(seconds=0.5))

check("generator mode: two packets", len(packets) == 2)
check(
    "generator mode: first source",
    packets[0].source == "KD5EOC-10"
)
check(
    "generator mode: second source",
    packets[1].source == "AI6K-4"
)
check(
    "generator mode: second payload",
    packets[1].payload == "AI6K-4 Linux Node http://digipi.org/"
)


# --- Callback mode: same data, collected via callback instead of
# iteration.
kam = KAMXL("COM_FAKE")
kam.serial = FakeSerial(CHUNKS)

collected = []

kam.monitor(seconds=0.5, callback=collected.append)

check("callback mode: two packets", len(collected) == 2)
check(
    "callback mode: first source",
    collected[0].source == "KD5EOC-10"
)
check(
    "callback mode: second source",
    collected[1].source == "AI6K-4"
)


print()
print("All monitor() checks passed.")
