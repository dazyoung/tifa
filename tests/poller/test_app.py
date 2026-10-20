"""tifa.poller.app: the poll, budget and health endpoints."""

import base64
import json
import logging

import pytest
from fastapi.testclient import TestClient

from tests.poller.helpers import FakeInvoker, FakeMonitoring, FakeTfl, payload, status, utc
from tifa.config import Settings
from tifa.poller import app as poller_app
from tifa.poller.app import create_app, slot_from_header
from tifa.poller.guard import Guard
from tifa.poller.tfl_client import TflError
from tifa.store.memory import MemoryStore

T0 = utc(2026, 10, 19, 8, 0)
SLOT = T0 // 60
HEADER = "X-CloudScheduler-ScheduleTime"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Parts:
    def __init__(self, client=None, guard_mode="enforce"):
        self.store = MemoryStore()
        self.tfl = client or FakeTfl(payload())
        self.mon = FakeMonitoring()
        self.inv = FakeInvoker()
        self.guard = Guard(self.store, monitoring=self.mon, invoker=self.inv, mode=guard_mode, monotonic=lambda: 0.0)
        self.clock = Clock(T0 + 3)
        self.app = create_app(store=self.store, client=self.tfl, guard=self.guard, clock=self.clock)
        self.http = TestClient(self.app)


@pytest.fixture
def parts():
    p = Parts()
    with p.http:
        yield p


def budget_push(notification):
    data = base64.b64encode(json.dumps(notification).encode()).decode()
    return {"message": {"data": data, "attributes": {}, "messageId": "1"}, "subscription": "s"}


NOTIFICATION = {
    "costAmount": 4.0,
    "budgetAmount": 3.75,
    "currencyCode": "GBP",
    "costIntervalStart": "2026-10-01T07:00:00Z",
    "alertThresholdExceeded": 1.0,
}


# ---- slots ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "slot"),
    [
        ("2026-10-19T08:00:00Z", SLOT),
        ("2026-10-19T08:00:00.000Z", SLOT),
        ("2026-10-19T07:59:00Z", SLOT - 1),  # a late retry keeps its own minute
        ("2026-10-19T09:00:00+01:00", SLOT),
        ("2026-10-19T08:02:00Z", SLOT + 2),
        ("2026-10-19T08:30:00Z", SLOT),  # far ahead of the clock: not believed
        ("yesterday", SLOT),
        ("", SLOT),
        (None, SLOT),
    ],
)
def test_slot_from_the_schedule_time(header, slot):
    assert slot_from_header(header, T0 + 3) == slot


# ---- POST /internal/poll ----------------------------------------------------------------------------------------


def test_a_poll_is_recorded_once(parts):
    r = parts.http.post("/internal/poll", headers={HEADER: "2026-10-19T08:00:00Z"})
    assert r.status_code == 200 and r.json()["outcome"] == "applied" and r.json()["slot"] == SLOT
    assert r.headers["cache-control"] == "no-store"
    assert parts.store.get_state().last_poll == T0 + 3
    parts.clock.t += 10
    again = parts.http.post("/internal/poll", headers={HEADER: "2026-10-19T08:00:00Z"})
    assert again.status_code == 200 and again.json() == {"outcome": "duplicate", "slot": SLOT, "reason": "recorded"}
    assert parts.tfl.calls == 1


def test_without_the_header_the_slot_is_the_current_minute(parts):
    r = parts.http.post("/internal/poll")
    assert r.status_code == 200 and r.json()["slot"] == SLOT


def test_a_tfl_failure_is_a_502_for_scheduler_to_retry():
    p = Parts(client=FakeTfl(TflError("timeout", "TfL request timed out (read)")))
    with p.http:
        r = p.http.post("/internal/poll", headers={HEADER: "2026-10-19T08:00:00Z"})
    assert r.status_code == 502 and r.json() == {"outcome": "failed", "slot": SLOT, "code": "timeout"}
    assert p.store.get_polls("2026-10-19").failed == [{"slot": SLOT, "code": "timeout"}]


def test_a_bad_payload_is_a_502():
    p = Parts(client=FakeTfl([]))
    with p.http:
        r = p.http.post("/internal/poll")
    assert r.status_code == 502 and r.json()["code"] == "empty"


def test_a_store_failure_is_a_503(parts, monkeypatch):
    def broken(*args):
        raise RuntimeError("down")

    monkeypatch.setattr(parts.store, "poll_transaction", broken)
    r = parts.http.post("/internal/poll")
    assert r.status_code == 503 and r.json()["code"] == "store"


def test_an_unexpected_error_is_a_503_not_a_4xx(parts, monkeypatch, caplog):
    def boom(*args):
        raise AssertionError("bug")

    monkeypatch.setattr(poller_app, "run_poll", boom)
    r = parts.http.post("/internal/poll")
    assert r.status_code == 503
    assert "failed unexpectedly" in caplog.text


def test_the_guard_runs_after_the_poll_and_closes_the_api(parts):
    parts.mon.five = 50_000
    r = parts.http.post("/internal/poll")
    assert r.status_code == 200
    assert not parts.inv.public
    assert parts.store.get_guard()["closed_until"] == T0 + 3 + 1_800
    # recording carries on while the API is closed
    parts.clock.t += 60
    assert parts.http.post("/internal/poll").json()["outcome"] == "applied"


def test_health_marks_a_dry_runs_closure():
    p = Parts(guard_mode="dry-run")
    p.mon.five = 50_000
    p.http.post("/internal/poll")
    guard = p.http.get("/healthz").json()["guard"]
    assert p.inv.public and guard["mode"] == "dry-run" and guard["dry_run"] is True
    assert guard["closed_until"] == "2026-10-19T08:30:03+00:00" and guard["applied"] is None


