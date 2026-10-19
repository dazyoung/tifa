"""tifa.core.stats where it goes beyond the reference: query dicts, month payloads as Tifa stores them, line_report."""

import copy
import datetime as dt
import json
import time

import pytest

from tifa.core import stats
from tifa.core.causes import CAUSE_LABELS, cause
from tifa.core.timeutil import month_bounds
from tools import make_stats_fixtures as fx

MONTHS, INDEX = fx.read_months(fx.FIXTURES / "months")
IDX = stats.parse_index(INDEX)
NOW = fx.NOW
TODAY = dt.date(2025, 11, 3)
LINES = {line["id"]: line for line in IDX["lines"]}


def test_parse_period_query_dicts():
    assert stats.parse_period({"span": "7d", "end": "now"}, TODAY) == (("7d", "now"), None)
    assert stats.parse_period({"span": ["90d"], "end": ["2024-03-31"]}, TODAY) == (("90d", dt.date(2024, 3, 31)), None)
    # other parameters (the line, for /v1/stats) are left to the caller
    assert stats.parse_period({"span": "1d", "end": "now", "line": "central"}, TODAY) == (("1d", "now"), None)
    assert stats.parse_period({"span": ["1d", "7d"], "end": "now"}, TODAY)[0] is None
    assert stats.parse_period({"span": "1d"}, TODAY) == (None, "A period needs a span and an end, once each.")
    assert stats.parse_period({"span": 7, "end": "now"}, TODAY)[0] is None
    assert stats.parse_period("span=7d&end=now", TODAY) == (None, "That period could not be read.")
    assert stats.parse_period({"span": "7d", "end": "2" * 40}, TODAY) == (None, "That period could not be read.")


def test_parse_index_defaults():
    idx = stats.parse_index({"months": ["2025-06", "bad", "2025-07"], "lines": [{"id": "central"}, {"id": "Bad!"}]})
    assert idx["months"] == {"2025-06", "2025-07"}
    assert idx["first"] == month_bounds("2025-06")[0]
    assert idx["lines"] == [{"id": "central", "name": "central", "mode": None, "shared_with": []}]
    assert idx["causes"] == CAUSE_LABELS   # an index without causes gets the rules' labels
    assert stats.parse_index({"months": [], "first": 1700000000})["first"] == 1700000000.0
    assert stats.parse_index([]) is None


def test_parse_month_reads_eight_fields_of_eleven():
    payload = MONTHS["2025-10"]
    short = dict(payload, episodes=[r[:8] for r in payload["episodes"]])
    assert stats.parse_month(payload) == stats.parse_month(short)
    bad = {"episodes": [["central", 1, 0, 9, "delays", 0, "a"], ["central", "x", 2, 9, "delays", 0, "a"],
                        ["central", 1, 2, 9, "delays", 0, "z"], ["central", 1, 2, 9, "odd", 1, "a", "Not A Code"]],
           "watched": {"a": [[5, 1], [1, 5], [1]], "q": [[1, 2]]}}
    assert stats.parse_month(bad) == {"eps": [("central", 1, 2, 9, "other", True, "a", None)],
                                      "watched": {"a": [(1, 5)]}}


def test_gather_takes_parsed_months():
    p = stats.period_of("30d", "now", NOW, IDX["first"])
    parsed = {k: stats.parse_month(v) for k, v in MONTHS.items()}
    assert stats.gather(parsed, IDX, p["p0"], p["t1"], NOW) == stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW)
    # without an index every month given is read
    assert stats.gather(MONTHS, None, p["p0"], p["t1"], NOW) == stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW)


def test_gather_open_episodes_run_to_the_last_watched_time():
    p = stats.period_of("7d", "now", NOW, IDX["first"])
    data = stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW)
    open_central = [e for e in data["eps"]["central"] if e["ongoing"]]
    assert open_central and all(e["end"] == NOW for e in open_central)
    jubilee = [e for e in data["eps"]["jubilee"] if e["start"] == fx.ts("2025-10-31T22:00:00+00:00")]
    assert [(e["ongoing"], e["end"]) for e in jubilee] == [(False, fx.ts("2025-11-01T01:00:00+00:00"))]


def test_period_figures_are_plain_data():
    for span in fx.SPANS:
        p = stats.period_of(span, "now", NOW, IDX["first"])
        fig = stats.period_figures(IDX, stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW), p, NOW)
        assert json.loads(json.dumps(fig)) == fig
        assert fig["unplanned"] == sum(r["unplanned"] for r in fig["lines"])
        if fig["bins"]:
            assert all(abs(sum(b["parts"].values()) - b["disrupted_s"]) < 1e-6 for b in fig["bins"]["items"])


def test_period_figures_not_recorded():
    p = stats.period_of("30d", dt.date(2025, 4, 15), NOW, IDX["first"])
    fig = stats.period_figures(IDX, stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW), p, NOW)
    assert fig["watched"]["any"] == 0
    assert fig["lines"] == [] and fig["good_pct"] is None and fig["unwatched"] == [[p["t0"], p["t1"]]]


