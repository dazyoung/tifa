"""The public REST API over the three recorded days: each route against the scenario, and the routes against each
other (status, changes, months, disruptions, stats and coverage tell one story)."""

from __future__ import annotations

import pytest

from tests.integration import scenario as sc
from tests.integration.conftest import get
from tifa.core.causes import CAUSES
from tifa.core.tfl import CATEGORY_IDS, MODE_ORDER, current
from tifa.core.timeutil import day_bounds, iso

OCTOBER = "2026-10"


# ---------------------------------------------------------------- expected values, from the scenario


def rows(world, *, with_open: bool = True) -> list[list]:
    eps = list(world.expected.closed) + (list(world.expected.open) if with_open else [])
    return sorted((sc.month_row(ep) for ep in eps), key=sc.row_order)


def api_episode(row: list, now: int = sc.NOW) -> dict:
    """A month row as the API shows an episode."""
    line, start, end = row[0], row[1], row[2]
    return {
        "line": line,
        "name": sc.NAMES[line],
        "mode": sc.MODES[line],
        "start": iso(start),
        "end": None if end is None else iso(end),
        "end_estimated": bool(row[10]),
        "minutes": int(round(((now if end is None else end) - start) / 60)),
        "severity": row[3],
        "status": row[8],
        "category": row[4],
        "planned": bool(row[5]),
        "cause": row[7],
        "reason": row[9],
        "source": "a",
    }


def total(spans) -> float:
    return sum(b - a for a, b in spans)


def merged(spans) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        elif b > a:
            out.append([a, b])
    return out


def intersect(xs, ys) -> list[list[float]]:
    return [[max(a, c), min(b, d)] for a, b in xs for c, d in ys if min(b, d) > max(a, c)]


def figures(world, t0: int, t1: int) -> dict:
    """The network's figures over [t0, t1] worked out from the scenario: open episodes run to the last poll."""
    watched = intersect(world.expected.runs, [[t0, t1]])
    w = total(watched)
    last = world.polls[-1]
    eps = [dict(r, end=r["end"] if r.get("end") is not None else last)
           for r in world.expected.closed + world.expected.open]  # fmt: skip
    shown = [e for e in eps if e["category"] != "info" and e["end"] > t0 and e["start"] < t1]
    per_line = {}
    for line, _, _ in sc.LINES:
        spans = merged([[max(e["start"], t0), min(e["end"], t1)] for e in shown if e["line"] == line])
        per_line[line] = total(intersect(spans, watched))
    dis = sum(per_line.values())
    lines = len(sc.LINES)
    return {
        "watched": w / (t1 - t0),
        "unplanned": sum(1 for e in shown if not e["planned"]),
        "planned": sum(1 for e in shown if e["planned"]),
        "disrupted_s": dis,
        "good_pct": 100 - dis / (w * lines) * 100,
        "per_line": per_line,
        "unplanned_s": sum(
            total(intersect(merged([[max(e["start"], t0), min(e["end"], t1)] for e in shown
                                    if e["line"] == line and not e["planned"]]), watched))
            for line, _, _ in sc.LINES
        ),
    }  # fmt: skip


# ---------------------------------------------------------------- live


def test_status_is_tfls_answer_at_the_last_poll_with_the_open_episodes(world, api):
    data = get(api, "/v1/status")
    now = current(sc.payload(world.polls[-1]))
    assert data["lines"] == now["lines"]
    assert (data["disrupted"], data["total"]) == (now["disrupted"], now["total"]) == (2, 19)
    assert [x["id"] for x in data["lines"][:2]] == ["bakerloo", "elizabeth"]  # live problems before planned works
    want = [api_episode(r) for r in rows(world) if r[2] is None]
    for ep in want:
        ep.pop("end_estimated")
    assert data["open"] == want
    assert data["last_poll"] == iso(world.polls[-1]) and data["stale"] is False


