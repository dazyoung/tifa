"""Cloud Billing budget notifications, as Pub/Sub pushes them to `POST /internal/budget`.

The push body is an envelope:

    {"message": {"data": base64(JSON), "attributes": {...}, "messageId": "...", "publishTime": "..."},
     "subscription": "projects/.../subscriptions/..."}

and the data is the budget notification:

    {"budgetDisplayName": "...", "costAmount": 4.1, "budgetAmount": 3.75, "currencyCode": "GBP",
     "costIntervalStart": "2026-10-01T07:00:00Z", "alertThresholdExceeded": 1.0, ...}

Cloud Billing sends one several times a day, whether or not a threshold was crossed. Only actual spend counts:
`forecastThresholdExceeded` alone never closes anything. Nothing here logs the attributes (they hold the billing
account ID) or the budget's name.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tifa.core.timeutil import month_index, month_start

MAX_BODY = 64 * 1024
CLOSE_AT = 1.0  # share of the budget actually spent that closes the public API until the period ends
_RFC3339 = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)", re.IGNORECASE)


class BudgetMessageError(ValueError):
    """A push that is not a budget notification. The handler acknowledges it (HTTP 200) so Pub/Sub does not
    redeliver it for days; the message names what is wrong, never the content."""


@dataclass(frozen=True)
class BudgetNotice:
    cost: float
    budget: float
    currency: str | None
    threshold: float | None  # alertThresholdExceeded: the highest actual-spend threshold crossed this period
    forecast: float | None  # forecastThresholdExceeded
    period_start: int | None  # costIntervalStart, epoch seconds

    @property
    def ratio(self) -> float:
        """Share of the budget spent this period (0 when the budget amount is 0 or missing)."""
        return self.cost / self.budget if self.budget > 0 else 0.0

    @property
    def over(self) -> bool:
        """Actual spend has reached the budget."""
        return self.ratio >= CLOSE_AT or (self.threshold is not None and self.threshold >= CLOSE_AT)

    def period_end(self, now_ts: int) -> int:
        """When the budget period ends: the same time on the 1st of the month after costIntervalStart (budgets
        run by calendar month in the billing account's time zone), else the start of the next UTC month."""
        if self.period_start is None:
            return month_start(month_index(now_ts) + 1)
        # costIntervalStart is midnight on the 1st in the billing account's zone: within 14 h of a UTC month start
        i = month_index(self.period_start)
        first = min((i, i + 1), key=lambda k: abs(self.period_start - month_start(k)))
        offset = self.period_start - month_start(first)
        if abs(offset) > 14 * 3600:
            return month_start(month_index(now_ts) + 1)
        return month_start(first + 1) + offset


def parse_push(body: bytes) -> dict[str, Any]:
    """The decoded notification inside a Pub/Sub push envelope."""
    if len(body) > MAX_BODY:
        raise BudgetMessageError("push body too large")
    try:
        envelope = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise BudgetMessageError("push body is not JSON") from None
    message = envelope.get("message") if isinstance(envelope, dict) else None
    data = message.get("data") if isinstance(message, dict) else None
    if not isinstance(data, str) or not data:
        raise BudgetMessageError("push has no message data")
    try:
        urlsafe = "-" in data or "_" in data
        decoded = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_" if urlsafe else None)
        notification = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise BudgetMessageError("message data is not base64 JSON") from None
    if not isinstance(notification, dict):
        raise BudgetMessageError("message data is not a JSON object")
    return notification


def parse_notification(data: Mapping[str, Any]) -> BudgetNotice:
    cost, budget = _number(data.get("costAmount")), _number(data.get("budgetAmount"))
    if cost is None or budget is None:
        raise BudgetMessageError("notification has no costAmount or budgetAmount")
    currency = data.get("currencyCode")
    return BudgetNotice(
        cost=cost,
        budget=budget,
        currency=currency if isinstance(currency, str) and len(currency) <= 8 else None,
        threshold=_number(data.get("alertThresholdExceeded")),
        forecast=_number(data.get("forecastThresholdExceeded")),
        period_start=_rfc3339(data.get("costIntervalStart")),
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, int | float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _rfc3339(value: Any) -> int | None:
    m = _RFC3339.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    zone = m.group(7).upper()
    try:
        ts = int(dt.datetime(year, month, day, hour, minute, second, tzinfo=dt.UTC).timestamp())
    except (ValueError, OverflowError):
        return None
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        ts -= sign * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    return ts
