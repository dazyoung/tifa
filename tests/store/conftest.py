"""Store fixtures: every contract test runs against the in-memory store, and against Firestore when an emulator is
reachable through FIRESTORE_EMULATOR_HOST. Each Firestore test gets its own emulator project, so tests never
see each other's documents."""

import os
import uuid

import pytest

from tifa.store.memory import MemoryStore

EMULATOR = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
needs_emulator = [
    pytest.mark.emulator,
    pytest.mark.skipif(not EMULATOR, reason="FIRESTORE_EMULATOR_HOST is not set"),
]


def pytest_configure(config):
    config.addinivalue_line("markers", "emulator: needs the Firestore emulator (FIRESTORE_EMULATOR_HOST)")


def firestore_store():
    from tifa.store.firestore import FirestoreStore

    return FirestoreStore(project=f"demo-tifa-{uuid.uuid4().hex[:12]}")


@pytest.fixture(params=["memory", pytest.param("firestore", marks=needs_emulator)])
def store(request):
    return MemoryStore() if request.param == "memory" else firestore_store()


@pytest.fixture
def fs():
    """A Firestore store on the emulator, for tests of Firestore's own documents."""
    if not EMULATOR:
        pytest.skip("FIRESTORE_EMULATOR_HOST is not set")
    return firestore_store()
