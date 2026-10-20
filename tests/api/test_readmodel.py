"""The read model: how often it reads the store, what it keeps, and what it serves when the store fails."""

from __future__ import annotations

import collections
import threading

import pytest

from tests.api.test_routes import NOW, Clock, fixture_state, fixture_store, open_episodes, poll
from tifa.api import views
from tifa.api.readmodel import SETTLE, HistoryBusy, ReadModel, StoreUnavailable, day_start, public_payload
from tifa.core.months import merge_month, month_episode, new_month
from tifa.core.timeutil import DAY, iso, month_key, utc_day
from tifa.store.base import PollWrite
from tifa.store.memory import MemoryStore


class Counting:
    """A store wrapper that counts reads and can fail or stall them."""

    def __init__(self, inner: MemoryStore) -> None:
        self.inner = inner
        self.calls: collections.Counter = collections.Counter()
        self.down: set[str] = set()  # method names that raise
        self.gate: threading.Event | None = None  # the gated method waits for it when set
        self.gated = "get_state"
        self.entered = threading.Event()

    def __getattr__(self, name):
        attr = getattr(self.inner, name)

        def call(*args, **kwargs):
            self.calls[name] += 1
            if name == self.gated and self.gate is not None:
                self.entered.set()
                self.gate.wait(5)
            if name in self.down or "*" in self.down:
                raise ConnectionError(f"{name} failed")
            return attr(*args, **kwargs)

        return call


def model(store=None, sources=("a",), now=NOW, **kwargs):
    wall, mono = Clock(now), Clock(1000.0)
    store = store if store is not None else Counting(fixture_store(now))
    return ReadModel(store, public_sources=sources, clock=wall, monotonic=mono, **kwargs), store, wall, mono


def test_the_state_is_read_at_most_every_30_seconds():
    rm, store, _, mono = model()
    assert store.calls["get_state"] == 0  # nothing is read until asked
    rm.snapshot()
    rm.snapshot()
    rm.refresh()
    assert store.calls["get_state"] == 1 and not rm.due()
    mono.advance(29.9)
    rm.refresh()
    assert store.calls["get_state"] == 1
    mono.advance(0.1)
    assert rm.due()
    rm.refresh()
    assert store.calls["get_state"] == 2
    rm.refresh(force=True)
    assert store.calls["get_state"] == 3


def test_months_are_read_again_only_when_their_revision_changes():
    rm, store, _, mono = model()
    snap = rm.snapshot()
    loaded = store.calls["get_month"]
    assert loaded == len(store.inner.list_months()) == len(snap.months)
    poll(store.inner, NOW + 60, open_episodes())  # a poll that writes no month
    mono.advance(30)
    again = rm.refresh()
    assert store.calls["get_month"] == loaded and again.version != snap.version
    assert again.months["2025-10"] is snap.months["2025-10"]  # the same object: not read again
    doc = store.inner.get_month("2025-11")
    store.inner.put_month("2025-11", doc.payload)
    mono.advance(30)
    third = rm.refresh()
    assert store.calls["get_month"] == loaded + 1 and third.months["2025-11"].rev == doc.rev + 1
    mono.advance(30)
    assert rm.refresh().version == third.version  # nothing changed
    parsed = third.months["2025-10"].parsed()
    assert parsed is snap.months["2025-10"].parsed() and parsed["eps"]  # parsed once for every snapshot sharing it


def test_the_store_is_made_on_first_use():
    made = []

    def factory():
        made.append(1)
        return fixture_store()

    rm = ReadModel(factory, clock=lambda: NOW)
    assert made == []
    assert rm.snapshot().last_poll == NOW and made == [1]


def test_one_refresh_at_a_time_and_others_keep_serving():
    rm, store, _, mono = model()
    first = rm.snapshot()
    store.gate = threading.Event()
    store.entered.clear()
    done = []
    t = threading.Thread(target=lambda: done.append(rm.refresh(force=True)))
    t.start()
    assert store.entered.wait(5)  # the refresh is inside get_state
    assert rm.refresh(force=True) is first  # another one does not wait or read
    assert rm.snapshot() is first
    assert store.calls["get_state"] == 2
    store.gate.set()
    t.join(5)
    assert done and done[0] is not first and rm.snapshot() is done[0]


def test_the_first_load_is_waited_for():
    store = Counting(fixture_store())
    store.gate = threading.Event()
    rm, _, _, _ = model(store)
    got = []
    threads = [threading.Thread(target=lambda: got.append(rm.snapshot())) for _ in range(3)]
    for t in threads:
        t.start()
    assert store.entered.wait(5)
    store.gate.set()
    for t in threads:
        t.join(5)
    assert len(got) == 3 and all(s.last_poll == NOW for s in got)
    assert store.calls["get_state"] == 1


