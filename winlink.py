"""
Parsing/building helpers for the FBB forwarding protocol used by
Winlink RMS Packet gateways (milestone 8) -- the same relationship
pbbs.py has to raw PBBS text, and packet.py to raw MONITOR text.

Winlink is a real, separately-documented protocol layered on top of
ordinary AX.25 connected-mode sessions -- not something reverse
engineered from the KAM-XL manual. Two sources were used to build
this module, both fetched and cross-checked before writing any code:

  - The official B2F spec: https://winlink.org/B2F
  - The underlying FBB forwarding protocol: http://www.f6fbb.org/protocole.html
  - The secure-login algorithm specifically was ported from
    wl2k-go (https://github.com/la5nta/wl2k-go), a real, widely-used
    open-source Winlink client library -- and verified against its
    own published test vectors (see SECURE_LOGIN_TEST_VECTORS below
    and tests/test_winlink.py) before being trusted here. That's the
    one piece of this module that's actually confirmed correct, as
    opposed to "matches the spec as written."

SCOPE (chosen deliberately, see PROJECT.md milestone 8): plain ASCII
FBB tier only -- this module never claims B/B1/B2 (compressed
protocol) support in its own SID, so a real Winlink gateway (which
must stay backward-compatible with plain FBB per its own spec) will
never propose a compressed/binary message to us. That means no LZHUF
compression, no binary YAPP-style framing, no checksums to implement
-- proposals and message bodies are all plain text.

REAL CONSEQUENCE OF THAT CHOICE, WORTH KNOWING UP FRONT: per the B2F
spec itself, "If a station cannot support the B2 protocol then only
the message body is transmitted and information content of the header
is lost." That means messages received through this module will NOT
carry Winlink's normal structured address header (Mid:/Date:/Type:/
From:/To:/Subject:/Body:/File: fields, described in the B2F spec) --
just a plain FBB title line and body text, the same reduced shape a
classic non-Winlink FBB packet BBS message would have. This is a
genuine capability tradeoff of the ascii-only scope choice, not a bug.

RECEIVE-ONLY (MVP scope): this module has no support for building an
outbound proposal -- KAMXL.check_winlink_mail() (kamxl.py) always
tells the gateway it has nothing to send ("FF" immediately after
login) and only ever accepts whatever the gateway proposes back.
Composing/sending a new message is a followup, not this pass.

SINGLE BLOCK ONLY (MVP scope): the FBB protocol allows up to five
message proposals per block, with more blocks following if there's
more mail waiting. This module's caller (KAMXL.check_winlink_mail())
only reads one block per call -- if a real account ever has more than
five pending messages, only the first five come back on one call;
calling again should fetch the next batch, but this hasn't been
tested against real traffic where that would actually happen.

UNVERIFIED AGAINST A REAL RMS GATEWAY. Built from the spec and a
trusted reference implementation, not a captured live session -- per
this project's usual practice, treat this as a first draft, expect it
to need the same kind of real-hardware correction packet.py's
HEADER_RE and pbbs.py's parsing both needed.
"""

import hashlib
import re

from dataclasses import dataclass
from typing import List, Optional


# -----------------------------------------------------------------------
# Secure login (;PQ: challenge / ;PR: response)
# -----------------------------------------------------------------------

# Verbatim from wl2k-go's fbb/secure.go, which itself ported it from
# paclink-unix. This exact byte sequence is load-bearing -- it's a fixed
# salt mixed into every login response, not an arbitrary constant.
_SECURE_LOGIN_SALT = bytes([
    77, 197, 101, 206, 190, 249,
    93, 200, 51, 243, 93, 237,
    71, 94, 239, 138, 68, 108,
    70, 185, 225, 137, 217, 16,
    51, 122, 193, 48, 194, 195,
    198, 175, 172, 169, 70, 84,
    61, 62, 104, 186, 114, 52,
    61, 168, 66, 129, 192, 208,
    187, 249, 232, 193, 41, 113,
    41, 45, 240, 16, 29, 228,
    208, 228, 61, 20,
])

# wl2k-go's own published test vectors (fbb/secure_test.go) -- kept
# here, alongside tests/test_winlink.py actually asserting against
# them, so the connection between "this is the verified reference"
# and "here's the proof" isn't just a comment.
SECURE_LOGIN_TEST_VECTORS = (
    ("23753528", "FOOBAR", "72768415"),
    ("23753528", "FooBar", "95074758"),
)


