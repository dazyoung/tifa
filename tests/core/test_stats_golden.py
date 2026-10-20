"""tifa.core.stats against the reference implementation's outputs for the synthetic months.

The goldens come from tools/make_stats_fixtures.py golden REFERENCE and the months from its synth command. Values
kept in full are compared as JSON; the others by a digest of their JSON, which also tells ints from floats.
"""

import datetime as dt
import itertools
import json
import urllib.parse

import pytest

from tifa.core import stats
from tools import make_stats_fixtures as fx

GOLDEN = json.loads((fx.FIXTURES / "golden.json").read_text(encoding="utf-8"))
MONTHS, INDEX = fx.read_months(fx.FIXTURES / "months")
IDX = stats.parse_index(INDEX)
NOW = GOLDEN["now"]


def as_json(x):
    return json.loads(json.dumps(fx.plain(x)))


def period(span: str, end: str, now: int = NOW):
    p = stats.period_of(span, fx.end_of(end), now, IDX["first"])
    return p, stats.gather(MONTHS, IDX, p["p0"], p["t1"], now)


def test_fixture_months_are_the_generators():
    months, index = fx.build_months()
    assert as_json(months) == MONTHS
    assert as_json(index) == INDEX


@pytest.mark.parametrize("query,today,parsed,why", GOLDEN["parse_period"])
def test_parse_period(query, today, parsed, why):
    got = stats.parse_period(urllib.parse.parse_qs(query, keep_blank_values=True), dt.date.fromisoformat(today))
    assert (as_json(got[0]), got[1]) == (parsed, why)


@pytest.mark.parametrize("key", sorted(GOLDEN["period_of"]))
def test_period_of(key):
    span, end, now = key.split("|")
    p = stats.period_of(span, fx.end_of(end), int(now), IDX["first"])
    assert fx.period_entry(p) == GOLDEN["period_of"][key]


@pytest.mark.parametrize("key", sorted(GOLDEN["gather"]))
def test_gather(key):
    _, data = period(*key.split("|"))
    assert as_json(data) == GOLDEN["gather"][key]


def test_cover_of():
    _, data = period("7d", "now")
    members = stats.members_of(IDX)
    assert {line["id"]: as_json(stats.cover_of(line, members, data["watched"])) for line in IDX["lines"]} == GOLDEN[
        "cover_of"
    ]


@pytest.mark.parametrize("key", sorted(GOLDEN["line_period"]))
def test_line_period(key):
    p, data = period(*key.split("|"))
    want = GOLDEN["line_period"][key]
    extra = fx.extra_of(stats, p)
    assert as_json(extra["core"]) == want["core"]
    members = stats.members_of(IDX)
    for line in IDX["lines"]:
        if line["id"] not in want["lines"]:
            continue
        cover = stats.cover_of(line, members, data["watched"])
        eps = data["eps"].get(line["id"], [])
        got = {
            "before": stats.line_period(eps, cover, p["p0"], p["t0"]),
            "period": stats.line_period(eps, cover, p["t0"], p["t1"], extra),
        }
        assert as_json(got) == want["lines"][line["id"]], line["id"]


@pytest.mark.parametrize("key", sorted(GOLDEN["period_figures"]))
def test_period_figures(key):
    p, data = period(*key.split("|"))
    fig = stats.period_figures(IDX, data, p, NOW)
    want = GOLDEN["period_figures"][key]
    if "sha256" in want:
        assert fx.digest(fig) == want["sha256"]
    else:
        assert as_json(fig) == want
        assert fx.digest(fig) == fx.digest(want)  # the same types too: ints stay ints


@pytest.mark.parametrize("key", sorted(GOLDEN["core_spans"]))
def test_core_spans(key):
    span, end, now = key.split("|")
    p = stats.period_of(span, fx.end_of(end), int(now), IDX["first"])
    core = stats.core_spans(p)
    assert fx.core_entry(core) == GOLDEN["core_spans"][key]
    assert core == sorted(core) and all(a < b <= c for (a, b), (c, _) in itertools.pairwise(core))  # separate


@pytest.mark.parametrize("key", sorted(GOLDEN["period_figures_late"]))
def test_period_figures_late(key):
    span, end, now = key.split("|")
    p, data = period(span, end, int(now))
    fig = stats.period_figures(IDX, data, p, int(now))
    assert fx.digest(fig) == GOLDEN["period_figures_late"][key]["sha256"]
    assert all(r["core_share"] is None or 0 <= r["core_share"] <= 1 for r in fig["lines"])


@pytest.mark.parametrize("case", range(len(GOLDEN["window_stats"])))
def test_window_stats(case):
    eps, start, now, cov, want = GOLDEN["window_stats"][case]
    eps = [tuple(e) for e in eps]
    assert stats.window_stats(eps, start, now, cov) == want


@pytest.mark.parametrize("now", fx.REPORT_NOWS)
def test_line_report(now):
    want = GOLDEN["line_report"][str(now)]
    snap, open_eps = fx.snapshot(MONTHS, now)
    for line in IDX["lines"]:
        if line["shared_with"]:
            continue
        got = stats.line_report(line, snap, open_eps, now)
        for r in got["now"] + got["recent"]:
            del r["cause"]  # Tifa adds it; the reference has none
        entry = want[line["id"]]
        if "sha256" in entry:
            assert fx.digest(got) == entry["sha256"], line["id"]
        else:
            assert as_json(got) == entry, line["id"]
