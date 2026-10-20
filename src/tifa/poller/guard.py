"""The fair-use guard: closes the public API when traffic or spend runs away (contracts: "Poller", "Fair use").

It runs from the poll path, at most every `guard_every` seconds (2 minutes) across instances, never on a schedule
of its own, and never fails a poll. Each run:

1. reads the public service's request count from Cloud Monitoring (`run.googleapis.com/request_count`, per minute
   over the last hour in one call: the busiest 5 minutes ending in the last 10, so Monitoring's few minutes of lag
   cannot hide a flood, and the hour) and, every `mtd_every` seconds, its month-to-date usage (requests,
   `container/network/sent_bytes_count`, `container/billable_instance_time`) for a cost estimate;
2. decides with `decide()`, a pure function of the stored state, those readings and the clock:
   - over `guard_5min` requests in 5 minutes: closed for `close_5min` (30 min), doubling for each trip that comes
     within `close_max` of the last one ending, up to `close_max` (24 h);
   - over `guard_1h` requests in an hour: closed for `close_1h` (2 h);
   - an estimated month-to-date cost over `guard_month_usd`: closed until the 1st of the next UTC month;
   - (from `/internal/budget`) actual spend at 100% of the budget: closed until the budget period ends.
   Request trips are checked only while the API is open, so a closure is not extended run after run; the cost and
   budget trips can extend a shorter closure to the month's end. A closure ends by itself when its time is up.
3. applies it (mode "enforce"): while closed, makes sure `allUsers` is not in the service's `roles/run.invoker`
   binding (so it also undoes a manual reopen), and when a closure the guard applied ends, puts `allUsers` back.
   It never touches the binding otherwise, nor any other binding or member.
4. stores everything in `guard/state` (below), with `heartbeat` = the run's time.

Modes:

- "enforce" decides and applies, as above.
- "dry-run" decides and logs, and records its decisions apart from the real state (`dry_run` below), so switching
  to enforce never applies a closure or a doubling a dry run decided. A real closure (applied before the switch to
  dry-run, or a manual one) stays as it is: the dry run neither extends it nor closes the API again, and when it
  ends puts `allUsers` back, as the guard closed the API itself.
- "off" decides nothing and never closes the API; it only puts `allUsers` back when a real closure the guard
  applied ends (when it has an Invoker), so switching off during a closure cannot leave the API closed for good.

`guard/state`:

    {"schema": 1, "mode": "enforce" | "dry-run" | "off", "heartbeat": epoch | None,
     "closed_until": epoch | None, "reason": "requests_5min" | "requests_1h" | "month_cost" | "budget" | "manual"
     | None, "applied": "closed" | "open" | None (what the guard last made of the binding),
     "strikes": int, "last_5min_until": epoch | None,
     "trips": [{"at", "reason", "until", "dry_run"?: true}] (the last 20),
     "dry_run": {"until", "reason", "strikes", "last_5min_until"} | None,
     "readings": {"at", "requests_5min", "requests_1h"} | None,
     "mtd": {"month", "at", "requests", "sent_bytes", "billable_s", "cost_usd"} | None,
     "budget": {"at", "ratio", "threshold", "forecast", "currency", "period_end"} | None,
     "error": str | None}

`dry_run` is set while the document holds a dry run's decisions: `until` and `reason` name the dry run's own
closure shown in `closed_until`/`reason` (None when it shows none; any other closure there is real), and `strikes`
and `last_5min_until` keep the real values from before the dry run changed the top-level ones. `real_state()`
undoes all of it, and enforce and off runs start from that.

`guard/state` is read, decided on and written without a store transaction (the store has none for it), so a run
re-reads it after acting and decides again when someone else wrote it meanwhile (a budget push on the other
instance, `tools/guard_cli.py`); see `Guard._settle`.

Every Google call goes through `GoogleRest` (httpx with google-auth's default credentials) and is injectable:
tests pass fakes for `Monitoring` and `Invoker`.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from tifa.config import Limits, Settings
from tifa.core.timeutil import iso, month_index, month_key, month_start
from tifa.poller.budget import BudgetNotice
from tifa.store.base import Store

log = logging.getLogger("tifa.guard")

SCHEMA = 1
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MONITORING_URL = "https://monitoring.googleapis.com/v3"
RUN_URL = "https://run.googleapis.com/v2"
INVOKER_ROLE = "roles/run.invoker"
PUBLIC = "allUsers"
MODES = ("enforce", "dry-run", "off")
REASONS = ("requests_5min", "requests_1h", "month_cost", "budget", "manual")

RUN_BUDGET_S = 12.0  # wall time one guard run may spend on Google calls (the poll's Scheduler deadline is 40 s)
MTD_EVERY = 1800  # seconds between month-to-date usage reads
RECENT_WINDOWS = 600  # 5-minute windows ending this recently count (Monitoring lags by up to about 3 minutes)
TRIPS_KEPT = 20
MAX_STRIKES = 20
SETTLE_TRIES = 3  # decisions per run when guard/state keeps changing under it

METRIC_REQUESTS = "run.googleapis.com/request_count"
METRIC_SENT_BYTES = "run.googleapis.com/container/network/sent_bytes_count"
METRIC_BILLABLE = "run.googleapis.com/container/billable_instance_time"


class GuardError(RuntimeError):
    """A Google call that failed. Messages name the API and the HTTP status, never a URL, token or project."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- pricing


