"""The Store contract, run against every backend (see conftest.py)."""

import logging
import threading
import time

import pytest

from tifa.store import base, codec
from tifa.store.base import APPLIED, DUPLICATE, MonthDoc, PollsDoc, PollWrite, State, Store

T0 = 1_792_454_340  # 2026-10-19 23:59 UTC: the next minute is another UTC day
SLOT = T0 // 60
NOON = 1_792_411_200  # 2026-10-19 12:00 UTC
MONTH = "2026-10"

STATUS = {
    "lines": [
        {
            "id": "central",
            "name": "Central",
            "mode": "tube",
            "severity": 6,
            "status": "Severe Delays",
            "reasons": ["Central Line: Severe delays due to an earlier signal failure at Bank."],
            "good": False,
            "live": True,
        },
        {
            "id": "victoria",
            "name": "Victoria",
            "mode": "tube",
            "severity": 10,
            "status": "Good Service",
            "reasons": [],
            "good": True,
            "live": True,
        },
    ],
    "disrupted": 1,
    "total": 2,
}
OPEN = [
    {
        "line": "central",
        "name": "Central",
        "mode": "tube",
        "severity": 6,
        "status": "Severe Delays",
        "category": "delays",
        "planned": False,
        "start": T0 - 600,
        "reason": "Severe delays due to an earlier signal failure at Bank.",
    }
]
EPISODE = [
    "central",
    T0 - 7200,
    T0 - 3600,
    6,
    "delays",
    0,
    "a",
    "signal",
    "Severe Delays",
    "Severe delays due to an earlier signal failure at Bank.",
    0,
]


def month_payload(month=MONTH, episodes=(EPISODE,), watched=None):
    return {
        "month": month,
        "episodes": [list(e) for e in episodes],
        "watched": watched or {"a": [[T0 - 86400, T0]], "f": []},
    }


def new_state(slot, **fields):
    ts = slot * 60
    values = {
        "last_poll": ts,
        "last_slot": slot,
        "run": [T0 - 86400, ts],
        "open": OPEN,
        "lines": {"central": {"name": "Central", "mode": "tube", "first_seen": T0 - 86400, "last_seen": ts}},
        "status": STATUS,
        "status_etag": "etag-1",
        "causes_sig": "sig-1",
        "updated_at": ts,
    }
    return State(**(values | fields))


def success(slot, **write):
    """fn for a successful poll at `slot`."""
    return lambda state: PollWrite(**({"state": new_state(slot), "poll_at": slot * 60} | write))


class Calls:
    """fn wrapper that counts calls."""

    def __init__(self, fn):
        self.fn, self.n = fn, 0

    def __call__(self, state):
        self.n += 1
        return self.fn(state)


def test_implements_protocol(store):
    assert isinstance(store, Store)


def test_empty_store(store):
    assert store.get_state() is None
    assert store.list_months() == {}
    assert store.get_month(MONTH) is None
    assert store.get_polls("2026-10-19") is None
    assert store.get_guard() is None
    assert store.get_snapshot(T0) is None
    assert store.get_import("missing") is None


def test_first_poll_commits_everything(store):
    seen = []
    write = success(SLOT, months={MONTH: month_payload()}, snapshot=(T0, STATUS))

    def fn(state):
        seen.append(state)
        return write(state)

    assert store.poll_transaction(SLOT, fn) == APPLIED
    assert seen == [None]
    assert store.get_state() == new_state(SLOT, months={MONTH: {"rev": 1, "n": 1}})
    payload = month_payload()
    assert store.get_month(MONTH) == MonthDoc(MONTH, 1, payload, codec.digest(payload), len(codec.encode(payload)))
    assert store.list_months() == {MONTH: 1}
    assert store.get_polls("2026-10-19") == PollsDoc("2026-10-19", [T0], 1, [])
    assert store.get_snapshot(T0) == STATUS


def test_fn_sees_the_stored_state(store):
    store.poll_transaction(SLOT, success(SLOT))
    seen = []
    store.poll_transaction(SLOT + 1, lambda state: seen.append(state))
    assert seen == [store.get_state()]


