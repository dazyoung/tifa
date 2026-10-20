"""tifa.poller.poll.run_poll: one poll, one transaction; idempotent per slot; failures recorded; months touched
only where a poll changed them."""

import copy
import threading

import pytest

from tests.poller.helpers import LINES, FakeTfl, payload, sample_payload, status, utc
from tifa.core.causes import CAUSES_SIG
from tifa.core.months import new_month
from tifa.core.tfl import current
from tifa.core.timeutil import day_bounds, month_bounds
from tifa.poller import poll as pl
from tifa.poller.poll import run_poll, snapshot_payload
from tifa.poller.tfl_client import FetchResult, TflError
from tifa.store import codec
from tifa.store.base import PollWrite, State
from tifa.store.memory import MemoryStore

T0 = utc(2026, 10, 19, 8, 0)  # a Monday, 09:00 in London
SLOT = T0 // 60
DELAYS = {"central": [status(9, "Central Line: Minor delays due to an earlier signal failure at Bank.")]}
SEVERE = {"central": [status(6, "Central Line: Severe delays due to an earlier signal failure at Bank.")]}


def poll_at(store, client, ts, slot=None):
    return run_poll(store, client, ts // 60 if slot is None else slot, ts)


def series(store, client, start, minutes, step=60):
    """Poll every `step` seconds for `minutes` polls; returns the results."""
    return [poll_at(store, client, start + i * step) for i in range(minutes)]


# ---- a first poll and the state it leaves --------------------------------------------------------------------


def test_first_poll_records_state_status_lines_and_a_snapshot(store):
    raw = payload(DELAYS)
    result = run_poll(store, FakeTfl(raw), SLOT, T0 + 4)
    assert (result.outcome, result.http_status, result.slot) == ("applied", 200, SLOT)
    assert (result.open, result.opened, result.closed, result.snapshot) == (1, 1, 0, True)
    assert result.months == ()  # a run of one poll has watched nothing yet, and nothing has closed

    state = store.get_state()
    assert (state.last_poll, state.last_slot, state.run) == (T0 + 4, SLOT, [T0 + 4, T0 + 4])
    assert state.status == current(raw)
    assert state.status_etag == codec.digest(current(raw))
    assert state.tfl_auth == "ok" and state.causes_sig == CAUSES_SIG and state.updated_at == T0 + 4
    assert set(state.lines) == {line_id for line_id, _, _ in LINES}
    assert state.lines["elizabeth"] == {
        "name": "Elizabeth line",
        "mode": "elizabeth-line",
        "first_seen": T0 + 4,
        "last_seen": T0 + 4,
    }
    (ep,) = state.open
    assert (ep["line"], ep["severity"], ep["planned"], ep["start"]) == ("central", 9, False, T0 + 4)
    assert ep["reason"] == "Minor delays due to an earlier signal failure at Bank."

    polls = store.get_polls("2026-10-19")
    assert (polls.at, polls.n, polls.failed) == ([T0 + 4], 1, [])
    snap = store.get_snapshot(T0 + 4)
    assert snap == snapshot_payload(raw)
    assert snap["schema"] == 1


def test_snapshots_keep_only_what_observe_and_current_read():
    raw = sample_payload()
    snap = snapshot_payload(raw)
    central = next(line for line in snap["raw"] if line["id"] == "central")
    assert set(central) == {"id", "name", "modeName", "lineStatuses"}
    (s,) = central["lineStatuses"]
    assert set(s) == {"statusSeverity", "statusSeverityDescription", "reason", "validityPeriods", "disruption"}
    assert s["disruption"] == {"category": "RealTime"}
    assert s["validityPeriods"] == [
        {"fromDate": "2026-10-19T13:53:11Z", "toDate": "2026-10-20T00:29:00Z", "isNow": True}
    ]
    # what history is computed from is unchanged by the slimming
    from tifa.core.episodes import observe

    assert observe(snap["raw"]) == observe(raw)
    assert current(snap["raw"]) == current(raw)


def test_a_real_payload_is_recorded(store):
    result = run_poll(store, FakeTfl(sample_payload()), SLOT, T0)
    assert result.outcome == "applied" and result.lines == 19
    assert [e["line"] for e in store.get_state().open] == ["central"]


def test_the_watched_run_grows_and_the_month_records_it(store):
    client = FakeTfl(payload())
    poll_at(store, client, T0)
    result = poll_at(store, client, T0 + 60)
    assert result.months == ("2026-10",) and not result.snapshot  # nothing changed: no snapshot
    month = store.get_month("2026-10")
    assert month.payload["watched"] == {"a": [[T0, T0 + 60]], "f": []}
    assert month.payload["episodes"] == []
    assert store.get_state().run == [T0, T0 + 60]
    assert store.get_snapshot(T0 + 60) is None
    poll_at(store, client, T0 + 120)
    assert store.get_month("2026-10").payload["watched"]["a"] == [[T0, T0 + 120]]
    assert store.get_month("2026-10").rev == 2


def test_an_episode_opens_continues_and_closes_into_its_month(store):
    client = FakeTfl(payload(), payload(DELAYS), payload(DELAYS), payload(DELAYS), payload())
    results = series(store, client, T0, 5)
    assert [(r.opened, r.closed, r.open) for r in results] == [(0, 0, 0), (1, 0, 1), (0, 0, 1), (0, 0, 1), (0, 1, 0)]
    assert [r.snapshot for r in results] == [True, True, False, False, True]
    rows = store.get_month("2026-10").payload["episodes"]
    assert rows == [
        [
            "central",
            T0 + 60,
            T0 + 240,
            9,
            "delays",
            0,
            "a",
            "signal",
            "Minor Delays",
            "Minor delays due to an earlier signal failure at Bank.",
            0,
        ]
    ]
    assert store.get_state().open == []


def test_a_change_of_reason_is_a_snapshot_but_the_same_episode(store):
    other = {"central": [status(9, "Central Line: Minor delays due to train cancellations.")]}
    client = FakeTfl(payload(DELAYS), payload(other))
    first, second = series(store, client, T0, 2)
    assert second.snapshot and (second.opened, second.closed) == (0, 0)
    assert store.get_state().open[0]["reason"] == "Minor delays due to train cancellations."


def test_planned_works_coming_into_effect_are_a_snapshot(store):
    later = {"district": [status(4, "Planned closure tonight.", planned=True, now=False)]}
    tonight = {"district": [status(4, "Planned closure tonight.", planned=True, now=True)]}
    client = FakeTfl(payload(later), payload(later), payload(tonight))
    results = series(store, client, T0, 3)
    # current() lists the closure all along, so only the episode opening makes the third poll a snapshot
    assert current(payload(later)) == current(payload(tonight))
    assert [r.snapshot for r in results] == [True, False, True]
    assert results[2].opened == 1


def test_lines_seen_keep_their_first_sighting(store):
    client = FakeTfl(payload())
    poll_at(store, client, T0)
    poll_at(store, client, T0 + 60)
    line = store.get_state().lines["central"]
    assert (line["first_seen"], line["last_seen"]) == (T0, T0 + 60)


def test_auth_state_is_recorded(store):
    poll_at(store, FakeTfl(payload(), auth="degraded"), T0)
    assert store.get_state().tfl_auth == "degraded"
    poll_at(store, FakeTfl(payload(), auth="anonymous"), T0 + 60)
    assert store.get_state().tfl_auth == "anonymous"


# ---- idempotency ---------------------------------------------------------------------------------------------


def test_the_same_slot_twice_is_a_no_op_without_asking_tfl(store):
    client = FakeTfl(payload(DELAYS))
    assert run_poll(store, client, SLOT, T0 + 2).outcome == "applied"
    again = run_poll(store, client, SLOT, T0 + 14)  # Scheduler's retry of the same run
    assert (again.outcome, again.reason, again.http_status) == ("duplicate", "recorded", 200)
    assert client.calls == 1
    assert store.get_polls("2026-10-19").n == 1
    assert store.get_state().last_poll == T0 + 2


def test_a_late_slot_is_a_no_op(store):
    client = FakeTfl(payload())
    run_poll(store, client, SLOT + 1, T0 + 61)
    late = run_poll(store, client, SLOT, T0 + 75)  # a retry of the previous minute arriving after the next
    assert (late.outcome, late.reason) == ("duplicate", "recorded")
    assert store.get_state().last_slot == SLOT + 1 and client.calls == 1


def test_a_poll_not_after_the_last_one_is_a_no_op(store):
    client = FakeTfl(payload())
    run_poll(store, client, SLOT, T0 + 59)
    early = run_poll(store, client, SLOT + 1, T0 + 59)  # a newer slot, but the clock says no time has passed
    assert (early.outcome, early.reason) == ("duplicate", "late")
    assert store.get_state().last_slot == SLOT


def test_a_slot_recorded_while_tfl_was_being_asked_is_a_duplicate(store):
    """Two attempts of one slot on two instances: the second finds the slot recorded inside the transaction."""

    class Racing(FakeTfl):
        def fetch_status(self):
            result = super().fetch_status()
            if self.calls == 1:  # the other instance commits the same slot meanwhile
                run_poll(store, FakeTfl(payload()), SLOT, T0 + 3)
            return result

    result = run_poll(store, Racing(payload(DELAYS)), SLOT, T0 + 2)
    assert (result.outcome, result.reason) == ("duplicate", "recorded")
    assert store.get_state().last_poll == T0 + 3 and store.get_state().open == []


def test_concurrent_attempts_of_one_slot_apply_once(store):
    barrier = threading.Barrier(8)
    results = []

    class Together(FakeTfl):
        def fetch_status(self):
            barrier.wait(timeout=5)
            return super().fetch_status()

    def attempt(i):
        results.append(run_poll(store, Together(payload(DELAYS)), SLOT, T0 + i))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(r.outcome for r in results) == ["applied"] + ["duplicate"] * 7
    assert store.get_polls("2026-10-19").n == 1
    assert len(store.get_state().open) == 1


def test_concurrent_slots_never_go_backwards(store):
    barrier = threading.Barrier(6)
    results = []

    class Together(FakeTfl):
        def fetch_status(self):
            barrier.wait(timeout=5)
            return super().fetch_status()

    def attempt(i):
        results.append(run_poll(store, Together(payload(DELAYS)), SLOT + i, T0 + 60 * i))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    applied = sorted(r.slot for r in results if r.outcome == "applied")
    assert applied and all(r.outcome in ("applied", "duplicate") for r in results)
    state = store.get_state()
    assert state.last_slot == max(applied) and state.last_poll == T0 + 60 * (max(applied) - SLOT)
    polls = store.get_polls("2026-10-19")
    assert polls.at == sorted(polls.at) and polls.n == len(applied)
    assert len(state.open) == 1 and state.open[0]["start"] == T0 + 60 * (min(applied) - SLOT)


class Rerunning(MemoryStore):
    """A store that runs the transaction function twice and commits the second result, as Firestore does when
    a transaction loses to a concurrent one."""

    def poll_transaction(self, slot, fn):
        def twice(state):
            first = fn(copy.deepcopy(state))
            second = fn(copy.deepcopy(state))
            assert first == second
            return second

        return super().poll_transaction(slot, twice)


def test_the_transaction_function_can_run_twice():
    store, baseline = Rerunning(), MemoryStore()
    items = [payload(), payload(DELAYS), payload(SEVERE), payload(), payload()]
    a = series(store, FakeTfl(*copy.deepcopy(items)), T0, 5)
    b = series(baseline, FakeTfl(*copy.deepcopy(items)), T0, 5)
    assert [r.as_dict() for r in a] == [r.as_dict() for r in b]
    assert store.get_state() == baseline.get_state()
    assert store.get_month("2026-10").payload == baseline.get_month("2026-10").payload


def test_a_month_written_during_the_poll_is_merged_not_overwritten():
    """An import stores the month between the poll's read and its transaction; the poll re-reads it."""
    foi_row = ["victoria", T0 - 86400, T0 - 86000, 6, "delays", 0, "f", "signal", "Severe Delays", "x", 0]

    class Importing(MemoryStore):
        armed = False

        def poll_transaction(self, slot, fn):
            if self.armed:
                self.armed = False
                imported = new_month("2026-10") | {"episodes": [foi_row]}
                imported["watched"]["f"] = [[T0 - 90000, T0 - 80000]]
                current_month = self.get_month("2026-10")
                if current_month:
                    imported["watched"]["a"] = current_month.payload["watched"]["a"]
                self.put_month("2026-10", imported)
            return super().poll_transaction(slot, fn)

    store = Importing()
    client = FakeTfl(payload())
    poll_at(store, client, T0)
    poll_at(store, client, T0 + 60)
    store.armed = True
    result = poll_at(store, client, T0 + 120)
    assert result.outcome == "applied"
    month = store.get_month("2026-10").payload
    assert foi_row in month["episodes"]
    assert month["watched"] == {"a": [[T0, T0 + 120]], "f": [[T0 - 90000, T0 - 80000]]}


def test_months_that_keep_changing_fail_the_attempt(store, monkeypatch):
    client = FakeTfl(payload())
    poll_at(store, client, T0)
    poll_at(store, client, T0 + 60)
    monkeypatch.setattr(store, "get_month", lambda month: None)  # never matches the index
    result = poll_at(store, client, T0 + 120)
    assert (result.outcome, result.code, result.http_status) == ("failed", "store", 503)
    assert store.get_state().last_poll == T0 + 60


# ---- failures ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["timeout", "network", "http_500", "http_429", "bad_json", "too_large"])
def test_a_failed_fetch_records_the_slot_and_keeps_the_state(store, code):
    client = FakeTfl(payload(DELAYS))
    poll_at(store, client, T0)
    before = store.get_state()
    client.push(TflError(code, "TfL failed"))
    client.items.pop(0)
    result = poll_at(store, client, T0 + 60)
    assert (result.outcome, result.code, result.http_status) == ("failed", code, 502)
    assert store.get_state() == before
    assert store.get_polls("2026-10-19").failed == [{"slot": SLOT + 1, "code": code}]


def test_a_retry_after_a_failure_applies(store):
    client = FakeTfl(TflError("timeout", "slow"), payload())
    assert run_poll(store, client, SLOT, T0 + 1).http_status == 502
    assert run_poll(store, client, SLOT, T0 + 15).outcome == "applied"
    polls = store.get_polls("2026-10-19")
    assert (polls.at, polls.failed) == ([T0 + 15], [{"slot": SLOT, "code": "timeout"}])


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ({"lines": []}, "not_list"),
        ([], "empty"),
        ([{"id": "central"}], "bad_line"),
        ([{"id": "central", "name": "Central", "lineStatuses": []}], "bad_status"),
        ([{"id": "central", "name": "Central", "lineStatuses": [{"statusSeverity": "6"}]}], "bad_status"),
        ("<html>", "not_list"),
    ],
)
def test_a_bad_payload_is_refused_before_anything_changes(store, raw, code):
    client = FakeTfl(payload(DELAYS))
    poll_at(store, client, T0)
    before = store.get_state()
    client.items = [raw]
    result = poll_at(store, client, T0 + 60)
    assert (result.outcome, result.code, result.http_status) == ("failed", code, 502)
    assert store.get_state() == before  # the open episode was not closed by a broken answer
    assert store.get_polls("2026-10-19").failed == [{"slot": SLOT + 1, "code": code}]


