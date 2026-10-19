"""tifa.core.months: month splitting, watched runs, month rows and merging, against the reference's month files."""

import copy
import datetime as dt
import functools
import json
import random
from pathlib import Path

import pytest

from tifa.core import months as mo
from tifa.core.episodes import advance, observe
from tifa.core.timeutil import iso, month_name, month_start

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "core"
MIN = 60


@functools.cache
def golden() -> dict:
    return json.loads((FIXTURES / "golden_months.json").read_text(encoding="utf-8"))


def ts(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).timestamp())


# ---- matches the reference -----------------------------------------------------------------------------------


def test_split_months_matches_the_reference():
    g = golden()["split"]
    for r in g["ranges"]:
        first, last = r["first"], r["last"]
        names = {month_name(i) for i in range(first, last + 1)}
        got = {name: [] for name in sorted(names)}
        for line, start, end, *_ in g["episodes"]:
            for name in mo.split_months(start, end, now_ts=month_start(last)):
                if name in names:
                    got[name].append(line)
        assert got == r["months"], (first, last)


def test_spans_and_runs_match_the_reference():
    for case in golden()["clip"]:
        assert mo.clip_spans(case["spans"], case["a"], case["b"]) == case["out"]
    for case in golden()["merge_spans"]:
        assert mo.merge_spans(case["spans"]) == case["out"]
    for case in golden()["runs"]:
        assert mo.runs_of(case["times"]) == case["out"]
        run, ends = None, []
        for t in case["times"]:  # one poll at a time, as the poller keeps its run
            new = mo.extend_run(run, t)
            if run is not None and new[0] != run[0]:
                ends.append(run)
            run = new
        assert ends + [run] == case["out"]


def test_month_episode_matches_the_reference():
    for (line, start, end, severity, cat, planned, source, reason, status), row in golden()["month_episode"]:
        ep = {
            "line": line,
            "start": start,
            "end": end,
            "severity": severity,
            "category": cat,
            "planned": planned,
            "reason": reason,
            "status": status,
            "end_estimated": end is not None and severity == 6,
        }
        got = mo.month_episode(ep, source)
        assert got[:8] == row
        assert got[8:] == [
            status if isinstance(status, str) else None,
            reason if isinstance(reason, str) else None,
            int(end is not None and severity == 6),
        ]


def record(polls, payloads, whole_run: bool = False) -> tuple:
    """A poll stream through tifa.core as the poller records it: the closed episodes and the piece of watched time
    each poll added (or, with whole_run, the whole watched run) merged into stored months. Returns (stored months,
    final state, every closed episode, the months each poll touched)."""
    stored, state, run, closed_all, touched = {}, None, None, [], []

    def put(name, eps=(), watched=()):
        stored[name] = mo.merge_month(stored.get(name) or mo.new_month(name), eps, watched, mo.SOURCE_LIVE)
        names.add(name)

    for at, idx in polls:
        names = set()
        state, closed, _ = advance(state, *observe(payloads[idx]), at)
        closed_all += closed
        for e in closed:
            for name in mo.split_months(e["start"], e["end"]):
                put(name, [e])
        prev, run = run, mo.extend_run(run, at)
        piece = list(run) if whole_run else mo.run_segment(prev, run)
        if piece is not None:
            for name in mo.split_months(*piece):
                put(name, (), [piece])
        touched.append(names)
    assert all(row[2] is not None for p in stored.values() for row in p["episodes"])  # stored: closed only
    return stored, state, closed_all, touched


def replay(g: dict) -> tuple:
    """The reference's poll stream recorded into months; then, as the API serves them, the open episodes added."""
    stored, state, closed_all, _ = record(g["polls"], g["payloads"])
    served = dict(stored)
    for e in state["open"]:
        for name in mo.split_months(e["start"], None, now_ts=state["last_poll"]):
            served[name] = mo.merge_month(served[name], [e])
    return served, state, closed_all


def test_month_payloads_match_the_references_month_files():
    g = golden()["stream"]
    served, state, closed_all = replay(g)
    assert sorted(served) == sorted(g["months"]) == ["2026-09", "2026-10", "2026-11"]
    for name, ref in g["months"].items():
        mine = served[name]
        assert mine["month"] == ref["month"] == name
        assert [row[:8] for row in mine["episodes"]] == ref["episodes"], name
        assert mine["watched"] == ref["watched"], name
        assert all(len(row) == 11 for row in mine["episodes"])
    assert [dict(c, start=iso(c["start"]), end=iso(c["end"])) for c in closed_all] == g["closed"]
    assert [dict(e, start=iso(e["start"])) for e in state["open"]] == g["open"]
    assert iso(state["last_poll"]) == g["last_poll"]