@dataclass(frozen=True)
class Pricing:
    """Cloud Run list prices (USD, tier 1 region, request-based billing, no free tier) and the API's size."""

    per_million_requests: float = 0.40
    per_vcpu_second: float = 0.000024
    per_gib_second: float = 0.0000025
    per_gib_egress: float = 0.12
    vcpu: float = 1.0
    memory_gib: float = 0.5
    fallback_s_per_request: float = 0.1  # billable time per request when the metric could not be read

    def cost(self, requests: int, sent_bytes: int | None, billable_s: float | None) -> float:
        if billable_s is None:
            billable_s = requests * self.fallback_s_per_request
        instance = billable_s * (self.vcpu * self.per_vcpu_second + self.memory_gib * self.per_gib_second)
        egress = (sent_bytes or 0) / 2**30 * self.per_gib_egress
        return round(requests / 1e6 * self.per_million_requests + instance + egress, 4)


# ---------------------------------------------------------------- Google REST


class DefaultToken:
    """An OAuth access token from google-auth's application default credentials (the service's identity on Cloud
    Run), refreshed when it expires."""

    def __init__(self) -> None:
        self._credentials: Any = None
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._credentials is None:
                import google.auth

                self._credentials, _ = google.auth.default(scopes=[SCOPE])
            if not self._credentials.valid:
                from google.auth.transport.requests import Request

                self._credentials.refresh(Request())
            return self._credentials.token


