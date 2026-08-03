"""
Shared fake serial helpers for the offline test suite. These stand in
for pyserial's Serial object so kamxl.py can be exercised without a
real KAM-XL attached. No third-party test framework or packages
required -- this project's sandbox (and possibly a user's machine)
may not have network access to pip install anything, so everything
here is standard-library only.
"""

import sys
from pathlib import Path

# kamxl.py / packet.py live one directory up from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kamxl import KAMXL


PROMPT = b"cmd:"


def make_kam(serial):
    """
    Build a KAMXL instance wired directly to a fake serial object,
    bypassing connect() (and therefore the real pyserial.Serial
    constructor) entirely.
    """
    kam = KAMXL("COM_FAKE")
    kam.serial = serial

    return kam


class ScriptedSerial:
    """
    Fake serial connection that answers written commands with
    pre-scripted responses, simulating the KAM-XL's actual
    command/response protocol closely enough to exercise KAMXL's
    parsing logic: an optional command echo (real hardware can have
    ECHO ON or OFF), a response body, and a trailing "cmd:" prompt.

    ``script`` maps an exact command string (case-insensitive, as
    sent to the KAM-XL -- e.g. "MONITOR" or "MONITOR ON/OFF") to the
    text that should come back. If a command isn't found in
    ``script``, only the echo (if enabled) and prompt are sent back,
    which matches how a "set" command's response often looks on real
    hardware -- no separate value line, just confirmation.
    """

    def __init__(self, script=None, echo=True):
        self.is_open = True
        self.echo = echo
        self.script = dict(script or {})
        self.written = []
        self._out = bytearray()

    def close(self):
        self.is_open = False

    def write(self, data):
        self.written.append(data)

        command = data.decode("ascii").strip("\r\n")

        if self.echo:
            self._out.extend(command.encode("ascii") + b"\r\n")

        response = self.script.get(command.upper())

        if response is not None:
            body = response.encode("ascii")

            if not body.endswith((b"\r\n", b"\n")):
                body += b"\r\n"

            self._out.extend(body)

        self._out.extend(PROMPT)

        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._out.clear()

    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, n):
        if not self._out:
            return b""

        n = max(1, min(n, len(self._out)))
        data = bytes(self._out[:n])
        del self._out[:n]

        return data


class SilentSerial:
    """
    A connected-but-never-responds fake serial connection, for
    exercising timeout paths (KAMTimeoutError) without waiting out a
    real multi-second timeout -- callers should pass a short explicit
    timeout when using this.
    """

    def __init__(self):
        self.is_open = True
        self.written = []

    def close(self):
        self.is_open = False

    def write(self, data):
        self.written.append(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    @property
    def in_waiting(self):
        return 0

    def read(self, n):
        return b""


class CannedSerial:
    """
    Fake serial connection that ignores what's written and instead
    delivers a fixed sequence of pre-set response chunks, regardless
    of the command sent.

    connect_station()/disconnect_station() (and enter_command_mode())
    don't wait for a "cmd:" prompt the way send_command() does --
    they scan for regex markers via _read_until_any() -- so
    ScriptedSerial's command-aware echo/prompt behavior doesn't fit.
    This is for exercising that different code path directly,
    including reproducing the exact chunk splits observed on real
    hardware (e.g. a VIA digipeat banner arriving as two separate
    reads).

    Chunks are only ever released after at least one write() has
    happened (see read()/in_waiting below) -- added for milestone 7,
    when the daemon's monitor-polling background thread became
    always-on rather than only running while a client had subscribed
    to it. Without this gate, that thread's own read_available() polls
    -- which start firing the instant a KAMDaemon is constructed, well
    before a test's own connect_station()/disconnect_station()/etc.
    call ever runs -- would race the test and silently steal its
    pre-queued response chunks. Real hardware never has this problem
    (nothing arrives on the wire before the command that provokes it),
    so gating on "has anything been written yet" makes this fake match
    that reality instead of assuming it's the only reader.
    """

    def __init__(self, chunks=()):
        self.is_open = True
        self.written = []
        self._chunks = [
            chunk.encode("ascii") if isinstance(chunk, str) else chunk
            for chunk in chunks
        ]

    def close(self):
        self.is_open = False

    def write(self, data):
        self.written.append(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        # A no-op here: the pre-queued chunks represent responses
        # that haven't "arrived" yet at the point connect_station()
        # clears the buffer before writing its command, so there's
        # nothing for a real reset to discard in this simulation.
        pass

    @property
    def in_waiting(self):
        if not self.written or not self._chunks:
            return 0

        return len(self._chunks[0])

    def read(self, n):
        if not self.written or not self._chunks:
            return b""

        return self._chunks.pop(0)


class ChunkSerial:
    """
    Fake serial connection fed a fixed queue of pre-chunked byte
    strings, one chunk becoming available per poll. Used for
    listen()/monitor() tests where the exact chunk boundaries matter
    -- mirroring what real hardware does, where reads don't align to
    line boundaries.
    """

    def __init__(self, chunks):
        self.is_open = True
        self._chunks = [
            chunk.encode("ascii") if isinstance(chunk, str) else chunk
            for chunk in chunks
        ]

    def close(self):
        self.is_open = False

    @property
    def in_waiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, n):
        if not self._chunks:
            return b""

        return self._chunks.pop(0)
