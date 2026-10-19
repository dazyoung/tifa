"""The document layout shared by the backends, checked without any backend."""

import pytest

from tifa.store import base, codec
from tifa.store.base import PollWrite, State

T0 = 1_792_454_340  # 2026-10-19 23:59 UTC
STATUS = {"lines": [{"id": "central", "reasons": ["Minor delays."]}], "disrupted": 1, "total": 1}


def test_state_document_gzips_status():
    state = State(
        last_poll=T0,
        last_slot=T0 // 60,
        run=[T0 - 60, T0],
        status=STATUS,
        status_etag="e",
        months={"2026-10": {"rev": 2, "n": 5}},
        tfl_auth="degraded",
        updated_at=T0,
    )
    doc = base.state_to_doc(state)
    assert isinstance(doc["status"], bytes)
    assert codec.decode(doc["status"]) == STATUS
    assert set(doc) == {
        "schema",
        "last_poll",
        "last_slot",
        "run",
        "open",
        "lines",
        "status",
        "status_etag",
        "months",
        "causes_sig",
        "tfl_auth",
        "updated_at",
    }
    assert doc["schema"] == 1
    assert base.state_from_doc(doc) == state


def test_state_from_doc_tolerates_old_and_new_fields():
    assert base.state_from_doc({"last_slot": 5, "status": {"lines": []}, "added_later": 1}) == State(
        last_slot=5, status={"lines": []}
    )


def test_month_document():
    payload = {
        "month": "2026-10",
        "episodes": [["central", 1, 2, 6, "delays", 0, "a", None]],
        "watched": {"a": [], "f": []},
    }
    doc = base.month_to_doc("2026-10", payload, 3, T0)
    assert doc == {
        "month": "2026-10",
        "rev": 3,
        "payload": codec.encode(payload),
        "bytes": len(doc["payload"]),
        "sha256": codec.digest(payload),
        "updated_at": T0,
    }
    month = base.month_from_doc(doc)
    assert (month.month, month.rev, month.payload, month.sha256, month.size) == (
        "2026-10",
        3,
        payload,
        codec.digest(payload),
        doc["bytes"],
    )
    assert base.month_to_doc("2026-10", {"episodes": []}, 1, T0)["rev"] == 1  # "month" in the payload is optional


def test_plan_poll():
    current = State(last_slot=T0 // 60 - 1, months={"2026-09": {"rev": 4, "n": 9}, "2026-10": {"rev": 1, "n": 0}})
    write = PollWrite(
        state=State(last_slot=T0 // 60, months={}),
        months={"2026-10": {"episodes": [[1], [2]]}, "2026-11": {"episodes": []}},
        poll_at=T0,
        failed={"slot": T0 // 60 + 1, "code": "timeout", "extra": "dropped"},
        snapshot=(T0, STATUS),
    )
    plan = base.plan_poll(current, write, T0)
    assert base.state_from_doc(plan.state).months == {
        "2026-09": {"rev": 4, "n": 9},
        "2026-10": {"rev": 2, "n": 2},
        "2026-11": {"rev": 1, "n": 0},
    }
    assert {m: d["rev"] for m, d in plan.months.items()} == {"2026-10": 2, "2026-11": 1}
    assert plan.polls == {"2026-10-19": ([T0], []), "2026-10-20": ([], [{"slot": T0 // 60 + 1, "code": "timeout"}])}
    assert plan.snapshot == ("1792454340", {"hash": codec.digest(STATUS), "payload": codec.encode(STATUS)})
    assert current.months["2026-10"] == {"rev": 1, "n": 0}  # the current state is not changed


def test_plan_poll_leaves_state_alone_unless_asked():
    assert base.plan_poll(State(), PollWrite(failed={"slot": 1, "code": "x"}), T0).state is None
    # writing a month without a new state still records it in the current state's index
    plan = base.plan_poll(State(last_slot=7), PollWrite(months={"2026-10": {}}), T0)
    assert base.state_from_doc(plan.state) == State(last_slot=7, months={"2026-10": {"rev": 1, "n": 0}})
    with pytest.raises(TypeError):
        base.plan_poll(None, {"state": None}, T0)


def test_check_value():
    for ok in (None, True, 1, -(2**63), 1.5, "s", b"\x00", {"a": [1, {"b": [2]}]}, [{"x": [1]}], (1, 2)):
        base.check_value(ok)
    for bad in ({"a": [[1]]}, [(1, 2)], {"": 1}, {2: 1}, 2**63, {1, 2}, object()):
        with pytest.raises(ValueError):
            base.check_value(bad)
    deep = 0
    for _ in range(25):
        deep = {"d": deep}
    with pytest.raises(ValueError):
        base.check_value(deep)


def test_ids_and_days():
    assert base.utc_day(T0) == "2026-10-19"
    assert base.utc_day(T0 + 60) == "2026-10-20"
    assert base.check_month("2026-10") == "2026-10"
    assert base.check_day("2026-02-29") == "2026-02-29"  # shape only
    for bad in ("2026-00", "26-10", "2026-10-01", 202610):
        with pytest.raises(ValueError):
            base.check_month(bad)
    for bad in ("2026-10", "2026-10-32", "../x"):
        with pytest.raises(ValueError):
            base.check_day(bad)
    assert base.is_duplicate(State(last_slot=5), 5)
    assert base.is_duplicate(State(last_slot=5), 4)
    assert not base.is_duplicate(State(last_slot=5), 6)
    assert not base.is_duplicate(State(), 1)
    assert not base.is_duplicate(None, 1)