def test_month_rows_carry_status_reason_and_end_estimated():
    g = golden()["stream"]
    served, _, _ = replay(g)
    logged = {(c["line"], c["severity"], int(c["planned"]), ts(c["start"])): c for c in g["closed"]}
    seen = 0
    for payload in served.values():
        for row in payload["episodes"]:
            c = logged.get((row[0], row[3], row[5], row[1]))
            if row[2] is None:
                assert c is None or ts(c["end"]) < row[1] or row[10] == 0
                continue
            assert (row[8], row[9], row[10]) == (c["status"], c["reason"], int(c["end_estimated"]))
            assert ts(c["end"]) == row[2]
            seen += 1
    assert seen >= len(g["closed"])
    estimated = sum(row[10] for p in served.values() for row in p["episodes"])
    assert estimated >= 10


def test_the_stream_covers_month_ends_and_pauses():
    g = golden()["stream"]
    sep, octo = g["months"]["2026-09"], g["months"]["2026-10"]
    oct1 = ts("2026-10-01T00:00:00+00:00")
    over = {(e[0], e[1]) for e in sep["episodes"]} & {(e[0], e[1]) for e in octo["episodes"]}
    assert over  # some run over the month end
    assert any(e[2] == oct1 for e in sep["episodes"])  # one ends exactly as October begins ...
    assert not any(e[2] == oct1 and e[1] < oct1 for e in octo["episodes"])  # ... and is not in October
    assert any(e[1] == oct1 for e in octo["episodes"])
    assert len(octo["watched"]["a"]) > 2  # pauses split the watched time
    assert any(e[2] is None for e in g["months"]["2026-11"]["episodes"])


# ---- the reference's own month case ---------------------------------------------------------------------------


def closed_ep(line, sev, cat, planned, start, end, reason, status, estimated=False):
    return {
        "line": line,
        "name": line.title(),
        "mode": "tube",
        "severity": sev,
        "status": status,
        "category": cat,
        "planned": planned,
        "start": ts(start),
        "end": ts(end),
        "end_estimated": estimated,
        "reason": reason,
    }


def test_months_ongoing_and_closing_later():
    a = ts("2026-09-30T22:00:00+00:00")
    now = a + 4 * 3600 + 15 * MIN  # 02:15 on 1 October
    severe = closed_ep(
        "district",
        6,
        "delays",
        False,
        "2026-09-30T23:00:00+00:00",
        "2026-10-01T01:00:00+00:00",
        "Severe delays due to a signal failure at Earl's Court.",
        "Severe Delays",
    )
    info = closed_ep(
        "bakerloo",
        19,
        "info",
        False,
        "2026-09-30T22:15:00+00:00",
        "2026-09-30T22:45:00+00:00",
        "Lift out of service at Oxford Circus.",
        "Information",
    )
    minor_open = {  # with the latest reason, from the last poll
        "line": "central",
        "name": "Central",
        "mode": "tube",
        "severity": 9,
        "status": "Minor Delays",
        "category": "delays",
        "planned": False,
        "start": ts("2026-09-30T23:30:00+00:00"),
        "reason": "Minor delays due to a signal failure.",
    }
    stored = {}
    for e in (info, severe):
        for name in mo.split_months(e["start"], e["end"]):
            stored[name] = mo.merge_month(stored.get(name) or mo.new_month(name), [e])
    for name in mo.split_months(a, now):
        stored[name] = mo.merge_month(stored.get(name) or mo.new_month(name), (), [[a, now]])
    served = {name: mo.merge_month(p, [minor_open]) for name, p in stored.items()}

    sep, octo = served["2026-09"], served["2026-10"]
    severe_row = ["district", severe["start"], severe["end"], 6, "delays", 0, "a", "signal"]
    assert [r[:8] for r in sep["episodes"]] == [
        ["bakerloo", info["start"], info["end"], 19, "info", 0, "a", "equipment"],
        severe_row,
        ["central", minor_open["start"], None, 9, "delays", 0, "a", "signal"],  # ongoing: no end yet
    ]
    assert octo["episodes"] == sep["episodes"][1:]  # both run on into October; the info does not
    assert sep["watched"] == {"a": [[a, ts("2026-10-01T00:00:00+00:00")]], "f": []}
    assert octo["watched"] == {"a": [[ts("2026-10-01T00:00:00+00:00"), now]], "f": []}

    ended = dict(minor_open, end=now + 15 * MIN, end_estimated=False)  # the delays end at 02:30
    for name in mo.split_months(ended["start"], ended["end"]):
        stored[name] = mo.merge_month(stored[name], [ended])
        assert stored[name]["episodes"][-1][:8] == [
            "central",
            minor_open["start"],
            now + 15 * MIN,
            9,
            "delays",
            0,
            "a",
            "signal",
        ]
    assert mo.merge_month(stored["2026-09"], [minor_open]) == stored["2026-09"]  # the closed copy wins


