"""What three days of minute polls leave in the store: episodes, months, polls, failed slots and snapshots.

Everything is checked against what tests/integration/scenario.py says TfL reported and when the polls happened,
worked out without tifa.core, so the poller, the core and the store are tested together.
"""

from __future__ import annotations

import collections

from tests.integration import scenario as sc
from tifa.core.episodes import GAP, advance, observe
from tifa.core.tfl import current
from tifa.core.timeutil import day_bounds, london, utc_day
from tifa.store.base import DUPLICATE

OCTOBER = "2026-10"


def closed_rows(world) -> list[list]:
    return sorted((sc.month_row(ep) for ep in world.expected.closed), key=sc.row_order)


def test_every_delivery_has_the_outcome_the_schedule_says(world):
    outcomes = collections.Counter((a.expect, r.outcome) for a, r, _ in world.results)
    assert set(outcomes) == {("applied", "applied"), ("failed", "failed"), ("duplicate", "duplicate")}, outcomes
    assert outcomes[("applied", "applied")] == len(sc.SLOTS) - 26  # 5 + 20 TfL failures and one error object
    for attempt, result, fetches in world.results:
        if attempt.expect == "duplicate":
            assert result.reason == "recorded" and fetches == 0  # TfL is not even asked
        elif attempt.expect == "failed":
            assert result.code == attempt.fail and result.http_status == 502 and fetches == 1
        else:
            assert result.poll_at == attempt.at and result.http_status == 200 and fetches == 1


def test_the_state_at_the_end(world):
    state = world.store.get_state()
    assert state.last_poll == world.polls[-1] and state.last_slot == sc.SLOTS[-1]
    assert state.run == world.expected.runs[-1]
    assert [(e["line"], e["severity"], e["planned"], e["start"], e["reason"]) for e in state.open] == [
        (e["line"], e["severity"], e["planned"], e["start"], e["reason"]) for e in world.expected.open
    ]
    assert [e["line"] for e in state.open] == ["bakerloo", "elizabeth"]
    assert set(state.lines) == {lid for lid, _, _ in sc.LINES}
    assert all(v["first_seen"] == world.polls[0] and v["last_seen"] == world.polls[-1] for v in state.lines.values())
    assert state.status == current(sc.payload(world.polls[-1]))
    assert state.tfl_auth == "ok" and state.months == {OCTOBER: {"rev": state.months[OCTOBER]["rev"], "n": 16}}


def test_the_month_holds_every_closed_episode_once_with_its_cause(world):
    doc = world.store.get_month(OCTOBER)
    assert world.store.list_months() == {OCTOBER: doc.rev}
    assert doc.payload["month"] == OCTOBER
    assert doc.payload["episodes"] == closed_rows(world)
    assert len(doc.payload["episodes"]) == 16
    assert doc.payload["watched"] == {"a": world.expected.runs, "f": []}


