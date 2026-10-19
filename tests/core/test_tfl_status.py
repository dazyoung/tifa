"""tifa.core.tfl: ranks, categories, reasons, the current status and payload checks."""

import copy
import functools
import json
import random
from pathlib import Path

import pytest

from tifa.core import tfl
from tifa.core.episodes import observe

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "core"


@functools.cache
def golden() -> dict:
    return json.loads((FIXTURES / "golden_tfl.json").read_text(encoding="utf-8"))


def outcome(fn, *args) -> dict:
    try:
        return {"ok": fn(*args)}
    except Exception as err:  # the nearest built-in type: PayloadError is a RuntimeError, as the reference raised
        return {"error": next(c.__name__ for c in type(err).__mro__ if c.__module__ == "builtins")}


# ---- matches the reference -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(golden()["payloads"]))
def test_current_and_summarise_match_the_reference(name):
    case = golden()["payloads"][name]
    raw = copy.deepcopy(case["raw"])
    assert outcome(tfl.current, raw) == case["current"]
    assert [outcome(tfl.summarise, x) for x in raw if isinstance(x, dict)] == case["summarise"]
    assert raw == case["raw"]  # the payload is not changed


def test_clean_reason_matches_the_reference():
    for text, name, cleaned in golden()["clean_reason"]:
        assert tfl.clean_reason(text, name) == cleaned, (text, name)


def test_rank_and_category_match_the_reference():
    for sev, rank in golden()["rank"]:
        assert tfl.rank(sev) == rank, sev
    for sev, cat in golden()["category"]:
        assert tfl.CATEGORY(sev) == cat, sev
        assert tfl.category(sev) == cat
        assert tfl.CATEGORY[sev] == cat


def test_the_real_sample_is_valid_and_summarised():
    sample = golden()["payloads"]["sample"]["raw"]
    assert tfl.validate_payload(sample, {x["id"] for x in sample}) == (True, None)
    now = tfl.current(sample)
    assert now["total"] == len(sample)
    assert {x["mode"] for x in now["lines"]} <= set(tfl.MODES)


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


def line(line_id, name, mode, *statuses):
    good = {"statusSeverity": 10, "statusSeverityDescription": "Good Service", "validityPeriods": []}
    return {"id": line_id, "name": name, "modeName": mode, "lineStatuses": list(statuses) or [good]}


FIXTURE = [  # as the API returns them: alphabetical by id
    line("bakerloo", "Bakerloo", "tube"),
    line(
        "central",
        "Central",
        "tube",
        status(9, "Minor Delays", "Central Line: Minor delays due to a signal failure.  ", "RealTime", True),
    ),
    line(
        "district",
        "District",
        "tube",
        status(5, "Part Closure", "District Line: Saturday, no service between A and B.", "PlannedWork", False),
    ),
    line("dlr", "DLR", "dlr", status(20, "Service Closed")),
]
CURRENT = {
    "lines": [
        {
            "id": "central",
            "name": "Central",
            "mode": "tube",
            "severity": 9,
            "status": "Minor Delays",
            "reasons": ["Minor delays due to a signal failure."],
            "good": False,
            "live": True,
        },
        {
            "id": "dlr",
            "name": "DLR",
            "mode": "dlr",
            "severity": 20,
            "status": "Service Closed",
            "reasons": [],
            "good": False,
            "live": False,
        },
        {
            "id": "district",
            "name": "District",
            "mode": "tube",
            "severity": 5,
            "status": "Part Closure",
            "reasons": ["Saturday, no service between A and B."],
            "good": False,
            "live": False,
        },
        {
            "id": "bakerloo",
            "name": "Bakerloo",
            "mode": "tube",
            "severity": 10,
            "status": "Good Service",
            "reasons": [],
            "good": True,
            "live": False,
        },
    ],
    "disrupted": 3,
    "total": 4,
}


def test_current_status_as_the_widget_shows_it():
    assert tfl.current(FIXTURE) == CURRENT


def test_categories():
    expected = {
        9: "delays",
        6: "delays",
        7: "delays",
        2: "suspended",
        3: "suspended",
        1: "closure",
        4: "closure",
        5: "closure",
        11: "closure",
        16: "closure",
        0: "other",
        8: "other",
        12: "other",
        14: "other",
        15: "other",
        17: "other",
        13: "info",
        19: "info",
        42: "other",
    }
    for sev, cat in expected.items():
        assert tfl.CATEGORY(sev) == cat, sev
    assert [c["id"] for c in tfl.CATEGORIES] == ["delays", "suspended", "closure", "other", "info"]
    assert [c["label"] for c in tfl.CATEGORIES] == ["Delays", "Suspended", "Closures", "Other", "Information"]
    assert tfl.CATEGORY_IDS == tuple(c["id"] for c in tfl.CATEGORIES)


