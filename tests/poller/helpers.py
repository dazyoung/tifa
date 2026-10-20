"""Builders and fakes shared by the poller tests: TfL payloads, a scripted TfL client and fake Google services."""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from tifa.core.tfl import SEVERITY_NAMES
from tifa.poller.guard import MonthUsage, is_public, with_public
from tifa.poller.tfl_client import FetchResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "core"
KEY = "k3y" + "0123456789abcdef" * 2  # shaped like a TfL key; not a real one

LINES = [
    ("bakerloo", "Bakerloo", "tube"),
    ("central", "Central", "tube"),
    ("circle", "Circle", "tube"),
    ("district", "District", "tube"),
    ("jubilee", "Jubilee", "tube"),
    ("northern", "Northern", "tube"),
    ("victoria", "Victoria", "tube"),
    ("elizabeth", "Elizabeth line", "elizabeth-line"),
    ("mildmay", "Mildmay", "overground"),
    ("dlr", "DLR", "dlr"),
]


def utc(*args: int) -> int:
    return int(dt.datetime(*args, tzinfo=dt.UTC).timestamp())


def sample_payload() -> list:
    """A real TfL answer (19 lines, Central with minor delays)."""
    return json.loads((FIXTURES / "tfl_status_sample.json").read_text(encoding="utf-8"))


def status(
    severity: int,
    reason: str | None = None,
    *,
    planned: bool = False,
    now: bool | None = True,
    description: str | None = None,
) -> dict[str, Any]:
    """A lineStatuses entry. now=None gives no validity periods (in effect); False a period that is not now."""
    s: dict[str, Any] = {
        "statusSeverity": severity,
        "statusSeverityDescription": description or SEVERITY_NAMES.get(severity, f"Severity {severity}"),
        "created": "0001-01-01T00:00:00",
        "validityPeriods": []
        if now is None
        else [{"fromDate": "2026-10-19T07:00:00Z", "toDate": "2026-10-20T00:29:00Z", "isNow": now}],
    }
    if reason:
        s["reason"] = reason
    if severity not in (10, 18):
        s["disruption"] = {
            "category": "PlannedWork" if planned else "RealTime",
            "categoryDescription": "PlannedWork" if planned else "RealTime",
            "description": reason or "",
        }
    return s


def payload(disrupted: dict[str, list[dict]] | None = None, *, lines: Iterable = LINES) -> list[dict[str, Any]]:
    """Every line in good service, except those given their own statuses."""
    disrupted = disrupted or {}
    out = []
    for line_id, name, mode in lines:
        out.append(
            {
                "id": line_id,
                "name": name,
                "modeName": mode,
                "created": "2026-09-22T09:53:41.037Z",
                "modified": "2026-09-22T09:53:41.037Z",
                "lineStatuses": copy.deepcopy(disrupted.get(line_id) or [status(10)]),
            }
        )
    return out


class FakeTfl:
    """A scripted TfL client: each fetch takes the next item (a raw payload, a FetchResult or an exception to
    raise); the last item repeats."""

    def __init__(self, *items: Any, auth: str = "ok") -> None:
        self.items = list(items) or [payload()]
        self.auth = auth
        self.calls = 0
        self.has_key = auth != "anonymous"

    def push(self, *items: Any) -> None:
        self.items.extend(items)

    def fetch_status(self) -> FetchResult:
        self.calls += 1
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FetchResult):
            return item
        return FetchResult(raw=copy.deepcopy(item), auth=self.auth, status=200, size=1000, elapsed_ms=5)


class FakeMonitoring:
    def __init__(self, five: int = 0, hour: int = 0, usage: MonthUsage | None = None) -> None:
        self.five, self.hour = five, hour
        self.usage = usage or MonthUsage(0, 0, 0.0)
        self.recent_calls = 0
        self.month_calls = 0
        self.fail: BaseException | None = None
        self.fail_month: BaseException | None = None
        self.during: Callable[[], Any] | None = None  # runs once inside the next read (another writer meanwhile)

    def recent_requests(self, now_ts: int, deadline: float | None = None) -> tuple[int, int]:
        self.recent_calls += 1
        if self.during is not None:
            during, self.during = self.during, None
            during()
        if self.fail:
            raise self.fail
        return self.five, self.hour

    def month_usage(self, now_ts: int, deadline: float | None = None) -> MonthUsage:
        self.month_calls += 1
        if self.fail_month:
            raise self.fail_month
        return self.usage


class FakeInvoker:
    """An IAM policy in memory, edited the way the real invoker edits it."""

    def __init__(self, public: bool = True, other_members: Iterable[str] = ("domain:example.com",)):
        members = list(other_members) + (["allUsers"] if public else [])
        self.policy: dict[str, Any] = {
            "version": 1,
            "etag": "BwX1",
            "bindings": [
                {"role": "roles/run.invoker", "members": members},
                {"role": "roles/run.developer", "members": ["domain:example.org"]},
            ],
        }
        self.sets = 0
        self.gets = 0
        self.fail: BaseException | None = None
        self.before_set: Callable[[], Any] | None = None  # runs once at the start of the next set_public

    @property
    def public(self) -> bool:
        return is_public(self.policy)

    def is_public(self, deadline: float | None = None) -> bool:
        self.gets += 1
        return self.public

    def set_public(self, public: bool, deadline: float | None = None) -> bool:
        if self.before_set is not None:
            before, self.before_set = self.before_set, None
            before()
        self.gets += 1
        if self.fail:
            raise self.fail
        policy, changed = with_public(self.policy, public)
        if changed:
            self.sets += 1
            self.policy = policy
        return changed
