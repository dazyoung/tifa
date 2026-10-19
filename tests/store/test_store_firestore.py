"""Firestore's stored documents, read back raw from the emulator."""

import os

import pytest

from tifa.store import codec
from tifa.store.base import PollWrite, State

pytestmark = [
    pytest.mark.emulator,
    pytest.mark.skipif(not os.environ.get("FIRESTORE_EMULATOR_HOST"), reason="FIRESTORE_EMULATOR_HOST is not set"),
]

T0 = 1_792_454_340  # 2026-10-19 23:59 UTC
SLOT = T0 // 60
STATUS = {"lines": [{"id": "central", "reasons": ["Minor delays."]}], "disrupted": 1, "total": 1}
PAYLOAD = {
    "month": "2026-10",
    "episodes": [["central", T0 - 600, T0 - 60, 9, "delays", 0, "a", None]],
    "watched": {"a": [[T0 - 3600, T0]], "f": []},
}


def raw(store, path):
    snap = store._db.document(path).get()
    return snap.to_dict() if snap.exists else None


def test_uses_the_emulator(fs):
    assert fs._db._emulator_host == os.environ["FIRESTORE_EMULATOR_HOST"]


def test_raw_documents(fs):
    state = State(last_poll=T0, last_slot=SLOT, status=STATUS, updated_at=T0)
    fs.poll_transaction(
        SLOT,
        lambda s: PollWrite(
            state=state,
            months={"2026-10": PAYLOAD},
            poll_at=T0,
            failed={"slot": SLOT, "code": "retry"},
            snapshot=(T0, STATUS),
        ),
    )
    doc = raw(fs, "meta/state")
    assert isinstance(doc["status"], bytes) and codec.decode(doc["status"]) == STATUS
    assert doc["months"] == {"2026-10": {"rev": 1, "n": 1}}
    month = raw(fs, "months/2026-10")
    assert set(month) == {"month", "rev", "payload", "bytes", "sha256", "updated_at"}
    assert month["payload"] == codec.encode(PAYLOAD) and month["bytes"] == len(month["payload"])
    assert month["sha256"] == codec.digest(PAYLOAD)
    assert raw(fs, "polls/2026-10-19") == {"at": [T0], "n": 1, "failed": [{"slot": SLOT, "code": "retry"}]}
    assert raw(fs, f"snapshots/{T0}") == {"hash": codec.digest(STATUS), "payload": codec.encode(STATUS)}