def test_the_watched_runs_bridge_short_gaps_and_break_at_long_ones(world):
    runs = world.expected.runs
    assert len(runs) == 2  # the 20-minute outage breaks the run; the 5-minute one and the lone failures do not
    gap = runs[1][0] - runs[0][1]
    assert gap > GAP and runs[0][1] < sc.LONG_OUTAGE[0] * 60 < sc.LONG_OUTAGE[-1] * 60 < runs[1][0]
    assert runs[0][0] == world.polls[0] == sc.START + sc.jitter(sc.START // 60)
    assert runs[1][1] == world.polls[-1]


def test_episodes_follow_the_gap_rule(world):
    rows = closed_rows(world)

    def of(line):
        return [r for r in rows if r[0] == line]

    # a five-minute outage does not break the Northern line's severe delays
    northern = of("northern")
    assert [(r[3], r[10]) for r in northern] == [(6, 0), (9, 0)]
    assert northern[0][2] == northern[1][1]  # minor delays start at the poll where severe delays end
    # the twenty-minute outage ends what was open at the last poll before it, marked estimated, and it starts
    # again at the first poll after
    last_before = max(t for t in world.polls if t < sc.LONG_OUTAGE[0] * 60)
    first_after = min(t for t in world.polls if t > sc.LONG_OUTAGE[-1] * 60)
    district = of("district")
    assert [(r[1], r[2], r[10]) for r in district] == [
        (world.polls[0], last_before, 1),
        (first_after, min(t for t in world.polls if t >= sc.ldn(26, 4, 30)), 0),
    ]
    piccadilly = of("piccadilly")
    assert [(r[2], r[10]) for r in piccadilly][0] == (last_before, 1) and piccadilly[1][1] == first_after
    assert [r[4] for r in piccadilly] == ["info", "info"]
    # one episode for Central although TfL rewrote its reason: the latest one is kept
    (central,) = of("central")
    assert central[9] == "Minor delays while we fix a signal failure at Liverpool Street."
    # Service Closed is never an episode; works announced for later are not one either
    assert of("waterloo-city") == []
    assert [r[3] for r in of("metropolitan")] == [5, 9]


def test_episodes_across_the_clock_change(world):
    rows = {r[0]: r for r in closed_rows(world) if r[0] in ("jubilee", "hammersmith-city")}
    jubilee, hc = rows["jubilee"], rows["hammersmith-city"]
    assert jubilee[2] - jubilee[1] == 110 * 60 + sc.jitter(jubilee[2] // 60) - sc.jitter(jubilee[1] // 60)
    assert (london(jubilee[1]).strftime("%H:%M %Z"), london(jubilee[2]).strftime("%H:%M %Z")) == (
        "00:50 BST",
        "01:40 GMT",
    )
    # starts in the first 01:20 and ends in the second 01:10: earlier on the clock, 50 minutes later
    assert (london(hc[1]).strftime("%H:%M %Z"), london(hc[2]).strftime("%H:%M %Z")) == ("01:20 BST", "01:10 GMT")
    assert 50 * 60 - 10 < hc[2] - hc[1] < 50 * 60 + 10


def test_no_slot_is_missing_or_recorded_twice_across_the_clock_change(world):
    at, failed = [], []
    for day in ("2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"):
        doc = world.store.get_polls(day)
        assert doc.n == len(doc.at) == len(set(doc.at))  # no poll counted twice
        assert all(utc_day(t) == day for t in doc.at)
        at += doc.at
        failed += doc.failed
    assert world.store.get_polls("2026-10-27") is None
    assert sorted(at) == world.polls
    slots = [t // 60 for t in at]
    assert len(slots) == len(set(slots))  # one poll per slot, the retry within a minute included
    assert set(slots) | {f["slot"] for f in failed} == set(sc.SLOTS)  # every minute polled or recorded as failed
    assert sorted(failed, key=lambda f: f["slot"]) == world.failed
    assert {f["code"] for f in failed} == {"http_503", "timeout", "missing_lines", "not_list"}
    # the London hour 01:00-02:00 on 25 October happens twice: 120 polls, one each UTC minute
    repeated = [t for t in at if sc.ldn(25, 1) <= t < sc.ldn(25, 2)]
    assert len(repeated) == 120 and len({t // 60 for t in repeated}) == 120
    assert sorted({london(t).strftime("%Z") for t in repeated}) == ["BST", "GMT"]


def test_london_days_have_23_24_or_25_hours_of_polls(world):
    hours = {}
    for day in sc.DAYS:
        a, b = day_bounds(day)
        hours[day] = (b - a) // 3600
        polls = [t for t in world.polls if a <= t < b]
        failed = [s for s in (f["slot"] for f in world.failed) if a <= s * 60 < b and s * 60 not in polls]
        assert len({t // 60 for t in polls}) + len(set(failed) - {t // 60 for t in polls}) == (b - a) // 60
    assert hours == {"2026-10-24": 24, "2026-10-25": 25, "2026-10-26": 24}


def test_snapshots_are_written_only_when_something_changed(world):
    expected, prev = [], None
    changed = set(world.expected.changed)
    for t in world.polls:
        status = current(sc.payload(t))
        if prev is None or status != prev or t in changed:
            expected.append(t)
        prev = status
    assert world.store.snapshot_times == expected
    assert len(expected) < len(world.polls) // 20  # most minutes nothing changes


def test_the_history_can_be_recomputed_from_the_snapshots_alone(world):
    """Each poll's TfL answer is the latest snapshot at or before it (snapshots are kept whenever the status
    changed), so the poll times and the snapshots give back every episode."""
    snaps = [(t, world.store.get_snapshot(t)) for t in world.store.snapshot_times]
    assert all(doc["schema"] == 1 for _, doc in snaps)
    state, closed, i = {"last_poll": None, "open": []}, [], -1
    for t in world.polls:
        while i + 1 < len(snaps) and snaps[i + 1][0] <= t:
            i += 1
        obs, meta = observe(snaps[i][1]["raw"])
        state, done, _ = advance(state, obs, meta, t)
        closed += done
    got = sorted((e["line"], e["start"], e["end"], e["severity"], e["reason"], e["end_estimated"]) for e in closed)
    want = sorted((r[0], r[1], r[2], r[3], r[9], bool(r[10])) for r in closed_rows(world))
    assert got == want
    assert [(e["line"], e["start"]) for e in state["open"]] == [(e["line"], e["start"]) for e in world.expected.open]


def test_a_late_or_repeated_delivery_changes_nothing(world):
    state = world.store.get_state()
    rev = world.store.list_months()
    fetches = world.client.fetches
    from tests.integration.conftest import poll

    for slot in (sc.CLOCKS_BACK // 60, sc.SLOTS[-1], sc.LONG_OUTAGE[3]):
        result, asked = poll(world.store, world.client, sc.Attempt(slot, sc.END + 30, None, "duplicate"))
        assert result.outcome == DUPLICATE and asked == 0
    assert world.store.get_state() == state and world.store.list_months() == rev
    assert world.client.fetches == fetches