def secure_login_response(challenge: str, password: str) -> str:
    """
    Compute the 8-digit response to a Winlink ";PQ:" secure-login
    challenge for the given account password.

    NOT case-normalizing -- matches the reference implementation
    exactly (see SECURE_LOGIN_TEST_VECTORS: "FOOBAR" and "FooBar"
    produce different responses). Winlink account passwords are
    canonically upper-case only (the system upper-cases them if you
    somehow enter lower-case) -- pass ``password`` exactly as your
    account has it configured. A login failure against a password
    you're sure is correct is worth double-checking for stray case
    differences before looking anywhere else.
    """
    payload = (
        challenge.encode("ascii")
        + password.encode("ascii")
        + _SECURE_LOGIN_SALT
    )
    digest = hashlib.md5(payload).digest()

    packed = (
        ((digest[3] & 0x3F) << 24)
        | (digest[2] << 16)
        | (digest[1] << 8)
        | digest[0]
    )

    return f"{packed:08d}"[-8:]


_CHALLENGE_RE = re.compile(r"^;PQ:\s*(?P<challenge>\S+)\s*$", re.IGNORECASE)


def parse_secure_challenge(line: str) -> Optional[str]:
    """
    Parse a ";PQ: <digits>" secure-login challenge line. Returns None
    if this line isn't one (most lines during handshake aren't --
    callers should just skip it and keep reading, not treat a
    non-match as an error).
    """
    match = _CHALLENGE_RE.match(line.strip())

    return match.group("challenge") if match else None


# -----------------------------------------------------------------------
# SID (System Identification) line
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteSID:
    app_name: str
    app_version: str
    codes: str   # e.g. "B2FIHM$" -- see has_code()


_SID_RE = re.compile(r"^\[(?P<app>[^-\]]+)-(?P<version>[^-\]]+)-(?P<codes>[^\]]+)\]\s*$")


def parse_sid(line: str) -> Optional[RemoteSID]:
    """
    Parse a gateway's own SID line (e.g. "[WL2K-5.0-B2FIHM$]"). Returns
    None if this line isn't a SID line at all.
    """
    match = _SID_RE.match(line.strip())

    if match is None:
        return None

    return RemoteSID(
        app_name=match.group("app"),
        app_version=match.group("version"),
        codes=match.group("codes").upper(),
    )


def sid_has_code(sid: RemoteSID, code: str) -> bool:
    """
    Whether the remote's SID advertises a given single/multi-character
    feature code (e.g. "F" for plain FBB ASCII proposals, "B2" for the
    compressed protocol this module doesn't implement).
    """
    return code.upper() in sid.codes


def build_sid(app_name: str, app_version: str) -> str:
    """
    Our own outgoing SID line.

    Deliberately claims only "F" (plain FBB ASCII-basic proposals) and
    "$" (BID/MID field, always present in every proposal line this
    module sends or expects) -- not "B"/"B1"/"B2" (compressed
    protocol), since this module doesn't implement compression. "$"
    must be the last character per the SID format.
    """
    return f"[{app_name}-{app_version}-F$]"


# -----------------------------------------------------------------------
# Handshake response (;FW: / SID / ;PR:)
# -----------------------------------------------------------------------

