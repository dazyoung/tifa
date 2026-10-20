"""The public API's routes over an in-memory store holding real-shaped data.

The store is what the poller would leave at the fixture's "now" (Monday 3 November 2025, 12:00 UTC): the synthetic
months from tests/fixtures/stats (FOI rows, then five months of live recording with gaps, open episodes and both
clock changes), the open episodes and line list in the state, the current status from a real TfL payload, and a
poll each minute for the last two hours with one failed slot. The other API test modules import these builders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tifa.api import views
from tifa.api.app import create_app
from tifa.config import Settings, load_settings
from tifa.core import tfl
from tifa.core.causes import CAUSES, CAUSES_SIG
from tifa.core.timeutil import iso
from tifa.store.base import PollWrite, State
from tifa.store.memory import MemoryStore
from tools import make_stats_fixtures as fx

ROOT = Path(__file__).resolve().parents[2]
NOW = fx.NOW
MONTHS, INDEX = fx.read_months(fx.FIXTURES / "months")
TFL_SAMPLE = json.loads((ROOT / "tests" / "fixtures" / "core" / "tfl_status_sample.json").read_text(encoding="utf-8"))
POLLS = 120  # minutes of polls before now
FAILED = 30  # the minute (from the first poll) whose slot failed
ROOMY = {"TIFA_IP_BUCKET": "100000", "TIFA_GLOBAL_BURST": "100000", "TIFA_UNCACHED_PER_S": "100000"}
ATTRIBUTION, NOTICE = views.ATTRIBUTION, views.NOTICE


class Clock:
    """A clock that moves only when told."""

    def __init__(self, t: float) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def live_lines(now: int = NOW) -> dict[str, dict[str, Any]]:
    return {
        lid: {"name": name, "mode": mode, "first_seen": fx.LIVE_FROM, "last_seen": now}
        for lid, name, mode, shared in fx.LINES
        if not shared
    }


def fixture_state(t: int, open_eps: list, now: int = NOW, status: dict | None = None) -> State:
    return State(
        last_poll=t,
        last_slot=t // 60,
        run=[t - 3600, t],
        open=open_eps,
        lines=live_lines(now),
        status=status if status is not None else tfl.current(TFL_SAMPLE),
        status_etag="sample",
        causes_sig=CAUSES_SIG,
        tfl_auth="ok",
        updated_at=t,
    )


def poll(store: MemoryStore, t: int, open_eps: list, now: int = NOW, status: dict | None = None) -> str:
    write = PollWrite(state=fixture_state(t, open_eps, now, status), poll_at=t)
    return store.poll_transaction(t // 60, lambda current, w=write: w)


def fixture_store(now: int = NOW, *, polls: int = POLLS, failed: tuple[int, ...] = (FAILED,)) -> MemoryStore:
    """A MemoryStore as the poller would leave it at `now`."""
    snap, open_eps = fx.snapshot(MONTHS, now)
    store = MemoryStore()
    for month, payload in sorted(snap.items()):
        store.put_month(month, payload)
    start = now - polls * 60
    for i in range(polls + 1):
        t = start + i * 60
        if i in failed:
            write = PollWrite(failed={"slot": t // 60, "code": "http_503"})
            store.poll_transaction(t // 60, lambda current, w=write: w)
        else:
            poll(store, t, open_eps, now)
    return store


def open_episodes(now: int = NOW) -> list:
    return fx.snapshot(MONTHS, now)[1]


def make_app(store=None, *, now: int = NOW, env: dict | None = None, settings: Settings | None = None, **kwargs):
    """(app, wall clock, monotonic clock). Limits are roomy unless env says otherwise; the clocks only move when
    told, so neither the buckets nor the caches refill or expire by themselves."""
    wall, mono = Clock(now), Clock(1000.0)
    settings = settings or load_settings(ROOMY if env is None else env)
    app = create_app(
        settings,
        store=store if store is not None else fixture_store(now),
        clock=wall,
        monotonic=mono,
        mcp_factory=kwargs.pop("mcp_factory", None),
        trust_forwarded=kwargs.pop("trust_forwarded", True),
    )
    assert not kwargs, kwargs
    return app, wall, mono


@pytest.fixture(scope="module")
def shared_store() -> MemoryStore:
    return fixture_store()


@pytest.fixture
def client(shared_store):
    app, _, _ = make_app(shared_store)
    with TestClient(app) as c:
        yield c


def assert_envelope(r, *, error: bool = False) -> dict:
    body = r.json()
    assert set(body) == ({"error", "meta"} if error else {"data", "meta"}), body
    m = body["meta"]
    assert m["attribution"] == ATTRIBUTION and m["notice"] == NOTICE
    assert m["licence"] == views.LICENCE and m["api_version"] == "1" and m["sources"] == ["a"]
    assert r.headers["access-control-allow-origin"] == "*"
    for h in ("ratelimit-limit", "ratelimit-remaining", "ratelimit-reset", "ratelimit-policy"):
        assert h in r.headers, h
    return body


# ---- every route -----------------------------------------------------------------------------------------------

ROUTES = {
    # path: Cache-Control
    "/v1/status": "public, max-age=30, stale-while-revalidate=30",
    f"/v1/changes?since={NOW - 3600}": "public, max-age=15",
    "/v1/lines": "public, max-age=3600",
    "/v1/lines/central": "public, max-age=60",
    "/v1/months": "public, max-age=60",
    "/v1/months/2025-11": "public, max-age=60",
    "/v1/months/2025-10": "public, max-age=86400",
    "/v1/disruptions": "public, max-age=60",
    "/v1/disruptions?from=2025-07-01&to=2025-07-31": "public, max-age=3600",
    "/v1/stats?span=7d&end=now": "public, max-age=60",
    "/v1/stats?span=30d&end=2025-10-15": "public, max-age=3600",
    "/v1/coverage": "public, max-age=60",
    "/v1/meta": "public, max-age=3600",
    "/healthz": "no-store",
}


@pytest.mark.parametrize("path", list(ROUTES))
def test_every_route_answers_with_the_envelope_and_its_cache_control(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert_envelope(r)
    assert r.headers["cache-control"] == ROUTES[path]
    assert r.headers["content-type"] == "application/json"
    assert r.headers["etag"].startswith('"') and r.headers["etag"].endswith('"')
    assert r.headers["vary"] == "Accept-Encoding"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_status(client):
    data = client.get("/v1/status").json()["data"]
    want = tfl.current(TFL_SAMPLE)
    assert data["lines"] == want["lines"] and (data["total"], data["disrupted"]) == (want["total"], want["disrupted"])
    assert data["last_poll"] == iso(NOW) and data["stale"] is False
    opened = open_episodes()
    assert [(e["line"], e["start"]) for e in data["open"]] == [(e["line"], iso(e["start"])) for e in opened]
    assert all(e["end"] is None and e["source"] == "a" for e in data["open"])
    assert {e["cause"] for e in data["open"]} <= set(views.CAUSE_LABELS) | {None}


def test_changes(client):
    since = NOW - 3600
    data = client.get(f"/v1/changes?since={since}").json()["data"]
    assert data["cursor"] == NOW and data["last_poll"] == iso(NOW)
    assert data["polls"] == list(range(since + 60, NOW + 1, 60))
    assert all(e["end"] >= iso(since) for e in data["closed"])
    assert data["status"]["total"] == 19 and data["open"]


def test_changes_never_move_the_cursor_past_a_poll_they_did_not_send():
    """Mid-day, two consumers: one request reads the state, another (25 s later) today's poll times, a poll lands,
    and the next request (6 s later) brings a newer state. Its answer must carry that poll."""
    store = fixture_store()
    app, wall, mono = make_app(store)
    with TestClient(app) as c:
        c.get("/v1/status")  # the state is read
        mono.advance(25)
        data = c.get(f"/v1/changes?since={NOW - 60}").json()["data"]  # today's poll times are read
        assert (data["cursor"], data["polls"]) == (NOW, [NOW])
        seen = list(data["polls"])
        poll(store, NOW + 60, open_episodes())
        wall.advance(60)
        mono.advance(6)  # the state is due again; the poll times were read 6 s ago
        for _ in range(4):
            data = c.get(f"/v1/changes?since={data['cursor']}").json()["data"]
            assert all(t <= data["cursor"] for t in data["polls"])
            seen += data["polls"]
            mono.advance(31)
            wall.advance(31)
    assert seen == [NOW, NOW + 60]  # each poll once


def test_changes_keep_the_cursor_while_a_month_cannot_be_read():
    """The poll at NOW + 60 closes an episode (writing it into its month); a refresh then reads the newer state but
    not that month. The answer keeps the cursor where it was, so the next one, once the month is read, sends the
    episode."""
    from tests.api.test_readmodel import Counting, close_one

    inner = fixture_store()
    store = Counting(inner)
    app, wall, mono = make_app(store)
    with TestClient(app) as c:
        cursor = c.get(f"/v1/changes?since={NOW - 60}").json()["data"]["cursor"]
        assert cursor == NOW
        ep = close_one(inner, NOW + 60)
        poll(inner, NOW + 120, list(inner.get_state().open))
        wall.advance(125)
        mono.advance(125)
        store.down = {"get_month"}
        r = c.get(f"/v1/changes?since={cursor}")
        data = r.json()["data"]
        assert r.status_code == 200 and data["stale"] is True and r.headers["cache-control"] == "public, max-age=15"
        assert data["cursor"] == NOW and data["closed"] == [] and data["polls"] == []
        store.down = set()
        mono.advance(31)
        wall.advance(31)
        data = c.get(f"/v1/changes?since={data['cursor']}").json()["data"]
    assert [(e["line"], e["start"], e["end"]) for e in data["closed"]] == [
        (ep["line"], iso(ep["start"]), iso(NOW + 60))
    ]
    assert data["cursor"] == NOW + 120 and data["polls"] == [NOW + 60, NOW + 120] and data["stale"] is False


def test_changes_errors(client):
    r = client.get(f"/v1/changes?since={NOW - 36 * 86400}")
    assert r.status_code == 410
    assert assert_envelope(r, error=True)["error"]["code"] == "cursor_expired"
    for query in ("", "?since=abc"):
        r = client.get(f"/v1/changes{query}")
        assert r.status_code == 400, query
        assert_envelope(r, error=True)


def test_lines(client):
    lines = client.get("/v1/lines").json()["data"]["lines"]
    ids = [x["id"] for x in lines]
    assert set(ids) == set(live_lines()) and "circle-hammersmith-city" not in ids  # FOI is not published
    assert ids.index("central") < ids.index("elizabeth") < ids.index("mildmay") < ids.index("dlr")
    central = next(x for x in lines if x["id"] == "central")
    assert central == {
        "id": "central",
        "name": "Central",
        "mode": "tube",
        "shared_with": [],
        "first_seen": iso(fx.LIVE_FROM),
        "last_seen": iso(NOW),
    }


def test_line(client):
    data = client.get("/v1/lines/central").json()["data"]
    assert (data["id"], data["name"], data["mode"], data["source"]) == ("central", "Central", "tube", "a")
    assert list(data["stats"]) == ["24h", "7d", "30d", "90d"] and len(data["starts_by_hour"]) == 24
    assert client.get("/v1/lines/CENTRAL").json()["data"]["id"] == "central"
    r = client.get("/v1/lines/nope")
    assert r.status_code == 404 and assert_envelope(r, error=True)["error"]["code"] == "unknown_line"
    r = client.get("/v1/lines/not_a_line!")
    assert r.status_code == 400 and assert_envelope(r, error=True)["error"]["code"] == "bad_line"


def test_months_index(client):
    data = client.get("/v1/months").json()["data"]
    assert list(data) == ["months", "first", "lines", "categories", "causes"]
    assert data["months"] == ["2025-06", "2025-07", "2025-08", "2025-09", "2025-10", "2025-11"]
    assert data["first"] == iso(fx.LIVE_FROM)
    assert data["categories"] == ["delays", "suspended", "closure", "other", "info"]
    assert data["causes"] == [{"code": c, "label": label} for c, label, _ in CAUSES]
    assert all(set(x) == {"id", "name", "mode", "shared_with"} for x in data["lines"])


def test_month(client):
    data = client.get("/v1/months/2025-11").json()["data"]
    assert set(data) == {"month", "episodes", "watched"} and data["month"] == "2025-11"
    assert set(data["watched"]) == {"a"}
    ongoing = [r for r in data["episodes"] if r[2] is None]
    assert {(r[0], r[1]) for r in ongoing} == {(e["line"], e["start"]) for e in open_episodes()}
    assert all(len(r) == 11 and r[6] == "a" for r in data["episodes"])
    assert [r[1] for r in data["episodes"]] == sorted(r[1] for r in data["episodes"])


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/v1/months/2025-13", 400, "bad_month"),
        ("/v1/months/2099-01", 404, "unknown_month"),
        ("/v1/months/2024-01", 404, "unknown_month"),  # FOI only, not published
        ("/v1/disruptions?from=2024-10-01&to=2025-11-01", 400, "range_too_long"),
        ("/v1/disruptions?line=nope", 404, "unknown_line"),
        ("/v1/disruptions?limit=501", 400, "bad_limit"),
        ("/v1/disruptions?source=f", 400, "bad_source"),
        ("/v1/disruptions?line=central&line=district", 400, "repeated_parameter"),
        ("/v1/stats?span=2d&end=now", 400, "bad_period"),
        ("/v1/stats?span=7d&end=2099-01-01", 400, "bad_period"),
        ("/v1/stats?span=7d&line=nope", 404, "unknown_line"),
        ("/v1/coverage?from=2025-01-01", 400, "range_too_long"),
        ("/v1/nothing-here", 404, "not_found"),
    ],
)
def test_errors_carry_the_envelope(client, path, status, code):
    r = client.get(path)
    assert r.status_code == status, r.text
    assert assert_envelope(r, error=True)["error"]["code"] == code
    assert r.headers["cache-control"] == "no-store"


def test_disruptions(client):
    data = client.get("/v1/disruptions?line=central&category=delays&planned=false&limit=5").json()["data"]
    assert data["count"] == 5 and data["total"] >= 5 and data["next_cursor"]
    assert all(e["line"] == "central" and e["category"] == "delays" and not e["planned"] for e in data["episodes"])
    second = client.get(
        f"/v1/disruptions?line=central&category=delays&planned=false&limit=5&cursor={data['next_cursor']}"
    )
    assert second.json()["data"]["episodes"][0]["start"] >= data["episodes"][-1]["start"]


def test_stats(client):
    data = client.get("/v1/stats?span=7d&end=now").json()["data"]
    assert data["span"] == "7d" and data["end"] == "now" and data["line"] is None
    assert data["watched"]["f"] == 0 and data["watched"]["a"] > 0.9
    assert data["period"]["label"].startswith("the last 7 days")
    one = client.get("/v1/stats?span=7d&end=now&line=central").json()["data"]
    assert one["line"] == "central" and [x["id"] for x in one["lines"]] == ["central"]
    assert client.get("/v1/stats").json()["data"]["span"] == "7d"


def test_coverage(client):
    data = client.get(f"/v1/coverage?from={NOW - 7200}&to={NOW + 60}").json()["data"]
    assert set(data["sources"]) == {"a"}
    day = data["days"][-1]
    assert day["date"] == "2025-11-03" and day["polls"] == POLLS  # 121 slots in [from, to), one failed
    assert day["failed"] == [
        {"slot": (NOW - 7200) // 60 + FAILED, "at": iso(NOW - 7200 + FAILED * 60), "code": "http_503"}
    ]


def test_coverage_costs_no_reads_outside_the_recording():
    from tests.api.test_readmodel import Counting

    store = Counting(fixture_store())
    app, _, mono = make_app(store)
    d90 = 90 * 86400
    with TestClient(app) as c:
        for a in range(946684800, 946684800 + 20 * d90, d90):  # twenty 90-day windows from 2000
            assert c.get(f"/v1/coverage?from={a}&to={a + d90}").status_code == 200
        for _ in range(20):  # a window in 2096, again and again
            mono.advance(31)
            assert c.get(f"/v1/coverage?from=4000000000&to={4000000000 + d90}").status_code == 200
        assert store.calls["get_polls"] == 0
        for query in ("from=0&to=1", "from=99999999999", f"to={4133980800}"):  # before 2000 or after 2100
            r = c.get(f"/v1/coverage?{query}")
            assert r.status_code == 400 and r.json()["error"]["code"] == "bad_time", query


def test_coverage_answers_503_with_retry_after_when_too_many_days_were_read(monkeypatch):
    from tifa.api import readmodel

    monkeypatch.setattr(readmodel, "DAY_READS_BURST", 10)
    monkeypatch.setattr(readmodel, "DAY_READS_PER_S", 0)
    app, _, _ = make_app()
    with TestClient(app) as c:
        r = c.get(f"/v1/coverage?from={NOW - 60 * 86400}")
        assert r.status_code == 503 and r.headers["retry-after"] == "60" and r.headers["cache-control"] == "no-store"
        assert assert_envelope(r, error=True)["error"]["code"] == "over_capacity"
        assert c.get(f"/v1/coverage?from={NOW - 3600}").status_code == 200  # today costs no budget


def test_meta(client):
    data = client.get("/v1/meta").json()["data"]
    assert data["attribution"] == ATTRIBUTION and data["notice"] == NOTICE
    assert [s["id"] for s in data["sources"]] == ["a"]
    assert data["fair_use"]["per_ip"]["costs"] == {"cached": 1, "uncached": 5, "mcp_tool_call": 3}
    assert data["causes"] == [{"code": c, "label": label} for c, label, _ in CAUSES]


def test_root_docs_and_openapi(client):
    r = client.get("/")
    assert r.status_code == 200 and assert_envelope(r)["data"]["links"]["docs"] == "/docs"
    r = client.get("/docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "/openapi.json" in r.text
    # Swagger UI at an exact version; no third-party favicon, no validator badge
    assert r.text.count("https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14/") == 2
    assert "swagger-ui-dist@5/" not in r.text and "tiangolo" not in r.text and '"validatorUrl": null' in r.text
    assert 'href="data:,"' in r.text
    r = client.get("/openapi.json")
    schema = r.json()
    assert r.status_code == 200 and schema["info"]["title"] == "Tifa" and ATTRIBUTION in schema["info"]["description"]
    paths = set(schema["paths"])
    assert {
        "/v1/status",
        "/v1/changes",
        "/v1/lines",
        "/v1/lines/{line_id}",
        "/v1/months",
        "/v1/months/{month}",
    } <= paths
    assert {"/v1/disruptions", "/v1/stats", "/v1/coverage", "/v1/meta", "/healthz"} <= paths
    assert (
        r.headers["etag"]
        and client.get("/openapi.json", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    )


def test_openapi_snapshot_matches_the_live_schema():
    """docs/openapi.json is the committed schema. After changing a route, regenerate it with
    uv run python -c 'import json, sys; from tifa.api.app import app; json.dump(app.openapi(), sys.stdout, indent=2,
    sort_keys=True); print()' > docs/openapi.json
    (the same command CI diffs against)."""
    from tifa.api.app import app

    live = json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"
    committed = (ROOT / "docs" / "openapi.json").read_text(encoding="utf-8")
    assert live == committed


# ---- methods, CORS and HEAD -------------------------------------------------------------------------------------


def test_cors_preflight(client):
    r = client.options("/v1/status", headers={"Origin": "https://example.org", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["access-control-allow-methods"] == "GET, HEAD, OPTIONS"
    assert "If-None-Match" in r.headers["access-control-allow-headers"]
    r = client.get("/v1/status", headers={"Origin": "https://example.org"})
    assert r.headers["access-control-allow-origin"] == "*" and "ETag" in r.headers["access-control-expose-headers"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_only_reads_are_allowed(client, method):
    r = client.request(method, "/v1/status")
    assert r.status_code == 405 and r.headers["allow"] == "GET, HEAD, OPTIONS"
    assert assert_envelope(r, error=True)["error"]["code"] == "method_not_allowed"


def test_head_has_the_headers_and_no_body(client):
    got = client.get("/v1/meta", headers={"Accept-Encoding": "identity"})
    r = client.head("/v1/meta", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200 and r.content == b""
    assert r.headers["etag"] == got.headers["etag"] and r.headers["content-length"] == str(len(got.content))


# ---- health and stale data --------------------------------------------------------------------------------------


def test_healthz(shared_store):
    app, wall, mono = make_app(shared_store)
    shared_store.put_guard({"schema": 1, "mode": "enforce", "heartbeat": NOW - 90, "closed_until": None})
    with TestClient(app) as c:
        data = c.get("/healthz").json()["data"]
        assert data["status"] == "ok" and data["last_poll_age_s"] == 0 and data["tfl_auth"] == "ok"
        assert data["guard"] == {
            "heartbeat": iso(NOW - 90),
            "heartbeat_age_s": 90,
            "closed": False,
            "closed_until": None,
            "mode": "enforce",
        }
        wall.advance(601)
        r = c.get("/healthz")
        assert r.status_code == 503 and r.headers["cache-control"] == "no-store"
        assert assert_envelope(r)["data"]["status"] == "stale"


def test_healthz_before_any_poll():
    app, _, _ = make_app(MemoryStore())
    with TestClient(app) as c:
        r = c.get("/healthz")
        assert r.status_code == 503 and r.json()["data"]["last_poll"] is None
        data = c.get("/v1/status").json()["data"]
        assert data == {"lines": [], "disrupted": 0, "total": 0, "open": [], "last_poll": None, "stale": True}
        assert c.get("/v1/months").json()["data"]["months"] == []


class FlakyStore:
    """A store whose reads can be made to fail."""

    def __init__(self, inner: MemoryStore) -> None:
        self.inner = inner
        self.down = False

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            if self.down:
                raise ConnectionError("store unavailable")
            return attr(*args, **kwargs)

        return call


def test_store_errors_serve_the_last_good_data_marked_stale():
    store = FlakyStore(fixture_store())
    app, wall, mono = make_app(store)
    with TestClient(app) as c:
        before = c.get("/v1/status").json()
        assert before["data"]["stale"] is False and before["meta"]["stale"] is False
        store.down = True
        mono.advance(31)
        r = c.get("/v1/status")
        assert r.status_code == 200 and r.json()["data"]["lines"] == before["data"]["lines"]
        assert r.json()["data"]["stale"] is True and r.json()["meta"]["stale"] is True
        assert r.headers["cache-control"] == "public, max-age=15"
        health = c.get("/healthz").json()["data"]
        assert health["store"] == "error" and health["status"] == "ok"  # the last poll is still recent
        assert c.get("/v1/months/2025-10").status_code == 200
        # nothing cached for these days: the store is needed
        r = c.get("/v1/coverage?from=2025-09-01&to=2025-09-08")
        assert r.status_code == 503 and assert_envelope(r, error=True)["error"]["code"] == "store_unavailable"
        store.down = False
        mono.advance(31)
        r = c.get("/v1/status").json()
        assert r["data"]["stale"] is False and c.get("/healthz").json()["data"]["store"] == "ok"


def test_internal_errors_carry_the_envelope(monkeypatch):
    app, _, _ = make_app()

    def broken(rm):
        raise RuntimeError("boom")

    monkeypatch.setattr(views, "meta_view", broken)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/v1/meta")
        assert r.status_code == 500 and "boom" not in r.text
        assert assert_envelope(r, error=True)["error"]["code"] == "internal"


# ---- the MCP mount ----------------------------------------------------------------------------------------------

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}


def tool_call(name: str, request_id: int = 2) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": {}}}


def test_mcp_is_mounted_with_its_lifespan():
    from tifa.api import mcp as tifa_mcp

    settings = load_settings({**ROOMY, "TIFA_IP_BUCKET": "10", "TIFA_ALLOWED_HOSTS": "testserver"})
    app, _, _ = make_app(settings=settings, mcp_factory=lambda rm: tifa_mcp.build_mcp_app(rm, settings=settings))
    with TestClient(app) as c:
        r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]
        assert r.headers["ratelimit-remaining"] == "9" and r.headers["access-control-allow-origin"] == "*"
        r = c.post("/mcp", json=tool_call("causes"), headers=MCP_HEADERS)
        assert r.status_code == 200 and r.headers["ratelimit-remaining"] == "6"  # a tool call costs 3
        assert r.json()["result"]["structuredContent"]["meta"]["attribution"] == ATTRIBUTION
        r = c.options("/mcp", headers={"Origin": "https://example.org", "Access-Control-Request-Method": "POST"})
        assert r.status_code == 204 and r.headers["access-control-allow-methods"] == "POST, OPTIONS"
        assert "Mcp-Protocol-Version" in r.headers["access-control-allow-headers"]
        r = c.post("/mcp", content=b"x" * (65 * 1024), headers=MCP_HEADERS)
        assert r.status_code == 413 and assert_envelope(r, error=True)["error"]["code"] == "too_large"
        assert c.get("/healthz").json()["data"]["mcp"] is True


def test_mcp_lifespan_shapes():
    """build_mcp_app may hand back a context manager, a function returning one, or a Starlette-style lifespan."""
    import contextlib

    entered = []

    async def ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})

    @contextlib.asynccontextmanager
    async def plain():
        entered.append("plain")
        yield

    @contextlib.asynccontextmanager
    async def starlette_style(app):
        entered.append(("app", app is ok))
        yield

    for lifespan in (plain, plain(), starlette_style, None):
        app, _, _ = make_app(mcp_factory=lambda rm, lf=lifespan: (ok, lf))
        with TestClient(app) as c:
            assert c.post("/mcp", json=INIT).status_code == 200
    assert entered == ["plain", "plain", ("app", True)]


def test_mcp_unavailable_without_a_server():
    app, _, _ = make_app(mcp_factory=None)
    with TestClient(app) as c:
        r = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 503 and assert_envelope(r, error=True)["error"]["code"] == "mcp_unavailable"
        r = c.put("/mcp", json=INIT)
        assert r.status_code == 405 and r.headers["allow"] == "POST, OPTIONS"