# ---- merging -------------------------------------------------------------------------------------------------

OCT = "2026-10"
O1 = ts("2026-10-01T00:00:00+00:00")
N1 = ts("2026-11-01T00:00:00+00:00")


def row(line, start, end, source="a", sev=9, planned=0, reason="Minor delays.", estimated=0):
    return [line, start, end, sev, "delays", planned, source, None, "Minor Delays", reason, estimated]


def test_merge_month_dedupes_and_keeps_other_sources():
    payload = mo.merge_month(
        mo.new_month(OCT), [row("central", O1 + 60, O1 + 600, "f"), row("central", O1 + 60, O1 + 120)]
    )
    before = copy.deepcopy(payload)
    newer = row("central", O1 + 60, O1 + 900, reason="Updated.")
    out = mo.merge_month(payload, [newer, row("central", O1 + 60, None)], [[O1 + 60, O1 + 900]])
    assert payload == before  # not changed
    assert out["episodes"] == [newer, row("central", O1 + 60, O1 + 600, "f")]  # by start, line, source
    assert out["watched"] == {"a": [[O1 + 60, O1 + 900]], "f": []}
    planned = row("central", O1 + 60, O1 + 300, planned=1)
    assert len(mo.merge_month(out, [planned])["episodes"]) == 3  # planned is another episode
    assert len(mo.merge_month(out, [row("central", O1 + 60, O1 + 300, sev=6)])["episodes"]) == 3


def test_merge_month_order():  # as the reference wrote month files: start, line, source, severity, planned
    rows = [
        row("central", O1 + 60, None, sev=6, planned=1),
        row("central", O1 + 60, O1 + 90, sev=6),
        row("central", O1 + 60, O1 + 90, "f", sev=2),
        row("central", O1 + 60, O1 + 90, sev=2),
        row("bakerloo", O1 + 60, O1 + 90, "f"),
        row("bakerloo", O1, O1 + 90, "f"),
    ]
    out = mo.merge_month(mo.new_month(OCT), rows)
    assert out["episodes"] == [rows[5], rows[4], rows[3], rows[1], rows[0], rows[2]]


def test_merge_month_open_never_replaces_closed_but_closed_replaces_open():
    opened = mo.merge_month(mo.new_month(OCT), [row("central", O1, None)])
    closed = mo.merge_month(opened, [row("central", O1, O1 + 60)])
    assert closed["episodes"] == [row("central", O1, O1 + 60)]
    assert mo.merge_month(closed, [row("central", O1, None)]) == closed


def test_merge_month_leaves_out_episodes_outside_the_month():
    eps = [
        row("a1", O1 - 600, O1),
        row("a2", O1 - 600, O1 + 1),
        row("a3", O1, O1),
        row("a4", N1, N1 + 60),
        row("a5", N1 - 1, None),
        row("a6", O1 - 3600, None),
        row("a7", N1 - 60, N1 + 60),
    ]
    out = mo.merge_month(mo.new_month(OCT), eps)
    assert [r[0] for r in out["episodes"]] == ["a6", "a2", "a3", "a7", "a5"]


def test_merge_month_clips_and_joins_watched_spans():
    out = mo.merge_month(mo.new_month(OCT), [], [[O1 - 600, O1 + 600], [N1 - 60, N1 + 600], [O1 + 5, O1 + 5]])
    assert out["watched"]["a"] == [[O1, O1 + 600], [N1 - 60, N1]]
    out = mo.merge_month(out, [], [[O1 + 600, O1 + 1200], [O1 + 300, O1 + 400]])
    assert out["watched"]["a"] == [[O1, O1 + 1200], [N1 - 60, N1]]
    foi = mo.merge_month(out, [], [[O1 - 10, N1 + 10]], mo.SOURCE_FOI)
    assert foi["watched"] == {"a": out["watched"]["a"], "f": [[O1, N1]]}


def test_merge_month_takes_episode_dicts_and_iso_times():
    ep = {
        "line": "central",
        "severity": 9,
        "planned": False,
        "category": "delays",
        "status": "Minor Delays",
        "start": "2026-10-01T00:01:00+00:00",
        "end": "2026-10-01T00:05:00+00:00",
        "end_estimated": True,
        "reason": "Minor delays due to a signal failure.",
    }
    out = mo.merge_month(mo.new_month(OCT), [ep], (), mo.SOURCE_FOI)
    assert out["episodes"] == [
        [
            "central",
            O1 + 60,
            O1 + 300,
            9,
            "delays",
            0,
            "f",
            "signal",
            "Minor Delays",
            "Minor delays due to a signal failure.",
            1,
        ]
    ]
    assert mo.row_dict(out["episodes"][0]) == {
        "line": "central",
        "start": O1 + 60,
        "end": O1 + 300,
        "severity": 9,
        "category": "delays",
        "planned": False,
        "source": "f",
        "cause": "signal",
        "status": "Minor Delays",
        "reason": "Minor delays due to a signal failure.",
        "end_estimated": True,
    }
    assert mo.row_dict(out["episodes"][0][:8]).keys() == set(mo.ROW_FIELDS[:8])