def test_category_is_read_only_and_never_missing():
    assert tfl.CATEGORY.get(42) == tfl.CATEGORY[42] == tfl.CATEGORY(42) == "other"
    assert tfl.CATEGORY.get(None) == tfl.CATEGORY([1]) == tfl.CATEGORY({}) == "other"
    assert tfl.CATEGORY.get(9) == "delays"
    assert 9 in tfl.CATEGORY and 42 not in tfl.CATEGORY and [1] not in tfl.CATEGORY  # `in`: the listed codes
    assert dict(tfl.CATEGORY) == {
        9: "delays",
        6: "delays",
        7: "delays",
        2: "suspended",
        3: "suspended",
        1: "closure",
        4: "closure",
        5: "closure",
        11: "closure",
        16: "closure",
        13: "info",
        19: "info",
    }
    assert len(tfl.CATEGORY) == 12 and set(tfl.CATEGORY.values()) <= set(tfl.CATEGORY_IDS)
    with pytest.raises(TypeError):
        tfl.CATEGORY[42] = "delays"
    with pytest.raises(AttributeError):
        tfl.CATEGORY.update({42: "delays"})
    assert tfl.CATEGORY(42) == "other"


def test_constants():
    assert tfl.URL_PATH == "/Line/Mode/" + ",".join(tfl.MODES) + "/Status"
    assert list(tfl.MODE_ORDER) == list(tfl.MODES)
    assert tfl.GOOD == {10, 18} and tfl.NOT_RECORDED == {10, 18, 20}
    assert set(tfl.RANK) == set(tfl.SEVERITY_NAMES) == set(range(21))
    assert tfl.rank(42) == tfl.UNKNOWN_RANK and tfl.rank(None) == tfl.UNKNOWN_RANK


def test_clean_reason():
    assert tfl.clean_reason("Central Line:   Minor   delays.", "Central") == "Minor delays."
    assert tfl.clean_reason("DOCKLANDS LIGHT RAILWAY: Severe delays.", "DLR") == "Severe delays."
    assert tfl.clean_reason("Waterloo & City line: minor delays.", "Waterloo & City") == "Minor delays."
    assert tfl.clean_reason("Note: lifts out of service.", "Central") == "Note: lifts out of service."
    assert tfl.clean_reason("12:30 trains cancelled", "Central") == "12:30 trains cancelled"


def test_a_line_without_an_id_fails_the_summary():
    with pytest.raises(RuntimeError):
        tfl.current([{"name": "No id", "lineStatuses": []}])
    with pytest.raises(tfl.PayloadError):
        tfl.current([])


# ---- payload checks ------------------------------------------------------------------------------------------

GOOD_STATUS = {"statusSeverity": 10, "statusSeverityDescription": "Good Service", "validityPeriods": []}


def test_validate_payload():
    ids = {"bakerloo", "central", "district", "dlr", "victoria"}
    good = FIXTURE + [line("victoria", "Victoria", "tube")]
    assert tfl.validate_payload(good, ids) == (True, None)
    assert tfl.validate_payload(good, ()) == (True, None)
    assert tfl.validate_payload(FIXTURE, ids) == (True, None)  # 4 of 5 known lines: 80%
    assert tfl.validate_payload(FIXTURE[:3], ids) == (False, "missing_lines")
    assert tfl.validate_payload(FIXTURE[:1], set()) == (True, None)  # nothing known yet
    assert tfl.validate_payload({"lines": []}, ids) == (False, "not_list")
    assert tfl.validate_payload(None, ids) == (False, "not_list")
    assert tfl.validate_payload([], ids) == (False, "empty")
    assert tfl.validate_payload(FIXTURE + ["junk"], ids) == (False, "bad_line")
    assert tfl.validate_payload([{"id": "central", "name": "Central"}], ()) == (False, "bad_status")
    assert tfl.validate_payload([{"id": "central", "lineStatuses": [GOOD_STATUS]}], ()) == (False, "bad_line")
    assert tfl.validate_payload([{"id": "", "name": "X", "lineStatuses": [GOOD_STATUS]}], ()) == (False, "bad_line")
    assert tfl.validate_payload(
        [{"id": "central", "name": "Central", "modeName": {"x": 1}, "lineStatuses": []}], ()
    ) == (False, "bad_line")