def test_the_guard_runs_at_most_every_two_minutes(parts):
    for minute in range(5):
        parts.clock.t = T0 + 3 + minute * 60
        parts.http.post("/internal/poll")
    assert parts.mon.recent_calls == 3  # at 0, 2 and 4 minutes


def test_a_guard_failure_never_fails_a_poll(parts, monkeypatch):
    def broken(now):
        raise RuntimeError("guard bug")

    monkeypatch.setattr(parts.guard, "maybe_run", broken)
    r = parts.http.post("/internal/poll")
    assert r.status_code == 200 and r.json()["outcome"] == "applied"


def test_a_failing_monitoring_api_never_fails_a_poll(parts):
    parts.mon.fail = RuntimeError("monitoring down")
    parts.inv.fail = RuntimeError("iam down")
    r = parts.http.post("/internal/poll")
    assert r.status_code == 200
    assert parts.store.get_guard()["error"] == "requests: RuntimeError"


def test_never_logs_the_key(caplog):
    from tests.poller.helpers import KEY

    caplog.set_level(logging.DEBUG)
    p = Parts(client=FakeTfl(TflError("http_401", "TfL answered HTTP 401", auth="degraded")))
    with p.http:
        p.http.post("/internal/poll")
    assert KEY not in caplog.text


# ---- POST /internal/budget ----------------------------------------------------------------------------------------


def test_a_budget_at_100_percent_closes_the_api(parts):
    r = parts.http.post("/internal/budget", json=budget_push(NOTIFICATION))
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "ratio": round(4.0 / 3.75, 4),
        "closed_until": "2026-11-01T07:00:00+00:00",
        "reason": "budget",
        "action": "closed",
    }
    assert not parts.inv.public
    # the poller keeps recording
    assert parts.http.post("/internal/poll").json()["outcome"] == "applied"


def test_a_budget_under_100_percent_is_recorded_only(parts):
    r = parts.http.post(
        "/internal/budget", json=budget_push(NOTIFICATION | {"costAmount": 1.0, "alertThresholdExceeded": 0.5})
    )
    assert r.status_code == 200 and r.json()["closed_until"] is None and r.json()["action"] is None
    assert parts.inv.public


@pytest.mark.parametrize(
    "body",
    [b"", b"{not json", json.dumps({"message": {}}).encode(), json.dumps(budget_push({"hello": 1})).encode()],
)
def test_other_pushes_are_acknowledged_and_ignored(parts, body):
    r = parts.http.post("/internal/budget", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert parts.inv.public and parts.store.get_guard() is None


def test_a_budget_that_cannot_be_saved_is_redelivered(parts, monkeypatch):
    def broken(doc):
        raise RuntimeError("down")

    monkeypatch.setattr(parts.store, "put_guard", broken)
    r = parts.http.post("/internal/budget", json=budget_push(NOTIFICATION))
    assert r.status_code == 503


def test_an_oversized_push_is_ignored(parts):
    r = parts.http.post("/internal/budget", content=b"x" * 100_000)
    assert r.status_code == 200 and r.json() == {"ok": False, "ignored": "push body too large"}


# ---- GET /healthz ---------------------------------------------------------------------------------------------


def test_health_before_any_poll_is_503(parts):
    r = parts.http.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["last_poll"] is None and body["tfl_auth"] == "ok"
    assert body["guard"]["mode"] == "enforce"


def test_health_after_a_poll(parts):
    parts.tfl.items = [payload({"central": [status(6, "Severe delays.")]})]
    parts.http.post("/internal/poll")
    parts.clock.t += 30
    r = parts.http.get("/healthz")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body == {
        "ok": True,
        "role": "poller",
        "last_poll": "2026-10-19T08:00:03+00:00",
        "last_poll_age_s": 30,
        "last_slot": SLOT,
        "open": 1,
        "tfl_auth": "ok",
        "guard": {
            "mode": "enforce",
            "heartbeat": "2026-10-19T08:00:03+00:00",
            "closed_until": None,
            "reason": None,
            "applied": None,
            "dry_run": False,
            "error": None,
        },
    }
    assert parts.http.head("/healthz").status_code == 200


def test_health_goes_503_when_polls_stop(parts):
    parts.http.post("/internal/poll")
    parts.clock.t += 601
    assert parts.http.get("/healthz").status_code == 503


def test_health_with_the_store_down(parts, monkeypatch):
    monkeypatch.setattr(parts.store, "get_state", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    r = parts.http.get("/healthz")
    assert r.status_code == 503 and r.json()["error"] == "store"


def test_the_poll_endpoint_takes_only_post(parts):
    assert parts.http.get("/internal/poll").status_code == 405
    assert parts.http.get("/docs").status_code == 404 and parts.http.get("/openapi.json").status_code == 404


# ---- start-up from settings -------------------------------------------------------------------------------------


def test_parts_are_built_from_settings(monkeypatch):
    settings = Settings(role="poller", store="memory", guard_mode="off", tfl_base_url="https://api.tfl.example")
    app = create_app(settings=settings, clock=lambda: T0)
    with TestClient(app) as http:
        r = http.get("/healthz")
        assert r.status_code == 503 and r.json()["tfl_auth"] == "anonymous" and r.json()["guard"]["mode"] == "off"
    built = app.state.parts()
    assert isinstance(built.store, MemoryStore) and built.guard.mode == "off"


def test_the_module_app_is_a_poller():
    assert {route.path for route in poller_app.app.routes} >= {"/internal/poll", "/internal/budget", "/healthz"}