def test_health(world, api):
    r = api.get("/healthz")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()["data"]
    assert data["status"] == "ok" and data["last_poll"] == iso(world.polls[-1])
    assert data["last_poll_age_s"] == sc.NOW - world.polls[-1] and data["tfl_auth"] == "ok"


def test_changes_from_before_the_first_poll_give_the_whole_history(world, api):
    data = get(api, "/v1/changes", since=world.polls[0] - 1)
    assert data["polls"] == world.polls
    closed = sorted(rows(world, with_open=False), key=lambda r: (r[2], r[1], r[0], r[3], r[5]))
    assert data["closed"] == [api_episode(r) for r in closed]
    assert data["open"] == get(api, "/v1/status")["open"]
    assert data["status"]["lines"] == get(api, "/v1/status")["lines"]
    assert data["cursor"] == world.polls[-1] and data["last_poll"] == iso(world.polls[-1])


def test_changes_include_episodes_a_gap_closed_at_the_cursor(world, api):
    """The long outage ends what was open at its last poll: a client whose cursor is that poll still gets them."""
    last_before = max(t for t in world.polls if t < sc.LONG_OUTAGE[0] * 60)
    data = get(api, "/v1/changes", since=last_before)
    ends = {(e["line"], e["end"], e["end_estimated"]) for e in data["closed"]}
    assert {("district", iso(last_before), True), ("piccadilly", iso(last_before), True)} <= ends
    assert data["polls"][0] > sc.LONG_OUTAGE[-1] * 60


def test_changes_refuse_a_cursor_over_35_days_old(api):
    r = api.get("/v1/changes", params={"since": sc.NOW - 36 * sc.DAY})
    assert r.status_code == 410 and r.json()["error"]["code"] == "cursor_expired"


# ---------------------------------------------------------------- history


def test_the_months_index(world, api):
    data = get(api, "/v1/months")
    assert data["months"] == [OCTOBER] and data["first"] == iso(world.polls[0])
    order = sorted(sc.LINES, key=lambda x: (MODE_ORDER[x[2]], x[1].casefold(), x[0]))
    assert data["lines"] == [{"id": i, "name": n, "mode": m, "shared_with": []} for i, n, m in order]
    assert data["categories"] == list(CATEGORY_IDS)
    assert data["causes"] == [{"code": c, "label": label} for c, label, _ in CAUSES]