def test_store_errors_keep_the_last_good_data():
    rm, store, wall, mono = model()
    good = rm.snapshot()
    assert good.store_error is None and not views.is_stale(good, rm.now())
    store.down = {"get_state"}
    mono.advance(30)
    bad = rm.refresh()
    assert bad.store_error == "ConnectionError" and bad.error_since == NOW
    assert bad.state is good.state and bad.months is good.months
    assert views.is_stale(bad, rm.now()) and views.status_view(rm)["stale"] is True
    assert views.status_view(rm)["lines"] == good.status["lines"]
    wall.advance(60)
    mono.advance(30)
    assert rm.refresh().error_since == NOW  # still failing since the first failure
    store.down = set()
    mono.advance(30)
    back = rm.refresh()
    assert back.store_error is None and back.error_since is None


def test_a_month_that_cannot_be_read_keeps_its_old_revision():
    rm, store, _, mono = model()
    old = rm.snapshot().months["2025-11"]
    doc = store.inner.get_month("2025-11")
    store.inner.put_month("2025-11", doc.payload)
    store.down = {"get_month"}
    mono.advance(30)
    snap = rm.refresh()
    assert snap.store_error == "ConnectionError" and snap.months["2025-11"] is old
    store.down = set()
    mono.advance(30)
    assert rm.refresh().months["2025-11"].rev == old.rev + 1


class FlakyMonths(Counting):
    """Some months cannot be read."""

    def __init__(self, inner: MemoryStore, bad=()) -> None:
        super().__init__(inner)
        self.bad = set(bad)

    def get_month(self, month):
        self.calls["get_month"] += 1
        if month in self.bad:
            raise ConnectionError("get_month failed")
        return self.inner.get_month(month)


