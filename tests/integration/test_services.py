"""Both services as they run: the poller app taking Scheduler's requests over HTTP and the API app reading what it
wrote from the same store, the guard's decisions as the API reports them, and tifa.main starting either role."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tests.integration import scenario as sc
from tests.integration.conftest import BASE, Clock, make_api, settings
from tifa import main as entry
from tifa.config import load_settings
from tifa.core.timeutil import iso
from tifa.poller.app import create_app as create_poller
from tifa.poller.guard import Guard, MonthUsage
from tifa.store.memory import MemoryStore

FIRST, LAST = sc.ldn(25, 1, 30), sc.ldn(25, 2, 30)  # 00:30 to 02:30 UTC: London's 01:30 BST to 02:30 GMT


class ClockedTfl(sc.FakeTfl):
    """The scenario's TfL, answering for the poller's own clock."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock

    def fetch_status(self):
        self.at = int(self.clock())
        return super().fetch_status()


def scheduler_time(slot: int) -> str:
    """X-CloudScheduler-ScheduleTime as Cloud Scheduler sends it (RFC 3339, UTC)."""
    return iso(slot * 60).replace("+00:00", ".000Z")


@pytest.fixture
def services():
    store, clock = MemoryStore(), Clock(FIRST)
    poller = create_poller(settings=load_settings({"TIFA_ROLE": "poller"}), store=store, client=ClockedTfl(clock),
                           clock=clock)  # fmt: skip
    api, wall, mono = make_api(store, FIRST)
    with TestClient(poller, base_url="http://poller.internal") as p, TestClient(api, base_url=BASE) as a:
        yield store, clock, p, a, wall, mono


def test_scheduler_polls_over_http_and_the_api_serves_them(services):
    store, clock, poller, api, wall, mono = services
    assert poller.get("/health").status_code == 503 and api.get("/health").status_code == 503  # no poll yet
    for slot in range(FIRST // 60, LAST // 60 + 1):
        clock.t = slot * 60 + sc.jitter(slot)
        headers = {"X-CloudScheduler-ScheduleTime": scheduler_time(slot)}
        r = poller.post("/internal/poll", headers=headers)
        assert r.status_code == 200 and r.json()["outcome"] == "applied", r.text
        if slot % 20 == 0:  # Scheduler delivers it again
            again = poller.post("/internal/poll", headers=headers)
            assert again.status_code == 200 and again.json() == {
                "outcome": "duplicate",
                "slot": slot,
                "reason": "recorded",
            }
    health = poller.get("/health").json()
    assert health["ok"] and health["last_slot"] == LAST // 60 and health["tfl_auth"] == "ok"

    first, last = FIRST + sc.jitter(FIRST // 60), int(clock.t)
    wall.t, mono.t = last + 10, mono.t + 60
    status = api.get("/v1/status").json()["data"]
    assert status["last_poll"] == iso(last) and not status["stale"]
    # what was in effect at the first poll here starts there; the two delays end across the clock change
    assert [(e["line"], e["start"]) for e in status["open"]] == [("district", iso(first))]
    rows = api.get("/v1/months/2026-10").json()["data"]["episodes"]
    ends = {r[0]: r[2] for r in rows if r[2] is not None}
    assert ends == {
        "hammersmith-city": sc.ldn(25, 1, 10, fold=1) + sc.jitter(sc.ldn(25, 1, 10, fold=1) // 60),
        "jubilee": sc.ldn(25, 1, 40, fold=1) + sc.jitter(sc.ldn(25, 1, 40, fold=1) // 60),
    }
    coverage = api.get("/v1/coverage", params={"from": iso(first), "to": iso(last)}).json()["data"]
    assert coverage["days"] == [{"date": "2026-10-25", "polls": 120, "expected": 1440, "failed": []}]  # to excluded
    assert coverage["gaps"] == [] and coverage["coverage_pct"] == 100
    assert api.get("/health").status_code == 200


def test_a_slot_never_comes_from_a_clock_that_is_wrong(services):
    store, clock, poller, *_ = services
    clock.t = FIRST + 2
    r = poller.post("/internal/poll", headers={"X-CloudScheduler-ScheduleTime": scheduler_time(FIRST // 60 + 600)})
    assert r.status_code == 200 and r.json()["slot"] == FIRST // 60  # ten hours ahead: the current minute instead
    r = poller.post("/internal/poll", headers={"X-CloudScheduler-ScheduleTime": "not a time"})
    assert r.json() == {"outcome": "duplicate", "slot": FIRST // 60, "reason": "recorded"}


class Flood:
    """Monitoring reporting a flood of requests to the public API."""

    def recent_requests(self, now_ts, deadline=None):
        return 20_000, 30_000

    def month_usage(self, now_ts, deadline=None):
        return MonthUsage(requests=500_000, sent_bytes=10**9, billable_s=3_000.0)


class Invoker:
    def __init__(self) -> None:
        self.public = True

    def is_public(self, deadline=None):
        return self.public

    def set_public(self, public, deadline=None):
        changed, self.public = self.public != public, public
        return changed


@pytest.mark.parametrize(("mode", "closed"), [("dry-run", False), ("enforce", True)])
def test_the_api_reports_what_the_guard_did(services, mode, closed):
    """A dry run decides and records a closure but applies none, so /health must not say the API is closed."""
    store, clock, _, api, wall, mono = services
    invoker = Invoker()
    guard = Guard(store, monitoring=Flood(), invoker=invoker, limits=settings().limits, mode=mode)
    run = guard.maybe_run(FIRST)
    assert run.trip == "requests_5min" and invoker.public is (mode == "dry-run")
    wall.t, mono.t = FIRST + 30, mono.t + 60
    data = api.get("/health").json()["data"]["guard"]
    assert data["mode"] == mode and data["heartbeat"] == iso(FIRST)
    assert data["closed_until"] == iso(FIRST + 1800) and data["closed"] is closed


@pytest.mark.parametrize("role", ["api", "poller"])
def test_the_entry_point_starts_either_role_behind_the_proxy(role):
    app = entry.build_app(load_settings({"TIFA_ROLE": role, "TIFA_PROXY_HEADERS": "true"}))
    assert isinstance(app, entry.ProxyHeaders)
    with TestClient(app, base_url=BASE) as http:
        r = http.get("/health", headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})
        assert r.status_code == 503  # nothing polled in this process's own memory store
        if role == "api":
            meta = http.get("/v1/meta").json()
            assert [s["id"] for s in meta["data"]["sources"]] == ["a"] and meta["meta"]["sources"] == ["a"]
        else:
            assert r.json()["role"] == "poller"
            assert http.get("/v1/status").status_code == 404  # the poller serves nothing public
