"""tifa.core.episodes: observations, and episodes opening, continuing and closing over polls (the GAP rule)."""

import copy
import datetime as dt
import functools
import hashlib
import json
from pathlib import Path

import pytest

from tifa.core import episodes as ep
from tifa.core.timeutil import iso

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "core"
MIN = 60
T0 = int(dt.datetime(2026, 10, 19, 8, 0, tzinfo=dt.UTC).timestamp())  # a Monday, 09:00 in London


@functools.cache
def golden(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def as_reference(closed: list) -> list:
    """Closed episodes as the reference logged them (times as ISO 8601)."""
    return [dict(c, start=iso(c["start"]), end=iso(c["end"])) for c in closed]


def open_as_reference(state: dict) -> list:
    return [dict(e, start=iso(e["start"])) for e in state["open"]]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


# ---- matches the reference -----------------------------------------------------------------------------------


def test_constants_match_the_reference():
    g = golden("golden_episodes.json")
    assert (ep.GAP, ep.REASON_MAX) == (g["gap"], g["reason_max"]) == (900, 400)


@pytest.mark.parametrize("name", sorted(golden("golden_tfl.json")["payloads"]))
def test_observe_matches_the_reference(name):
    case = golden("golden_tfl.json")["payloads"][name]
    raw = copy.deepcopy(case["raw"])
    obs, meta = ep.observe(raw)
    assert [[k[0], k[1], k[2], v["status"], v["reason"]] for k, v in obs.items()] == case["observe"]["obs"]
    assert {k: list(v) for k, v in meta.items()} == case["observe"]["meta"]
    assert all(isinstance(v, tuple) for v in meta.values())
    assert raw == case["raw"]


def test_in_effect_matches_the_reference():
    for status, expected in golden("golden_tfl.json")["in_effect"]:
        assert ep.in_effect(status) is expected, status


@pytest.mark.parametrize("name", sorted(golden("golden_episodes.json")["scenarios"]))
def test_poll_streams_match_the_reference(name):
    g = golden("golden_episodes.json")
    scenario, payloads = g["scenarios"][name], g["payloads"]
    state = None
    for i, ((at, idx), step) in enumerate(zip(scenario["polls"], scenario["steps"], strict=True)):
        before = copy.deepcopy(state)
        obs, meta = ep.observe(payloads[idx])
        new, closed, opened = ep.advance(state, obs, meta, at)
        assert state == before, "advance changed its input"
        if step is None:  # not after the last poll: nothing recorded
            assert (new, closed, opened) == (state, [], []), i
            continue
        assert as_reference(closed) == step["closed"], (name, i, at)
        if "open" in step:
            assert open_as_reference(new) == step["open"], (name, i, at)
        else:
            assert digest(open_as_reference(new)) == step["open_sha"], (name, i, at)
        assert opened == [e for e in new["open"] if e["start"] == at]
        assert new["last_poll"] == at
        state = new


def test_the_random_streams_cover_the_gap_rule():
    g = golden("golden_episodes.json")["scenarios"]
    closed = [c for name in g if name.startswith("random") for s in g[name]["steps"] if s for c in s["closed"]]
    assert sum(c["end_estimated"] for c in closed) >= 10
    assert sum(not c["end_estimated"] for c in closed) >= 30
    gaps = [b[0] - a[0] for name in g for a, b in zip(g[name]["polls"], g[name]["polls"][1:], strict=False)]
    assert {900, 901} <= set(gaps)


# ---- the reference's own cases ------------------------------------------------------------------------------


def status(sev, desc=None, reason=None, category=None, now=None):
    s = {
        "statusSeverity": sev,
        "statusSeverityDescription": desc or f"Code {sev}",
        "validityPeriods": []
        if now is None
        else [{"fromDate": "2026-10-19T07:00:00Z", "toDate": "2026-10-20T00:29:00Z", "isNow": now}],
    }
    if reason is not None:
        s["reason"] = reason
    if category is not None:
        s["disruption"] = {"category": category, "categoryDescription": category}
    return s


GOOD = {"statusSeverity": 10, "statusSeverityDescription": "Good Service", "validityPeriods": []}


def line(line_id, name, mode, *statuses):
    return {"id": line_id, "name": name, "modeName": mode, "lineStatuses": list(statuses) or [dict(GOOD)]}


def minor(reason="Central Line: Minor delays due to a signal failure.", now=True):
    return status(9, "Minor Delays", reason, "RealTime", now)


def part_closure(reason="District Line: Saturday, no service between Earl's Court and Richmond.", now=True):
    return status(5, "Part Closure", reason, "PlannedWork", now)


def api(central=(), district=(), bakerloo=()):
    return [
        line("bakerloo", "Bakerloo", "tube", *bakerloo),
        line("central", "Central", "tube", *central),
        line("district", "District", "tube", *district),
    ]


class Poller:
    """Polls one after another, keeping the state between them."""

    def __init__(self):
        self.state = ep.empty_state()

    def __call__(self, raw, at):
        obs, meta = ep.observe(raw)
        self.state, closed, _ = ep.advance(self.state, obs, meta, at)
        return closed

    def open(self, key):
        return next(e for e in self.state["open"] if ep.key_of(e) == key)

    def keys(self):
        return [ep.key_of(e) for e in self.state["open"]]


def test_in_effect_now():
    assert ep.in_effect(status(9, now=True))
    assert ep.in_effect(status(9))  # no validity periods at all
    assert ep.in_effect({"statusSeverity": 9})
    assert not ep.in_effect(status(5, now=False))  # planned works next weekend
    two = status(5, now=False)
    two["validityPeriods"].append({"fromDate": "x", "toDate": "y", "isNow": True})
    assert ep.in_effect(two)  # any period that is now
    assert not ep.in_effect({"validityPeriods": [{"isNow": "true"}]})


def test_filtering():
    raw = [
        line("central", "Central", "tube", minor(), part_closure(now=False)),
        line("bakerloo", "Bakerloo", "tube", dict(GOOD)),
        line("waterloo-city", "Waterloo & City", "tube", status(20, "Service Closed")),
        line("elizabeth", "Elizabeth line", "elizabeth-line", status(18, "No Issues")),
        line("dlr", "DLR", "dlr", status(6, "Severe Delays", category="RealTime")),
        line("victoria", "Victoria", "tube", {"statusSeverity": "9"}, {"statusSeverity": True}, "junk"),
    ]
    obs, meta = ep.observe(raw)
    assert set(obs) == {("central", 9, False), ("dlr", 6, False)}
    assert obs[("central", 9, False)] == {"status": "Minor Delays", "reason": "Minor delays due to a signal failure."}
    assert obs[("dlr", 6, False)]["reason"] is None
    assert meta["elizabeth"] == ("Elizabeth line", "elizabeth-line")
    assert len(meta) == 6  # every line is known, disrupted or not


def test_planned():
    raw = [
        line(
            "central",
            "Central",
            "tube",
            status(5, category="PlannedWork"),
            status(3, category="RealTime"),
            status(19, category="Information"),
            status(2),
        )
    ]
    obs, _ = ep.observe(raw)
    assert set(obs) == {("central", 5, True), ("central", 3, False), ("central", 19, False), ("central", 2, False)}


def test_reason_cleaned_and_capped():
    long = "Central Line:   " + "word " * 200
    obs, _ = ep.observe([line("central", "Central", "tube", minor(long))])
    reason = obs[("central", 9, False)]["reason"]
    assert reason.startswith("Word word")
    assert len(reason) <= ep.REASON_MAX
    assert reason.endswith("…")


def test_same_status_twice_is_one_observation():
    raw = [
        line(
            "district",
            "District",
            "tube",
            part_closure("District Line: No service between Earl's Court and Richmond."),
            part_closure("District Line: No service between Tower Hill and Upminster."),
            part_closure("District Line: no service between Tower Hill and Upminster."),
        )
    ]
    obs, _ = ep.observe(raw)
    assert list(obs) == [("district", 5, True)]
    assert (
        obs[("district", 5, True)]["reason"]
        == "No service between Earl's Court and Richmond. No service between Tower Hill and Upminster."
    )


def test_open_continue_close():
    poll = Poller()
    assert poll(api(central=[minor("Central Line: First reason.")]), T0) == []
    e = poll.open(("central", 9, False))
    assert (e["start"], e["category"], e["status"], e["reason"]) == (T0, "delays", "Minor Delays", "First reason.")
    assert poll(api(central=[minor("Central Line: Second reason.")]), T0 + 120) == []
    assert poll(api(central=[minor(reason=None)]), T0 + 240) == []  # no reason: keep the last
    closed = poll(api(), T0 + 360)
    assert closed == [
        {
            "line": "central",
            "name": "Central",
            "mode": "tube",
            "severity": 9,
            "status": "Minor Delays",
            "category": "delays",
            "planned": False,
            "start": T0,
            "end": T0 + 360,
            "end_estimated": False,
            "reason": "Second reason.",
        }
    ]
    assert poll.state == {"last_poll": T0 + 360, "open": []}


def test_a_change_of_severity_is_a_new_episode():
    poll = Poller()
    poll(api(central=[minor()]), T0)
    closed = poll(api(central=[status(6, "Severe Delays", "Severe.", "RealTime", True)]), T0 + 120)
    assert [(c["severity"], c["end"]) for c in closed] == [(9, T0 + 120)]
    assert poll.keys() == [("central", 6, False)]
    assert poll.open(("central", 6, False))["start"] == T0 + 120


def test_two_statuses_at_once_on_one_line():
    poll = Poller()
    poll(api(district=[part_closure()]), T0)
    poll(api(district=[part_closure(), minor("District Line: Minor delays.")]), T0 + 120)
    assert set(poll.keys()) == {("district", 5, True), ("district", 9, False)}
    closed = poll(api(district=[part_closure()]), T0 + 240)
    assert [(c["severity"], c["planned"], c["start"], c["end"]) for c in closed] == [(9, False, T0 + 120, T0 + 240)]
    assert poll.open(("district", 5, True))["start"] == T0


def test_planned_and_unplanned_of_one_severity_are_separate():
    poll = Poller()
    poll(api(district=[part_closure(), status(5, "Part Closure", "Fault.", "RealTime", True)]), T0)
    assert set(poll.keys()) == {("district", 5, True), ("district", 5, False)}


def test_gap_over_15_minutes_ends_episodes_estimated():
    poll = Poller()
    poll(api(central=[minor()], district=[part_closure()]), T0)
    poll(api(central=[minor()], district=[part_closure()]), T0 + 120)
    closed = poll(api(district=[part_closure()]), T0 + 120 + 16 * MIN)  # not watched, central over meanwhile
    assert sorted((c["line"], c["start"], c["end"], c["end_estimated"]) for c in closed) == [
        ("central", T0, T0 + 120, True),
        ("district", T0, T0 + 120, True),
    ]
    assert poll.keys() == [("district", 5, True)]  # still there afterwards: a new episode
    assert poll.open(("district", 5, True))["start"] == T0 + 120 + 16 * MIN


def test_15_minutes_exactly_keeps_continuity():
    poll = Poller()
    poll(api(central=[minor()]), T0)
    assert poll(api(central=[minor()]), T0 + 15 * MIN) == []
    closed = poll(api(), T0 + 30 * MIN)
    assert (closed[0]["start"], closed[0]["end"], closed[0]["end_estimated"]) == (T0, T0 + 30 * MIN, False)


def test_future_status_is_never_an_episode():
    poll = Poller()
    poll(api(district=[part_closure(now=False)]), T0)
    assert poll.state["open"] == []


# ---- advance as a pure function -------------------------------------------------------------------------------


def test_advance_keeps_other_state_keys_and_changes_nothing_it_was_given():
    obs, meta = ep.observe(api(central=[minor()]))
    state = {
        "schema": 1,
        "last_poll": T0 - 60,
        "last_slot": 7,
        "open": [
            {
                "line": "central",
                "name": "Central",
                "mode": "tube",
                "severity": 9,
                "status": "Minor Delays",
                "category": "delays",
                "planned": False,
                "start": T0 - 600,
                "reason": "Old.",
                "extra": "dropped",
            }
        ],
    }
    before = copy.deepcopy(state)
    new, closed, opened = ep.advance(state, obs, meta, T0)
    assert state == before
    assert (new["schema"], new["last_slot"], new["last_poll"]) == (1, 7, T0)
    assert (closed, opened) == ([], [])
    assert new["open"][0]["start"] == T0 - 600 and new["open"][0]["reason"] == "Minor delays due to a signal failure."
    assert list(new["open"][0]) == list(ep.OPEN_FIELDS)


def test_advance_from_nothing_and_what_opened():
    obs, meta = ep.observe(api(central=[minor()], district=[part_closure()]))
    new, closed, opened = ep.advance(None, obs, meta, T0)
    assert closed == [] and new["last_poll"] == T0
    assert [ep.key_of(e) for e in opened] == [("central", 9, False), ("district", 5, True)]
    assert opened == new["open"] and opened[0] is not new["open"][0]


def test_a_poll_not_after_the_last_records_nothing():
    obs, meta = ep.observe(api(central=[minor()]))
    state, _, _ = ep.advance(None, obs, meta, T0)
    for at in (T0, T0 - 60):
        assert ep.advance(state, *ep.observe(api()), at) == (state, [], [])


def test_closed_and_open_order():
    raw = api(
        central=[minor(), status(6, "Severe Delays", "Severe.", "RealTime", True)],
        district=[part_closure(), status(5, "Part Closure", "Fault.", "RealTime", True)],
        bakerloo=[status(2, "Suspended", "Suspended.", "RealTime", True)],
    )
    state, _, _ = ep.advance(None, *ep.observe(raw), T0)
    assert [ep.key_of(e) for e in state["open"]] == [
        ("bakerloo", 2, False),
        ("central", 6, False),
        ("central", 9, False),
        ("district", 5, False),
        ("district", 5, True),
    ]
    _, closed, _ = ep.advance(state, *ep.observe(api()), T0 + 60)
    assert [(c["line"], c["severity"], c["planned"]) for c in closed] == [
        ("bakerloo", 2, False),
        ("central", 6, False),
        ("central", 9, False),
        ("district", 5, False),
        ("district", 5, True),
    ]


# ---- damaged stored state ------------------------------------------------------------------------------------

STORED = {
    "line": "central",
    "name": "Central",
    "mode": "tube",
    "severity": 9,
    "status": "Minor Delays",
    "category": "delays",
    "planned": False,
    "start": T0 - 600,
    "reason": "Old.",
}


def test_clean_open_repairs_display_fields_and_drops_what_cannot_be_recorded():
    repaired = {
        "line": "district",
        "severity": 5,
        "planned": 1,
        "start": float(T0 - 300),
        "name": "",
        "mode": {"x": 1},
        "status": None,
        "category": "nonsense",
        "reason": ["x"],
    }
    unusable = [
        None,
        "junk",
        {},
        dict(STORED, line=""),
        dict(STORED, line=None),
        dict(STORED, severity="9"),
        dict(STORED, severity=True),
        dict(STORED, severity=9.0),
        dict(STORED, planned=None),
        dict(STORED, planned="no"),
        dict(STORED, planned=2),
        dict(STORED, start=None),
        dict(STORED, start="2026-10-19T08:00:00+00:00"),
        dict(STORED, start=float("inf")),
        dict(STORED, start=float("nan")),
        dict(STORED, start=False),
    ]
    unusable += [{k: v for k, v in STORED.items() if k != field} for field in ("line", "severity", "planned", "start")]
    entries = [STORED, repaired, *unusable]
    before = copy.deepcopy(entries)
    kept, dropped = ep.clean_open(entries)
    assert entries == before
    assert dropped == len(unusable)
    assert kept == [
        STORED,
        {
            "line": "district",
            "name": "district",
            "mode": None,
            "severity": 5,
            "status": "Severity 5",
            "category": "closure",
            "planned": True,
            "start": T0 - 300,
            "reason": None,
        },
    ]
    assert all(list(e) == list(ep.OPEN_FIELDS) for e in kept)
    assert ep.clean_open([{k: v for k, v in STORED.items() if k in ("line", "severity", "planned", "start")}]) == (
        [
            {
                "line": "central",
                "name": "central",
                "mode": None,
                "severity": 9,
                "status": "Severity 9",
                "category": "delays",
                "planned": False,
                "start": T0 - 600,
                "reason": None,
            }
        ],
        0,
    )
    for junk in (None, "x", {"a": 1}, 5):
        assert ep.clean_open(junk) == ([], 0)


def test_advance_keeps_recording_from_a_damaged_state():
    """One bad stored episode must not stop every later poll: it is dropped, the rest carry on."""
    state = {
        "last_poll": T0 - 60,
        "open": [
            {"line": "bakerloo", "severity": 2},
            STORED,
            {k: v for k, v in STORED.items() if k != "name"} | {"line": "district", "severity": 6},
        ],
    }
    new, closed, opened = ep.advance(state, *ep.observe(api(central=[minor()])), T0)
    assert [ep.key_of(e) for e in new["open"]] == [("central", 9, False)]
    assert new["open"][0]["start"] == T0 - 600 and opened == []
    assert [(c["line"], c["name"], c["severity"], c["end"], c["end_estimated"]) for c in closed] == [
        ("district", "district", 6, T0, False)
    ]
    later, closed, _ = ep.advance(new, *ep.observe(api()), T0 + 60)
    assert later["open"] == [] and [c["line"] for c in closed] == ["central"]


def test_advance_with_a_damaged_last_poll_or_open_list():
    obs, meta = ep.observe(api(central=[minor()]))
    for last in ("yesterday", [1], float("nan"), True):
        new, closed, opened = ep.advance({"last_poll": last, "open": [STORED]}, obs, meta, T0)
        assert new["last_poll"] == T0 and closed == [] and [e["start"] for e in new["open"]] == [T0 - 600]
    new, _, opened = ep.advance({"last_poll": T0 - 60, "open": {"central": STORED}}, obs, meta, T0)
    assert [e["start"] for e in opened] == [T0]