class GoogleRest:
    """JSON calls to Google APIs over httpx with a bearer token. Errors become GuardError without the URL."""

    def __init__(
        self,
        token: Callable[[], str] | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        connect_timeout: float = 3.0,
        read_timeout: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token or DefaultToken()
        self._connect, self._read = connect_timeout, read_timeout
        self._monotonic = monotonic
        self._http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=connect_timeout, pool=1.0),
            headers={"Accept": "application/json", "User-Agent": "tifa-guard/0.1"},
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def call(
        self,
        api: str,
        method: str,
        url: str,
        *,
        params: Any = None,
        body: Any = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        remaining = None if deadline is None else deadline - self._monotonic()
        if remaining is not None and remaining < 0.5:
            raise GuardError(f"{api}: out of time for this run")
        connect = self._connect if remaining is None else min(self._connect, remaining)
        read = self._read if remaining is None else min(self._read, remaining)
        timeout = httpx.Timeout(connect=connect, read=read, write=connect, pool=1.0)
        error: GuardError | None = None
        resp = None
        try:
            token = self._token()
        except Exception as exc:  # google-auth errors can be long; keep the type only
            token, error = None, GuardError(f"{api}: no credentials ({type(exc).__name__})")
        if token is not None:
            try:
                resp = self._http.request(
                    method, url, params=params, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
                )
            except httpx.TimeoutException:
                error = GuardError(f"{api}: timed out")
            except httpx.HTTPError as exc:
                error = GuardError(f"{api}: request failed ({type(exc).__name__})")
        # raised outside the except blocks, so the httpx exception (with its URL) is not chained
        if error is not None:
            raise error
        assert resp is not None
        if resp.status_code != 200:
            raise GuardError(f"{api}: HTTP {resp.status_code}{_google_status(resp)}", status=resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise GuardError(f"{api}: the answer is not a JSON object")
        return data


def _google_status(resp: httpx.Response) -> str:
    """ " (PERMISSION_DENIED)" from a Google error body, or ""; never the message, which can name resources."""
    try:
        status = resp.json().get("error", {}).get("status")
    except (ValueError, AttributeError):
        return ""
    return f" ({status})" if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,40}", status) else ""


# ---------------------------------------------------------------- Cloud Monitoring


@dataclass(frozen=True)
class MonthUsage:
    requests: int
    sent_bytes: int | None
    billable_s: float | None


class Monitoring(Protocol):
    def recent_requests(self, now_ts: int, deadline: float | None = None) -> tuple[int, int]:
        """(the busiest 5 minutes ending in the last 10, requests in the last hour) to the public service."""
        ...

    def month_usage(self, now_ts: int, deadline: float | None = None) -> MonthUsage: ...


class CloudMonitoring:
    """Reads the public service's metrics with `projects.timeSeries.list` (REST)."""

    def __init__(self, rest: GoogleRest, project: str, region: str, service: str) -> None:
        self._rest, self._project, self._region, self._service = rest, project, region, service

    def _filter(self, metric: str) -> str:
        return (
            f'metric.type = "{metric}" AND resource.type = "cloud_run_revision" '
            f'AND resource.labels.service_name = "{self._service}" AND resource.labels.location = "{self._region}"'
        )

    def points(self, metric: str, start: int, end: int, period: int, deadline: float | None = None) -> list:
        """[(end epoch, value)] of `metric` summed over every revision, aligned to `period` seconds."""
        params: dict[str, Any] = {
            "filter": self._filter(metric),
            "interval.startTime": _rfc3339(start),
            "interval.endTime": _rfc3339(end),
            "aggregation.alignmentPeriod": f"{period}s",
            "aggregation.perSeriesAligner": "ALIGN_SUM",
            "aggregation.crossSeriesReducer": "REDUCE_SUM",
            "view": "FULL",
        }
        out: list[tuple[int, float]] = []
        url = f"{MONITORING_URL}/projects/{self._project}/timeSeries"
        for _ in range(10):  # pages; one series after REDUCE_SUM, so one page in practice
            data = self._rest.call("monitoring", "GET", url, params=params, deadline=deadline)
            for series in data.get("timeSeries") or ():
                for point in series.get("points") or ():
                    at = _parse_time((point.get("interval") or {}).get("endTime"))
                    value = _point_value(point.get("value") or {})
                    if at is not None and value is not None:
                        out.append((at, value))
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return sorted(out)

    def recent_requests(self, now_ts: int, deadline: float | None = None) -> tuple[int, int]:
        """One call for the last hour in minutes. Monitoring shows a minute's requests a few minutes late, so the
        newest 5 minutes are always partly empty: the 5-minute figure is the busiest 5 minutes among windows that end
        in the last 10 minutes (older ones were seen by earlier runs)."""
        pts = self.points(METRIC_REQUESTS, now_ts - 3600, now_ts, 60, deadline)
        hour = sum(v for at, v in pts if at > now_ts - 3600)
        ends = [at for at, _ in pts if at > now_ts - RECENT_WINDOWS]
        five = max((sum(v for at, v in pts if end - 300 < at <= end) for end in ends), default=0)
        return int(five), int(hour)

    def month_usage(self, now_ts: int, deadline: float | None = None) -> MonthUsage:
        start = month_start(month_index(now_ts))
        if now_ts - start < 120:
            return MonthUsage(0, 0, 0.0)
        requests = int(sum(v for _, v in self.points(METRIC_REQUESTS, start, now_ts, 3600, deadline)))
        sent: int | None
        billable: float | None
        try:
            sent = int(sum(v for _, v in self.points(METRIC_SENT_BYTES, start, now_ts, 3600, deadline)))
        except GuardError as exc:
            log.warning("guard: month-to-date egress unknown (%s)", exc)
            sent = None
        try:
            billable = float(sum(v for _, v in self.points(METRIC_BILLABLE, start, now_ts, 3600, deadline)))
        except GuardError as exc:
            log.warning("guard: month-to-date instance time unknown (%s)", exc)
            billable = None
        return MonthUsage(requests, sent, billable)


def _rfc3339(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_TIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)")


def _parse_time(text: Any) -> int | None:
    m = _TIME.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        return None
    zone = "+00:00" if m.group(2) == "Z" else m.group(2)
    try:
        return int(dt.datetime.fromisoformat(m.group(1) + zone).timestamp())
    except ValueError:
        return None


def _point_value(value: Mapping[str, Any]) -> float | None:
    for key in ("int64Value", "doubleValue"):
        if key in value:
            try:
                number = float(value[key])
            except (TypeError, ValueError):
                return None
            return number if number >= 0 else None
    return None


# ---------------------------------------------------------------- Cloud Run IAM


class Invoker(Protocol):
    def is_public(self, deadline: float | None = None) -> bool: ...

    def set_public(self, public: bool, deadline: float | None = None) -> bool:
        """Make the service public (allUsers may invoke) or not; True when the policy had to change."""
        ...


def is_public(policy: Mapping[str, Any]) -> bool:
    return any(
        b.get("role") == INVOKER_ROLE and PUBLIC in (b.get("members") or ()) for b in policy.get("bindings") or ()
    )


def with_public(policy: Mapping[str, Any], public: bool) -> tuple[dict[str, Any], bool]:
    """A copy of an IAM policy with `allUsers` added to (or removed from) `roles/run.invoker`, and whether that
    changed anything. Other roles, members, conditions, the etag and the version are kept as they are."""
    out = copy.deepcopy(dict(policy))
    bindings = list(out.get("bindings") or [])
    if public == is_public(out):
        return out, False
    if public:
        plain = next((b for b in bindings if b.get("role") == INVOKER_ROLE and not b.get("condition")), None)
        if plain is None:
            bindings.append({"role": INVOKER_ROLE, "members": [PUBLIC]})
        else:
            plain["members"] = list(plain.get("members") or []) + [PUBLIC]
    else:
        kept = []
        for b in bindings:
            if b.get("role") == INVOKER_ROLE and PUBLIC in (b.get("members") or ()):
                b["members"] = [m for m in b["members"] if m != PUBLIC]
                if not b["members"]:
                    continue
            kept.append(b)
        bindings = kept
    out["bindings"] = bindings
    return out, True


class CloudRunInvoker:
    """The public service's IAM policy through the Cloud Run Admin API v2 (`services.getIamPolicy/setIamPolicy`).
    Needs `run.services.getIamPolicy` and `run.services.setIamPolicy` on that one service."""

    def __init__(self, rest: GoogleRest, project: str, region: str, service: str) -> None:
        self._rest = rest
        self._url = f"{RUN_URL}/projects/{project}/locations/{region}/services/{service}"

    def get_policy(self, deadline: float | None = None) -> dict[str, Any]:
        return self._rest.call(
            "run.getIamPolicy",
            "GET",
            f"{self._url}:getIamPolicy",
            params={"options.requestedPolicyVersion": 3},
            deadline=deadline,
        )

    def set_policy(self, policy: Mapping[str, Any], deadline: float | None = None) -> dict[str, Any]:
        return self._rest.call(
            "run.setIamPolicy", "POST", f"{self._url}:setIamPolicy", body={"policy": dict(policy)}, deadline=deadline
        )

    def is_public(self, deadline: float | None = None) -> bool:
        return is_public(self.get_policy(deadline))

    def set_public(self, public: bool, deadline: float | None = None) -> bool:
        for attempt in (1, 2):
            policy, changed = with_public(self.get_policy(deadline), public)
            if not changed:
                return False
            try:
                self.set_policy(policy, deadline)
                return True
            except GuardError as exc:
                if attempt == 2 or exc.status not in (409, 412):  # the policy changed since it was read
                    raise
        return False  # pragma: no cover


# ---------------------------------------------------------------- decisions (pure)


@dataclass(frozen=True)
class Readings:
    requests_5min: int | None = None
    requests_1h: int | None = None
    mtd_cost: float | None = None


@dataclass(frozen=True)
class Decision:
    doc: dict[str, Any]  # the new guard state (heartbeat, readings and error are the caller's)
    closed: bool  # the public API should be closed now
    trip: str | None = None  # the reason of a closure that started or grew in this decision
    ended: bool = False  # a closure ended in this decision


def empty_doc(mode: str = "enforce") -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "mode": mode,
        "heartbeat": None,
        "closed_until": None,
        "reason": None,
        "applied": None,
        "strikes": 0,
        "last_5min_until": None,
        "trips": [],
        "dry_run": None,
        "readings": None,
        "mtd": None,
        "budget": None,
        "error": None,
    }


