import logging
import re
import serial
import time

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from packet import Packet, PacketParser
from pbbs import PBBSMessage, PBBSMessageSummary, parse_message, parse_message_list
from winlink import (
    WinlinkMessage,
    build_fs_line,
    build_handshake_response,
    has_end_of_block_marker,
    has_fq_marker,
    parse_message_block,
    parse_proposals,
    parse_secure_challenge,
    split_message_blocks,
)


# Library code stays silent by default (NullHandler) -- an app like
# kamxl_daemon.py that configures logging (its -v/--verbose) picks
# this up automatically, since logging.basicConfig() applies to every
# logger, not just the one it names.
logger = logging.getLogger("kamxl")
logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Command metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandInfo:
    type: str
    writable: bool = True
    # Allowed values for "choice" / "multiport_choice" types, e.g.
    # DIGIPEAT's ("ON", "UIONLY", "OFF") or FULLDUP's ("ON", "OFF",
    # "LOOPBACK"). Left as None when every value is legal (or validation
    # isn't practical, e.g. free-form strings).
    choices: Optional[Tuple[str, ...]] = None


COMMANDS: Dict[str, CommandInfo] = {
    "MONITOR": CommandInfo("multiport_bool"),
    # DIGIPEAT is a *single* value (not Multi-Port per the manual), with
    # three legal states rather than a simple ON/OFF.
    "DIGIPEAT": CommandInfo(
        "choice",
        choices=("ON", "UIONLY", "OFF")
    ),
    "MCON": CommandInfo("multiport_bool"),
    # FULLDUP is Multi-Port, but each port has three legal states
    # (ON/OFF/LOOPBACK), not just ON/OFF.
    "FULLDUP": CommandInfo(
        "multiport_choice",
        choices=("ON", "OFF", "LOOPBACK")
    ),

    "MYCALL": CommandInfo("multiport_string"),
    "HBAUD": CommandInfo("multiport_int"),

    "PORT": CommandInfo("int"),

    "VERSION": CommandInfo("string", writable=False),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class KAMError(Exception):
    """Base exception for KAM-XL errors."""
    pass


class KAMCommandError(KAMError):
    """The KAM-XL rejected or did not understand a command."""
    pass


class KAMTimeoutError(KAMError):
    """The KAM-XL did not respond within the expected time."""
    pass


class KAMConnectionError(KAMError):
    """An AX.25 connected-mode connection failed."""
    pass


# ---------------------------------------------------------------------------
# KAM-XL
# ---------------------------------------------------------------------------

class KAMXL:
    PROMPT: bytes = b"cmd:"

    # Markers used while attempting an AX.25 CONNECT.
    #
    # The manual's own sample transcripts are inconsistent about the exact
    # text the KAM-XL prints: page 31 shows "*** CONNECTED TO callsign"
    # (upper-case "TO"), but the live examples on page 37 and in the
    # message reference on page 196 show "*** CONNECTED to callsign" /
    # "***CONNECTED to call" (lower-case "to", sometimes no space after
    # "***"). These patterns are matched case-insensitively and tolerate
    # a missing space so a real unit isn't mistaken for a timeout.
    #
    # The busy message is "***(callsign) busy" -- the callsign sits
    # between "***" and "busy", so it can't be matched as a fixed string.
    CONNECT_MARKERS: Dict[str, Union[bytes, re.Pattern]] = {
        "connected": re.compile(
            rb"\*\*\*\s*connected\s+to",
            re.IGNORECASE
        ),
        "retry_exceeded": re.compile(
            rb"\*\*\*\s*retry count exceeded",
            re.IGNORECASE
        ),
        "disconnected": re.compile(
            rb"\*\*\*\s*disconnected",
            re.IGNORECASE
        ),
        "busy": re.compile(
            rb"\*\*\*\S*\s*busy",
            re.IGNORECASE
        ),
        "eh": b"EH?",
    }

    # Markers used while waiting for a DISCONNECT to complete.
    DISCONNECT_MARKERS: Dict[str, Union[bytes, re.Pattern]] = {
        "disconnected": re.compile(
            rb"\*\*\*\s*disconnected",
            re.IGNORECASE
        ),
        "cant_disconnect": re.compile(
            rb"can't\s+disconnect",
            re.IGNORECASE
        ),
    }

    def __init__(
        self,
        port: str,
        baudrate: int = 19200,
        timeout: float = 0.25
    ) -> None:
        self.port: str = port
        self.baudrate: int = baudrate
        self.timeout: float = timeout
        self.serial: Optional[serial.Serial] = None

    # -----------------------------------------------------------------------
    # Serial connection
    # -----------------------------------------------------------------------

    def connect(self) -> None:
        """
        Open the serial connection to the KAM-XL.
        """
        if self.serial and self.serial.is_open:
            return

        self.serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout
        )

        time.sleep(0.1)

    def disconnect(self) -> None:
        """
        Close the serial connection to the KAM-XL.
        """
        if self.serial and self.serial.is_open:
            self.serial.close()

    @property
    def is_connected(self) -> bool:
        return bool(
            self.serial
            and self.serial.is_open
        )

    def _require_connection(self) -> None:
        if not self.is_connected:
            raise KAMError("KAM-XL is not connected")

    def _drain_input(self) -> None:
        """
        Discard anything sitting in the receive buffer, including
        bytes still in flight.

        A plain reset_input_buffer() can race with a trailing "cmd:"
        prompt that hasn't fully arrived yet (observed right before
        issuing CONNECT), leaving a stray fragment for the next read
        to pick up. Clear, give the KAM-XL a moment to finish
        transmitting, then clear again.
        """
        self.serial.reset_input_buffer()
        time.sleep(0.05)
        self.serial.reset_input_buffer()

    def _strip_leading_prompt(self, text: str) -> str:
        """
        Remove a stray leading "cmd:" prompt glued to the front of a
        read, left over from a race with _drain_input(). It carries
        no useful content and otherwise leaks into things like
        connect_station()'s returned banner.
        """
        prompt = self.PROMPT.decode("ascii")

        if text.startswith(prompt):
            return text[len(prompt):]

        return text

    # -----------------------------------------------------------------------
    # Low-level serial reading
    # -----------------------------------------------------------------------

    def _read_until_prompt(self, timeout: float = 10) -> bytes:
        """
        Read until the KAM-XL command prompt is received.
        """
        self._require_connection()

        data = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            chunk = self.serial.read(
                self.serial.in_waiting or 1
            )

            if chunk:
                data.extend(chunk)

                if self.PROMPT in data:
                    return bytes(data)

        raise KAMTimeoutError(
            "Timed out waiting for KAM-XL command prompt"
        )

    def _read_until_any(
        self,
        markers: Dict[str, Union[bytes, re.Pattern]],
        timeout: float = 30,
        require_line_end: bool = False,
        line_end_grace: float = 0.5
    ) -> Tuple[str, Optional[str]]:
        """
        Read until one of the supplied markers is received.

        ``markers`` is a dict mapping a name to either a bytes literal or
        a compiled bytes regular expression. Regular expressions are
        useful when the text to match includes variable content, such as
        a callsign embedded between "***" and "busy".

        Some markers only need to see the *start* of a line to match
        (e.g. CONNECT_MARKERS' "connected" regex, which fires on
        "*** connected to" alone). Without ``require_line_end``, the
        rest of that line can still be in flight and gets returned
        truncated -- observed on real hardware with a VIA digipeat
        banner: "*** CONNECTED to KD5EOC-10 VIA RS" returned, with
        "STN\\r\\n" trickling in a moment later as if it were ordinary
        post-connect traffic. When ``require_line_end`` is True, once a
        marker matches, reading continues for up to ``line_end_grace``
        seconds (extended each time new bytes arrive) until the
        buffered data ends with a newline.

        Returns:
            tuple:
                decoded_text,
                matched_name

        If the timeout expires before any marker matches, matched_name
        will be None.
        """
        self._require_connection()

        data = bytearray()
        deadline = time.monotonic() + timeout
        matched_name: Optional[str] = None

        while time.monotonic() < deadline:
            chunk = self.serial.read(
                self.serial.in_waiting or 1
            )

            if chunk:
                data.extend(chunk)

                if matched_name is None:
                    for name, marker in markers.items():
                        if isinstance(marker, re.Pattern):
                            found = marker.search(data)
                        else:
                            found = marker in data

                        if found:
                            matched_name = name
                            break

                if matched_name is not None:
                    if not require_line_end:
                        break

                    if data.endswith((b"\r\n", b"\n")):
                        break

                    # Give the rest of the line a short grace period
                    # to arrive rather than returning it truncated.
                    deadline = time.monotonic() + line_end_grace

        return (
            bytes(data).decode(
                "ascii",
                errors="replace"
            ),
            matched_name
        )

    def read_available(self) -> str:
        """
        Return any bytes currently waiting from the KAM-XL.

        Useful for monitored packet traffic.
        """
        self._require_connection()

        if not self.serial.in_waiting:
            return ""

        data = self.serial.read(
            self.serial.in_waiting
        )

        return data.decode(
            "ascii",
            errors="replace"
        )

    def listen(
        self,
        seconds: float = 60,
        callback: Optional[Callable[[str], None]] = None
    ) -> str:
        """
        Listen to unsolicited KAM-XL output for a period of time.

        If callback is supplied, each chunk of decoded text is passed to it.

        Otherwise received text is returned as one string.
        """
        self._require_connection()

        deadline = time.monotonic() + seconds
        received: List[str] = []

        while time.monotonic() < deadline:
            text = self.read_available()

            if text:
                if callback:
                    callback(text)
                else:
                    received.append(text)

            time.sleep(0.05)

        return "".join(received)

    def monitor(
        self,
        seconds: Optional[float] = None,
        callback: Optional[Callable[[Packet], None]] = None
    ) -> Optional[Iterator[Packet]]:
        """
        Monitor unsolicited KAM-XL traffic, decoded into Packet
        objects instead of raw text.

        Unlike listen(), which captures a fixed window of raw text,
        monitor() defaults to running indefinitely (seconds=None) --
        it's meant for continuous use, with the caller deciding when
        to stop.

        If callback is supplied, each Packet is passed to it as soon
        as it's parsed, and this call blocks for up to seconds (or
        forever, until interrupted, if seconds is None):

            kam.monitor(callback=my_function)

        Otherwise, monitor() returns a generator yielding Packets as
        they arrive:

            for packet in kam.monitor():
                ...

        Note: a packet still being assembled when the generator stops
        (deadline reached, or the caller breaks out of the loop
        early) is only flushed and yielded if it was the deadline
        that ended things -- Python doesn't allow a generator to
        yield once more after being closed early via break/.close().
        """
        self._require_connection()

        parser = PacketParser()

        deadline: Optional[float] = (
            None
            if seconds is None
            else time.monotonic() + seconds
        )

        def packets() -> Iterator[Packet]:
            while (
                deadline is None
                or time.monotonic() < deadline
            ):
                text = self.read_available()

                if text:
                    for packet in parser.feed(text):
                        yield packet

                time.sleep(0.05)

            for packet in parser.flush():
                yield packet

        if callback is None:
            return packets()

        for packet in packets():
            callback(packet)

        return None

    # -----------------------------------------------------------------------
    # Terminal command handling
    # -----------------------------------------------------------------------

    def _remove_command_echo(self, text: str, command: str) -> str:
        """
        Remove the first line if it is simply the KAM echoing the command.

        This makes the library work with either ECHO ON or ECHO OFF.
        """
        lines = text.splitlines()

        if (
            lines
            and lines[0].strip().upper()
            == command.strip().upper()
        ):
            lines = lines[1:]

        return "\r\n".join(lines)

    def send_command(
        self,
        command: str,
        command_timeout: float = 10
    ) -> str:
        """
        Send a Terminal Mode command and wait for cmd:.

        Returns the command response without the trailing prompt.
        """
        self._require_connection()

        command = str(command).strip()

        self.serial.reset_input_buffer()

        self.serial.write(
            command.encode("ascii") + b"\r"
        )
        self.serial.flush()

        raw = self._read_until_prompt(
            timeout=command_timeout
        )

        response, _, _ = raw.partition(
            self.PROMPT
        )

        text = response.decode(
            "ascii",
            errors="replace"
        )

        text = text.strip("\r\n")

        # Account for ECHO ON.
        text = self._remove_command_echo(
            text,
            command
        )

        text = text.strip("\r\n")

        if "EH?" in text:
            raise KAMCommandError(
                f"KAM-XL rejected command "
                f"{command!r}: {text!r}"
            )

        return text

    # -----------------------------------------------------------------------
    # Generic get/set
    # -----------------------------------------------------------------------

    def get(self, command: str) -> str:
        """
        Query a KAM-XL parameter and return its raw value.
        """
        command = command.upper()

        response = self.send_command(command)

        lines = response.splitlines()

        if not lines:
            return ""

        # Most parameter queries return:
        #
        # COMMAND   VALUE
        #
        # Use the first meaningful line.
        line = lines[0].strip()

        parts = line.split(maxsplit=1)

        if len(parts) == 2:
            return parts[1]

        return ""

    def set(self, command: str, value: Any) -> str:
        """
        Set a raw KAM-XL parameter value.
        """
        command = command.upper()

        return self.send_command(
            f"{command} {value}"
        )

    # -----------------------------------------------------------------------
    # DISPLAY / configuration
    # -----------------------------------------------------------------------

    def get_configuration(self) -> Dict[str, str]:
        """
        Return the common one-line DISPLAY settings as a dictionary.

        Some KAM-XL DISPLAY entries are multiline and require specialized
        parsers; this method intentionally handles the normal one-line
        configuration entries.
        """
        response = self.send_command(
            "DISPLAY",
            command_timeout=20
        )

        config: Dict[str, str] = {}

        for line in response.splitlines():
            line = line.strip()

            if not line:
                continue

            parts = line.split(maxsplit=1)

            if len(parts) == 2:
                key, value = parts
                config[key] = value
            elif len(parts) == 1:
                # Some commands display with no trailing value at all
                # when they're unset (e.g. a blank MYCALL). Record them
                # with an empty value instead of silently dropping them.
                config[parts[0]] = ""

        return config

    # -----------------------------------------------------------------------
    # Default radio port
    # -----------------------------------------------------------------------

    def get_default_port(self) -> int:
        """
        Return the configured default radio port.

        Note:
            PORT is the default radio port setting, not necessarily the
            currently active I/O stream.
        """
        return int(
            self.get("PORT")
        )

    def set_default_port(self, port: int) -> int:
        """
        Change the KAM-XL default radio port setting.
        """
        if port not in (1, 2):
            raise ValueError(
                "KAM-XL default port must be 1 or 2"
            )

        self.set(
            "PORT",
            port
        )

        actual = self.get_default_port()

        if actual != port:
            raise KAMError(
                f"Requested default port {port}, "
                f"but KAM-XL reports {actual}"
            )

        return actual

    # -----------------------------------------------------------------------
    # Boolean conversion
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_on_off(value: str) -> bool:
        value = value.strip().upper()

        if value == "ON":
            return True

        if value == "OFF":
            return False

        raise ValueError(
            f"Expected ON or OFF, got {value!r}"
        )

    @staticmethod
    def _format_on_off(value: bool) -> str:
        if not isinstance(value, bool):
            raise TypeError(
                "Value must be True or False"
            )

        return "ON" if value else "OFF"

    def get_bool(self, command: str) -> bool:
        return self._parse_on_off(
            self.get(command)
        )

    def set_bool(self, command: str, value: bool) -> str:
        return self.set(
            command,
            self._format_on_off(value)
        )

    # -----------------------------------------------------------------------
    # Restricted-choice parameters (e.g. DIGIPEAT: ON/UIONLY/OFF)
    # -----------------------------------------------------------------------

    def get_choice(self, command: str) -> str:
        """
        Query a single-value, restricted-choice parameter.
        """
        return self.get(command).strip().upper()

    def set_choice(
        self,
        command: str,
        value: Any,
        choices: Optional[Tuple[str, ...]] = None
    ) -> str:
        """
        Set a single-value, restricted-choice parameter.

        If choices is supplied, value is validated against it before being
        sent to the KAM-XL.
        """
        value = str(value).strip().upper()

        if choices and value not in choices:
            raise ValueError(
                f"{command} must be one of {choices}, got {value!r}"
            )

        self.set(command, value)

        return self.get_choice(command)

    # -----------------------------------------------------------------------
    # Multi-port parameters
    # -----------------------------------------------------------------------

    def get_multiport(self, command: str) -> Tuple[str, str]:
        """
        Return a two-port value as:

            (port1_value, port2_value)
        """
        value = self.get(command)

        parts = value.split(
            "/",
            maxsplit=1
        )

        if len(parts) != 2:
            raise KAMError(
                f"{command} did not return "
                f"a multi-port value: {value!r}"
            )

        return (
            parts[0].strip(),
            parts[1].strip()
        )

    def get_multiport_bool(self, command: str) -> Tuple[bool, bool]:
        port1, port2 = self.get_multiport(
            command
        )

        return (
            self._parse_on_off(port1),
            self._parse_on_off(port2),
        )

    def set_multiport_bool(
        self,
        command: str,
        port: int,
        value: bool
    ) -> Tuple[bool, bool]:
        """
        Change a boolean setting for only one radio port.

        Port 1:
            ON/
            OFF/

        Port 2:
            /ON
            /OFF
        """
        if port not in (1, 2):
            raise ValueError(
                "KAM-XL port must be 1 or 2"
            )

        formatted = self._format_on_off(
            value
        )

        if port == 1:
            parameter = f"{formatted}/"
        else:
            parameter = f"/{formatted}"

        self.set(
            command,
            parameter
        )

        return self.get_multiport_bool(
            command
        )

    def get_multiport_choice(self, command: str) -> Tuple[str, str]:
        """
        Return a two-port restricted-choice value as:

            (port1_value, port2_value)

        Values are returned upper-cased and stripped (e.g. FULLDUP's
        ON / OFF / LOOPBACK).
        """
        port1, port2 = self.get_multiport(
            command
        )

        return (
            port1.strip().upper(),
            port2.strip().upper(),
        )

    def set_multiport_choice(
        self,
        command: str,
        port: int,
        value: Any,
        choices: Optional[Tuple[str, ...]] = None
    ) -> Tuple[str, str]:
        """
        Change a restricted-choice setting for only one radio port.

        If choices is supplied, value is validated against it before being
        sent to the KAM-XL.
        """
        if port not in (1, 2):
            raise ValueError(
                "KAM-XL port must be 1 or 2"
            )

        value = str(value).strip().upper()

        if choices and value not in choices:
            raise ValueError(
                f"{command} must be one of {choices}, got {value!r}"
            )

        if port == 1:
            parameter = f"{value}/"
        else:
            parameter = f"/{value}"

        self.set(
            command,
            parameter
        )

        return self.get_multiport_choice(
            command
        )

    # -----------------------------------------------------------------------
    # Typed parameters
    # -----------------------------------------------------------------------

    def get_typed(self, command: str) -> Any:
        """
        Query a known KAM-XL parameter and convert it into an appropriate
        Python type.
        """
        command = command.upper()

        metadata = COMMANDS.get(
            command
        )

        if metadata is None:
            return self.get(command)

        value_type = metadata.type

        if value_type == "multiport_bool":
            return self.get_multiport_bool(
                command
            )

        if value_type == "multiport_choice":
            return self.get_multiport_choice(
                command
            )

        if value_type == "multiport_int":
            left, right = self.get_multiport(
                command
            )

            return (
                int(left),
                int(right)
            )

        if value_type == "multiport_string":
            return self.get_multiport(
                command
            )

        if value_type == "choice":
            return self.get_choice(
                command
            )

        if value_type == "int":
            return int(
                self.get(command)
            )

        return self.get(command)

    def set_typed(self, command: str, value: Any) -> Any:
        """
        Set a known parameter using normal Python values.

        The new value is read back after the write.
        """
        command = command.upper()

        metadata = COMMANDS.get(
            command
        )

        # Unknown commands can still use the raw API.
        if metadata is None:
            return self.set(
                command,
                value
            )

        if not metadata.writable:
            raise KAMError(
                f"{command} is read-only"
            )

        value_type = metadata.type

        if value_type == "multiport_bool":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(
                    isinstance(v, bool)
                    for v in value
                )
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two booleans"
                )

            formatted = (
                f"{self._format_on_off(value[0])}/"
                f"{self._format_on_off(value[1])}"
            )

            self.set(
                command,
                formatted
            )

            return self.get_typed(
                command
            )

        if value_type == "multiport_choice":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two values"
                )

            formatted_values = []

            for v in value:
                v = str(v).strip().upper()

                if (
                    metadata.choices
                    and v not in metadata.choices
                ):
                    raise ValueError(
                        f"{command} must be one of "
                        f"{metadata.choices}, got {v!r}"
                    )

                formatted_values.append(v)

            self.set(
                command,
                f"{formatted_values[0]}/{formatted_values[1]}"
            )

            return self.get_typed(
                command
            )

        if value_type == "multiport_int":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(
                    isinstance(v, int)
                    for v in value
                )
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two integers"
                )

            self.set(
                command,
                f"{value[0]}/{value[1]}"
            )

            return self.get_typed(
                command
            )

        if value_type == "multiport_string":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(
                    isinstance(v, str)
                    for v in value
                )
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two strings"
                )

            self.set(
                command,
                f"{value[0]}/{value[1]}"
            )

            return self.get_typed(
                command
            )

        if value_type == "choice":
            return self.set_choice(
                command,
                value,
                choices=metadata.choices
            )

        if value_type == "int":
            if not isinstance(value, int):
                raise TypeError(
                    f"{command} requires an integer"
                )

            self.set(
                command,
                value
            )

            return self.get_typed(
                command
            )

        self.set(
            command,
            value
        )

        return self.get_typed(
            command
        )

    # -----------------------------------------------------------------------
    # AX.25 connected mode
    # -----------------------------------------------------------------------

    def connect_station(
        self,
        callsign: str,
        via: Optional[Union[str, List[str]]] = None,
        timeout: float = 60
    ) -> str:
        """
        Attempt an AX.25 connected-mode connection.

        Examples:

            kam.connect_station("KD5EOC-10")

            kam.connect_station(
                "KD5EOC-10",
                via="RSSTN"
            )

            kam.connect_station(
                "TARGET",
                via=["DIGI1", "DIGI2"]
            )

        A successful CONNECT may put the KAM-XL into Convers mode.
        """
        self._require_connection()

        callsign = callsign.strip().upper()

        command = f"CONNECT {callsign}"

        if via:
            if isinstance(via, str):
                via = [via]

            path = ",".join(
                str(digi).strip().upper()
                for digi in via
            )

            command += f" VIA {path}"

        self._drain_input()

        self.serial.write(
            command.encode("ascii") + b"\r"
        )
        self.serial.flush()

        text, marker = self._read_until_any(
            self.CONNECT_MARKERS,
            timeout=timeout,
            require_line_end=True
        )

        text = self._strip_leading_prompt(text)

        if marker == "connected":
            return text

        if marker is None:
            raise KAMTimeoutError(
                f"Timed out attempting connection "
                f"to {callsign}"
            )

        raise KAMConnectionError(
            f"Could not connect to {callsign}: "
            f"{text.strip()}"
        )

    def send_connected(
        self,
        text: Any,
        add_cr: bool = True
    ) -> None:
        """
        Send text while the KAM-XL is in Convers mode.
        """
        self._require_connection()

        data = str(text).encode("ascii")

        if add_cr:
            data += b"\r"

        self.serial.write(data)
        self.serial.flush()

    def read_connected(
        self,
        timeout: float = 5
    ) -> str:
        """
        Collect connected-mode data for up to timeout seconds.
        """
        self._require_connection()

        data = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            chunk = self.serial.read(
                self.serial.in_waiting or 1
            )

            if chunk:
                data.extend(chunk)

        return bytes(data).decode(
            "ascii",
            errors="replace"
        )

    def enter_command_mode(
        self,
        timeout: float = 5
    ) -> str:
        """
        Send Ctrl-C to return from Convers mode to Command mode.
        """
        self._require_connection()

        self._drain_input()

        self.serial.write(
            b"\x03"
        )
        self.serial.flush()

        text, marker = self._read_until_any(
            {"prompt": self.PROMPT},
            timeout=timeout
        )

        if marker is None:
            raise KAMTimeoutError(
                "Timed out returning to Command mode"
            )

        return text

    def disconnect_station(
        self,
        timeout: float = 30,
        command_mode_timeout: float = 5
    ) -> str:
        """
        Return to Command mode and disconnect the current AX.25 link.

        ``timeout`` bounds the DISCONNECT confirmation step;
        ``command_mode_timeout`` separately bounds the initial Ctrl-C
        step that gets back to Command mode first (previously always
        hardcoded to enter_command_mode()'s own 5s default, regardless
        of what a caller passed for ``timeout`` -- surprising, since
        the two steps run sequentially and a caller asking for a more
        patient disconnect overall would reasonably expect that to
        cover both).
        """
        self._require_connection()

        self.enter_command_mode(timeout=command_mode_timeout)

        self._drain_input()

        self.serial.write(
            b"DISCONNE\r"
        )
        self.serial.flush()

        text, marker = self._read_until_any(
            self.DISCONNECT_MARKERS,
            timeout=timeout,
            require_line_end=True
        )

        text = self._strip_leading_prompt(text)

        if marker is None:
            raise KAMTimeoutError(
                "Timed out waiting for AX.25 disconnect"
            )

        return text

    # -----------------------------------------------------------------------
    # PBBS (Milestone 6)
    # -----------------------------------------------------------------------
    #
    # Not a BBS this project implements -- the KAM-XL's own firmware
    # PBBS already handles message storage, forwarding, and SYSOP
    # access. These two methods just drive it through the same
    # connected-mode primitives above (connect_station() /
    # send_connected() / read_connected() / disconnect_station()) and
    # hand the raw text off to pbbs.py for parsing. Per the manual, a
    # connect from the local serial terminal gets automatic SYSOP
    # privilege -- no password exchange needed.

    def _poll_until(
        self,
        done: Callable[[str], bool],
        timeout: float
    ) -> str:
        """
        Collect connected-mode text in short slices, stopping as soon
        as ``done(accumulated_text)`` returns True rather than always
        waiting out one fixed-duration read_connected() call.

        Generalized from a PBBS-specific helper (originally
        _collect_pbbs_response(), now a thin wrapper around this) after
        a real bug: a single read_connected(timeout=N) call just
        returns whatever arrived in N seconds, whether or not the
        far end was actually done sending -- a real PBBS message
        (found live, on hardware) had its last line silently truncated
        because it took slightly longer than the old fixed 5s window
        to fully arrive. Polling in short slices and checking a
        predicate means the common case returns as soon as it's
        actually finished, and ``timeout`` becomes a true worst-case
        ceiling instead of "the length of every single call, whether
        needed or not." Milestone 8 (Winlink) reuses this directly,
        since its multi-stage handshake/proposal/message exchange has
        several different "are we done yet" conditions, not just one
        fixed prompt string the way PBBS has.
        """
        deadline = time.monotonic() + timeout
        text = ""

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            text += self.read_connected(timeout=min(1.0, remaining))

            if done(text):
                break

        return text

    def _collect_pbbs_response(self, timeout: float) -> str:
        """
        Collect connected-mode text from a PBBS command, stopping as
        soon as its "ENTER COMMAND:" prompt reappears (meaning it's
        finished and waiting for the next command). See _poll_until().
        """
        return self._poll_until(
            lambda text: "ENTER COMMAND" in text.upper(),
            timeout
        )

    def list_pbbs_messages(
        self,
        mypbbs: Optional[str] = None,
        connect_timeout: float = 15,
        read_timeout: float = 10
    ) -> List[PBBSMessageSummary]:
        """
        Connect to the KAM-XL's own PBBS and list its messages.

        ``mypbbs`` defaults to whatever MYPBBS is currently set to on
        the KAM-XL. ``read_timeout`` is a worst-case ceiling -- see
        _collect_pbbs_response(), which normally returns as soon as
        PBBS's prompt reappears, well before this elapses. Disconnects
        with disconnect_station() when done -- deliberately not PBBS's
        own "B" (bye) command, so ending the session reuses the
        already-hardened disconnect path instead of adding a second,
        PBBS-specific way to do the same thing.
        """
        if mypbbs is None:
            mypbbs = self.get("MYPBBS")

        self.connect_station(mypbbs, timeout=connect_timeout)

        try:
            self.send_connected("L")
            text = self._collect_pbbs_response(read_timeout)
        finally:
            self.disconnect_station()

        # parse_message_list() silently skips lines it doesn't
        # recognize -- by design, so a real-hardware format surprise
        # degrades gracefully instead of raising. The tradeoff: a
        # genuinely empty mailbox and a total parsing mismatch both
        # come back as an empty list, indistinguishable from the
        # return value alone. Logging the raw text here is what makes
        # them distinguishable, for anyone running with -v.
        logger.debug("pbbs list raw: %r", text)

        return parse_message_list(text)

    def read_pbbs_message(
        self,
        number: int,
        mypbbs: Optional[str] = None,
        connect_timeout: float = 15,
        read_timeout: float = 10
    ) -> Optional[PBBSMessage]:
        """
        Connect to the KAM-XL's own PBBS and read one message.

        ``read_timeout`` is a worst-case ceiling -- see
        _collect_pbbs_response(). Returns None if the response didn't
        look like a message (e.g. the number doesn't exist) rather
        than raising -- see pbbs.parse_message()'s docstring.
        """
        if mypbbs is None:
            mypbbs = self.get("MYPBBS")

        self.connect_station(mypbbs, timeout=connect_timeout)

        try:
            self.send_connected(f"R {number}")
            text = self._collect_pbbs_response(read_timeout)
        finally:
            self.disconnect_station()

        logger.debug("pbbs read raw: %r", text)

        return parse_message(text)

    # -----------------------------------------------------------------------
    # Winlink (Milestone 8)
    # -----------------------------------------------------------------------
    #
    # Connects to a real Winlink RMS Packet gateway over AX.25 and
    # downloads whatever mail is waiting -- receive-only for this
    # first pass (see winlink.py's module docstring for the full scope
    # writeup: plain-ASCII FBB tier only, no compression, single
    # message block per call, no outbound send yet). Built from the
    # public B2F/FBB specs and a trusted open-source reference
    # implementation, UNVERIFIED against a real gateway -- expect
    # adjustment once actually tested, the same way pbbs.py's parsing
    # was before Dave's real-hardware pass confirmed (and partially
    # corrected) it.

    def check_winlink_mail(
        self,
        gateway: str,
        password: str,
        mycall: Optional[str] = None,
        connect_timeout: float = 60,
        read_timeout: float = 30,
    ) -> List[WinlinkMessage]:
        """
        Connect to a Winlink RMS Packet gateway, complete the
        secure-login handshake, and download whatever mail is waiting
        -- up to one proposal block (5 messages) per call; see
        winlink.py's module docstring for why. Never proposes an
        outbound message of our own (receive-only MVP scope) -- this
        can't be used to send mail yet.

        ``mycall`` defaults to the KAM-XL's own MYCALL (its first port
        value, if MYCALL is a multi-port setting) -- this needs to
        match your registered Winlink account callsign, or the
        gateway's secure login will reject it. ``password`` is your
        real Winlink account password, sent as an 8-digit challenge
        response, never in the clear (see winlink.secure_login_response()) --
        still, don't log it; this method deliberately never does.

        Returns an empty list if there's no mail waiting (the gateway
        replies "FQ" instead of proposing anything) -- not an error.
        """
        if mycall is None:
            # MYCALL can be a multi-port value like "AI6K-10/AI6K-10"
            # -- Winlink account identity isn't tied to a specific
            # radio port, so only the first one is used.
            mycall = self.get("MYCALL").split("/")[0].strip()

        self.connect_station(gateway, timeout=connect_timeout)

        try:
            handshake_text = self._poll_until(
                lambda text: text.rstrip().endswith(">"),
                read_timeout
            )

            logger.debug("winlink handshake raw: %r", handshake_text)

            challenge = None

            for line in handshake_text.splitlines():
                challenge = parse_secure_challenge(line)

                if challenge is not None:
                    break

            response = build_handshake_response(
                mycall,
                secure_challenge=challenge,
                password=password if challenge else None,
            )

            # "FF" tells the gateway we have nothing to propose --
            # receive-only MVP, see this method's docstring.
            self.send_connected(response + "\rFF")

            proposal_text = self._poll_until(
                lambda text: (
                    has_end_of_block_marker(text) or has_fq_marker(text)
                ),
                read_timeout
            )

            logger.debug("winlink proposals raw: %r", proposal_text)

            if has_fq_marker(proposal_text):
                return []

            proposals = parse_proposals(proposal_text)

            if not proposals:
                return []

            self.send_connected(build_fs_line(len(proposals)))

            messages_text = self._poll_until(
                lambda text: text.count("\x1a") >= len(proposals),
                read_timeout
            )

            logger.debug("winlink messages raw: %r", messages_text)
        finally:
            self.disconnect_station()

        blocks = split_message_blocks(messages_text, len(proposals))

        return [
            parse_message_block(block, proposal)
            for block, proposal in zip(blocks, proposals)
        ]