def test_duplicate_slot_is_a_no_op(store):
    assert store.poll_transaction(SLOT, success(SLOT, months={MONTH: month_payload()})) == APPLIED
    retry = Calls(success(SLOT, months={MONTH: month_payload(episodes=[])}))
    assert store.poll_transaction(SLOT, retry) == DUPLICATE
    assert retry.n == 0
    assert store.get_month(MONTH).rev == 1
    assert store.get_polls("2026-10-19").n == 1


def test_late_slot_is_a_duplicate(store):
    store.poll_transaction(SLOT + 5, success(SLOT + 5))
    late = Calls(success(SLOT))
    assert store.poll_transaction(SLOT, late) == DUPLICATE
    assert late.n == 0
    assert store.get_state().last_slot == SLOT + 5


def test_fn_returning_none_writes_nothing(store):
    assert store.poll_transaction(SLOT, lambda state: None) == DUPLICATE
    assert store.get_state() is None
    assert store.get_polls("2026-10-19") is None


def test_fn_gets_its_own_copy(store):
    store.poll_transaction(SLOT, success(SLOT))

    def meddle(state):
        state.lines.clear()
        state.months["2000-01"] = {"rev": 99, "n": 0}
        state.open.append({"line": "x"})

    assert store.poll_transaction(SLOT + 1, meddle) == DUPLICATE
    assert store.get_state() == new_state(SLOT)


def test_errors_write_nothing(store):
    store.poll_transaction(SLOT, success(SLOT, months={MONTH: month_payload()}))
    before = store.get_state()
    unencodable = month_payload("2026-11") | {"bad": {1, 2}}  # a set is not JSON
    with pytest.raises(TypeError):
        store.poll_transaction(
            SLOT + 1,
            success(SLOT + 1, months={MONTH: month_payload(), "2026-11": unencodable}, snapshot=(T0 + 60, STATUS)),
        )

    def boom(state):
        raise RuntimeError("fn failed")

    with pytest.raises(RuntimeError):
        store.poll_transaction(SLOT + 1, boom)
    assert store.get_state() == before
    assert store.list_months() == {MONTH: 1}
    assert store.get_month("2026-11") is None
    assert store.get_polls("2026-10-20") is None
    assert store.get_snapshot(T0 + 60) is None


def test_month_revisions_increment(store):
    store.poll_transaction(SLOT, success(SLOT, months={MONTH: month_payload()}))
    two = month_payload(
        episodes=[EPISODE, ["victoria", T0 - 50, T0 - 10, 9, "delays", 0, "a", None, "Minor Delays", "", 1]]
    )
    store.poll_transaction(SLOT + 1, success(SLOT + 1, months={MONTH: two, "2026-11": month_payload("2026-11")}))
    assert store.list_months() == {MONTH: 2, "2026-11": 1}
    assert store.get_month(MONTH).payload == two
    assert store.put_month(MONTH, month_payload(watched={"a": [], "f": [[1, 2]]})) == 3
    assert store.put_month("2020-01", month_payload("2020-01", episodes=[])) == 1
    assert store.list_months() == {"2020-01": 1, MONTH: 3, "2026-11": 1}
    state = store.get_state()
    assert state.months == {"2020-01": {"rev": 1, "n": 0}, MONTH: {"rev": 3, "n": 1}, "2026-11": {"rev": 1, "n": 1}}
    assert state.last_slot == SLOT + 1  # put_month leaves the rest of the state alone
    # a poll writing another month keeps the index of the others
    store.poll_transaction(SLOT + 2, success(SLOT + 2, months={"2026-11": month_payload("2026-11")}))
    assert store.list_months() == {"2020-01": 1, MONTH: 3, "2026-11": 2}


def test_store_owns_the_month_index(store):
    store.put_month(MONTH, month_payload())
    bogus = new_state(SLOT, months={"1999-01": {"rev": 7, "n": 7}})
    store.poll_transaction(SLOT, lambda state: PollWrite(state=bogus, poll_at=T0))
    assert store.get_state().months == {MONTH: {"rev": 1, "n": 1}}


