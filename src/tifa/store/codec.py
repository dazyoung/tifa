"""Canonical JSON and gzip for stored blobs.

The same value always encodes to the same bytes: keys sorted, no whitespace, UTF-8, and a gzip header with no
timestamp, no file name and a fixed OS byte (Python's own header varies between versions and platforms).
`sha256()` of the canonical JSON identifies content independently of how it was compressed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import zlib
from typing import Any

LEVEL = 6  # zlib's default: close to level 9's size on JSON for much less CPU

# gzip member header: magic, deflate, no flags, mtime 0, no extra flags, OS 255 ("unknown")
_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"


def dumps(value: Any) -> bytes:
    """Canonical JSON bytes. NaN and infinities are refused, as strict JSON clients would."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def loads(data: bytes | bytearray | memoryview) -> Any:
    return json.loads(bytes(data))


def compress(data: bytes) -> bytes:
    """gzip with a fixed header, so equal input gives equal output."""
    deflate = zlib.compressobj(LEVEL, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = deflate.compress(data) + deflate.flush()
    return _HEADER + body + struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF)


def decompress(blob: bytes | bytearray | memoryview) -> bytes:
    return gzip.decompress(bytes(blob))


def encode(value: Any) -> bytes:
    """gzip of the canonical JSON."""
    return compress(dumps(value))


def decode(blob: bytes | bytearray | memoryview) -> Any:
    return loads(decompress(blob))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """sha256 of the canonical JSON: the same for equal values, whatever their key order."""
    return sha256(dumps(value))