def close_one(store: MemoryStore, t: int) -> dict:
    """A poll at t that closes the first open episode, writing it into its month as the poller does."""
    st = store.get_state()
    ep, others = dict(st.open[0]), list(st.open[1:])
    m = month_key(t)
    doc = store.get_month(m)
    payload = merge_month(
        doc.payload if doc else new_month(m), [month_episode({**ep, "end": t, "end_estimated": False}, "a")], (), "a"
    )
    write = PollWrite(state=fixture_state(t, others), months={m: payload}, poll_at=t)
    assert store.poll_transaction(t // 60, lambda current, w=write: w) == "applied"
    return ep


def test_a_month_that_cannot_be_read_keeps_the_whole_last_snapshot():
    """The poll that closes an episode also writes it into its month. A newer state served with the older month
    would have it neither open nor closed while the cursor moves past it, so the last whole snapshot stays."""
    rm, store, wall, mono = model()
    good = rm.snapshot()
    ep = close_one(store.inner, NOW + 60)
    poll(store.inner, NOW + 120, list(store.inner.get_state().open))  # the next poll writes no month
    store.down = {"get_month"}
    wall.advance(125)
    mono.advance(30)
    bad = rm.refresh()
    assert bad.store_error == "ConnectionError" and bad.error_since == NOW + 125 and not bad.partial
    assert bad.state is good.state and bad.months is good.months and bad.version == good.version
    data = views.changes_view(rm, NOW)
    assert data["cursor"] == NOW and data["closed"] == [] and len(data["open"]) == len(good.open_rows())
    assert data["stale"] is True
    store.down = set()
    mono.advance(30)
    back = rm.refresh()
    assert back.store_error is None and back.last_poll == NOW + 120
    data = views.changes_view(rm, NOW)  # the cursor of the answer before: the closed episode comes now
    assert [(e["line"], e["end"]) for e in data["closed"]] == [(ep["line"], iso(NOW + 60))]
    assert data["cursor"] == NOW + 120 and len(data["open"]) == len(good.open_rows()) - 1


def test_months_read_by_a_failed_refresh_are_not_read_again():
    inner = fixture_store()
    store = FlakyMonths(inner)
    rm, _, _, mono = model(store)
    rm.snapshot()
    for m in ("2025-10", "2025-11"):
        inner.put_month(m, inner.get_month(m).payload)
    store.bad = {"2025-11"}
    reads = store.calls["get_month"]
    mono.advance(30)
    assert rm.refresh().store_error == "ConnectionError" and store.calls["get_month"] == reads + 2
    store.bad = set()
    mono.advance(30)
    snap = rm.refresh()
    assert store.calls["get_month"] == reads + 3  # 2025-10 was kept aside: only 2025-11 is read again
    assert snap.store_error is None and snap.months["2025-10"].rev == inner.list_months()["2025-10"]


def test_a_month_that_cannot_be_read_on_the_first_load():
    """Nothing older to serve: what could be read is served, marked partial and stale; sync answers refuse it."""
    store = FlakyMonths(fixture_store(), bad={"2025-11"})
    rm, _, _, mono = model(store)
    snap = rm.snapshot()
    assert snap.partial and snap.store_error == "ConnectionError" and snap.last_poll == NOW
    assert "2025-11" not in snap.months and "2025-10" in snap.months
    assert views.status_view(rm)["stale"] is True
    with pytest.raises(views.ViewError) as exc:
        views.changes_view(rm, NOW - 60)
    assert (exc.value.status, exc.value.code) == (503, "store_unavailable")
    store.bad = set()
    mono.advance(30)
    full = rm.refresh()
    assert not full.partial and full.store_error is None and "2025-11" in full.months
    assert views.changes_view(rm, NOW - 60)["cursor"] == NOW


def test_a_store_down_from_the_start():
    store = Counting(fixture_store())
    store.down = {"*"}
    rm, _, _, _ = model(store)
    snap = rm.snapshot()
    assert snap.state is None and snap.store_error == "ConnectionError"
    assert views.status_view(rm)["stale"] is True and views.months_index_view(rm)["months"] == []


def test_only_published_sources_are_kept():
    rm, _, _, _ = model()
    snap = rm.snapshot()
    assert all(r[6] == "a" for e in snap.months.values() for r in e.payload["episodes"])
    assert all(set(e.payload["watched"]) <= {"a"} for e in snap.months.values())
    assert "2024-06" not in snap.month_names(NOW) and "circle-hammersmith-city" not in snap.lines
    both, _, _, _ = model(sources=("a", "f"))
    snap = both.snapshot()
    assert "2024-06" in snap.month_names(NOW) and snap.first == 1704067200  # 2024-01-01
    shared = snap.lines["circle-hammersmith-city"]
    assert shared["shared_with"] == ["circle", "hammersmith-city"] and shared["mode"] == "tube"
    assert any(r[6] == "f" for r in snap.months["2024-06"].payload["episodes"])


def test_public_payload():
    payload = {
        "month": "2025-01",
        "episodes": [["central", 1, 2, 9, "delays", 0, "a"], ["central", 1, 2, 9, "delays", 0, "f"], "junk"],
        "watched": {"a": [[1, 2]], "f": [[0, 5]], "z": [[0, 1]]},
    }
    assert public_payload(payload, "2025-01", ("a",)) == {
        "month": "2025-01",
        "episodes": [["central", 1, 2, 9, "delays", 0, "a"]],
        "watched": {"a": [[1, 2]]},
    }


def test_poll_days_are_cached():
    rm, store, wall, mono = model()
    today, past = "2025-11-03", "2025-11-02"
    rm.polls(today), rm.polls(past)
    rm.polls(today), rm.polls(past)
    assert store.calls["get_polls"] == 2
    mono.advance(30)  # today's is read again after the state's interval, a finished day after an hour
    rm.polls(today), rm.polls(past)
    assert store.calls["get_polls"] == 3
    mono.advance(3600)
    rm.polls(past)
    assert store.calls["get_polls"] == 4
    assert rm.polls(past) is None and rm.polls(today).at[-1] == NOW  # no polls were recorded on the 2nd
    store.down = {"get_polls"}
    mono.advance(3600)
    assert rm.polls(today).at[-1] == NOW  # the old copy, while the store is down
    with pytest.raises(StoreUnavailable):
        rm.polls("2025-10-01")
    assert rm.poll_days(NOW - 86400, NOW) == ["2025-11-02", "2025-11-03"]


def test_a_day_read_before_the_snapshot_last_poll_is_read_again():
    """Mid-day: the poll times are read 25 s after the state, a poll lands, and 6 s later the next snapshot has it
    as its last poll. The copy is young but older than that snapshot, so it is read again for it."""
    rm, store, wall, mono = model()
    today = utc_day(NOW)
    snap = rm.snapshot()
    mono.advance(25)
    assert rm.polls(today, snap.last_poll).at[-1] == NOW
    reads = store.calls["get_polls"]
    poll(store.inner, NOW + 60, open_episodes())
    wall.advance(60)
    mono.advance(6)
    newer = rm.refresh()
    assert newer.last_poll == NOW + 60
    assert rm.polls(today, newer.last_poll).at[-1] == NOW + 60 and store.calls["get_polls"] == reads + 1
    assert rm.polls(today, newer.last_poll).at[-1] == NOW + 60 and rm.polls(today, NOW).at[-1] == NOW + 60
    assert store.calls["get_polls"] == reads + 1  # that copy holds both: kept


def test_a_day_read_while_current_is_read_again_once_it_is_over():
    """Just before UTC midnight the day is read; its last poll (23:59) lands after. The first snapshot past
    midnight reads it again; that copy is complete, and once the day has settled it is kept for DAY_TTL."""
    midnight = day_start(utc_day(NOW)) + DAY  # 00:00 UTC on 4 November
    day = utc_day(midnight - 1)
    store = Counting(MemoryStore())
    times = [midnight - 175, midnight - 115]  # 23:57:05 and 23:58:05
    for t in times:
        poll(store.inner, t, [])
    rm, _, wall, mono = model(store, now=times[-1] + 20)
    assert rm.polls(day, rm.snapshot().last_poll).at == times
    for t in (midnight - 55, midnight + 5):  # 23:59:05 and 00:00:05
        poll(store.inner, t, [])
    wall.t = midnight + 25
    mono.advance(5)  # the copy is 5 s old: young, but older than the next snapshot
    snap = rm.refresh(force=True)
    assert snap.last_poll == midnight + 5
    assert rm.polls(day, snap.last_poll).at == [*times, midnight - 55]
    reads = store.calls["get_polls"]
    poll(store.inner, midnight + 65, [])
    wall.t = midnight + 85
    mono.advance(10)
    later = rm.refresh(force=True)
    assert later.last_poll == midnight + 65
    rm.polls(day, later.last_poll)
    assert store.calls["get_polls"] == reads  # read after a poll of the next day: complete, not read again
    mono.advance(31)
    rm.polls(day, later.last_poll)
    assert store.calls["get_polls"] == reads + 1  # not settled yet: kept for state_ttl
    wall.t = midnight + SETTLE
    mono.advance(31)
    rm.polls(day, later.last_poll)
    mono.advance(3000)
    rm.polls(day, later.last_poll)
    assert store.calls["get_polls"] == reads + 2  # read once settled, then kept for DAY_TTL


def test_strict_reads_never_serve_a_copy_older_than_the_snapshot():
    rm, store, wall, mono = model()
    today = utc_day(NOW)
    rm.polls(today, rm.snapshot().last_poll)
    poll(store.inner, NOW + 60, open_episodes())
    wall.advance(60)
    mono.advance(30)
    snap = rm.refresh()
    store.down = {"get_polls"}
    assert rm.polls(today, snap.last_poll).at[-1] == NOW  # for coverage: the last copy while the store is down
    with pytest.raises(StoreUnavailable):
        rm.polls(today, snap.last_poll, strict=True)  # for a sync answer: it lacks the snapshot's last poll
    with pytest.raises(views.ViewError) as exc:
        views.changes_view(rm, NOW)
    assert (exc.value.status, exc.value.code) == (503, "store_unavailable")
    store.down = set()
    assert views.changes_view(rm, NOW)["polls"] == [NOW + 60]


def test_one_read_per_day_at_a_time():
    rm, store, _, _ = model()
    rm.snapshot()
    store.gated, store.gate = "get_polls", threading.Event()
    store.entered.clear()
    got = []
    threads = [threading.Thread(target=lambda: got.append(rm.polls("2025-11-02", NOW))) for _ in range(5)]
    for t in threads:
        t.start()
    assert store.entered.wait(5)
    store.gate.set()
    for t in threads:
        t.join(5)
    assert got == [None] * 5 and store.calls["get_polls"] == 1


def test_settled_days_are_read_within_a_budget():
    rm, store, _, mono = model(day_reads=(3, 0.5))
    last = rm.snapshot().last_poll
    days = [f"2025-10-0{d}" for d in range(1, 7)]
    for d in days[:3]:
        assert rm.polls(d, last) is None
    with pytest.raises(HistoryBusy):
        rm.polls(days[3], last)
    assert rm.polls(days[0], last) is None and rm.polls(utc_day(NOW), last).at[-1] == NOW  # kept; today is free
    assert store.calls["get_polls"] == 4
    mono.advance(2)  # one more read
    assert rm.polls(days[3], last) is None
    with pytest.raises(HistoryBusy):
        rm.polls(days[4], last)
    mono.advance(3600)  # every copy is past DAY_TTL, and the budget holds three reads again
    for d in days[:3]:
        rm.polls(d, last)
    reads = store.calls["get_polls"]
    assert rm.polls(days[3], last) is None and store.calls["get_polls"] == reads  # a settled copy, served as it is
    with pytest.raises(HistoryBusy):
        rm.polls(days[5], last)


def test_guard_is_cached():
    rm, store, _, mono = model()
    store.inner.put_guard({"heartbeat": NOW})
    assert rm.guard() == {"heartbeat": NOW} and rm.guard() == {"heartbeat": NOW}
    assert store.calls["get_guard"] == 1
    store.down = {"get_guard"}
    mono.advance(31)
    assert rm.guard() == {"heartbeat": NOW}  # the last copy when the store fails


def test_snapshots_do_not_change_under_readers():
    rm, store, _, mono = model()
    held = rm.snapshot()
    open_before = list(held.open)
    poll(store.inner, NOW + 60, [])  # every episode closed... as far as the state goes
    mono.advance(30)
    assert rm.refresh().open == [] and held.open == open_before