def test_the_month_has_every_episode_and_the_watched_runs(world, api):
    r = api.get(f"/v1/months/{OCTOBER}")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"  # the current UTC month
    data = r.json()["data"]
    assert data["episodes"] == rows(world)  # stored closed episodes, and the open ones with end null
    assert [r[2] for r in data["episodes"] if r[2] is None] == [None, None]
    assert data["watched"] == {"a": world.expected.runs}  # only published sources: never "f"
    etag = r.headers["etag"]
    again = api.get(f"/v1/months/{OCTOBER}", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""


@pytest.mark.parametrize("month", ["2026-09", "2026-11"])
def test_months_without_data_are_404(api, month):
    r = api.get(f"/v1/months/{month}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_month"


def test_disruptions_page_through_the_month(world, api):
    want = [api_episode(r) for r in rows(world)]
    seen, cursor = [], None
    while True:
        params = {"from": "2026-10-24", "to": "2026-10-26", "limit": 5, **({"cursor": cursor} if cursor else {})}
        data = get(api, "/v1/disruptions", **params)
        assert data["total"] == len(want) == 18 and data["count"] == len(data["episodes"]) <= 5
        assert data["from"] == iso(sc.START) and data["to"] == iso(sc.END)  # London dates; "to" takes all its day
        seen += data["episodes"]
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert seen == want  # oldest first, each once


@pytest.mark.parametrize(
    ("params", "lines"),
    [
        ({"line": "northern"}, ["northern", "northern"]),
        ({"category": "info"}, ["piccadilly", "piccadilly"]),
        ({"cause": "unknown"}, ["liberty"]),
        ({"cause": "fire,staff"}, ["dlr", "victoria"]),
        ({"planned": "true"}, ["district", "mildmay", "metropolitan", "district", "elizabeth"]),
        ({"line": "jubilee,hammersmith-city", "from": "2026-10-25", "to": "2026-10-25"},
         ["jubilee", "hammersmith-city"]),
        ({"from": iso(sc.ldn(26, 23, 0)), "to": iso(sc.END)}, ["bakerloo", "elizabeth"]),
    ],
)  # fmt: skip
def test_disruptions_filter(api, params, lines):
    params = {"from": "2026-10-24", "to": "2026-10-26", **params}
    data = get(api, "/v1/disruptions", **params)
    assert [e["line"] for e in data["episodes"]] == lines


def test_disruptions_refuse_an_unpublished_source(api):
    r = api.get("/v1/disruptions", params={"source": "f"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_source"


def test_coverage_shows_the_gaps_and_the_polls_per_day(world, api):
    data = get(api, "/v1/coverage", **{"from": "2026-10-24", "to": "2026-10-26"})
    runs = world.expected.runs
    wall = sc.END - sc.START
    assert data["from"] == iso(sc.START) and data["to"] == iso(sc.END)
    assert data["sources"] == {
        "a": {
            "watched": [[iso(a), iso(b)] for a, b in runs],
            "watched_s": total(runs),
            "coverage_pct": round(100 * total(runs) / wall, 2),
        }
    }
    assert data["coverage_pct"] == data["sources"]["a"]["coverage_pct"] > 99.4
    gaps = [(sc.START, runs[0][0]), (runs[0][1], runs[1][0]), (runs[1][1], sc.END)]
    assert data["gaps"] == [{"start": iso(a), "end": iso(b), "minutes": int(round((b - a) / 60))} for a, b in gaps]
    assert [d["date"] for d in data["days"]] == ["2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"]
    assert sum(d["polls"] for d in data["days"]) == len(world.polls)
    assert [d["polls"] for d in data["days"]] == [60, 1440 - 5, 1440 - 20, 1440 - 1]
    failed = [(f["slot"], f["code"]) for d in data["days"] for f in d["failed"]]
    assert failed == [(f["slot"], f["code"]) for f in world.failed]
    assert all(f["at"] == iso(f["slot"] * 60) for d in data["days"] for f in d["failed"])


def test_coverage_and_months_agree(world, api):
    month = get(api, f"/v1/months/{OCTOBER}")
    coverage = get(api, "/v1/coverage", **{"from": iso(sc.ldn(1, 0)), "to": iso(sc.NOW)})
    assert coverage["sources"]["a"]["watched"] == [[iso(a), iso(b)] for a, b in month["watched"]["a"]]


# ---------------------------------------------------------------- figures


@pytest.mark.parametrize("day", sc.DAYS)
def test_a_london_days_figures(world, api, day):
    data = get(api, "/v1/stats", span="1d", end=day)
    t0, t1 = day_bounds(day)
    assert (data["t0"], data["t1"]) == (t0, t1)
    assert data["t1"] - data["t0"] == (25 if day == "2026-10-25" else 24) * 3600
    want = figures(world, t0, t1)
    assert data["watched"]["a"] == pytest.approx(want["watched"]) and data["watched"]["f"] == 0
    assert data["lines_watched"] == len(sc.LINES)
    assert (data["unplanned"], data["planned"]) == (want["unplanned"], want["planned"])
    assert data["disrupted_s"] == pytest.approx(want["disrupted_s"])
    assert data["good_pct"] == pytest.approx(want["good_pct"])
    if day == "2026-10-24":
        assert data["comparable"] is False and data["before"] is None  # nothing was watched the day before
    else:  # against the London day before, watched enough to compare
        before = figures(world, *day_bounds(sc.DAYS[sc.DAYS.index(day) - 1]))
        assert data["comparable"] is True
        assert (data["before"]["unplanned"], data["before"]["planned"]) == (before["unplanned"], before["planned"])
        assert data["before"]["disrupted_s"] == pytest.approx(before["disrupted_s"])
    by_line = {x["id"]: x for x in data["lines"]}
    for line, seconds in want["per_line"].items():
        assert by_line[line]["disrupted_s"] == pytest.approx(seconds), line
    share = {k: v / (want["watched"] * (t1 - t0)) for k, v in want["per_line"].items()}
    assert data["most_disrupted"]["line"] in {k for k, v in share.items() if v == pytest.approx(max(share.values()))}
    assert data["period"]["label"] and data["period"]["next"] in ("now", "2026-10-25", "2026-10-26")


def test_the_long_sunday(world, api):
    data = get(api, "/v1/stats", span="1d", end="2026-10-25")
    assert data["t1"] - data["t0"] == 25 * 3600
    assert data["watched"]["a"] == pytest.approx((25 * 3600 - 21 * 60) / (25 * 3600), abs=0.001)
    longest = data["longest_unplanned"]
    assert (longest["line"], longest["status"], longest["cause"]) == ("jubilee", "Severe Delays", "customer")
    assert round(longest["duration_s"] / 60) == 110  # 00:50 BST to 01:40 GMT
    line = get(api, "/v1/stats", span="1d", end="2026-10-25", line="jubilee")
    assert line["line"] == "jubilee" and line["lines_watched"] == 1 and line["unplanned"] == 1


def test_a_week_has_one_25_hour_day(world, api):
    data = get(api, "/v1/stats", span="7d", end="2026-10-26")
    assert data["t0"] == day_bounds("2026-10-20")[0] and data["t1"] == sc.END
    bins = data["bins"]["items"]
    assert data["bins"]["per"] == "day" and len(bins) == 7
    assert [b["t1"] - b["t0"] for b in bins] == [86400] * 5 + [90000, 86400]
    assert data["watched"]["a"] == pytest.approx(total(world.expected.runs) / (7 * 86400 + 3600))
    for day, b in zip(sc.DAYS, bins[4:], strict=True):
        assert b["disrupted_s"] == pytest.approx(figures(world, *day_bounds(day))["disrupted_s"])
    busiest = max(sc.DAYS, key=lambda d: figures(world, *day_bounds(d))["unplanned_s"])
    assert data["busiest_day"]["date"] == busiest
    assert data["busiest_day"]["unplanned_s"] == pytest.approx(figures(world, *day_bounds(busiest))["unplanned_s"])


def test_the_line_report(world, api):
    data = get(api, "/v1/lines/central")
    assert data["id"] == "central" and data["now"] == [] and data["source"] == "a"
    (recent,) = data["recent"]
    assert (recent["cause"], recent["reason"]) == (
        "signal",
        "Minor delays while we fix a signal failure at Liverpool Street.",
    )
    assert data["stats"]["7d"]["episodes"] == 1
    assert data["starts_by_hour"][7] == 1  # London hours
    jubilee = get(api, "/v1/lines/jubilee")
    assert jubilee["starts_by_hour"][0] == 1  # 00:50 BST
    bakerloo = get(api, "/v1/lines/bakerloo")
    assert [(e["status"], e["cause"]) for e in bakerloo["now"]] == [("Minor Delays", "signal")]
    r = api.get("/v1/lines/circle-hammersmith-city")  # the FOI release's shared line: not published
    assert r.status_code == 404


def test_lines_and_meta(world, api):
    lines = get(api, "/v1/lines")["lines"]
    assert {x["id"] for x in lines} == {lid for lid, _, _ in sc.LINES}
    assert all(x["first_seen"] == iso(world.polls[0]) and x["last_seen"] == iso(world.polls[-1]) for x in lines)
    meta = get(api, "/v1/meta")
    assert [s["id"] for s in meta["sources"]] == ["a"]
    assert meta["attribution"] == "Powered by TfL Open Data"