def test_put_month_before_any_poll(store):
    assert store.put_month(MONTH, month_payload()) == 1
    state = store.get_state()
    assert state == State(months={MONTH: {"rev": 1, "n": 1}})
    seen = []
    assert store.poll_transaction(SLOT, lambda s: seen.append(s) or success(SLOT)(s)) == APPLIED
    assert seen == [state]


def test_put_month_rejects_bad_input(store):
    with pytest.raises(ValueError):
        store.put_month("2026-13", month_payload("2026-13"))
    with pytest.raises(ValueError):
        store.put_month("2026-1", month_payload("2026-1"))
    with pytest.raises(ValueError):
        store.put_month(MONTH, month_payload("2026-09"))
    with pytest.raises(ValueError):
        store.put_month(MONTH, [])
    with pytest.raises(ValueError):
        store.get_month("2026/10")
    assert store.get_state() is None


def test_gzip_round_trips(store):
    payload = month_payload(
        episodes=[
            [
                "hammersmith-city",
                T0 - 900,
                None,
                20,
                "closure",
                1,
                "a",
                "engineering",
                "Service Closed",
                "No service between King’s Cross St. Pancras and Moorgate — engineering works. ✓",
                0,
            ],
            ["elizabeth", 1_577_836_800, 1_577_840_400, 3, "suspended", 0, "f", None, "Part Suspended", "", 1],
        ]
    ) | {"extra": {"float": 0.5, "big": 2**53 + 1, "true": True, "none": None, "nested": [[1, [2]], []]}}
    status = STATUS | {"note": "Ünïcødé 🚇", "matrix": [[1, 2], [3]]}
    store.poll_transaction(SLOT, success(SLOT, months={MONTH: payload}, snapshot=(T0, status)))
    store.poll_transaction(SLOT + 1, lambda s: PollWrite(state=new_state(SLOT + 1, status=status)))
    assert store.get_month(MONTH).payload == payload
    assert store.get_state().status == status
    assert store.get_snapshot(T0) == status


def test_polls_array_union(store):
    a, b = NOON // 60, NOON // 60 + 1
    store.poll_transaction(a, success(a))
    store.poll_transaction(b, success(b))
    store.poll_transaction(b + 1, lambda s: PollWrite(state=new_state(b + 1), poll_at=b * 60))  # time already there
    store.poll_transaction(SLOT + 1, success(SLOT + 1))
    day = store.get_polls("2026-10-19")
    assert day.at == [NOON, NOON + 60]
    assert day.failed == []
    assert store.get_polls("2026-10-20") == PollsDoc("2026-10-20", [T0 + 60], 1, [])


def test_failed_slot_keeps_state_and_allows_a_retry(store):
    store.poll_transaction(SLOT, success(SLOT))
    before = store.get_state()
    failed = SLOT + 1  # 2026-10-20 00:00 UTC
    for code in ("timeout", "timeout", "http_503"):
        record = {"slot": failed, "code": code}
        assert store.poll_transaction(failed, lambda s, r=record: PollWrite(failed=r)) == APPLIED
    assert store.get_state() == before
    assert store.get_polls("2026-10-20") == PollsDoc(
        "2026-10-20", [], 0, [{"slot": failed, "code": "timeout"}, {"slot": failed, "code": "http_503"}]
    )
    assert store.poll_transaction(failed, success(failed)) == APPLIED
    assert store.get_state().last_slot == failed
    day = store.get_polls("2026-10-20")
    assert (day.at, day.n, len(day.failed)) == ([T0 + 60], 1, 2)
    assert store.poll_transaction(failed, lambda s: PollWrite(failed={"slot": failed, "code": "late"})) == DUPLICATE


def test_failed_slot_before_any_state(store):
    assert store.poll_transaction(SLOT, lambda s: PollWrite(failed={"slot": SLOT, "code": "invalid"})) == APPLIED
    assert store.get_state() is None
    assert store.get_polls("2026-10-19") == PollsDoc("2026-10-19", [], 0, [{"slot": SLOT, "code": "invalid"}])


