"""tifa.core.timeutil: London days and hours over the clock changes, the UK-rule fallback and UTC months."""

import datetime as dt
import functools
import json
import zoneinfo
from pathlib import Path

import pytest

from tifa.core import timeutil as tu

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "core"
ZONE = zoneinfo.ZoneInfo("Europe/London")


@functools.cache
def golden() -> dict:
    return json.loads((FIXTURES / "golden_time.json").read_text(encoding="utf-8"))


def ts(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).timestamp())


# ---- matches the reference -----------------------------------------------------------------------------------


def test_iso_and_epoch_match_the_reference():
    for t, text in golden()["iso"]:
        assert tu.iso(t) == text
    for text, t in golden()["epoch"]:
        assert tu.epoch(text) == t


def test_london_wall_time_hour_weekday_and_year_match_the_reference():
    for t, wall, hour, weekday, year in golden()["london"]:
        assert (tu.london(t).isoformat(), tu.london_hour(t), tu.london_weekday(t), tu.london_year(t)) == (
            wall,
            hour,
            weekday,
            year,
        ), t
        assert tu.london_day(t) == wall[:10]
    for t, year in golden()["london_year"]:
        assert tu.london_year(t) == year


def test_year_starts_and_london_days_match_the_reference():
    for year, start in golden()["year_start"]:
        assert tu.year_start(year) == start
    for a, b, days in golden()["london_days"]:
        assert [list(d) for d in tu.london_days(a, b)] == days, (a, b)


def test_utc_months_match_the_reference():
    for t, i, name, start, end in golden()["months"]:
        assert (tu.month_index(t), tu.month_name(i), tu.month_start(i), tu.month_start(i + 1)) == (i, name, start, end)
        assert tu.month_key(t) == name
        assert tu.month_bounds(name) == (start, end)
        assert tu.parse_month(name) == i


def test_midnights_and_month_keys_match_the_reference():
    g = golden()
    for day, t in g["midnight"]:
        assert tu.london_midnight(day) == t
        assert tu.london_midnight(dt.date.fromisoformat(day)) == t
    for a, b, out in g["midnights"]:
        assert [[t, d.isoformat()] for t, d in tu.midnights(a, b)] == out, (a, b)
    for a, b, keys in g["month_keys"]:
        assert tu.month_keys(a, b) == keys


def test_uk_rule_fallback_matches_the_reference_fallback():
    uk = tu.UKTime()
    for t, wall in golden()["fallback_london"]:
        assert dt.datetime.fromtimestamp(t, uk).isoformat() == wall, t
    for day, t in golden()["fallback_midnight"]:
        d = dt.date.fromisoformat(day)
        assert dt.datetime(d.year, d.month, d.day, tzinfo=uk).timestamp() == t


# ---- the fallback against the time zone database -------------------------------------------------------------


def _around_changes(years):
    for year in years:
        for month in (3, 10):
            start = ts(f"{year}-{month:02d}-24T00:00:00+00:00")
            yield from range(start, start + 9 * tu.DAY, 15 * 60)


def test_fallback_agrees_with_zoneinfo_from_utc():
    uk = tu.UKTime()
    for t in _around_changes(range(1996, 2041)):
        mine, real = dt.datetime.fromtimestamp(t, uk), dt.datetime.fromtimestamp(t, ZONE)
        assert (mine.replace(tzinfo=None), mine.fold, mine.utcoffset(), mine.tzname()) == (
            real.replace(tzinfo=None),
            real.fold,
            real.utcoffset(),
            real.tzname(),
        ), t
        assert mine.dst() == real.dst()