def report(line_id, months=None, now=NOW):
    snap, open_eps = fx.snapshot(MONTHS if months is None else months, now)
    return stats.line_report(LINES[line_id], snap, open_eps, now)


def test_line_report_shape():
    r = report("central")
    assert list(r) == ["id", "name", "mode", "now", "stats", "coverage_pct", "recent", "starts_by_hour"]
    assert list(r["stats"]) == ["24h", "7d", "30d", "90d"]
    assert fx.iso(NOW - 9 * 3600) in [n["start"] for n in r["now"]]
    assert [n["minutes"] for n in r["now"]] == [stats.minutes(NOW - fx.ts(n["start"])) for n in r["now"]]
    assert len(r["recent"]) == stats.RECENT
    starts = [x["start"] for x in r["recent"]]
    assert starts == sorted(starts, reverse=True)
    assert any(x["end"] is None for x in r["recent"]) and all(x["reason"] for x in r["recent"])
    assert {x["cause"] for x in r["recent"]} <= set(CAUSE_LABELS) | {None}


def test_line_report_ignores_open_rows_and_other_sources():
    snap, open_eps = fx.snapshot(MONTHS, NOW)
    base = stats.line_report(LINES["central"], snap, open_eps, NOW)
    noisy = copy.deepcopy(snap)
    for e in open_eps:   # as /v1/months serves them: open episodes added to the months
        noisy["2025-11"]["episodes"].append([e["line"], e["start"], None, e["severity"], e["category"],
                                             int(e["planned"]), "a", None, e["status"], e["reason"], 0])
    noisy["2025-11"]["episodes"].append(["central", NOW - 3600, NOW - 60, 2, "suspended", 0, "f", None,
                                         "Suspended", None, 0])
    assert stats.line_report(LINES["central"], noisy, open_eps, NOW) == base


def test_line_report_reads_back_only_as_far_as_needed():
    recent_keys = [k for k in MONTHS if month_bounds(k)[1] > NOW - stats.SPAN]
    # a busy line: the windows' months hold its latest episodes, so older months change nothing
    assert report("central", {k: MONTHS[k] for k in recent_keys}) == report("central")
    # a quiet line: its latest episodes reach further back
    quiet = report("victoria")
    assert len(report("victoria", {k: MONTHS[k] for k in recent_keys})["recent"]) <= len(quiet["recent"])


def test_starts_by_hour_in_london_time():
    # 00:30 UTC (01:30 BST) and 01:10 UTC (01:10 GMT), the night the clocks went back: both start at 01 in London
    snap, open_eps = fx.snapshot(MONTHS, NOW)
    only = {k: dict(v, episodes=[r for r in v["episodes"] if r[0] != "elizabeth" or r[1] in (
        fx.ts("2025-10-26T00:30:00+00:00"), fx.ts("2025-10-26T01:10:00+00:00"))]) for k, v in snap.items()}
    hours = stats.line_report(LINES["elizabeth"], only, open_eps, NOW)["starts_by_hour"]
    assert hours[1] == 2 and sum(hours) == 2


class MultiDict(dict):
    """A query as web frameworks give it: get() takes one value (the last), getlist() all of them."""

    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = pairs

    def getlist(self, name):
        return [v for k, v in self.pairs if k == name]


def test_parse_period_multidict_refuses_repeats():
    assert stats.parse_period(MultiDict([("span", "7d"), ("end", "now")]), TODAY) == (("7d", "now"), None)
    repeated = MultiDict([("span", "7d"), ("span", "1d"), ("end", "now")])
    assert repeated.get("span") == "1d"
    assert stats.parse_period(repeated, TODAY) == (None, "A period needs a span and an end, once each.")
    assert stats.parse_period(MultiDict([("span", "7d")]), TODAY)[0] is None


def test_parse_index_causes_and_months():
    listed = stats.parse_index({"months": [], "causes": [{"code": "signal", "label": "Signals"}, {"code": "Bad"}]})
    assert listed["causes"] == {"signal": "Signals"}
    assert stats.parse_index({"months": [], "causes": {"signal": "Signals"}})["causes"] == {"signal": "Signals"}
    assert stats.parse_index({"months": [], "causes": []})["causes"] == {}
    # only real months, in ASCII digits
    assert stats.parse_index({"months": ["2025-13", "2025-00", "２０２５-01", "2025-12"]})["months"] == {"2025-12"}


def london_ts(*args) -> int:
    return int(dt.datetime(*args, tzinfo=stats.LONDON).timestamp())