def normalise(doc: Mapping[str, Any] | None, mode: str | None = None) -> dict[str, Any]:
    """A stored guard document with every field present and of the right type (a damaged field is reset).
    `mode` replaces the stored mode when given."""
    stored = doc.get("mode") if isinstance(doc, Mapping) else None
    out = empty_doc(mode or (stored if stored in MODES else "enforce"))
    if not isinstance(doc, Mapping):
        return out
    for key in ("heartbeat", "closed_until", "last_5min_until"):
        out[key] = _epoch(doc.get(key))
    if doc.get("reason") in REASONS:
        out["reason"] = doc["reason"]
    if doc.get("applied") in ("closed", "open"):
        out["applied"] = doc["applied"]
    out["strikes"] = _strikes(doc.get("strikes"))
    trips = doc.get("trips")
    out["trips"] = [dict(t) for t in trips if isinstance(t, Mapping)][-TRIPS_KEPT:] if isinstance(trips, list) else []
    dry = doc.get("dry_run")
    if isinstance(dry, Mapping):
        until = _epoch(dry.get("until"))
        reason = dry.get("reason") if until is not None and dry.get("reason") in REASONS else None
        out["dry_run"] = {
            "until": until if reason is not None else None,
            "reason": reason,
            "strikes": _strikes(dry.get("strikes")),
            "last_5min_until": _epoch(dry.get("last_5min_until")),
        }
    for key in ("readings", "mtd", "budget"):
        out[key] = dict(doc[key]) if isinstance(doc.get(key), Mapping) else None
    if isinstance(doc.get("error"), str):
        out["error"] = doc["error"]
    if out["closed_until"] is None:
        out["reason"] = None
    return out


