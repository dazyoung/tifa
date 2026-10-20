"""Poller fixtures: `store` is the in-memory store, and also Firestore when an emulator is reachable through
FIRESTORE_EMULATOR_HOST (each Firestore test gets its own emulator project)."""

import os
import uuid

import pytest

from tifa.store.memory import MemoryStore

EMULATOR = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
needs_emulator = [
    pytest.mark.emulator,
    pytest.mark.skipif(not EMULATOR, reason="FIRESTORE_EMULATOR_HOST is not set"),
]


@pytest.fixture(params=["memory", pytest.param("firestore", marks=needs_emulator)])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    from tifa.store.firestore import FirestoreStore

    return FirestoreStore(project=f"demo-tifa-{uuid.uuid4().hex[:12]}")