def test_core_spans_period_starting_late_in_a_day():
    # periods ending now at 23:30 (BST and GMT) and at 23:59 on a Monday: their first London day starts after its
    # core times, and the next day's core times are counted once
    for now in (london_ts(2026, 10, 20, 23, 30), london_ts(2025, 10, 29, 23, 30), london_ts(2025, 10, 27, 23, 59)):
        for span, weekdays in (("1d", 1), ("7d", 5)):
            p = stats.period_of(span, "now", now, None)
            core = stats.core_spans(p)
            assert core == sorted(core) and len(core) == 2 * weekdays
            assert stats.total(core) == weekdays * 5 * 3600


def test_core_share_at_most_one():
    # disrupted through both of Tuesday's core times, the period starting at 23:30 on Monday
    now = london_ts(2026, 10, 20, 23, 30)
    idx = stats.parse_index({"months": ["2026-10"], "lines": [{"id": "central", "name": "Central", "mode": "tube"}]})
    payload = {"month": "2026-10", "watched": {"a": [[now - 3 * 86400, now]]}, "episodes": [
        ["central", london_ts(2026, 10, 20, 7), london_ts(2026, 10, 20, 9, 30), 9, "delays", 0, "a", "signal"],
        ["central", london_ts(2026, 10, 20, 16, 30), london_ts(2026, 10, 20, 19), 9, "delays", 0, "a", "signal"]]}
    p = stats.period_of("1d", "now", now, None)
    fig = stats.period_figures(idx, stats.gather({"2026-10": payload}, idx, p["p0"], p["t1"], now), p, now)
    assert fig["lines"][0]["core_share"] == 1.0


def test_gather_reads_only_the_sources_given():
    p = stats.period_of("365d", dt.date(2025, 6, 30), NOW, IDX["first"])   # the FOI release, a gap, live recording
    both = stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW)
    assert stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW, sources=("f", "a")) == both
    assert both["watched"]["f"] and any(e["src"] == "f" for v in both["eps"].values() for e in v)
    live = stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW, sources=("a",))
    assert live["watched"] == {"f": [], "a": both["watched"]["a"]}
    assert all(e["src"] == "a" for v in live["eps"].values() for e in v)
    fig = stats.period_figures(IDX, live, p, NOW)
    assert fig["watched"]["f"] == 0 and "circle-hammersmith-city" not in [r["id"] for r in fig["lines"]]
    assert fig == stats.period_figures(IDX, stats.gather(MONTHS, IDX, p["p0"], p["t1"], NOW, sources=["a"]), p, NOW)


def test_line_report_skips_what_is_not_well_formed():
    snap, open_eps = fx.snapshot(MONTHS, NOW)
    base = stats.line_report(LINES["central"], snap, open_eps, NOW)
    noisy = copy.deepcopy(snap)
    noisy["2025-13"] = noisy["not-a-month"] = noisy["２０２５-11"] = {"episodes": [], "watched": {}}
    noisy[7] = "not a payload"
    noisy["2025-11"]["episodes"] += [
        ["central", "2025-11-01T00:00:00", NOW - 60, 9, "delays", 0, "a", None, "Minor Delays", "x", 0],
        ["central", NOW - 600, "later", 9, "delays", 0, "a", None, "Minor Delays", "x", 0],
        ["central", NOW - 600, NOW - 900, 9, "delays", 0, "a", None, "Minor Delays", "x", 0],
        ["central", NOW - 600, NOW - 60, "9", "delays", 0, "a", None, "Minor Delays", "x", 0],
        "not a row", ["central"]]
    noisy["2025-11"]["watched"]["a"] += [[NOW - 60], ["a", "b"], [NOW, NOW - 60], "x"]
    bad_open = [{"line": "central", "severity": 9, "status": "Minor Delays", "category": "delays", "planned": False,
                 "start": "yesterday", "reason": "x"},
                {"line": "central", "severity": None, "start": NOW - 60}, "not an episode"]
    assert stats.line_report(LINES["central"], noisy, open_eps + bad_open, NOW) == base


def test_line_report_open_start_as_iso_is_utc(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("needs time.tzset")
    monkeypatch.setenv("TZ", "America/New_York")   # the host's zone must not matter
    time.tzset()
    try:
        start = NOW - 3600
        ep = {"line": "dlr", "severity": 9, "status": "Minor Delays", "category": "delays", "planned": False,
              "reason": "Minor delays due to a signal failure."}
        as_int = stats.line_report(LINES["dlr"], {}, [dict(ep, start=start)], NOW)
        naive = stats.line_report(LINES["dlr"], {}, [dict(ep, start=fx.iso(start)[:-6])], NOW)   # no offset: UTC
        assert naive == as_int and as_int["now"][0]["minutes"] == 60
    finally:
        monkeypatch.undo()
        time.tzset()


def test_line_report_causes_of_open_episodes():
    r = report("central")
    opened = [x for x in r["recent"] if x["end"] is None]
    assert opened and len(r["now"]) == len(opened)
    for x in r["now"] + opened:
        assert x["cause"] == cause(x["reason"], x["planned"], x["status"])
    assert "signal" in {x["cause"] for x in r["now"]}