def _epoch(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _strikes(value: Any) -> int:
    return max(min(value, MAX_STRIKES), 0) if isinstance(value, int) and not isinstance(value, bool) else 0


def is_dry_closure(doc: Mapping[str, Any]) -> bool:
    """Whether the closure in `closed_until`/`reason` of a normalised document is a dry run's own (never applied)."""
    dry = doc.get("dry_run")
    return (
        isinstance(dry, Mapping)
        and doc.get("closed_until") is not None
        and (doc.get("closed_until"), doc.get("reason")) == (dry.get("until"), dry.get("reason"))
    )


def real_state(doc: Mapping[str, Any] | None) -> dict[str, Any]:
    """The guard document without a dry run's decisions: the dry run's own closure dropped and the real strikes
    back. Any other closure is real and stays: one applied before the switch to dry-run, or a manual one (written
    over the dry run's by `tools/guard_cli.py`, so it no longer matches `dry_run`)."""
    d = normalise(doc)
    dry = d["dry_run"]
    if dry is None:
        return d
    if is_dry_closure(d):
        d["closed_until"] = d["reason"] = None
    d["strikes"], d["last_5min_until"] = dry["strikes"], dry["last_5min_until"]
    d["dry_run"] = None
    return d


def next_month_start(now_ts: int) -> int:
    return month_start(month_index(now_ts) + 1)


def decide(doc: Mapping[str, Any] | None, readings: Readings, now_ts: int, limits: Limits) -> Decision:
    """The guard's decision for one run. Pure: the same inputs always give the same output."""
    d = normalise(doc)
    until = d["closed_until"]
    ended = False
    if until is not None and until <= now_ts:
        until, d["reason"], ended = None, None, True

    closing: list[tuple[int, str]] = []
    if until is None:
        r5 = readings.requests_5min
        if r5 is not None and r5 > limits.guard_5min:
            last = d["last_5min_until"]
            repeat = last is not None and now_ts - last <= limits.close_max  # within a day of the last one ending
            strikes = min(d["strikes"] + 1, MAX_STRIKES) if repeat else 1
            seconds = min(limits.close_5min * 2 ** (strikes - 1), limits.close_max)
            d["strikes"], d["last_5min_until"] = strikes, now_ts + seconds
            closing.append((now_ts + seconds, "requests_5min"))
        r1 = readings.requests_1h
        if r1 is not None and r1 > limits.guard_1h:
            closing.append((now_ts + limits.close_1h, "requests_1h"))
    cost = readings.mtd_cost
    if cost is not None and cost > limits.guard_month_usd:
        closing.append((next_month_start(now_ts), "month_cost"))
    return _close(d, until, closing, now_ts, ended)


def budget_reading(notice: BudgetNotice, now_ts: int) -> dict[str, Any]:
    """What `guard/state` keeps of a budget notification (never its name or the billing account)."""
    return {
        "at": now_ts,
        "ratio": round(notice.ratio, 4),
        "threshold": notice.threshold,
        "forecast": notice.forecast,
        "currency": notice.currency,
        "period_end": notice.period_end(now_ts),
    }


def decide_budget(doc: Mapping[str, Any] | None, notice: BudgetNotice, now_ts: int) -> Decision:
    """A budget notification: at 100% of actual spend, closed until the budget period ends."""
    d = normalise(doc)
    until = d["closed_until"]
    ended = False
    if until is not None and until <= now_ts:
        until, d["reason"], ended = None, None, True
    d["budget"] = budget_reading(notice, now_ts)
    end = d["budget"]["period_end"]
    closing = []
    if notice.over and now_ts < end <= now_ts + 32 * 86400:  # a notification for a period that has ended is stale
        closing.append((end, "budget"))
    return _close(d, until, closing, now_ts, ended)


def dry_run(doc: Mapping[str, Any] | None, now_ts: int, step: Callable[[dict[str, Any]], Decision]) -> Decision:
    """A dry run's decision: what `step` (`decide` or `decide_budget` with their other inputs bound) decides,
    recorded but kept apart from the real state (see `dry_run` in the module docstring). Pure.

    The Decision's `closed` says whether a real closure is in effect (the binding should stay as the guard made
    it); `trip` and `ended` are the dry run's own decisions, for its log lines. While a real closure is in effect
    the dry run leaves it as it is and only reports a trip that would extend it."""
    d = normalise(doc)
    real = real_state(d)
    real_until = real["closed_until"]
    if real_until is not None and real_until > now_ts:
        would = step(real)
        out = would.doc
        trip = would.trip
        if trip:
            new = {**out["trips"][-1], "dry_run": True}
            seen = any(  # reported by an earlier run already (the extension is never kept, so it recurs)
                t.get("dry_run") is True and (t.get("reason"), t.get("until")) == (new["reason"], new["until"])
                for t in real["trips"]
            )
            if seen:
                out["trips"], trip = real["trips"], None
            else:
                out["trips"][-1] = new
        out["closed_until"], out["reason"] = real_until, real["reason"]
        return Decision(doc=out, closed=True, trip=trip)  # `out` is the real state: no `dry_run`
    saved = d["dry_run"] or {"strikes": d["strikes"], "last_5min_until": d["last_5min_until"]}
    sim = step(d)
    out = sim.doc
    if sim.trip:
        _mark_dry(out)
    out["dry_run"] = {
        "until": out["closed_until"],
        "reason": out["reason"],
        "strikes": saved["strikes"],
        "last_5min_until": saved["last_5min_until"],
    }
    return Decision(doc=out, closed=False, trip=sim.trip, ended=sim.ended)


def released(doc: Mapping[str, Any] | None, now_ts: int) -> Decision:
    """Mode off: no decisions. A dry run's leftovers are dropped and a real closure ends by itself when its time is
    up; `closed` says whether one is still in effect."""
    d = real_state(doc)
    until = d["closed_until"]
    ended = until is not None and until <= now_ts
    if ended:
        d["closed_until"] = d["reason"] = None
    return Decision(doc=d, closed=d["closed_until"] is not None, ended=ended)


def _mark_dry(d: dict[str, Any]) -> None:
    """Mark the trip a decision just appended as a dry run's."""
    if d["trips"]:
        d["trips"][-1] = {**d["trips"][-1], "dry_run": True}


def _close(d: dict[str, Any], until: int | None, closing: list, now_ts: int, ended: bool) -> Decision:
    trip = None
    if closing:
        end, reason = max(closing)
        if until is None or end > until:
            until, trip = end, reason
            d["reason"] = reason
            d["trips"] = (d["trips"] + [{"at": now_ts, "reason": reason, "until": end}])[-TRIPS_KEPT:]
    d["closed_until"] = until
    if until is None:
        d["reason"] = None
    return Decision(doc=d, closed=until is not None, trip=trip, ended=ended and until is None)


# ---------------------------------------------------------------- the guard


@dataclass
class GuardRun:
    """What one run did: the stored document, whether a real closure is in effect, and any errors."""

    doc: dict[str, Any]
    closed: bool
    trip: str | None = None  # the closure that started or grew (in dry-run: that the dry run would start)
    action: str | None = None  # "closed", "opened", "would_close", "would_open" or None
    errors: list[str] = field(default_factory=list)


Step = Callable[[dict[str, Any]], Decision]


class Guard:
    def __init__(
        self,
        store: Store,
        *,
        monitoring: Monitoring | None,
        invoker: Invoker | None,
        limits: Limits | None = None,
        mode: str = "enforce",
        pricing: Pricing | None = None,
        mtd_every: int = MTD_EVERY,
        run_budget_s: float = RUN_BUDGET_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"guard mode must be one of {', '.join(MODES)}")
        if mode != "off" and (monitoring is None or invoker is None):
            raise ValueError("the guard needs Monitoring and Invoker clients unless its mode is off")
        self.store = store
        self.monitoring = monitoring
        self.invoker = invoker  # in mode off, only to put allUsers back when a closure the guard applied ends
        self.limits = limits or Limits()
        self.mode = mode
        self.pricing = pricing or Pricing()
        self.mtd_every = mtd_every
        self.run_budget_s = run_budget_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._skip_until: int | None = None  # this instance's own record of the last run, to spare a store read

    # -- from the poll path

    def maybe_run(self, now_ts: int) -> GuardRun | None:
        """Run if the last run (on any instance) was at least `guard_every` seconds ago. Never raises.

        In mode off it only checks, as often, whether a closure the guard applied has ended and must be undone;
        without an Invoker it does nothing at all."""
        if self.mode == "off" and self.invoker is None:
            return None
        if self._skip_until is not None and now_ts < self._skip_until:
            return None
        if not self._lock.acquire(blocking=False):
            return None  # a run or a budget message is in progress on this instance
        try:
            doc = self.store.get_guard()
            if self.mode == "off":
                self._skip_until = now_ts + self.limits.guard_every
                return self._release(now_ts, doc)
            beat = _epoch((doc or {}).get("heartbeat")) if isinstance(doc, Mapping) else None
            if beat is not None and 0 <= now_ts - beat < self.limits.guard_every:
                self._skip_until = beat + self.limits.guard_every
                return None
            self._skip_until = now_ts + self.limits.guard_every
            return self._run(now_ts, doc)
        except Exception as exc:
            log.error("guard run failed: %s", _brief(exc))
            return None
        finally:
            self._lock.release()

    def run(self, now_ts: int) -> GuardRun | None:
        """One run now, whenever the last one was (for tools and tests). Raises only if the state cannot be
        read or written. In mode off it returns None unless it had a closure to undo."""
        with self._lock:
            doc = self.store.get_guard()
            if self.mode == "off":
                return self._release(now_ts, doc) if self.invoker is not None else None
            return self._run(now_ts, doc)

    def _run(self, now_ts: int, doc: Mapping[str, Any] | None) -> GuardRun:
        deadline = self._monotonic() + self.run_budget_s
        errors: list[str] = []
        five = hour = None
        try:
            assert self.monitoring is not None
            five, hour = self.monitoring.recent_requests(now_ts, deadline)
        except Exception as exc:
            errors.append(f"requests: {_brief(exc)}")
        mtd = self._month_to_date(normalise(doc).get("mtd"), now_ts, deadline, errors)
        readings = Readings(five, hour, mtd["cost_usd"] if mtd else None)

        def step(prev: dict[str, Any]) -> Decision:
            decision = self._decide(prev, now_ts, lambda d: decide(d, readings, now_ts, self.limits))
            decision.doc["readings"] = {"at": now_ts, "requests_5min": five, "requests_1h": hour}
            decision.doc["mtd"] = mtd
            return decision

        # read again: the Monitoring calls took a while, and a budget push or an operator may have written since
        return self._settle(self.store.get_guard(), step, now_ts, deadline, errors)

    def _release(self, now_ts: int, doc: Mapping[str, Any] | None) -> GuardRun | None:
        """Mode off: put allUsers back if a closure the guard applied has ended. Writes nothing otherwise."""
        decision = released(doc, now_ts)
        if decision.closed or decision.doc["applied"] != "closed":
            return None
        deadline = self._monotonic() + self.run_budget_s
        return self._settle(doc, lambda prev: released(prev, now_ts), None, deadline, [])

    def _decide(self, prev: dict[str, Any], now_ts: int, step: Step) -> Decision:
        """`step` (decide or decide_budget) as this guard's mode takes it."""
        if self.mode == "dry-run":
            return dry_run(prev, now_ts, step)
        if self.mode == "off":
            return released(prev, now_ts)
        if is_dry_closure(prev):
            log.warning("guard: dropped a dry run's closure until %s (it was never applied)", iso(prev["closed_until"]))
        return step(real_state(prev))

    def _month_to_date(
        self, mtd: Mapping[str, Any] | None, now_ts: int, deadline: float, errors: list[str]
    ) -> dict[str, Any] | None:
        month = month_key(now_ts)
        current = dict(mtd) if isinstance(mtd, Mapping) and mtd.get("month") == month else None
        at = _epoch((current or {}).get("at"))
        if current is not None and at is not None and 0 <= now_ts - at < self.mtd_every:
            return current
        try:
            assert self.monitoring is not None
            usage = self.monitoring.month_usage(now_ts, deadline)
        except Exception as exc:
            errors.append(f"month to date: {_brief(exc)}")
            return current  # this month's last estimate, if any
        return {
            "month": month,
            "at": now_ts,
            "requests": usage.requests,
            "sent_bytes": usage.sent_bytes,
            "billable_s": None if usage.billable_s is None else round(usage.billable_s, 1),
            "cost_usd": self.pricing.cost(usage.requests, usage.sent_bytes, usage.billable_s),
        }

    # -- budget notifications

    def budget(self, notice: BudgetNotice, now_ts: int) -> GuardRun:
        """Record a budget notification; at 100% of actual spend close the public API until the period ends
        (in dry-run: record that it would; in off: record the notification only). Raises only if the state cannot
        be read or written (the handler then asks Pub/Sub to redeliver)."""
        with self._lock:
            deadline = self._monotonic() + self.run_budget_s

            def step(prev: dict[str, Any]) -> Decision:
                decision = self._decide(prev, now_ts, lambda d: decide_budget(d, notice, now_ts))
                if self.mode == "off":
                    decision.doc["budget"] = budget_reading(notice, now_ts)
                return decision

            run = self._settle(self.store.get_guard(), step, None, deadline, [])  # the heartbeat stays the poll's
            if run.trip == "budget":
                verb = "a dry run would close" if self.mode == "dry-run" else "closed"
                tail = f"; {verb} the public API until {iso(run.doc['budget']['period_end'])}"
            else:
                tail = ""
            log.warning("guard: budget notification at %.0f%% of the budget%s", notice.ratio * 100, tail)
            return run

    # -- applying and saving

    def _settle(
        self,
        doc: Mapping[str, Any] | None,
        step: Step,
        now_ts: int | None,
        deadline: float,
        errors: list[str],
    ) -> GuardRun:
        """Decide on `doc` (as read), apply, then read guard/state again: if someone wrote it meanwhile (a budget
        push on the other instance, an operator's close or open), decide again on what they wrote, up to
        SETTLE_TRIES times, and save. A decision taken again knows what this run's own IAM calls did to the
        binding, so it undoes or keeps them as the newer state requires. The store has no compare-and-set for
        guard/state, so only a write landing between the last read and the save can still be lost."""
        made: str | None = None  # the binding as this run's own IAM calls left it
        action: str | None = None
        for attempt in range(1, SETTLE_TRIES + 1):
            prev = normalise(doc)
            if made is not None:
                prev["applied"] = made
            decision = step(prev)
            d = decision.doc
            act, did = self._apply(d, decision, deadline, errors)
            made, action = did or made, act or action
            fresh = self.store.get_guard()
            if fresh == doc:
                break
            doc = fresh
            if attempt < SETTLE_TRIES:
                log.info("guard: guard/state changed during this run; deciding again on the newer state")
            else:
                log.warning("guard: guard/state kept changing during this run; saving its last decision")
        return self._save(d, now_ts, decision, action, errors)

    def _apply(
        self, d: dict[str, Any], decision: Decision, deadline: float, errors: list[str]
    ) -> tuple[str | None, str | None]:
        """Make the binding match the decision. Returns (action, binding): what was done, and "closed" or "open"
        when an IAM call confirmed the binding so."""
        if self.mode == "enforce":
            return self._enforce(d, decision, deadline, errors)
        action = binding = None
        if not decision.closed and d["applied"] == "closed" and self.invoker is not None:
            # a closure the guard applied (before the switch to this mode) has ended: put allUsers back
            try:
                self.invoker.set_public(True, deadline)
                d["applied"] = binding = "open"
                action = "opened"
                log.warning("guard (%s): reopened the public API (its closure has ended)", self.mode)
            except Exception as exc:
                errors.append(f"iam: {_brief(exc)}")
                log.error("guard: could not change the public API's invoker binding: %s", _brief(exc))
        if self.mode == "dry-run":
            if decision.trip:
                until = d["trips"][-1].get("until") if d["trips"] else None
                log.warning(
                    "guard (dry run): would close the public API%s (%s)",
                    f" until {iso(until)}" if isinstance(until, int) else "",
                    decision.trip,
                )
                action = action or "would_close"
            elif decision.ended:
                log.warning("guard (dry run): would reopen the public API")
                action = action or "would_open"
        return action, binding

    def _enforce(
        self, d: dict[str, Any], decision: Decision, deadline: float, errors: list[str]
    ) -> tuple[str | None, str | None]:
        assert self.invoker is not None
        until = d["closed_until"]
        try:
            if decision.closed:
                changed = self.invoker.set_public(False, deadline)
                d["applied"] = "closed"
                if changed:
                    log.warning("guard: closed the public API until %s (%s)", iso(until), d["reason"])
                    return "closed", "closed"
                return None, "closed"
            if d["applied"] == "closed":
                self.invoker.set_public(True, deadline)
                d["applied"] = "open"
                log.warning("guard: reopened the public API")
                return "opened", "open"
        except Exception as exc:
            errors.append(f"iam: {_brief(exc)}")
            log.error("guard: could not change the public API's invoker binding: %s", _brief(exc))
        return None, None

    def _save(
        self, d: dict[str, Any], now_ts: int | None, decision: Decision, action: str | None, errors: list[str]
    ) -> GuardRun:
        d["mode"] = self.mode
        if now_ts is not None:
            d["heartbeat"] = now_ts
        errors = list(dict.fromkeys(errors))  # a step decided again can repeat an error
        d["error"] = "; ".join(errors)[:500] or None
        if errors:
            log.warning("guard: %s", d["error"])
        self.store.put_guard(d)
        return GuardRun(doc=d, closed=decision.closed, trip=decision.trip, action=action, errors=errors)


def _brief(exc: BaseException) -> str:
    """An exception for a log line: GuardError text as is (it is built without URLs), else the type only."""
    if isinstance(exc, GuardError):
        return str(exc)
    return type(exc).__name__


def build_guard(settings: Settings, store: Store, rest: GoogleRest | None = None) -> Guard:
    """The guard for a poller's settings: real Google clients, except that mode off reads no metrics and gets an
    Invoker only when the project and region are set (to undo a closure applied before the switch to off)."""
    mode = settings.guard_mode
    if mode == "off":
        invoker = None
        if settings.project and settings.region:
            rest = rest or GoogleRest()
            invoker = CloudRunInvoker(rest, settings.project, settings.region, settings.api_service)
        return Guard(store, monitoring=None, invoker=invoker, limits=settings.limits, mode="off")
    rest = rest or GoogleRest()
    return Guard(
        store,
        monitoring=CloudMonitoring(rest, settings.project, settings.region, settings.api_service),
        invoker=CloudRunInvoker(rest, settings.project, settings.region, settings.api_service),
        limits=settings.limits,
        mode=mode,
    )
