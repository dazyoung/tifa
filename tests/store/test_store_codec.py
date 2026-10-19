import gzip
import hashlib
import json
import math

import pytest

from tifa.store import codec


def test_canonical_json():
    assert codec.dumps({"b": 1, "a": [1, {"d": None, "c": True}]}) == b'{"a":[1,{"c":true,"d":null}],"b":1}'
    assert codec.dumps({"name": "King’s Cross"}) == '{"name":"King’s Cross"}'.encode()  # UTF-8, not \u escapes
    with pytest.raises(ValueError):
        codec.dumps({"x": math.nan})


def test_equal_values_give_equal_bytes():
    a = {"month": "2026-10", "episodes": [["central", 1, 2]], "watched": {"a": [[0, 9]], "f": []}}
    b = {"watched": {"f": [], "a": [[0, 9]]}, "episodes": [["central", 1, 2]], "month": "2026-10"}
    assert codec.encode(a) == codec.encode(b)
    assert codec.digest(a) == codec.digest(b)
    assert codec.encode(a) != codec.encode(a | {"month": "2026-11"})


def test_gzip_header_is_fixed():
    blob = codec.encode({"a": 1})
    assert blob[:10] == b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"  # no mtime, no name, OS unknown


def test_round_trip_is_standard_gzip():
    value = {"text": "Ünïcødé 🚇", "nested": [[1, [2.5]], []], "none": None, "big": 2**60}
    blob = codec.encode(value)
    assert json.loads(gzip.decompress(blob)) == value
    assert codec.decode(blob) == value
    assert codec.decode(bytearray(blob)) == value
    assert codec.decode(memoryview(blob)) == value
    assert codec.decode(gzip.compress(codec.dumps(value))) == value  # any gzip of the JSON decodes


def test_compression_helps():
    value = {"episodes": [["central", 1_792_454_340 + i, None, 6, "delays"] for i in range(500)]}
    assert len(codec.encode(value)) < len(codec.dumps(value)) / 5


def test_corruption_is_detected():
    blob = bytearray(codec.encode({"a": "b" * 100}))
    blob[-5] ^= 0xFF  # inside the CRC/size trailer
    with pytest.raises((OSError, EOFError)):
        codec.decode(bytes(blob))


def test_sha256():
    assert codec.sha256(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert codec.digest({"b": 1, "a": 2}) == hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