def test_snapshots(store):
    store.add_snapshot(T0, STATUS)
    store.add_snapshot(T0 + 60, {"lines": [], "disrupted": 0, "total": 0})
    assert store.get_snapshot(T0) == STATUS
    assert store.get_snapshot(T0 + 60) == {"lines": [], "disrupted": 0, "total": 0}


def test_guard_documents(store):
    doc = {
        "closed_until": T0 + 1800,
        "reason": "requests_5min",
        "trips": [{"at": T0, "minutes": 30}],
        "heartbeat": T0,
        "mtd_cost": 0.42,
    }
    store.put_guard(doc)
    assert store.get_guard() == doc
    store.put_guard({"closed_until": None, "heartbeat": T0 + 120})  # a put replaces the whole document
    assert store.get_guard() == {"closed_until": None, "heartbeat": T0 + 120}


def test_imports(store):
    doc = {"kind": "jsonl", "episodes": 120, "polls": 700, "months": ["2026-09", MONTH], "at": T0}
    store.record_import("jsonl-2026-10-19", doc)
    assert store.get_import("jsonl-2026-10-19") == doc
    store.record_import("jsonl-2026-10-19", doc | {"episodes": 121})
    assert store.get_import("jsonl-2026-10-19")["episodes"] == 121
    for bad in ("", "a/b", "..", "__id__"):
        with pytest.raises(ValueError):
            store.record_import(bad, doc)


def test_values_firestore_cannot_hold_are_refused(store):
    for doc in ({"spans": [[1, 2]]}, {1: "int key"}, {"x": {"y": object()}}, {"n": 2**63}):
        with pytest.raises(ValueError):
            store.put_guard(doc)
    with pytest.raises(ValueError):
        store.poll_transaction(SLOT, lambda s: PollWrite(state=new_state(SLOT, open=[[1, 2]])))
    assert store.get_guard() is None
    assert store.get_state() is None


def test_values_come_back_as_firestore_returns_them(store):
    store.poll_transaction(SLOT, lambda s: PollWrite(state=new_state(SLOT, run=(T0 - 60, T0))))
    assert store.get_state().run == [T0 - 60, T0]


def test_month_size_limits(store, monkeypatch, caplog):
    monkeypatch.setattr(base, "MONTH_WARN_BYTES", 50)
    with caplog.at_level(logging.WARNING, logger="tifa.store"):
        store.put_month(MONTH, month_payload())
    assert any(MONTH in r.getMessage() and "bytes gzipped" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(base, "MONTH_MAX_BYTES", 50)
    with pytest.raises(ValueError):
        store.poll_transaction(SLOT, success(SLOT, months={MONTH: month_payload()}))
    assert store.get_state().last_slot is None
    assert store.list_months() == {MONTH: 1}


def run_threads(n, target):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = target(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def slow(fn):
    def wrapped(state):
        time.sleep(0.01)  # widen the window for transactions to overlap
        return fn(state)

    return wrapped


def test_concurrent_retries_of_one_slot_apply_once(store):
    slot = NOON // 60
    results = run_threads(6, lambda i: store.poll_transaction(slot, slow(success(slot))))
    assert sorted(results) == [APPLIED] + [DUPLICATE] * 5
    assert store.get_polls("2026-10-19").n == 1


def test_concurrent_slots_lose_no_updates(store):
    first = NOON // 60

    def poll(i):
        slot = first + i

        def fn(state):
            lines = dict(state.lines) if state else {}
            lines[f"line-{slot}"] = {"name": str(slot), "mode": "tube", "first_seen": slot * 60, "last_seen": slot * 60}
            return PollWrite(state=new_state(slot, lines=lines), poll_at=slot * 60)

        return store.poll_transaction(slot, slow(fn))

    results = run_threads(6, poll)
    applied = {first + i for i, r in enumerate(results) if r == APPLIED}
    assert first + 5 in applied  # nothing can beat the highest slot
    state = store.get_state()
    assert state.last_slot == first + 5
    assert set(state.lines) == {f"line-{s}" for s in applied}  # every applied poll saw the one before it
    day = store.get_polls("2026-10-19")
    assert day.n == len(applied)
    assert day.at == sorted(s * 60 for s in applied)
