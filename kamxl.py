import re
import serial
import time

from dataclasses import dataclass

from packet import PacketParser


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
    choices: tuple = None


COMMANDS = {
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
    PROMPT = b"cmd:"

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
    CONNECT_MARKERS = {
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
    DISCONNECT_MARKERS = {
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
        port,
        baudrate=19200,
        timeout=0.25
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial = None

    # -----------------------------------------------------------------------
    # Serial connection
    # -----------------------------------------------------------------------

    def connect(self):
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

    def disconnect(self):
        """
        Close the serial connection to the KAM-XL.
        """
        if self.serial and self.serial.is_open:
            self.serial.close()

    @property
    def is_connected(self):
        return bool(
            self.serial
            and self.serial.is_open
        )

    def _require_connection(self):
        if not self.is_connected:
            raise KAMError("KAM-XL is not connected")

    def _drain_input(self):
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

    def _strip_leading_prompt(self, text):
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

    def _read_until_prompt(self, timeout=10):
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
        markers,
        timeout=30,
        require_line_end=False,
        line_end_grace=0.5
    ):
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
        matched_name = None

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

    def read_available(self):
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

    def listen(self, seconds=60, callback=None):
        """
        Listen to unsolicited KAM-XL output for a period of time.

        If callback is supplied, each chunk of decoded text is passed to it.

        Otherwise received text is returned as one string.
        """
        self._require_connection()

        deadline = time.monotonic() + seconds
        received = []

        while time.monotonic() < deadline:
            text = self.read_available()

            if text:
                if callback:
                    callback(text)
                else:
                    received.append(text)

            time.sleep(0.05)

        return "".join(received)

    def monitor(self, seconds=None, callback=None):
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

        deadline = (
            None
            if seconds is None
            else time.monotonic() + seconds
        )

        def packets():
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

    # -----------------------------------------------------------------------
    # Terminal command handling
    # -----------------------------------------------------------------------

    def _remove_command_echo(self, text, command):
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
        command,
        command_timeout=10
    ):
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

    def get(self, command):
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

    def set(self, command, value):
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

    def get_configuration(self):
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

        config = {}

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

    def get_default_port(self):
        """
        Return the configured default radio port.

        Note:
            PORT is the default radio port setting, not necessarily the
            currently active I/O stream.
        """
        return int(
            self.get("PORT")
        )

    def set_default_port(self, port):
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
    def _parse_on_off(value):
        value = value.strip().upper()

        if value == "ON":
            return True

        if value == "OFF":
            return False

        raise ValueError(
            f"Expected ON or OFF, got {value!r}"
        )

    @staticmethod
    def _format_on_off(value):
        if not isinstance(value, bool):
            raise TypeError(
                "Value must be True or False"
            )

        return "ON" if value else "OFF"

    def get_bool(self, command):
        return self._parse_on_off(
            self.get(command)
        )

    def set_bool(self, command, value):
        return self.set(
            command,
            self._format_on_off(value)
        )

    # -----------------------------------------------------------------------
    # Restricted-choice parameters (e.g. DIGIPEAT: ON/UIONLY/OFF)
    # -----------------------------------------------------------------------

    def get_choice(self, command):
        """
        Query a single-value, restricted-choice parameter.
        """
        return self.get(command).strip().upper()

    def set_choice(self, command, value, choices=None):
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

    def get_multiport(self, command):
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

    def get_multiport_bool(self, command):
        port1, port2 = self.get_multiport(
            command
        )

        return (
            self._parse_on_off(port1),
            self._parse_on_off(port2),
        )

    def set_multiport_bool(
        self,
        command,
        port,
        value
    ):
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

    def get_multiport_choice(self, command):
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
        command,
        port,
        value,
        choices=None
    ):
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

    def get_typed(self, command):
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

    def set_typed(self, command, value):
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
        callsign,
        via=None,
        timeout=60
    ):
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
        text,
        add_cr=True
    ):
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
        timeout=5
    ):
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
        timeout=5
    ):
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
        timeout=30
    ):
        """
        Return to Command mode and disconnect the current AX.25 link.
        """
        self._require_connection()

        self.enter_command_mode()

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
