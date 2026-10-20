"""The night October ends: what crosses a UTC month boundary, from the poller through the store to the API.

tests/integration/scenario.py has six hours from 21:00 UTC on 31 October to 03:00 UTC on 1 November, with an
episode that starts in October and ends in November, planned works still on at the end, and a TfL outage from
23:58 to 00:02 (under GAP) so that one poll extends the watched run across midnight. A sync client follows
/v1/changes every 7 minutes, as in test_sync.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.integration import scenario as sc
from tests.integration.conftest import BASE, RecordingStore, get, make_api, simulate
from tifa.core.timeutil import day_bounds, iso
from tifa.store.base import APPLIED

OCTOBER, NOVEMBER = "2026-10", "2026-11"
SYNC_EVERY = 7  # slots between the sync client's visits


@dataclass
class Night:
    store: RecordingStore
    polls: list[int]
    expected: sc.Expected
    failed: list[int]
    visits: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture(scope="module")
def night() -> Night:
    store, client = RecordingStore(), sc.FakeTfl(sc.ME_SPELLS)
    schedule = sc.month_end_attempts()
    polls = sc.poll_times(schedule)
    visits: list[dict[str, Any]] = []
    app, wall, mono = make_api(store, sc.ME_START)
    cursor = sc.ME_START - 60
    with TestClient(app, base_url=BASE) as http:

        def visit(attempt: sc.Attempt, result) -> None:
            nonlocal cursor
            if result.outcome != APPLIED:
                return
            if (attempt.slot - sc.ME_SLOTS[0]) % SYNC_EVERY and attempt.slot != sc.ME_SLOTS[-1]:
                return
            wall.t, mono.t = attempt.at + 5, mono.t + 60
            data = get(http, "/v1/changes", since=cursor)
            visits.append({"since": cursor, **data})
            cursor = data["cursor"]

        results = simulate(store, client, schedule, visit)
    assert [r.outcome for _, r, _ in results] == [a.expect for a in schedule]
    return Night(
        store=store,
        polls=polls,
        expected=sc.expected(polls, sc.ME_SPELLS),
        failed=[a.slot for a in schedule if a.expect == "failed"],
        visits=visits,
    )


@pytest.fixture(scope="module")
def api(night: Night):
    app, _, _ = make_api(night.store, sc.ME_NOW)
    with TestClient(app, base_url=BASE) as http:
        yield http


def rows(night: Night, month: str | None = None, *, with_open: bool = True) -> list[list]:
    """The expected month rows: every episode, or those overlapping a month (an open one runs to now)."""
    eps = night.expected.closed + (night.expected.open if with_open else [])
    out = [sc.month_row(ep) for ep in eps]
    if month is not None:
        a, b = (sc.ldn(1, 0), sc.MONTH_END) if month == OCTOBER else (sc.MONTH_END, sc.ldn(1, 0, month=12))
        out = [r for r in out if r[1] < b and (r[2] is None or r[2] > a)]
    return sorted(out, key=sc.row_order)


def test_the_night_has_what_the_scenario_says(night):
    assert [e["line"] for e in night.expected.closed] == ["northern", "central", "victoria"]
    assert [e["line"] for e in night.expected.open] == ["district"]
    (central,) = [e for e in night.expected.closed if e["line"] == "central"]
    assert central["start"] < sc.MONTH_END < central["end"]
    assert night.expected.runs == [[night.polls[0], night.polls[-1]]]  # the outage is under GAP
    before = max(t for t in night.polls if t < sc.MONTH_END)
    after = min(t for t in night.polls if t > sc.MONTH_END)
    assert sc.MONTH_END - before > 120 and after - sc.MONTH_END > 120  # one poll extends the run over midnight


# ---------------------------------------------------------------- the store


def test_each_month_holds_the_episodes_it_overlaps_and_its_part_of_the_run(night):
    store = night.store
    assert set(store.list_months()) == {OCTOBER, NOVEMBER}
    october, november = store.get_month(OCTOBER).payload, store.get_month(NOVEMBER).payload
    assert october["episodes"] == rows(night, OCTOBER, with_open=False)
    assert november["episodes"] == rows(night, NOVEMBER, with_open=False)
    assert [r[0] for r in october["episodes"]] == ["northern", "central"]
    assert [r[0] for r in november["episodes"]] == ["central", "victoria"]
    assert october["episodes"][1] == november["episodes"][0]  # one episode, the same row in both months
    # the watched run is split at the 1st: October's part ends at midnight and November's starts there
    assert october["watched"] == {"a": [[night.polls[0], sc.MONTH_END]], "f": []}
    assert november["watched"] == {"a": [[sc.MONTH_END, night.polls[-1]]], "f": []}
    state = store.get_state()
    assert set(state.months) == {OCTOBER, NOVEMBER} and state.run == night.expected.runs[-1]
    assert [e["line"] for e in state.open] == ["district"]


def test_the_poll_times_are_split_by_utc_day(night):
    october = night.store.get_polls("2026-10-31")
    november = night.store.get_polls("2026-11-01")
    assert october.at + november.at == night.polls
    assert all(t < sc.MONTH_END for t in october.at) and all(t >= sc.MONTH_END for t in november.at)
    assert [f["slot"] for f in october.failed] == sc.ME_OUTAGE[:2]  # 23:58 and 23:59
    assert [f["slot"] for f in november.failed] == sc.ME_OUTAGE[2:]  # 00:00 to 00:02


# ---------------------------------------------------------------- the API


def test_the_months_index_lists_both_months(night, api):
    data = get(api, "/v1/months")
    assert data["months"] == [OCTOBER, NOVEMBER] and data["first"] == iso(night.polls[0])


@pytest.mark.parametrize("month", [OCTOBER, NOVEMBER])
def test_each_month_payload_adds_the_open_episode(night, api, month):
    r = api.get(f"/v1/months/{month}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["episodes"] == rows(night, month)
    assert [r[0] for r in data["episodes"] if r[2] is None] == ["district"]
    part = [night.polls[0], sc.MONTH_END] if month == OCTOBER else [sc.MONTH_END, night.polls[-1]]
    assert data["watched"] == {"a": [part]}


def test_disruptions_across_the_month_end_give_each_episode_once(night, api):
    seen, cursor = [], None
    while True:
        params = {"from": "2026-10-31", "to": "2026-11-01", "limit": 1, **({"cursor": cursor} if cursor else {})}
        data = get(api, "/v1/disruptions", **params)
        assert data["total"] == 4
        seen += data["episodes"]
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert [(e["line"], e["start"]) for e in seen] == [(r[0], iso(r[1])) for r in rows(night)]
    (central,) = [e for e in seen if e["line"] == "central"]
    assert central["end"] == iso(night.expected.closed[1]["end"]) and central["end_estimated"] is False


def test_changes_across_the_month_end_give_each_episode_once(night, api):
    data = get(api, "/v1/changes", since=night.polls[0] - 1)
    assert [(e["line"], e["start"]) for e in data["closed"]] == [
        (e["line"], iso(e["start"])) for e in night.expected.closed
    ]
    assert [e["line"] for e in data["open"]] == ["district"]
    assert data["polls"] == night.polls and data["cursor"] == night.polls[-1]


def test_the_copy_gets_every_episode_once_across_the_month_end(night, api):
    closed: dict[tuple, dict] = {}
    for v in night.visits:
        assert all(v["since"] < t <= v["cursor"] for t in v["polls"])
        for e in v["closed"]:
            closed[(e["line"], e["severity"], e["planned"], e["start"])] = e
    history = get(api, "/v1/disruptions", **{"from": "2026-10-31", "to": "2026-11-01"})["episodes"]
    assert sorted(closed.values(), key=lambda e: (e["start"], e["line"])) == [e for e in history if e["end"]]
    assert [e["line"] for e in night.visits[-1]["open"]] == ["district"]


def test_the_copy_gets_every_poll_once_across_the_month_end(night):
    assert [t for v in night.visits for t in v["polls"]] == night.polls


def test_coverage_joins_the_run_across_the_month_end(night, api):
    data = get(api, "/v1/coverage", **{"from": "2026-10-31", "to": "2026-11-01"})
    assert data["sources"]["a"]["watched"] == [[iso(night.polls[0]), iso(night.polls[-1])]]
    days = {d["date"]: d for d in data["days"]}
    assert set(days) == {"2026-10-31", "2026-11-01"}
    assert days["2026-10-31"]["polls"] + days["2026-11-01"]["polls"] == len(night.polls)
    assert [f["slot"] for d in data["days"] for f in d["failed"]] == night.failed == sc.ME_OUTAGE


def test_the_days_figures_count_the_episode_across_midnight(api):
    october = get(api, "/v1/stats", span="1d", end="2026-10-31")
    assert (october["t0"], october["t1"]) == day_bounds("2026-10-31")
    assert (october["unplanned"], october["planned"]) == (2, 1)  # northern and central; the district works
    today = get(api, "/v1/stats", span="1d", end="2026-11-01")  # today's date means the 24 hours to now
    assert today["t1"] == sc.ME_NOW and today["t1"] - today["t0"] == sc.DAY
    assert (today["unplanned"], today["planned"]) == (3, 1)
    assert today == get(api, "/v1/stats", span="1d", end="now")
