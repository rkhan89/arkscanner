"""Pulling token details out of raw program logs.

The important trick in phase 1: pump.fun emits an Anchor event containing the
name, symbol, URI, mint and creator directly in the log stream as a base64
`Program data:` line. That means a pump.fun launch is fully captured for zero
extra RPC calls. Raydium logs carry no such payload, so those need a one-credit
getTransaction lookup instead (see resolver.py).
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass

import base58

PROGRAM_DATA_PREFIX = "Program data: "

# Anchor derives an event's 8-byte discriminator from sha256("event:<Name>").
# Computing it rather than hardcoding a magic constant means it cannot be
# copied down wrong.
CREATE_EVENT_DISCRIMINATOR = hashlib.sha256(b"event:CreateEvent").digest()[:8]

MAX_STRING_LEN = 512  # sanity bound; real names/symbols/URIs are far shorter


@dataclass(frozen=True)
class PumpFunCreate:
    mint: str
    name: str
    symbol: str
    uri: str
    creator: str
    raw_base64: str


class _Cursor:
    """Minimal Borsh reader. Raises ValueError on anything malformed."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("ran off the end of the buffer")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def string(self) -> str:
        length = self.u32()
        if length > MAX_STRING_LEN:
            raise ValueError(f"implausible string length {length}")
        return self.take(length).decode("utf-8")

    def pubkey(self) -> str:
        return base58.b58encode(self.take(32)).decode("ascii")

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos


def iter_program_data(logs: list[str]) -> list[str]:
    """The base64 payloads of every `Program data:` line in a log batch."""
    return [line[len(PROGRAM_DATA_PREFIX) :].strip() for line in logs if line.startswith(PROGRAM_DATA_PREFIX)]


def has_create_event(logs: list[str]) -> bool:
    """True when a CreateEvent is present, whether or not its body decodes.

    Lets the caller tell 'this was not a launch' apart from 'this was a launch
    whose payload we could not read', which is a layout change and needs to be
    noisy rather than silent.
    """
    for payload in iter_program_data(logs):
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            continue
        if len(raw) >= 8 and raw[:8] == CREATE_EVENT_DISCRIMINATOR:
            return True
    return False


def decode_pumpfun_create(logs: list[str]) -> PumpFunCreate | None:
    """Find and decode the CreateEvent in a pump.fun log batch.

    Returns None when the batch is not a launch (most of them are buys and
    sells, which emit a TradeEvent through the same `Program data:` channel).
    """
    for payload in iter_program_data(logs):
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            continue
        if len(raw) < 8 or raw[:8] != CREATE_EVENT_DISCRIMINATOR:
            continue
        try:
            return _decode_create_body(raw, payload)
        except ValueError:
            # Layout changed under us. Fall through and let the caller store the
            # row with whatever it has rather than dropping the observation.
            continue
    return None


def _decode_create_body(raw: bytes, payload: str) -> PumpFunCreate:
    cursor = _Cursor(raw)
    cursor.take(8)  # discriminator, already matched
    name = cursor.string()
    symbol = cursor.string()
    uri = cursor.string()
    mint = cursor.pubkey()
    cursor.pubkey()  # bonding curve account, not needed until phase 2
    user = cursor.pubkey()
    # Later versions of the event append an explicit `creator` field. When it is
    # present it is the more accurate deployer; when it is not, `user` is.
    creator = user
    if cursor.remaining >= 32:
        try:
            creator = cursor.pubkey()
        except ValueError:
            creator = user
    return PumpFunCreate(
        mint=mint,
        name=name,
        symbol=symbol,
        uri=uri,
        creator=creator,
        raw_base64=payload,
    )


LOG_PREFIX = "Program log: "


def _marker_matches(rest: str, marker: str) -> bool:
    """Match a marker against a log line at a word boundary.

    A plain substring test is not safe here. 'Instruction: Create' is a
    substring of 'Instruction: CreateFeeSharingConfig', and
    'Instruction: Initialize' is a substring of 'Instruction: InitializeAccount3'
    which the SPL token program emits inside ordinary swaps. Both produced
    silent false positives on the first live run.
    """
    if not rest.startswith(marker):
        return False
    tail = rest[len(marker) :]
    return tail == "" or not (tail[0].isalnum() or tail[0] == "_")


def looks_like_creation(logs: list[str], markers: tuple[str, ...]) -> bool:
    """True when a program log line is one of a program's creation markers."""
    for line in logs:
        if not line.startswith(LOG_PREFIX):
            continue
        rest = line[len(LOG_PREFIX) :].strip()
        if any(_marker_matches(rest, marker) for marker in markers):
            return True
    return False
