"""The integration tests' world: three simulated days of minute polls through the real poller code into a
MemoryStore, read back through the real API app (REST and MCP).

`world` (once per test session) runs every Scheduler delivery of tests/integration/scenario.py through
`run_poll()`. While it runs, a sync client follows `/v1/changes` on the API app over the same store, as a copy of
the history kept elsewhere would, so the tests can check what it saw. `api` serves the store as it is at the end.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.integration import scenario as sc
from tifa.api.app import create_app
from tifa.config import Settings, load_settings
from tifa.poller.poll import PollResult, run_poll
from tifa.store.base import APPLIED, PollWrite
from tifa.store.memory import MemoryStore

HOST = "tifa.example.org"
BASE = f"http://{HOST}"
ROOMY = {
    "TIFA_IP_BUCKET": "1000000",
    "TIFA_GLOBAL_BURST": "1000000",
    "TIFA_GLOBAL_RPS": "1000000",
    "TIFA_UNCACHED_PER_S": "1000000",
    "TIFA_EGRESS_PER_HOUR": str(10**12),
    "TIFA_ALLOWED_HOSTS": HOST,
}
SYNC_EVERY = 97  # slots between the sync client's visits (plus one after the clock change and the long outage)


class Clock:
    """A clock that moves only when told."""

    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


class RecordingStore(MemoryStore):
    """A MemoryStore that also notes the time of each snapshot a poll writes (the Store protocol cannot list them)."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshot_times: list[int] = []

    def poll_transaction(self, slot, fn):
        written: list[PollWrite | None] = []

        def wrapped(state):
            write = fn(state)
            written[:] = [write]
            return write

        result = super().poll_transaction(slot, wrapped)
        if result == APPLIED and written and written[0] is not None and written[0].snapshot is not None:
            self.snapshot_times.append(written[0].snapshot[0])
        return result


def settings(**env: str) -> Settings:
    return load_settings({**ROOMY, **env})


def make_api(store: MemoryStore, now: float, *, linked: bool = False, **kwargs: Any):
    """The public app over `store` with clocks that move only when told: (app, wall clock, monotonic clock).

    With `linked` the monotonic clock is the wall clock, so every cache ages exactly as time passes (as it does when
    several clients share one instance); otherwise each moves on its own."""
    wall = Clock(now)
    mono = wall if linked else Clock(10_000.0)
    app = create_app(
        kwargs.pop("settings", None) or settings(),
        store=store,
        clock=wall,
        monotonic=mono,
        mcp_factory=kwargs.pop("mcp_factory", None),
        trust_forwarded=True,
    )
    assert not kwargs, kwargs
    return app, wall, mono


@dataclass
class Visit:
    """One visit of the sync client: what it asked and what /v1/changes answered."""

    since: int
    at: int
    status: int
    data: dict[str, Any]
    etag: str | None


@dataclass
class World:
    store: RecordingStore
    client: sc.FakeTfl
    results: list[tuple[sc.Attempt, PollResult, int]]  # each delivery, its result and TfL fetches it made
    polls: list[int]
    failed: list[dict[str, Any]]
    expected: sc.Expected
    visits: list[Visit] = field(default_factory=list)


def poll(store: MemoryStore, client: sc.FakeTfl, attempt: sc.Attempt) -> tuple[PollResult, int]:
    client.at, client.fail = attempt.at, attempt.fail
    before = client.fetches
    result = run_poll(store, client, attempt.slot, attempt.at)
    return result, client.fetches - before


def simulate(store: MemoryStore, client: sc.FakeTfl, attempts: list[sc.Attempt], on_poll=None) -> list:
    out = []
    for attempt in attempts:
        result, fetches = poll(store, client, attempt)
        out.append((attempt, result, fetches))
        if on_poll is not None:
            on_poll(attempt, result)
    return out


@contextlib.contextmanager
def sync_client(store: MemoryStore, visits: list[Visit]) -> Iterator:
    """A function to call after each delivery: it visits /v1/changes every SYNC_EVERY slots, from the cursor of
    the last answer, with the API's clocks set to just after the poll."""
    app, wall, mono = make_api(store, sc.START)
    after = {sc.CLOCKS_BACK // 60 + 5, sc.LONG_OUTAGE[-1] + 1}
    state = {"cursor": sc.START - 60, "seen": set()}

    with TestClient(app, base_url=BASE) as http:

        def on_poll(attempt: sc.Attempt, result: PollResult) -> None:
            slot = attempt.slot
            if result.outcome != APPLIED or slot in state["seen"]:
                return
            state["seen"].add(slot)
            if (slot - sc.START // 60) % SYNC_EVERY and slot not in after and slot != sc.SLOTS[-1]:
                return
            wall.t = attempt.at + 5
            mono.t += 60  # the read model refreshes at most every 30 s
            since = state["cursor"]
            r = http.get("/v1/changes", params={"since": since})
            body = r.json()
            visits.append(Visit(since, int(wall.t), r.status_code, body.get("data") or {}, r.headers.get("etag")))
            if r.status_code == 200:
                state["cursor"] = body["data"]["cursor"]

        yield on_poll


@pytest.fixture(scope="session")
def world() -> World:
    store, client = RecordingStore(), sc.FakeTfl()
    visits: list[Visit] = []
    with sync_client(store, visits) as on_poll:
        results = simulate(store, client, sc.attempts(), on_poll)
    polls = sc.poll_times()
    return World(
        store=store,
        client=client,
        results=results,
        polls=polls,
        failed=sc.failed_records(),
        expected=sc.expected(polls),
        visits=visits,
    )


@pytest.fixture(scope="session")
def api(world: World) -> Iterator[TestClient]:
    """The public API (no MCP) over the store as the three days left it, at scenario.NOW."""
    app, _, _ = make_api(world.store, sc.NOW)
    with TestClient(app, base_url=BASE) as http:
        yield http


def get(http: TestClient, path: str, **params: Any) -> dict[str, Any]:
    """GET a route and return its data, checking the envelope and the attribution."""
    r = http.get(path, params=params or None)
    assert r.status_code == 200, (path, params, r.status_code, r.text[:500])
    body = r.json()
    assert set(body) == {"data", "meta"}
    meta = body["meta"]
    assert meta["attribution"] == "Powered by TfL Open Data"
    assert meta["notice"] == "Not affiliated with or endorsed by Transport for London."
    assert meta["sources"] == ["a"]
    return body["data"]