def test_split_months():
    assert mo.split_months(O1 - 60, O1) == ["2026-09"]  # ends as October begins
    assert mo.split_months(O1, O1) == ["2026-10"]  # zero length at the start
    assert mo.split_months(O1 - 60, O1 + 1) == ["2026-09", "2026-10"]
    assert mo.split_months(O1, N1 + 1) == ["2026-10", "2026-11"]
    assert mo.split_months(O1 - 60, None, now_ts=N1) == ["2026-09", "2026-10", "2026-11"]
    assert mo.split_months(ts("2026-12-31T23:00:00+00:00"), ts("2027-01-01T00:00:01+00:00")) == ["2026-12", "2027-01"]
    with pytest.raises(ValueError):
        mo.split_months(O1, None)


def test_extend_run():
    assert mo.extend_run(None, 100) == [100, 100]
    assert mo.extend_run([100, 100], 1000) == [100, 1000]  # 900 later: the same run
    assert mo.extend_run([100, 1000], 1901) == [1901, 1901]  # 901 later: a new one
    assert mo.extend_run([100, 1000], 999) == [100, 1000]  # earlier: nothing changes


def test_run_segment():
    assert mo.run_segment(None, [100, 100]) == [100, 100]  # the first poll
    assert mo.run_segment([100, 100], [100, 160]) == [100, 160]  # extended
    assert mo.run_segment([100, 1000], [100, 1900]) == [1000, 1900]
    assert mo.run_segment([100, 1000], [1901, 1901]) == [1901, 1901]  # a new run
    assert mo.run_segment([100, 1000], [100, 1000]) is None  # a poll at or before the end: nothing added
    for prev, at, piece in (
        (None, 5, [5, 5]),
        ([0, 10], 10, None),
        ([0, 10], 9, None),
        ([0, 10], 910, [10, 910]),
        ([0, 10], 911, [911, 911]),
    ):
        assert mo.run_segment(prev, mo.extend_run(prev, at)) == piece, (prev, at)


def test_recording_run_pieces_matches_recording_whole_runs():
    """Merging only each poll's piece of watched time gives the same months as merging the whole run every poll,
    and a poll touches at most two months, though the stream runs over months with pauses and backward polls."""
    g = golden()["stream"]
    by_piece, *_, touched = record(g["polls"], g["payloads"])
    by_run, *_ = record(g["polls"], g["payloads"], whole_run=True)
    assert by_piece == by_run
    assert max(map(len, touched)) <= 2

    rng = random.Random(7)
    payloads = [
        golden()["stream"]["payloads"][0],
        [  # the stream's first payload, and one quiet line
            {
                "id": "central",
                "name": "Central",
                "modeName": "tube",
                "lineStatuses": [{"statusSeverity": 10, "statusSeverityDescription": "Good Service"}],
            }
        ],
    ]
    for _ in range(20):
        at, polls = ts("2026-08-31T22:00:00+00:00"), []
        for _ in range(400):
            at += rng.choice((60, 60, 60, 300, 899, 900, 901, -120, 0, 3 * 86400, 12 * 3600))
            polls.append((at, rng.randrange(2)))
        by_piece, *_, touched = record(polls, payloads)
        by_run, *_ = record(polls, payloads, whole_run=True)
        assert by_piece == by_run
        assert max(map(len, touched)) <= 2


def test_a_long_run_touches_at_most_two_months_a_poll():
    run, start, crossed = None, ts("2026-01-01T00:00:00+00:00"), 0
    for at in range(start, start + 400 * 86400, 840):
        prev, run = run, mo.extend_run(run, at)
        names = mo.split_months(*mo.run_segment(prev, run))
        assert len(names) <= 2
        crossed += len(names) == 2
    assert run == [start, at] and len(mo.split_months(*run)) == 14
    assert 0 < crossed <= 13  # only the polls just after a month end touch two


def test_new_month_checks_the_name():
    assert mo.new_month(OCT) == {"month": OCT, "episodes": [], "watched": {"a": [], "f": []}}
    for bad in ("2026-13", "\uff12\uff10\uff12\uff16-10"):  # the second in fullwidth digits
        with pytest.raises(ValueError):
            mo.new_month(bad)