def test_an_answer_missing_most_known_lines_is_refused(store):
    client = FakeTfl(payload(DELAYS))
    poll_at(store, client, T0)
    client.items = [payload(DELAYS, lines=LINES[:7])]  # 7 of 10 lines: under 80%
    assert poll_at(store, client, T0 + 60).code == "missing_lines"
    client.items = [payload(DELAYS, lines=LINES[:8])]  # 8 of 10: enough
    assert poll_at(store, client, T0 + 120).outcome == "applied"


def test_only_lines_seen_in_the_last_day_are_expected(store):
    client = FakeTfl(payload())
    poll_at(store, client, T0)
    client.items = [payload(lines=LINES[:2])]
    later = T0 + 86400 + 120  # nothing heard for over a day: the old line list no longer binds
    assert poll_at(store, client, later).outcome == "applied"


def test_a_store_failure_is_a_503(store, monkeypatch):
    def broken(slot, fn):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(store, "poll_transaction", broken)
    result = poll_at(store, FakeTfl(payload()), T0)
    assert (result.outcome, result.code, result.http_status) == ("failed", "store", 503)


def test_an_unreadable_state_is_a_503(store, monkeypatch):
    monkeypatch.setattr(store, "get_state", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    client = FakeTfl(payload())
    assert poll_at(store, client, T0).http_status == 503 and client.calls == 0


def test_a_failure_that_cannot_be_recorded_is_still_a_502(store, monkeypatch):
    def broken(slot, fn):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(store, "poll_transaction", broken)
    result = poll_at(store, FakeTfl(TflError("timeout", "slow")), T0)
    assert (result.code, result.http_status) == ("timeout", 502)


def test_a_damaged_open_episode_does_not_stop_recording(store):
    store.poll_transaction(
        SLOT - 1,
        lambda s: PollWrite(state=State(last_poll=T0 - 60, last_slot=SLOT - 1, open=[{"line": "central"}])),
    )
    result = poll_at(store, FakeTfl(payload(DELAYS)), T0)
    assert result.outcome == "applied" and result.open == 1


# ---- gaps, runs and months -----------------------------------------------------------------------------------


def test_a_gap_over_15_minutes_ends_episodes_at_the_last_poll(store):
    client = FakeTfl(payload(DELAYS))
    poll_at(store, client, T0)
    poll_at(store, client, T0 + 60)
    result = poll_at(store, client, T0 + 60 + 901)
    assert (result.closed, result.opened, result.open) == (1, 1, 1)
    (row,) = store.get_month("2026-10").payload["episodes"]
    assert (row[1], row[2], row[10]) == (T0, T0 + 60, 1)  # ended at the last poll, estimated
    assert store.get_state().run == [T0 + 961, T0 + 961]
    assert store.get_month("2026-10").payload["watched"]["a"] == [[T0, T0 + 60]]


def test_exactly_15_minutes_keeps_continuity(store):
    client = FakeTfl(payload(DELAYS))
    poll_at(store, client, T0)
    result = poll_at(store, client, T0 + 900)
    assert (result.closed, result.open) == (0, 1)
    assert store.get_state().run == [T0, T0 + 900]


def test_an_imported_state_without_a_run_links_to_the_next_poll(store):
    store.poll_transaction(SLOT - 5, lambda s: PollWrite(state=State(last_poll=T0 - 300, last_slot=SLOT - 5)))
    poll_at(store, FakeTfl(payload()), T0)
    assert store.get_state().run == [T0 - 300, T0]
    assert store.get_month("2026-10").payload["watched"]["a"] == [[T0 - 300, T0]]


def test_a_month_end_with_an_open_episode(store):
    start = utc(2026, 10, 31, 23, 55)
    nov = utc(2026, 11, 1)
    items = [payload()] + [payload(SEVERE)] * 9 + [payload()] * 3  # severe delays 23:56 to 00:05
    results = series(store, FakeTfl(*items), start, len(items))
    assert all(r.outcome == "applied" for r in results)
    assert all(len(r.months) <= 2 for r in results)
    crossing = next(r for r in results if r.poll_at == nov)
    assert crossing.months == ("2026-10",)  # the minute up to midnight belongs to October
    oct_payload = store.get_month("2026-10").payload
    nov_payload = store.get_month("2026-11").payload
    assert oct_payload["watched"]["a"] == [[start, nov]]
    assert nov_payload["watched"]["a"] == [[nov, start + 60 * 12]]
    # the episode was open over midnight, so it reached the months only when it ended, and it is in both
    (oct_row,) = oct_payload["episodes"]
    (nov_row,) = nov_payload["episodes"]
    assert oct_row == nov_row
    assert (oct_row[0], oct_row[1], oct_row[2], oct_row[3]) == ("central", start + 60, start + 600, 6)
    closing = next(r for r in results if r.closed)
    assert closing.months == ("2026-10", "2026-11")


def test_an_episode_open_at_a_month_end_is_in_no_month_until_it_ends(store):
    start = utc(2026, 10, 31, 23, 58)
    series(store, FakeTfl(payload(SEVERE)), start, 5)
    assert store.get_month("2026-10").payload["episodes"] == []
    assert store.get_month("2026-11").payload["episodes"] == []
    assert len(store.get_state().open) == 1


def test_a_long_run_touches_one_month_per_poll(store):
    long_ago = utc(2026, 8, 1)
    store.poll_transaction(
        SLOT - 1,
        lambda s: PollWrite(state=State(last_poll=T0 - 60, last_slot=SLOT - 1, run=[long_ago, T0 - 60])),
    )
    result = poll_at(store, FakeTfl(payload()), T0)
    assert result.months == ("2026-10",)
    assert store.get_month("2026-08") is None and store.get_month("2026-09") is None
    assert store.get_month("2026-10").payload["watched"]["a"] == [[T0 - 60, T0]]


@pytest.mark.parametrize(
    ("day", "hours"),
    [("2026-10-25", 25), ("2026-03-29", 23)],  # clocks go back; clocks go forward
)
def test_a_clock_change_day_has_every_slot_once(day, hours):
    store = MemoryStore()  # 3,000 transactions: the in-memory store only
    a, b = day_bounds(day)
    assert b - a == hours * 3600
    client = FakeTfl(payload())
    results = series(store, client, a, (b - a) // 60 + 1)
    applied = [r.slot for r in results if r.outcome == "applied"]
    assert len(applied) == len(set(applied)) == hours * 60 + 1
    assert applied == list(range(a // 60, b // 60 + 1))
    for r in results:  # Scheduler's retry of each minute changes nothing
        assert run_poll(store, client, r.slot, r.poll_at + 20).outcome == "duplicate"
    month = store.get_month(day[:7]).payload
    assert month["watched"]["a"] == [[a, b]]
    lo, hi = month_bounds(day[:7])
    assert lo < a < b < hi
    polls = store.get_polls(day)  # the UTC day: London's day starts an hour earlier in summer
    assert polls.n == len(polls.at) == len(set(polls.at))


# ---- the result ----------------------------------------------------------------------------------------------


def test_results_as_json(store):
    applied = run_poll(store, FakeTfl(payload(DELAYS)), SLOT, T0)
    assert applied.as_dict() == {
        "outcome": "applied",
        "slot": SLOT,
        "poll_at": "2026-10-19T08:00:00+00:00",
        "tfl_auth": "ok",
        "fetch_ms": 5,
        "open": 1,
        "opened": 1,
        "closed": 0,
        "months": [],
        "snapshot": True,
        "lines": 10,
    }
    dup = run_poll(store, FakeTfl(payload()), SLOT, T0 + 9)
    assert dup.as_dict() == {"outcome": "duplicate", "slot": SLOT, "reason": "recorded"}
    failed = run_poll(store, FakeTfl(TflError("http_503", "x", auth="degraded")), SLOT + 1, T0 + 60)
    assert failed.as_dict() == {"outcome": "failed", "slot": SLOT + 1, "code": "http_503", "tfl_auth": "degraded"}


def test_fetch_results_pass_through_unchanged(store):
    raw = payload(DELAYS)
    fetched = FetchResult(raw=raw, auth="degraded", status=200, size=123, elapsed_ms=42)
    result = run_poll(store, FakeTfl(fetched), SLOT, T0)
    assert (result.tfl_auth, result.fetch_ms) == ("degraded", 42)
    assert raw == payload(DELAYS)  # not modified


def test_build_write_is_pure():
    prep = pl._Prepared(
        slot=SLOT,
        now_ts=T0,
        raw=payload(DELAYS),
        obs={},
        meta={},
        status={},
        etag="e",
        auth="ok",
    )
    from tifa.core.episodes import observe

    prep.obs, prep.meta = observe(prep.raw)
    state = State(last_poll=T0 - 60, last_slot=SLOT - 1, run=[T0 - 600, T0 - 60], status_etag="e")
    copy_before = copy.deepcopy(state)
    a = pl.build_write(state, prep, lambda m, r: new_month(m))
    b = pl.build_write(state, prep, lambda m, r: new_month(m))
    assert a == b and state == copy_before
