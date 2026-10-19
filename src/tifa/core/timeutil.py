"""Time helpers: epoch seconds and ISO 8601 in UTC, London days and hours, UTC calendar months.

Internally a time is integer epoch seconds (UTC). The API shows ISO 8601 with "+00:00". Days and hours shown to
people are London local time, and months are UTC calendar months ("YYYY-MM"). Nothing here reads the clock.
"""

import calendar
import datetime as dt
import functools
import re
import zoneinfo

UTC = dt.UTC
MINUTE = 60
HOUR = 3600
DAY = 86400

_GMT = dt.timedelta(0)
_BST = dt.timedelta(hours=1)
_MONTH_KEY = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")  # ASCII digits only: \d takes any script


# ---- London --------------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=256)
def _summer(year: int) -> tuple[dt.datetime, dt.datetime]:
    """British Summer Time in a year, as naive UTC datetimes: 01:00 UTC on the last Sunday in March to 01:00 UTC
    on the last Sunday in October."""

    def last_sunday(month: int) -> dt.datetime:
        d = dt.datetime(year, month, 31, 1)  # March and October both have 31 days
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)

    return last_sunday(3), last_sunday(10)


class UKTime(dt.tzinfo):
    """Europe/London by the UK rule in force since 1996, for when the IANA time zone database is missing.

    Wall times follow PEP 495 as zoneinfo does: in the skipped spring hour fold=0 takes GMT and fold=1 BST; in the
    repeated autumn hour fold=0 is the first (BST) occurrence and fold=1 the second (GMT)."""

    def utcoffset(self, d: dt.datetime | None) -> dt.timedelta | None:
        if d is None:
            return None
        wall = d.replace(tzinfo=None, fold=0)
        start, end = _summer(d.year)  # on those days 01:00 UTC is 01:00 GMT and 01:00 GMT (after 02:00 BST)
        if wall < start:
            return _GMT
        if wall < start + _BST:  # 01:00-02:00 does not exist in London
            return _BST if d.fold else _GMT
        if wall < end:
            return _BST
        if wall < end + _BST:  # 01:00-02:00 happens twice
            return _GMT if d.fold else _BST
        return _GMT

    def dst(self, d: dt.datetime | None) -> dt.timedelta | None:
        offset = self.utcoffset(d)
        return None if offset is None else offset - _GMT

    def tzname(self, d: dt.datetime | None) -> str | None:
        offset = self.utcoffset(d)
        return None if offset is None else ("BST" if offset else "GMT")

    def fromutc(self, d: dt.datetime) -> dt.datetime:
        utc = d.replace(tzinfo=None)
        start, end = _summer(utc.year)
        if start <= utc < end:
            return (utc + _BST).replace(tzinfo=self)
        return (utc + _GMT).replace(tzinfo=self, fold=1 if end <= utc < end + _BST else 0)

    def __repr__(self) -> str:
        return "UKTime()"


def _london_zone() -> dt.tzinfo:
    try:
        return zoneinfo.ZoneInfo("Europe/London")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return UKTime()


LONDON: dt.tzinfo = _london_zone()


def london(ts: float) -> dt.datetime:
    """The London wall time at ts, as an aware datetime."""
    return dt.datetime.fromtimestamp(ts, LONDON)


def london_date(ts: float) -> dt.date:
    return london(ts).date()


def london_day(ts: float) -> str:
    """The London date at ts, "YYYY-MM-DD"."""
    return london(ts).date().isoformat()


def london_hour(ts: float) -> int:
    return london(ts).hour


def london_weekday(ts: float) -> int:
    """Monday is 0."""
    return london(ts).weekday()


def london_year(ts: float) -> int:
    return london(ts).year


def _as_date(day: dt.date | str) -> dt.date:
    return dt.date.fromisoformat(day) if isinstance(day, str) else day


@functools.lru_cache(maxsize=8192)
def _midnight(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=LONDON).timestamp())


def london_midnight(day: dt.date | str) -> int:
    """00:00 in London at the start of a day (the clocks change at 01:00 UTC, never at midnight)."""
    return _midnight(_as_date(day))


def day_bounds(day: dt.date | str) -> tuple[int, int]:
    """[start, end) of a London day: 23, 24 or 25 hours long."""
    day = _as_date(day)
    return _midnight(day), _midnight(day + dt.timedelta(days=1))


def london_days(a: int, b: int) -> list[tuple[str, int, int, int]]:
    """[(London date, start, end, the day's end)] for each London day that [a, b] touches; zero length is in a's
    day."""
    day, out = london_date(a), []
    while True:
        end = _midnight(day + dt.timedelta(days=1))
        out.append((day.isoformat(), a, min(b, end), end))
        if b <= end:
            return out
        a, day = end, day + dt.timedelta(days=1)


def midnights(t0: float, t1: float) -> list[tuple[int, dt.date]]:
    """[(epoch, date)] of each London midnight strictly between t0 and t1."""
    out, day = [], london_date(t0) + dt.timedelta(days=1)
    while True:
        t = _midnight(day)
        if t >= t1:
            return out
        if t > t0:
            out.append((t, day))
        day += dt.timedelta(days=1)


def year_start(year: int) -> int:
    """00:00 on 1 January in London (always GMT, so also UTC)."""
    return _midnight(dt.date(year, 1, 1))


# ---- UTC -----------------------------------------------------------------------------------------------------


def iso(ts: float) -> str:
    """Epoch seconds -> ISO 8601 in UTC to the second, e.g. "2026-10-19T08:00:00+00:00"."""
    return dt.datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def epoch(text: str) -> int:
    """ISO 8601 -> epoch seconds (fractions dropped). A time without an offset is taken as UTC."""
    t = dt.datetime.fromisoformat(text)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return int(t.timestamp())


def utc_day(ts: float) -> str:
    """The UTC date at ts, "YYYY-MM-DD"."""
    return dt.datetime.fromtimestamp(ts, UTC).date().isoformat()


def month_index(ts: float) -> int:
    """The UTC month at ts as a number, year * 12 + month - 1."""
    d = dt.datetime.fromtimestamp(ts, UTC)
    return d.year * 12 + d.month - 1


def month_name(i: int) -> str:
    """A month number -> "YYYY-MM"."""
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def month_start(i: int) -> int:
    """00:00 UTC on the first day of month number i."""
    return calendar.timegm((i // 12, i % 12 + 1, 1, 0, 0, 0))


def month_key(ts: float) -> str:
    """The UTC month at ts, "YYYY-MM"."""
    return month_name(month_index(ts))


def parse_month(key: str) -> int:
    """ "YYYY-MM" -> month number. Raises ValueError for anything else."""
    m = _MONTH_KEY.fullmatch(key) if isinstance(key, str) else None
    if m is None:
        raise ValueError(f"not a month (YYYY-MM): {key!r}")
    return int(m.group(1)) * 12 + int(m.group(2)) - 1


def month_bounds(key: str) -> tuple[int, int]:
    """[start, end) of a UTC month "YYYY-MM"."""
    i = parse_month(key)
    return month_start(i), month_start(i + 1)


def month_keys(a: float, b: float) -> list[str]:
    """The UTC months overlapping [a, b)."""
    i, out = month_index(a), []
    while month_start(i) < b:
        out.append(month_name(i))
        i += 1
    return out
