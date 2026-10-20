"""The views shared by the REST API and the MCP server, over the read model."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.api.test_routes import MONTHS, NOW, Clock, fixture_store, open_episodes, poll
from tifa.api import views
from tifa.api.readmodel import ReadModel
from tifa.api.views import ViewError
from tifa.config import Limits
from tifa.core import stats
from tifa.core.causes import CAUSES
from tifa.core.timeutil import DAY, epoch, iso, london_midnight
from tifa.store.base import PollWrite, State
from tifa.store.memory import MemoryStore
from tools import make_stats_fixtures as fx


class MultiDict(dict):
    """A query as web frameworks give it: getlist() returns every value."""

    def __init__(self, pairs):
        super().__init__()
        self.pairs = list(pairs)
        for k, v in self.pairs:
            self[k] = v

    def getlist(self, key):
        return [v for k, v in self.pairs if k == key]


@pytest.fixture(scope="module")
def store():
    return fixture_store()


def rm_of(store, sources=("a",), now=NOW, **kwargs) -> ReadModel:
    return ReadModel(store, public_sources=sources, clock=Clock(now), monotonic=Clock(0), **kwargs)


@pytest.fixture
def rm(store):
    return rm_of(store)


def error_of(fn, *args) -> tuple[int, str]:
    with pytest.raises(ViewError) as exc:
        fn(*args)
    return exc.value.status, exc.value.code


# ---- parameters --------------------------------------------------------------------------------------------


def test_view_error():
    e = ViewError(404, "unknown_line", "No such line.")
    assert (e.status, e.code, e.message, str(e)) == (404, "unknown_line", "No such line.", "No such line.")


def test_param():
    assert (
        views.param({"a": " x "}, "a") == "x" and views.param({}, "a") is None and views.param({"a": ""}, "a") is None
    )
    assert views.param({"a": ["x"]}, "a") == "x" and views.param({"a": True}, "a") == "true"
    assert views.param({"a": 100}, "a") == "100" and views.param({"a": None}, "a") is None
    assert error_of(views.param, MultiDict([("a", "1"), ("a", "2")]), "a") == (400, "repeated_parameter")
    assert error_of(views.param, {"a": ["1", "2"]}, "a") == (400, "repeated_parameter")
    assert error_of(views.param, {"a": "x" * 201}, "a") == (400, "bad_parameter")
    assert error_of(views.param, {"a": {"x": 1}}, "a") == (400, "bad_parameter")


def test_parse_time():
    assert views.parse_time("1762171200", "t") == 1762171200
    assert views.parse_time("2025-11-03T12:00:00Z", "t") == NOW
    assert views.parse_time("2025-11-03T12:00:00+00:00", "t") == NOW
    assert views.parse_time("2025-11-03T12:00:00 00:00", "t") == NOW  # an unencoded "+" arrives as a space
    assert views.parse_time("2025-11-03T13:00:00+01:00", "t") == NOW
    assert views.parse_time("2025-11-03T12:00:00", "t") == NOW  # no offset: UTC
    # a date is a London day: its start, or for an end its end (the clocks went back on 26 October)
    assert views.parse_time("2025-10-26", "t") == london_midnight(dt.date(2025, 10, 26)) == epoch("2025-10-25T23:00:00")
    assert views.parse_time("2025-10-26", "t", end=True) == epoch("2025-10-27T00:00:00")
    assert views.parse_time(None, "t") is None
    for bad in ("yesterday", "2025-02-30", "1999-12-31", "2025-13-01T00:00:00"):
        assert error_of(views.parse_time, bad, "t") == (400, "bad_time"), bad
    # epoch seconds are held to the same years as the other forms, 2000 to 2100
    assert views.parse_time(str(epoch("2000-01-01T00:00:00")), "t") == epoch("2000-01-01T00:00:00")
    assert views.parse_time(str(epoch("2101-01-01T00:00:00") - 1), "t") == epoch("2101-01-01T00:00:00") - 1
    for bad in ("0", "1", str(epoch("2000-01-01T00:00:00") - 1), str(epoch("2101-01-01T00:00:00")), "99999999999"):
        assert error_of(views.parse_time, bad, "t") == (400, "bad_time"), bad


# ---- status and changes --------------------------------------------------------------------------------------


def test_status_view_and_staleness(store):
    assert views.status_view(rm_of(store))["stale"] is False
    late = views.status_view(rm_of(store, now=NOW + 301))
    assert late["stale"] is True and late["last_poll"] == iso(NOW)
    assert all(e["minutes"] == stats.minutes(NOW + 301 - epoch(e["start"])) for e in late["open"])


@pytest.mark.parametrize("hours", [0.25, 6, 20])
def test_open_episodes_stop_at_the_last_poll_once_the_recording_has_a_gap(store, hours):
    """No poll for over GAP: the gap rule will close the open episodes at the last poll, so no view counts them on
    past it, however long the recording is down."""
    now = NOW + int(hours * 3600) + 1
    rm = rm_of(store, now=now)
    assert views.open_until(rm.snapshot(), now) == NOW
    status = views.status_view(rm)
    assert status["open"] and all(e["minutes"] == stats.minutes(NOW - epoch(e["start"])) for e in status["open"])
    same = [(e["line"], e["start"], e["minutes"]) for e in status["open"]]
    assert [(e["line"], e["start"], e["minutes"]) for e in views.status_view(rm_of(store))["open"]] == same
    shown = views.disruptions_view(rm, {"from": str(NOW - DAY), "to": str(now)})["episodes"]
    assert all(e["minutes"] == stats.minutes(NOW - epoch(e["start"])) for e in shown if e["end"] is None)
    snap, open_eps = fx.snapshot(MONTHS, NOW)
    public = {
        m: {**p, "episodes": [r for r in p["episodes"] if r[6] == "a"], "watched": {"a": p["watched"]["a"]}}
        for m, p in snap.items()
    }
    want = stats.line_report({"id": "central", "name": "Central", "mode": "tube"}, public, open_eps, NOW)
    got = views.line_view(rm, "central")  # as of the last poll
    assert got["now"] == want["now"] and got["stats"] == want["stats"]


def test_open_episodes_run_to_now_within_a_gap():
    store = fixture_store()
    assert views.open_until(rm_of(store, now=NOW + 900).snapshot(), NOW + 900) == NOW + 900
    assert views.open_until(rm_of(store, now=NOW + 901).snapshot(), NOW + 901) == NOW
    assert views.open_until(rm_of(MemoryStore(), now=NOW).snapshot(), NOW) == NOW  # no poll yet


def small_store(since: int) -> MemoryStore:
    """Polls at since - 60 and since, then a gap: the next poll (since + 1200) closed what was open at `since`, with
    its end estimated at `since`."""
    store = MemoryStore()
    rows = [
        ["central", since - 3600, since - 60, 9, "delays", 0, "a", "signal", "Minor Delays", "Signal failure", 0],
        ["central", since - 1800, since, 6, "delays", 0, "a", "train", "Severe Delays", "Faulty train", 1],
        ["district", since + 600, since + 1200, 2, "suspended", 0, "a", None, "Suspended", None, 0],
        ["district", since - 600, since + 1200, 9, "delays", 0, "f", None, "Minor Delays", None, 0],
    ]
    store.put_month(
        "2025-11", {"month": "2025-11", "episodes": rows, "watched": {"a": [[since - 3600, since]], "f": []}}
    )
    for t in (since - 60, since, since + 1200):
        state = State(
            last_poll=t,
            last_slot=t // 60,
            lines={"central": {"name": "Central", "mode": "tube"}, "district": {"name": "District", "mode": "tube"}},
            status={"lines": [], "disrupted": 0, "total": 0},
            updated_at=t,
        )
        write = PollWrite(state=state, poll_at=t)
        store.poll_transaction(t // 60, lambda current, w=write: w)
    return store


def test_changes_include_an_episode_closed_at_a_gap():
    since = NOW - 7200
    rm = rm_of(small_store(since))
    data = views.changes_view(rm, since)
    assert [(e["line"], e["end"], e["end_estimated"]) for e in data["closed"]] == [
        ("central", iso(since), True),  # ended at the cursor's own poll: still sent
        ("district", iso(since + 1200), False),
    ]
    assert all(e["source"] == "a" for e in data["closed"])  # FOI rows are not published
    assert data["polls"] == [since + 1200] and data["cursor"] == since + 1200
    assert data["last_poll"] == iso(since + 1200) and data["status"]["total"] == 0
    # from the latest cursor, only what ended at that poll comes again
    assert [e["end"] for e in views.changes_view(rm, since + 1200)["closed"]] == [iso(since + 1200)]
    assert views.changes_view(rm, since + 1201)["closed"] == []


def test_changes_window():
    rm = rm_of(fixture_store())
    assert views.changes_view(rm, NOW - 35 * DAY)["cursor"] == NOW
    assert error_of(views.changes_view, rm, NOW - 35 * DAY - 1) == (410, "cursor_expired")
    assert error_of(views.changes_view, rm, NOW + 2 * DAY) == (400, "bad_since")
    assert error_of(views.changes_view, rm, "soon") == (400, "bad_since")
    assert views.changes_view(rm, str(NOW - 60))["polls"] == [NOW]


def test_changes_send_the_polls_up_to_the_cursor_only():
    """A poll recorded after the snapshot was read is in the poll times read for it, but past its cursor: it comes
    with the next answer, once."""
    store, wall, mono = fixture_store(), Clock(NOW), Clock(0)
    rm = ReadModel(store, clock=wall, monotonic=mono)
    rm.snapshot()
    poll(store, NOW + 60, open_episodes())
    first = views.changes_view(rm, NOW - 120)
    assert first["polls"] == [NOW - 60, NOW] and first["cursor"] == NOW
    wall.advance(60)
    mono.advance(30)
    rm.refresh()
    second = views.changes_view(rm, first["cursor"])
    assert second["polls"] == [NOW + 60] and second["cursor"] == NOW + 60


# ---- lines and months --------------------------------------------------------------------------------------------


def test_line_view_is_the_line_report(rm, store):
    snap, open_eps = fx.snapshot(MONTHS, NOW)
    public = {
        m: {**p, "episodes": [r for r in p["episodes"] if r[6] == "a"], "watched": {"a": p["watched"]["a"]}}
        for m, p in snap.items()
    }
    want = stats.line_report({"id": "central", "name": "Central", "mode": "tube"}, public, open_eps, NOW)
    got = views.line_view(rm, "central")
    assert {k: v for k, v in got.items() if k not in ("shared_with", "source")} == want
    assert error_of(views.line_view, rm, "nope") == (404, "unknown_line")
    assert error_of(views.line_view, rm, "../etc") == (400, "bad_line")


def test_months_index_with_and_without_the_foi_release(store):
    live = views.months_index_view(rm_of(store))
    both = views.months_index_view(rm_of(store, ("a", "f")))
    assert (
        live["months"][0] == "2025-06"
        and both["months"][0] == "2024-01"
        and both["first"] == "2024-01-01T00:00:00+00:00"
    )
    shared = next(x for x in both["lines"] if x["id"] == "circle-hammersmith-city")
    assert shared["shared_with"] == ["circle", "hammersmith-city"]
    assert stats.parse_index(both)["lines"] == both["lines"]  # stats reads the index as served


def test_month_view_adds_the_open_episodes(rm):
    october = views.month_view(rm, "2025-10")
    opened = {(e["line"], e["start"]) for e in open_episodes() if e["start"] < epoch("2025-11-01T00:00:00")}
    assert opened and {(r[0], r[1]) for r in october["episodes"] if r[2] is None} == opened
    november = views.month_view(rm, "2025-11")
    stored = rm.snapshot().payload("2025-11")
    assert len(november["episodes"]) == len(stored["episodes"]) + len(open_episodes())
    assert november["watched"] == {"a": stored["watched"]["a"]}
    assert stored["episodes"] is not november["episodes"]  # the snapshot is never changed


def test_a_closed_copy_beats_an_open_one():
    store = fixture_store()
    ep = open_episodes()[0]
    month = "2025-11"
    doc = store.get_month(month)
    closed = [
        ep["line"],
        ep["start"],
        NOW,
        ep["severity"],
        ep["category"],
        int(ep["planned"]),
        "a",
        None,
        ep["status"],
        ep["reason"],
        0,
    ]
    store.put_month(month, {**doc.payload, "episodes": doc.payload["episodes"] + [closed]})
    rows = [r for r in views.month_view(rm_of(store), month)["episodes"] if (r[0], r[1]) == (ep["line"], ep["start"])]
    assert len(rows) == 1 and rows[0][2] == NOW


def test_month_view_errors(rm):
    assert error_of(views.month_view, rm, "2025-1") == (400, "bad_month")
    assert error_of(views.month_view, rm, "2025-12") == (404, "unknown_month")
    assert error_of(views.month_view, rm, "2024-06") == (404, "unknown_month")
    assert views.month_view(rm_of(fixture_store(), ("a", "f")), "2024-06")["watched"]["f"]


def test_the_current_month_before_it_is_stored():
    store = MemoryStore()
    t = epoch("2025-12-01T00:00:30")
    ep = {
        "line": "central",
        "name": "Central",
        "mode": "tube",
        "severity": 9,
        "status": "Minor Delays",
        "category": "delays",
        "planned": False,
        "start": t - 3600,
        "reason": "Minor Delays due to a signal failure.",
    }
    poll(store, t, [ep], now=t)
    rm = rm_of(store, now=t)
    # no month is stored yet; the episode began in November and is still open, so it is in both months
    for name in ("2025-11", "2025-12"):
        month = views.month_view(rm, name)
        assert [r[:3] for r in month["episodes"]] == [["central", t - 3600, None]] and month["watched"] == {"a": []}
        assert month["episodes"][0][7] == "signal"
    assert views.months_index_view(rm)["months"] == ["2025-11", "2025-12"]


# ---- disruptions ---------------------------------------------------------------------------------------------------


def test_disruptions_paging_returns_every_episode_once(rm):
    q = {"from": "2025-09-01", "to": "2025-10-31"}
    whole = views.disruptions_view(rm, {**q, "limit": "500"})
    assert whole["total"] == whole["count"] and whole["next_cursor"] is None
    seen, cursor, pages = [], None, 0
    while True:
        page = views.disruptions_view(rm, {**q, "limit": "7", **({"cursor": cursor} if cursor else {})})
        assert page["total"] == whole["total"] and page["count"] <= 7
        seen += page["episodes"]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == whole["episodes"] and pages == -(-whole["total"] // 7)
    starts = [e["start"] for e in seen]
    assert starts == sorted(starts)
    frm, to = epoch(whole["from"]), epoch(whole["to"])
    assert frm == london_midnight(dt.date(2025, 9, 1)) and to == london_midnight(dt.date(2025, 11, 1))
    assert all(epoch(e["start"]) < to and (e["end"] is None or epoch(e["end"]) > frm) for e in seen)


def test_disruptions_filters(rm):
    every = views.disruptions_view(rm, {"limit": "500"})["episodes"]
    assert every and all(e["source"] == "a" for e in every)
    assert any(e["end"] is None for e in every)  # open episodes are in
    for params, keep in (
        ({"line": "central,district"}, lambda e: e["line"] in ("central", "district")),
        ({"category": "suspended,closure"}, lambda e: e["category"] in ("suspended", "closure")),
        ({"planned": "true"}, lambda e: e["planned"]),
        ({"planned": "no"}, lambda e: not e["planned"]),
        ({"cause": "signal"}, lambda e: e["cause"] == "signal"),
        ({"cause": "unknown"}, lambda e: e["cause"] is None),
        ({"source": "a"}, lambda e: True),
    ):
        got = views.disruptions_view(rm, {**params, "limit": "500"})["episodes"]
        assert got == [e for e in every if keep(e)], params
        assert got or params == {"cause": "unknown"}
    default = views.disruptions_view(rm, {})
    assert (default["from"], default["to"], default["limit"]) == (iso(NOW - 30 * DAY), iso(NOW), 100)


@pytest.mark.parametrize(
    "params,code",
    [
        ({"from": "2025-11-03", "to": "2025-11-01"}, "bad_range"),
        ({"from": "2024-01-01", "to": "2025-01-02"}, "range_too_long"),
        ({"category": "late"}, "bad_category"),
        ({"cause": "aliens"}, "bad_cause"),
        ({"planned": "maybe"}, "bad_parameter"),
        ({"source": "f"}, "bad_source"),
        ({"limit": "0"}, "bad_limit"),
        ({"limit": "ten"}, "bad_limit"),
        ({"cursor": "not-a-cursor"}, "bad_cursor"),
        ({"cursor": "WzEsMiwzXQ"}, "bad_cursor"),  # valid base64 JSON, wrong shape
        ({"line": "nope"}, "unknown_line"),
        ({"line": ",".join(f"l{i}" for i in range(21))}, "bad_parameter"),
    ],
)
def test_disruptions_errors(rm, params, code):
    assert error_of(views.disruptions_view, rm, params)[1] == code


def test_366_days_is_allowed(rm):
    assert views.disruptions_view(rm, {"from": str(NOW - 366 * DAY), "to": str(NOW)})["to"] == iso(NOW)


def test_disruptions_with_the_foi_release(store):
    rm = rm_of(store, ("a", "f"))
    got = views.disruptions_view(rm, {"from": "2024-03-01", "to": "2024-03-31", "source": "f", "limit": "500"})
    assert got["episodes"] and {e["source"] for e in got["episodes"]} == {"f"}


# ---- stats -------------------------------------------------------------------------------------------------------


def expected_figures(rm, span, end):
    snap = rm.snapshot()
    months = {m: snap.parsed(m) for m in snap.months}
    idx = snap.idx(NOW)
    p = stats.period_of(span, end, NOW, idx["first"])
    data = stats.gather(months, idx, p["p0"], p["t1"], NOW, sources=snap.public_sources)
    return stats.period_figures(idx, data, p, NOW)


@pytest.mark.parametrize(
    "span,end", [("1d", "now"), ("7d", "now"), ("90d", "now"), ("30d", "2025-10-26"), ("365d", "now")]
)
def test_stats_view_is_period_figures(rm, span, end):
    got = views.stats_view(rm, {"span": span, "end": end})
    want = expected_figures(rm, span, "now" if end == "now" else dt.date.fromisoformat(end))
    assert {k: v for k, v in got.items() if k not in ("period", "line")} == want
    assert got["period"]["today"] == "2025-11-03"


def test_stats_reads_only_published_sources(store):
    live = views.stats_view(rm_of(store), {"span": "30d", "end": "2024-06-30"})
    both = views.stats_view(rm_of(store, ("a", "f")), {"span": "30d", "end": "2024-06-30"})
    assert live["watched"]["any"] == 0 and live["lines"] == []
    assert both["watched"]["f"] > 0.9 and both["lines"]


def test_stats_for_one_line(store):
    rm = rm_of(store, ("a", "f"))
    for line in ("central", "circle", "circle-hammersmith-city"):
        got = views.stats_view(rm, {"span": "30d", "end": "2024-06-30", "line": line})
        network = views.stats_view(rm, {"span": "30d", "end": "2024-06-30"})
        row = next((x for x in network["lines"] if x["id"] == line), None)
        assert got["line"] == line
        if row is None:  # Circle on its own was not watched by the FOI release
            assert got["lines"] == [] and line == "circle"
        else:
            assert got["lines"] == [row] and got["unplanned"] == row["unplanned"]


def test_stats_errors_and_defaults(rm):
    assert views.stats_view(rm, {})["span"] == "7d"
    assert error_of(views.stats_view, rm, {"span": "8d"}) == (400, "bad_period")
    assert error_of(views.stats_view, rm, {"span": "7d", "end": "2025-11-04"}) == (400, "bad_period")
    assert error_of(views.stats_view, rm, MultiDict([("span", "7d"), ("span", "1d")])) == (400, "repeated_parameter")
    assert views.stats_is_past({"end": "2025-11-02"}, NOW) and not views.stats_is_past({"end": "now"}, NOW)
    assert not views.stats_is_past({}, NOW) and not views.stats_is_past({"end": "2025-11-03"}, NOW)


# ---- coverage, meta, causes ------------------------------------------------------------------------------------


def test_coverage_view(rm):
    data = views.coverage_view(rm, {"from": "2025-10-25T18:00:00Z", "to": "2025-10-26T00:00:00Z"})
    # the live recording was off from 20:00 to 22:00 on 25 October
    assert data["gaps"] == [{"start": "2025-10-25T20:00:00+00:00", "end": "2025-10-25T22:00:00+00:00", "minutes": 120}]
    assert data["coverage_pct"] == data["sources"]["a"]["coverage_pct"] == round(100 * 4 / 6, 2)
    assert data["sources"]["a"]["watched"][0] == ["2025-10-25T18:00:00+00:00", "2025-10-25T20:00:00+00:00"]
    assert [d["date"] for d in data["days"]] == ["2025-10-25"] and data["days"][0]["polls"] == 0
    default = views.coverage_view(rm, {})
    assert (default["from"], default["to"]) == (iso(NOW - 7 * DAY), iso(NOW)) and len(default["days"]) == 8
    assert views.coverage_view(rm, {"from": str(NOW - 90 * DAY)})["from"] == iso(NOW - 90 * DAY)
    assert error_of(views.coverage_view, rm, {"from": str(NOW - 90 * DAY - 1)}) == (400, "range_too_long")


def test_coverage_reads_only_the_days_the_recording_can_have():
    """Days before the live recording began or after now hold no polls: they are answered without a store read,
    so neither old nor future windows cost anything."""
    from tests.api.test_readmodel import Counting

    store = Counting(fixture_store())
    rm = rm_of(store)
    for frm, to in (("2000-01-01", "2000-02-29"), ("2024-01-01", "2024-02-29"), ("2100-01-01", "2100-02-28")):
        data = views.coverage_view(rm, {"from": frm, "to": to})
        assert len(data["days"]) >= 59 and all(d["polls"] == 0 and d["failed"] == [] for d in data["days"])
    assert store.calls["get_polls"] == 0
    snap = rm.snapshot()
    assert snap.polls_from == fx.LIVE_FROM  # the live recording's first poll
    data = views.coverage_view(rm, {"from": str(fx.LIVE_FROM - 5 * DAY), "to": str(fx.LIVE_FROM + 2 * DAY)})
    assert len(data["days"]) == 8 and store.calls["get_polls"] == 3  # 1 to 3 June: from the recording's first day
    future = views.coverage_view(rm, {"from": str(NOW - DAY), "to": str(NOW + 30 * DAY)})
    assert future["days"][-1]["polls"] == 0 and store.calls["get_polls"] <= 5  # yesterday and today only
    # today's polls in [now - 7 days, now), from the kept copy: two hours, one slot failed, the poll at now outside
    assert views.coverage_view(rm, {})["days"][-1]["polls"] == 119


def test_coverage_when_too_many_days_were_read():
    rm = rm_of(fixture_store(), day_reads=(5, 0))
    with pytest.raises(ViewError) as exc:
        views.coverage_view(rm, {"from": str(NOW - 60 * DAY)})
    assert (exc.value.status, exc.value.code, exc.value.retry_after) == (503, "over_capacity", 60)
    kept = views.coverage_view(rm, {"from": str(NOW - 60 * DAY), "to": str(NOW - 57 * DAY)})  # the days read
    assert [d["polls"] for d in kept["days"]] == [0, 0, 0, 0]
    assert views.coverage_view(rm, {"from": str(NOW - 3600)})["days"][0]["date"] == "2025-11-03"  # today is free
    # sync answers read at most 36 days, so the budget never stops them
    assert views.changes_view(rm, NOW - 20 * DAY)["polls"][-1] == NOW


def test_meta_and_causes(store):
    rm = rm_of(store, limits=Limits(ip_bucket=7))
    meta = views.meta_view(rm)
    assert meta["fair_use"]["per_ip"]["bucket"] == 7 and [s["id"] for s in meta["sources"]] == ["a"]
    assert meta["fair_use"]["per_ip"]["egress_bytes_per_hour"] == 10 * 1024 * 1024 // 8
    assert [c["id"] for c in meta["categories"]] == ["delays", "suspended", "closure", "other", "info"]
    causes = views.causes_view(rm)
    assert causes["causes"] == [{"code": c, "label": label} for c, label, _ in CAUSES] and causes["signature"]