@pytest.mark.parametrize(
    "statuses",
    [
        None,
        "x",
        {},
        [],
        [{}],
        ["x"],
        [None],
        [GOOD_STATUS, "x"],
        [{"statusSeverity": "10"}],
        [{"statusSeverity": True}],
        [{"statusSeverity": [10]}],
        [{"statusSeverity": 9.0}],
        [{"statusSeverity": 9, "disruption": "PlannedWork"}],
        [{"statusSeverity": 9, "disruption": ["x"]}],
        [{"statusSeverity": 9, "validityPeriods": "now"}],
        [{"statusSeverity": 9, "validityPeriods": ["x"]}],
    ],
)
def test_a_line_without_a_usable_status_fails(statuses):
    bad = [line("central", "Central", "tube"), {"id": "victoria", "name": "Victoria", "lineStatuses": statuses}]
    assert tfl.validate_payload(bad, ()) == (False, "bad_status")


def test_statuses_that_pass():
    ok = [
        {"statusSeverity": 9},
        {"statusSeverity": 9, "disruption": None, "validityPeriods": None},
        {"statusSeverity": 42, "disruption": {}, "validityPeriods": [{"isNow": "yes"}], "reason": 5},
    ]
    raw = [{"id": "central", "name": "Central", "lineStatuses": ok}, line("dlr", "DLR", None)]
    assert tfl.validate_payload(raw, ()) == (True, None)
    tfl.current(raw)


def test_no_statuses_anywhere_cannot_close_every_episode():
    """A response whose lines all lack statuses would close every open episode: it is not recorded."""
    sample = golden()["payloads"]["sample"]["raw"]
    for statuses in ([], [{}], ["x"]):
        raw = [dict(x, lineStatuses=copy.deepcopy(statuses)) for x in sample]
        assert tfl.validate_payload(raw, {x["id"] for x in sample}) == (False, "bad_status")


JUNK = (None, True, 0, 9, -1, 2.5, "", "x", [], ["x"], [{}], {}, {"x": 1})


def test_any_payload_that_passes_can_be_summarised_and_observed():
    """Random damage to the real sample: whatever validate_payload lets through, current() and observe() take
    without error, and every line keeps at least one status."""
    sample = golden()["payloads"]["sample"]["raw"]
    rng, passed = random.Random(11), 0
    for _ in range(3000):
        raw = copy.deepcopy(sample)
        for _ in range(rng.randint(1, 3)):
            x = rng.choice(raw)
            statuses = x["lineStatuses"] if isinstance(x.get("lineStatuses"), list) else []
            target = rng.choice([x] + [s for s in statuses if isinstance(s, dict)])
            field = rng.choice(sorted(target) + ["disruption", "validityPeriods", "modeName"])
            target[field] = copy.deepcopy(rng.choice(JUNK))
        if tfl.validate_payload(raw, ())[0]:
            passed += 1
            tfl.current(raw)
            obs, meta = observe(raw)
            assert set(meta) == {x["id"] for x in raw}
    assert passed > 300  # damage to fields nothing relies on (descriptions, reasons, dates) still passes


def test_recent_line_ids():
    now = 1_800_000_000
    lines = {
        "central": {"name": "Central", "last_seen": now - 60},
        "victoria": {"last_seen": now - tfl.LINE_RECENT},
        "retired": {"last_seen": now - tfl.LINE_RECENT - 1},
        "odd": {"last_seen": "yesterday"},
        "bad": None,
        "flag": {"last_seen": True},
    }
    assert tfl.recent_line_ids(lines, now) == {"central", "victoria"}
    assert tfl.recent_line_ids(lines, now, within=3600) == {"central"}
    assert tfl.recent_line_ids({}, now) == set()
    sample = golden()["payloads"]["sample"]["raw"]  # a retired line no longer counts against a full response
    seen = {x["id"]: {"last_seen": now - 60} for x in sample} | {
        f"old{i}": {"last_seen": now - 2 * tfl.LINE_RECENT} for i in range(len(sample))
    }
    assert tfl.validate_payload(sample, seen) == (False, "missing_lines")
    assert tfl.validate_payload(sample, tfl.recent_line_ids(seen, now)) == (True, None)


def test_validate_the_real_sample_against_more_lines():
    sample = golden()["payloads"]["sample"]["raw"]
    known = {x["id"] for x in sample}
    assert tfl.validate_payload(sample[: int(len(sample) * 0.8) + 1], known) == (True, None)
    assert tfl.validate_payload(sample[: int(len(sample) * 0.5)], known) == (False, "missing_lines")