def test_fallback_agrees_with_zoneinfo_for_wall_times_including_the_skipped_and_repeated_hours():
    uk = tu.UKTime()
    for year in (1996, 2020, 2026, 2027, 2040):
        for month in (3, 10):
            for day in range(24, 32):
                for minute in range(0, 24 * 60, 10):
                    for fold in (0, 1):
                        wall = dt.datetime(year, month, day, minute // 60, minute % 60, fold=fold)
                        assert wall.replace(tzinfo=uk).timestamp() == wall.replace(tzinfo=ZONE).timestamp(), wall
                        assert wall.replace(tzinfo=uk).utcoffset() == wall.replace(tzinfo=ZONE).utcoffset(), wall


def test_fallback_is_used_without_the_time_zone_database(monkeypatch):
    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(tu.zoneinfo, "ZoneInfo", missing)
    zone = tu._london_zone()
    assert isinstance(zone, tu.UKTime)
    assert dt.datetime.fromtimestamp(ts("2026-07-01T12:00:00+00:00"), zone).hour == 13
    assert zone.utcoffset(None) is None and zone.tzname(None) is None and repr(zone) == "UKTime()"


# ---- the clock changes ----------------------------------------------------------------------------------------


def test_london_day_lengths_over_the_clock_changes():
    assert day_hours("2026-03-29") == 23
    assert day_hours("2026-10-25") == 25
    assert day_hours("2026-10-26") == 24
    assert tu.day_bounds("2026-10-25") == (ts("2026-10-24T23:00:00+00:00"), ts("2026-10-26T00:00:00+00:00"))


def day_hours(day: str) -> float:
    a, b = tu.day_bounds(day)
    return (b - a) / 3600


def test_london_hour_across_dst():  # as unplanned starts are counted by hour
    assert tu.london_hour(ts("2026-10-24T07:10:00+00:00")) == 8  # BST
    assert tu.london_hour(ts("2026-10-26T08:10:00+00:00")) == 8  # GMT after 25 October
    assert tu.london_hour(ts("2026-10-26T17:30:00+00:00")) == 17
    assert tu.london_hour(ts("2026-03-28T08:05:00+00:00")) == 8  # GMT
    assert tu.london_hour(ts("2026-03-30T07:05:00+00:00")) == 8  # BST after 29 March
    assert tu.london_hour(ts("2026-10-25T00:30:00+00:00")) == 1  # the first 01:30
    assert tu.london_hour(ts("2026-10-25T01:30:00+00:00")) == 1  # the second
    assert tu.london_day(ts("2026-10-24T23:30:00+00:00")) == "2026-10-25"


def test_london_days_over_the_clock_change():
    a, b = ts("2026-10-24T23:00:00+00:00"), ts("2026-10-25T23:59:00+00:00")  # all of the 25-hour Sunday
    assert tu.london_days(a, b) == [("2026-10-25", a, b, ts("2026-10-26T00:00:00+00:00"))]
    assert tu.london_days(a, a) == [("2026-10-25", a, a, ts("2026-10-26T00:00:00+00:00"))]
    spring = tu.london_days(ts("2026-03-28T12:00:00+00:00"), ts("2026-03-30T12:00:00+00:00"))
    assert [d for d, *_ in spring] == ["2026-03-28", "2026-03-29", "2026-03-30"]
    assert spring[1][2] - spring[1][1] == 23 * 3600


def test_midnights_strictly_between():
    a, b = ts("2026-10-24T23:00:00+00:00"), ts("2026-10-26T00:00:00+00:00")
    assert tu.midnights(a, b) == []
    assert tu.midnights(a - 1, b + 1) == [(a, dt.date(2026, 10, 25)), (b, dt.date(2026, 10, 26))]


# ---- UTC ----------------------------------------------------------------------------------------------------


def test_epoch_takes_a_time_without_offset_as_utc():
    assert tu.epoch("2026-10-19T08:00:00") == ts("2026-10-19T08:00:00+00:00")
    assert tu.epoch("2026-10-19T08:00:00Z") == ts("2026-10-19T08:00:00+00:00")
    assert tu.iso(tu.epoch("2026-10-19T09:00:00+01:00")) == "2026-10-19T08:00:00+00:00"


def test_utc_day_and_months():
    t = ts("2026-10-31T23:30:00+00:00")
    assert (tu.utc_day(t), tu.london_day(t)) == ("2026-10-31", "2026-10-31")  # GMT again by then
    assert tu.utc_day(ts("2026-06-30T23:30:00+00:00")) == "2026-06-30"
    assert tu.london_day(ts("2026-06-30T23:30:00+00:00")) == "2026-07-01"
    assert tu.month_key(ts("2026-12-31T23:59:59+00:00")) == "2026-12"
    assert tu.month_bounds("2026-12") == (ts("2026-12-01T00:00:00+00:00"), ts("2027-01-01T00:00:00+00:00"))
    assert tu.month_keys(ts("2026-11-15T00:00:00+00:00"), ts("2027-01-01T00:00:01+00:00")) == [
        "2026-11",
        "2026-12",
        "2027-01",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "2026-13",
        "2026-00",
        "2026-1",
        "26-10",
        "2026-10-01",
        "",
        None,
        202610,
        "2026-10\n",
        " 2026-10",
        "\u0662\u0660\u0662\u0666-10",  # Arabic-Indic digits
        "\uff12\uff10\uff12\uff16-10",  # fullwidth digits
        "2026-\uff11\uff10",
    ],
)
def test_parse_month_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        tu.parse_month(bad)
    with pytest.raises(ValueError):
        tu.month_bounds(bad)