def build_handshake_response(
    mycall: str,
    app_name: str = "kamxl",
    app_version: str = "0.1",
    secure_challenge: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    """
    Build our side of the handshake: the ";FW:" line requesting mail
    for ``mycall``, our own SID line, and (if the gateway sent a
    ";PQ:" challenge) the ";PR:" secure-login response -- joined with
    "\\r" as a single block, ready to hand to KAMXL.send_connected().

    ``password`` is required if ``secure_challenge`` is given (raises
    ValueError otherwise, rather than silently skipping login and
    likely getting disconnected by the gateway).
    """
    if secure_challenge and not password:
        raise ValueError(
            "Gateway sent a secure-login challenge but no password "
            "was provided"
        )

    lines = [
        f";FW: {mycall}",
        build_sid(app_name, app_version),
    ]

    if secure_challenge:
        response = secure_login_response(secure_challenge, password)
        lines.append(f";PR: {response}")

    return "\r".join(lines)


# -----------------------------------------------------------------------
# Proposals (FB lines -- plain ASCII FBB tier)
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    """
    One "FB" proposal line -- the gateway offering us one message.

    ``msg_type`` is 'P' (private) or 'B' (bulletin). ``via`` is the
    "BBS of recipient" (@) field -- often the gateway itself when a
    message is addressed directly to us. ``mid`` is the message's
    unique ID (BID/MID) used for cross-network deduplication.
    """

    msg_type: str
    sender: str
    via: str
    recipient: str
    mid: str
    size: int
    raw: str


# "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345" -- ascii-basic tier
# proposal line (see f6fbb.org's "Ascii Basic Protocol" section). All
# seven fields (including the leading "FB") are mandatory per spec.
_PROPOSAL_RE = re.compile(
    r"^FB\s+(?P<type>[PB])\s+"
    r"(?P<sender>\S+)\s+"
    r"(?P<via>\S+)\s+"
    r"(?P<recipient>\S+)\s+"
    r"(?P<mid>\S+)\s+"
    r"(?P<size>\d+)\s*$"
)


def parse_proposal(line: str) -> Optional[Proposal]:
    """
    Parse one "FB ..." proposal line. Returns None if this line isn't
    a proposal (callers should skip it, not treat it as an error --
    e.g. it might be the "F>" end-of-block marker, or an unrelated
    comment line).
    """
    match = _PROPOSAL_RE.match(line.strip())

    if match is None:
        return None

    return Proposal(
        msg_type=match.group("type"),
        sender=match.group("sender"),
        via=match.group("via"),
        recipient=match.group("recipient"),
        mid=match.group("mid"),
        size=int(match.group("size")),
        raw=line.strip(),
    )


def parse_proposals(text: str) -> List[Proposal]:
    """
    Parse every "FB ..." proposal line out of a block of text (e.g.
    everything received up to and including the "F>" end-of-block
    marker). Non-matching lines are silently skipped.
    """
    proposals = []

    for line in text.splitlines():
        proposal = parse_proposal(line)

        if proposal is not None:
            proposals.append(proposal)

    return proposals


def build_fs_line(count: int, accept: bool = True) -> str:
    """
    Build an "FS ..." response line accepting (or rejecting) every
    proposed message, in order.

    MVP scope: always all-accept or all-reject -- no per-message
    accept/defer/reject selection yet (real FS lines support '+'/'-'/
    '=' per message; see f6fbb.org's spec for the fuller vocabulary).
    """
    code = "+" if accept else "-"

    return "FS " + (code * count)


def has_end_of_block_marker(text: str) -> bool:
    """
    Whether ``text`` contains the "F>" line that ends a proposal
    block, OR a bare "FF" (the gateway proposing nothing) -- either
    means the gateway is done sending proposals for now.
    """
    for line in text.splitlines():
        stripped = line.strip()

        if stripped == "F>" or stripped == "FF":
            return True

    return False


def has_fq_marker(text: str) -> bool:
    """Whether text contains a bare "FQ" line -- session ending."""
    return any(line.strip() == "FQ" for line in text.splitlines())


# -----------------------------------------------------------------------
# Message bodies
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class WinlinkMessage:
    """
    One received message, in the ASCII-basic tier's reduced shape --
    see the module docstring's "REAL CONSEQUENCE OF THAT CHOICE" note.
    ``title`` is the message's first line (a subject-like line, not a
    structured Winlink address header -- that's not present at this
    protocol tier). ``proposal`` is the "FB ..." line that offered
    this message, carrying whatever real metadata (sender/recipient/
    mid) is actually available.
    """

    title: str
    body: str
    proposal: Proposal
    raw: str


def split_message_blocks(text: str, count: int) -> List[str]:
    """
    Split raw received text into up to ``count`` Ctrl-Z-terminated
    message blocks (title + body, per f6fbb.org's ascii-basic
    protocol -- no binary framing at this tier). Each returned string
    still contains its own title as the first line, with the
    terminating Ctrl-Z itself stripped. Returns fewer than ``count``
    entries if the text doesn't contain that many yet -- callers
    polling incrementally should keep reading until they get exactly
    ``count`` back.
    """
    parts = text.split("\x1a")

    # Whatever follows the last Ctrl-Z (the gateway's next proposal
    # batch, FQ, etc.) isn't a message body -- only keep the parts
    # that were actually terminated by a Ctrl-Z, i.e. all but the
    # trailing remainder.
    blocks = [part.lstrip("\r\n") for part in parts[:-1]]

    return blocks[:count]


def parse_message_block(raw_text: str, proposal: Proposal) -> WinlinkMessage:
    """
    Turn one Ctrl-Z-delimited block (from split_message_blocks()) into
    a WinlinkMessage, pairing it with the Proposal that offered it.
    """
    lines = raw_text.splitlines()
    title = lines[0] if lines else ""
    body = "\n".join(lines[1:]).strip("\r\n")

    return WinlinkMessage(
        title=title,
        body=body,
        proposal=proposal,
        raw=raw_text,
    )
