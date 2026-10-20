"""tifa.poller.guard: the decision table, IAM edits, Cloud Monitoring reads (all with fakes) and the guard's runs."""

import json

import httpx
import pytest

from tests.poller.helpers import FakeInvoker, FakeMonitoring, utc
from tifa.config import Limits, Settings
from tifa.poller import guard as gd
from tifa.poller.budget import BudgetNotice
from tifa.poller.guard import (
    CloudMonitoring,
    CloudRunInvoker,
    Decision,
    GoogleRest,
    Guard,
    GuardError,
    MonthUsage,
    Pricing,
    Readings,
    decide,
    decide_budget,
    is_public,
    normalise,
    released,
    with_public,
)
from tifa.store.memory import MemoryStore

LIM = Limits()
NOW = utc(2026, 10, 19, 8)
NOV = utc(2026, 11, 1)
PROJECT, REGION, SERVICE = "demo-tifa", "europe-west1", "tifa-api"


def doc(**fields):
    return normalise(None) | fields


# ---- the decision table --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prev", "readings", "until", "reason", "strikes"),
    [
        # quiet
        ({}, Readings(100, 1_000, 0.10), None, None, 0),
        ({}, Readings(9_000, 36_000, 2.00), None, None, 0),  # at the limits is not over them
        ({}, Readings(None, None, None), None, None, 0),  # Monitoring unreadable: no trip
        # 5-minute trips, doubling on repeats within a day of the last one ending, up to a day
        ({}, Readings(9_001, 9_001), NOW + 1_800, "requests_5min", 1),
        ({"strikes": 1, "last_5min_until": NOW - 3_600}, Readings(9_001), NOW + 3_600, "requests_5min", 2),
        ({"strikes": 5, "last_5min_until": NOW - 7_200}, Readings(9_001), NOW + 57_600, "requests_5min", 6),
        ({"strikes": 7, "last_5min_until": NOW - 60}, Readings(9_001), NOW + 86_400, "requests_5min", 8),
        ({"strikes": 3, "last_5min_until": NOW - 86_401}, Readings(9_001), NOW + 1_800, "requests_5min", 1),
        # the hour
        ({}, Readings(10, 36_001), NOW + 7_200, "requests_1h", 0),
        ({}, Readings(9_001, 36_001), NOW + 7_200, "requests_1h", 1),  # the longer closure wins
        # the month's cost
        ({}, Readings(10, 10, 2.01), NOV, "month_cost", 0),
        # while closed, request trips do not extend the closure (no doubling every run)
        (
            {"closed_until": NOW + 600, "reason": "requests_5min", "strikes": 1},
            Readings(50_000, 90_000),
            NOW + 600,
            "requests_5min",
            1,
        ),
        # but the month's cost does
        (
            {"closed_until": NOW + 600, "reason": "requests_5min", "strikes": 1},
            Readings(0, 0, 3.0),
            NOV,
            "month_cost",
            1,
        ),
        # closed to the month's end already: nothing changes
        ({"closed_until": NOV, "reason": "budget"}, Readings(0, 0, 9.0), NOV, "budget", 0),
        ({"closed_until": NOW + 100, "reason": "manual"}, Readings(0, 0, 0.0), NOW + 100, "manual", 0),
        # a closure that has ended, then a new trip at once: a repeat
        (
            {"closed_until": NOW - 1, "reason": "requests_5min", "strikes": 1, "last_5min_until": NOW - 1},
            Readings(9_001),
            NOW + 3_600,
            "requests_5min",
            2,
        ),
    ],
)
def test_decision_table(prev, readings, until, reason, strikes):
    decision = decide(doc(**prev), readings, NOW, LIM)
    assert decision.doc["closed_until"] == until
    assert decision.doc["reason"] == reason
    assert decision.doc["strikes"] == strikes
    assert decision.closed is (until is not None)


def test_a_trip_is_logged_once():
    first = decide(None, Readings(9_001), NOW, LIM)
    assert first.trip == "requests_5min" and first.doc["trips"] == [
        {"at": NOW, "reason": "requests_5min", "until": NOW + 1_800}
    ]
    again = decide(first.doc, Readings(9_001), NOW + 120, LIM)
    assert again.trip is None and len(again.doc["trips"]) == 1


def test_a_closure_ends_by_itself():
    closed = decide(None, Readings(9_001), NOW, LIM).doc
    still = decide(closed, Readings(0), NOW + 1_799, LIM)
    assert still.closed and not still.ended
    over = decide(closed, Readings(0), NOW + 1_800, LIM)
    assert not over.closed and over.ended and over.doc["reason"] is None
    assert over.doc["strikes"] == 1  # remembered for the doubling


def test_lowered_limits_apply():
    low = Limits(guard_5min=10, close_5min=60, guard_1h=20, close_1h=120, guard_month_usd=0.01)
    assert decide(None, Readings(11), NOW, low).doc["closed_until"] == NOW + 60
    assert decide(None, Readings(0, 21), NOW, low).doc["closed_until"] == NOW + 120
    assert decide(None, Readings(0, 0, 0.02), NOW, low).doc["closed_until"] == NOV


def test_decide_is_pure():
    prev = doc(strikes=2, last_5min_until=NOW - 10)
    before = json.dumps(prev, sort_keys=True)
    assert decide(prev, Readings(9_001), NOW, LIM) == decide(prev, Readings(9_001), NOW, LIM)
    assert json.dumps(prev, sort_keys=True) == before


def test_a_damaged_document_is_reset_field_by_field():
    d = normalise({"closed_until": "soon", "strikes": True, "trips": "x", "reason": "nope", "mtd": [1], "mode": "?"})
    assert d == normalise(None)
    dry = normalise({"dry_run": {"until": "x", "reason": "requests_5min", "strikes": -3, "last_5min_until": 1.5}})
    assert dry["dry_run"] == {"until": None, "reason": None, "strikes": 0, "last_5min_until": None}
    assert normalise({"dry_run": [1]})["dry_run"] is None
    kept = normalise({"closed_until": NOW, "reason": "budget", "strikes": 99, "mode": "dry-run"})
    assert (kept["closed_until"], kept["reason"], kept["strikes"], kept["mode"]) == (NOW, "budget", 20, "dry-run")


OCT_PERIOD = utc(2026, 10, 1, 7)  # midnight on 1 October, Pacific time


def notice(cost=3.8, budget=3.75, start=OCT_PERIOD, threshold=None, forecast=None):
    return BudgetNotice(
        cost=cost, budget=budget, currency="GBP", threshold=threshold, forecast=forecast, period_start=start
    )


@pytest.mark.parametrize(
    ("n", "until"),
    [
        (notice(), utc(2026, 11, 1, 7)),
        (notice(cost=3.0), None),
        (notice(cost=1.0, threshold=1.0), utc(2026, 11, 1, 7)),
        (notice(cost=2.0, forecast=1.2), None),
        (notice(start=utc(2026, 9, 1, 7)), None),  # September's period has ended: a stale message
    ],
)
def test_budget_decisions(n, until):
    decision = decide_budget(None, n, NOW)
    assert decision.doc["closed_until"] == until
    assert decision.doc["budget"]["ratio"] == round(n.ratio, 4)
    assert decision.trip == ("budget" if until else None)


def test_a_budget_closure_outlasts_a_shorter_one():
    short = decide(None, Readings(9_001), NOW, LIM).doc
    decision = decide_budget(short, notice(), NOW + 60)
    assert decision.doc["closed_until"] == utc(2026, 11, 1, 7) and decision.doc["reason"] == "budget"


# ---- IAM policies ---------------------------------------------------------------------------------------------

POLICY = {
    "version": 1,
    "etag": "BwX1",
    "bindings": [
        {"role": "roles/run.invoker", "members": ["domain:example.com", "allUsers"]},
        {"role": "roles/run.viewer", "members": ["allUsers"]},
    ],
}


def test_closing_removes_only_all_users_from_the_invoker_role():
    closed, changed = with_public(POLICY, False)
    assert changed and not is_public(closed)
    assert closed["bindings"] == [
        {"role": "roles/run.invoker", "members": ["domain:example.com"]},
        {"role": "roles/run.viewer", "members": ["allUsers"]},
    ]
    assert (closed["etag"], closed["version"]) == ("BwX1", 1)
    assert is_public(POLICY)  # the input is not changed


def test_an_empty_invoker_binding_is_dropped():
    policy = {"etag": "e", "bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}
    closed, _ = with_public(policy, False)
    assert closed == {"etag": "e", "bindings": []}


def test_closing_also_clears_conditional_bindings():
    policy = {
        "version": 3,
        "bindings": [
            {"role": "roles/run.invoker", "members": ["allUsers", "domain:example.com"], "condition": {"x": 1}}
        ],
    }
    closed, changed = with_public(policy, False)
    assert changed and closed["bindings"][0]["members"] == ["domain:example.com"]


def test_opening_adds_all_users_to_the_plain_invoker_binding():
    closed, _ = with_public(POLICY, False)
    opened, changed = with_public(closed, True)
    assert changed and opened["bindings"][0] == {
        "role": "roles/run.invoker",
        "members": ["domain:example.com", "allUsers"],
    }
    fresh, _ = with_public({"etag": "e"}, True)
    assert fresh == {"etag": "e", "bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]}


def test_no_change_when_already_so():
    assert with_public(POLICY, True) == (POLICY, False)
    closed, _ = with_public(POLICY, False)
    assert with_public(closed, False)[1] is False


# ---- Google REST (fake transport) -----------------------------------------------------------------------------


class Google:
    """A MockTransport stand-in for monitoring.googleapis.com and run.googleapis.com."""

    def __init__(self):
        self.requests = []
        self.series = {}  # metric -> list of (endTime, value)
        self.policy = json.loads(json.dumps(POLICY))
        self.conflicts = 0
        self.fail = {}  # path fragment -> (status, body)
        self.pages = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for fragment, (code, body) in self.fail.items():
            if fragment in url:
                return httpx.Response(code, json=body)
        if request.url.host == "monitoring.googleapis.com":
            return self.monitoring(request)
        if url.endswith(":getIamPolicy") or ":getIamPolicy?" in url:
            return httpx.Response(200, json=self.policy)
        if url.endswith(":setIamPolicy"):
            if self.conflicts:
                self.conflicts -= 1
                self.policy["etag"] = "changed"
                return httpx.Response(409, json={"error": {"code": 409, "status": "ABORTED", "message": "etag"}})
            self.policy = json.loads(request.content)["policy"]
            return httpx.Response(200, json=self.policy)
        return httpx.Response(404)

    def monitoring(self, request):
        f = request.url.params["filter"]
        metric = f.split('"')[1]
        points = [
            {"interval": {"startTime": gd._rfc3339(at - 300), "endTime": gd._rfc3339(at)}, "value": value}
            for at, value in sorted(self.series.get(metric, []), reverse=True)  # newest first, as Monitoring sends
        ]
        if not points:
            return httpx.Response(200, json={"unit": "1"})
        if self.pages and "pageToken" not in request.url.params:
            return httpx.Response(200, json={"timeSeries": [{"points": points[:1]}], "nextPageToken": "p2"})
        if self.pages:
            points = points[1:]
        return httpx.Response(200, json={"timeSeries": [{"metric": {"type": metric}, "points": points}]})


@pytest.fixture
def google():
    return Google()


@pytest.fixture
def rest(google):
    return GoogleRest(token=lambda: "test-token", transport=httpx.MockTransport(google))


def test_request_counts_from_monitoring(google, rest):
    google.series[gd.METRIC_REQUESTS] = [
        (NOW, {"int64Value": "4200"}),
        (NOW - 300, {"int64Value": "800"}),
        (NOW - 3_300, {"int64Value": "5"}),
    ]
    mon = CloudMonitoring(rest, PROJECT, REGION, SERVICE)
    assert mon.recent_requests(NOW) == (4_200, 5_005)
    (req,) = google.requests
    assert req.url.path == f"/v3/projects/{PROJECT}/timeSeries"
    assert req.headers["authorization"] == "Bearer test-token"
    params = req.url.params
    assert params["filter"] == (
        'metric.type = "run.googleapis.com/request_count" AND resource.type = "cloud_run_revision" '
        f'AND resource.labels.service_name = "{SERVICE}" AND resource.labels.location = "{REGION}"'
    )
    assert params["aggregation.alignmentPeriod"] == "60s"
    assert params["aggregation.perSeriesAligner"] == "ALIGN_SUM"
    assert params["aggregation.crossSeriesReducer"] == "REDUCE_SUM"
    assert params["interval.endTime"] == "2026-10-19T08:00:00Z"
    assert params["interval.startTime"] == "2026-10-19T07:00:00Z"


def test_no_traffic_reads_as_zero(rest):
    assert CloudMonitoring(rest, PROJECT, REGION, SERVICE).recent_requests(NOW) == (0, 0)


def test_the_busiest_recent_5_minutes_count_despite_monitorings_lag(google, rest):
    # 2,000 requests a minute from 8 minutes ago; Monitoring has nothing yet for the last 3 minutes
    google.series[gd.METRIC_REQUESTS] = [(NOW - m * 60, {"int64Value": "2000"}) for m in range(3, 9)]
    five, hour = CloudMonitoring(rest, PROJECT, REGION, SERVICE).recent_requests(NOW)
    assert (five, hour) == (10_000, 12_000)
    assert five > LIM.guard_5min


def test_a_spike_that_ended_long_ago_is_not_the_last_5_minutes(google, rest):
    google.series[gd.METRIC_REQUESTS] = [(NOW - 1_200, {"int64Value": "9999"})]
    assert CloudMonitoring(rest, PROJECT, REGION, SERVICE).recent_requests(NOW) == (0, 9_999)


def test_pages_are_followed(google, rest):
    google.pages = True
    google.series[gd.METRIC_REQUESTS] = [(NOW, {"int64Value": "10"}), (NOW - 60, {"int64Value": "20"})]
    assert CloudMonitoring(rest, PROJECT, REGION, SERVICE).recent_requests(NOW) == (30, 30)
    assert google.requests[1].url.params["pageToken"] == "p2"


def test_month_usage(google, rest):
    google.series[gd.METRIC_REQUESTS] = [(NOW, {"int64Value": "1000000"})]
    google.series[gd.METRIC_SENT_BYTES] = [(NOW, {"int64Value": str(2 * 2**30)})]
    google.series[gd.METRIC_BILLABLE] = [(NOW, {"doubleValue": 4000.5}), (NOW - 3600, {"doubleValue": 6000})]
    usage = CloudMonitoring(rest, PROJECT, REGION, SERVICE).month_usage(NOW)
    assert usage == MonthUsage(1_000_000, 2 * 2**30, 10_000.5)
    assert {r.url.params["interval.startTime"] for r in google.requests} == {"2026-10-01T00:00:00Z"}
    assert {r.url.params["aggregation.alignmentPeriod"] for r in google.requests} == {"3600s"}


def test_month_usage_without_egress_or_instance_time(google, rest):
    google.series[gd.METRIC_REQUESTS] = [(NOW, {"int64Value": "10"})]
    google.fail["sent_bytes"] = (403, {"error": {"status": "PERMISSION_DENIED"}})
    google.fail["billable_instance_time"] = (500, {})
    assert CloudMonitoring(rest, PROJECT, REGION, SERVICE).month_usage(NOW) == MonthUsage(10, None, None)


def test_errors_name_the_api_and_status_but_not_the_project(google, rest):
    google.fail["timeSeries"] = (
        403,
        {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": f"Permission denied on {PROJECT}"}},
    )
    with pytest.raises(GuardError) as info:
        CloudMonitoring(rest, PROJECT, REGION, SERVICE).recent_requests(NOW)
    assert str(info.value) == "monitoring: HTTP 403 (PERMISSION_DENIED)" and info.value.status == 403
    assert PROJECT not in str(info.value) and "googleapis" not in str(info.value)
    assert info.value.__context__ is None


def test_transport_failures_and_credentials():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    rest = GoogleRest(token=lambda: "t", transport=httpx.MockTransport(timeout))
    with pytest.raises(GuardError, match="monitoring: timed out"):
        rest.call("monitoring", "GET", "https://monitoring.googleapis.com/v3/x")

    def no_creds():
        raise RuntimeError("no default credentials for demo-tifa")

    rest = GoogleRest(token=no_creds, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(GuardError) as info:
        rest.call("run.getIamPolicy", "GET", "https://run.googleapis.com/v2/x")
    assert str(info.value) == "run.getIamPolicy: no credentials (RuntimeError)"

    rest = GoogleRest(token=lambda: "t", transport=httpx.MockTransport(lambda r: httpx.Response(200, text="[]")))
    with pytest.raises(GuardError, match="not a JSON object"):
        rest.call("monitoring", "GET", "https://monitoring.googleapis.com/v3/x")


def test_calls_stop_when_the_run_is_out_of_time():
    clock = [100.0]
    rest = GoogleRest(
        token=lambda: "t",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        monotonic=lambda: clock[0],
    )
    assert rest.call("monitoring", "GET", "https://monitoring.googleapis.com/v3/x", deadline=105.0) == {}
    with pytest.raises(GuardError, match="out of time"):
        rest.call("monitoring", "GET", "https://monitoring.googleapis.com/v3/x", deadline=100.2)


def test_invoker_get_and_set(google, rest):
    inv = CloudRunInvoker(rest, PROJECT, REGION, SERVICE)
    assert inv.is_public()
    get = google.requests[0]
    assert get.method == "GET"
    assert get.url.path == f"/v2/projects/{PROJECT}/locations/{REGION}/services/{SERVICE}:getIamPolicy"
    assert get.url.params["options.requestedPolicyVersion"] == "3"

    assert inv.set_public(False) is True
    post = google.requests[-1]
    assert post.method == "POST" and post.url.path.endswith(f"/services/{SERVICE}:setIamPolicy")
    sent = json.loads(post.content)["policy"]
    assert sent["etag"] == "BwX1" and not is_public(sent)
    assert sent["bindings"][1] == {"role": "roles/run.viewer", "members": ["allUsers"]}  # other roles untouched

    n = len(google.requests)
    assert inv.set_public(False) is False  # already closed: read only
    assert len(google.requests) == n + 1 and google.requests[-1].method == "GET"


def test_invoker_retries_a_policy_changed_meanwhile(google, rest):
    google.conflicts = 1
    assert CloudRunInvoker(rest, PROJECT, REGION, SERVICE).set_public(False) is True
    assert [r.method for r in google.requests] == ["GET", "POST", "GET", "POST"]
    assert not is_public(google.policy)


def test_invoker_gives_up_after_two_conflicts(google, rest):
    google.conflicts = 5
    with pytest.raises(GuardError) as info:
        CloudRunInvoker(rest, PROJECT, REGION, SERVICE).set_public(False)
    assert info.value.status == 409


def test_default_credentials_are_refreshed_when_expired(monkeypatch):
    import google.auth

    class Creds:
        valid, token, refreshes = False, None, 0

        def refresh(self, request):
            self.refreshes += 1
            self.valid, self.token = True, f"token-{self.refreshes}"

    creds = Creds()
    monkeypatch.setattr(google.auth, "default", lambda scopes: (creds, None))
    token = gd.DefaultToken()
    assert token() == "token-1" and token() == "token-1"
    creds.valid = False
    assert token() == "token-2"


def test_build_guard(rest):
    store = MemoryStore()
    off = gd.build_guard(Settings(guard_mode="off"), store)
    assert off.mode == "off" and off.monitoring is None and off.invoker is None
    off = gd.build_guard(Settings(guard_mode="off", project=PROJECT, region=REGION), store, rest=rest)
    assert off.monitoring is None and isinstance(off.invoker, CloudRunInvoker)  # only to undo its own closures
    on = gd.build_guard(Settings(guard_mode="dry-run", project=PROJECT, region=REGION), store, rest=rest)
    assert isinstance(on.monitoring, CloudMonitoring) and isinstance(on.invoker, CloudRunInvoker)
    assert on.mode == "dry-run"
    with pytest.raises(ValueError):
        Guard(store, monitoring=None, invoker=None, mode="enforce")


# ---- the guard's runs (fakes) ---------------------------------------------------------------------------------


def make_guard(store=None, mon=None, inv=None, **kw):
    store = store if store is not None else MemoryStore()
    mon = mon if mon is not None else FakeMonitoring()
    inv = inv if inv is not None else FakeInvoker()
    return Guard(store, monitoring=mon, invoker=inv, monotonic=lambda: 0.0, **kw), store, mon, inv


def test_a_flood_closes_the_api_and_it_reopens_after_the_closure():
    guard, store, mon, inv = make_guard()
    mon.five = 12_000
    run = guard.run(NOW)
    assert run.action == "closed" and run.trip == "requests_5min"
    assert not inv.public and inv.sets == 1
    saved = store.get_guard()
    assert saved == run.doc
    assert (saved["closed_until"], saved["reason"], saved["applied"], saved["heartbeat"]) == (
        NOW + 1_800,
        "requests_5min",
        "closed",
        NOW,
    )
    assert saved["readings"] == {"at": NOW, "requests_5min": 12_000, "requests_1h": 0}
    # other members and roles are kept
    assert inv.policy["bindings"][0]["members"] == ["domain:example.com"]
    assert inv.policy["bindings"][1] == {"role": "roles/run.developer", "members": ["domain:example.org"]}

    mon.five = 0
    assert guard.run(NOW + 600).action is None and inv.sets == 1
    inv.policy, _ = with_public(inv.policy, True)  # someone reopened it by hand during the closure
    assert guard.run(NOW + 720).action == "closed" and not inv.public

    run = guard.run(NOW + 1_800)
    assert run.action == "opened" and inv.public
    assert store.get_guard()["applied"] == "open" and store.get_guard()["closed_until"] is None


def test_the_guard_never_opens_what_it_did_not_close():
    guard, store, mon, inv = make_guard(inv=FakeInvoker(public=False))
    run = guard.run(NOW)
    assert run.action is None and not inv.public and inv.sets == 0 and inv.gets == 0


def test_dry_run_decides_and_logs_but_never_edits_iam(caplog):
    guard, store, mon, inv = make_guard(mode="dry-run")
    mon.five = 12_000
    assert guard.run(NOW).action == "would_close"
    assert inv.public and inv.gets == 0
    assert "dry run" in caplog.text
    saved = store.get_guard()
    assert saved["mode"] == "dry-run" and saved["closed_until"] == NOW + 1_800 and saved["applied"] is None
    mon.five = 0
    assert guard.run(NOW + 1_800).action == "would_open"


def test_mode_off_does_nothing():
    store = MemoryStore()
    guard = Guard(store, monitoring=None, invoker=None, mode="off")
    assert guard.maybe_run(NOW) is None
    assert store.get_guard() is None


def test_runs_at_most_every_two_minutes_across_instances():
    store = MemoryStore()
    a, _, mon_a, _ = make_guard(store)
    b, _, mon_b, _ = make_guard(store)
    assert a.maybe_run(NOW) is not None
    assert a.maybe_run(NOW + 60) is None and b.maybe_run(NOW + 60) is None
    assert (mon_a.recent_calls, mon_b.recent_calls) == (1, 0)
    assert b.maybe_run(NOW + 120) is not None  # due, and b reads the heartbeat a stored
    assert a.maybe_run(NOW + 180) is None  # a reads b's heartbeat (its own record said due)
    assert (mon_a.recent_calls, mon_b.recent_calls) == (1, 1)
    assert a.maybe_run(NOW + 240) is not None


def test_a_monitoring_failure_is_recorded_and_changes_nothing():
    guard, store, mon, inv = make_guard()
    mon.fail = GuardError("monitoring: HTTP 503", status=503)
    run = guard.run(NOW)
    assert run.errors == ["requests: monitoring: HTTP 503"] and not run.closed
    assert store.get_guard()["error"] == "requests: monitoring: HTTP 503"
    assert store.get_guard()["heartbeat"] == NOW


def test_an_iam_failure_keeps_the_closure_to_retry(caplog):
    guard, store, mon, inv = make_guard()
    mon.five = 12_000
    inv.fail = GuardError("run.setIamPolicy: HTTP 403 (PERMISSION_DENIED)", status=403)
    run = guard.run(NOW)
    assert run.action is None and run.closed
    saved = store.get_guard()
    assert saved["closed_until"] == NOW + 1_800 and saved["applied"] is None
    assert "iam: run.setIamPolicy: HTTP 403" in saved["error"]
    inv.fail = None
    assert guard.run(NOW + 120).action == "closed" and not inv.public


def test_maybe_run_never_raises():
    guard, store, mon, inv = make_guard()

    def broken(doc):
        raise RuntimeError("firestore down")

    store.put_guard = broken
    assert guard.maybe_run(NOW) is None
    mon.fail = KeyError("odd")
    assert guard.maybe_run(NOW + 600) is None


def test_month_to_date_is_read_every_half_hour_and_per_month():
    usage = MonthUsage(1_000_000, 2 * 2**30, 10_000.0)
    guard, store, mon, inv = make_guard(mon=FakeMonitoring(usage=usage))
    guard.run(NOW)
    mtd = store.get_guard()["mtd"]
    assert mtd == {
        "month": "2026-10",
        "at": NOW,
        "requests": 1_000_000,
        "sent_bytes": 2 * 2**30,
        "billable_s": 10_000.0,
        "cost_usd": Pricing().cost(1_000_000, 2 * 2**30, 10_000.0),
    }
    guard.run(NOW + 120)
    assert mon.month_calls == 1
    guard.run(NOW + 1_800)
    assert mon.month_calls == 2
    late = utc(2026, 10, 31, 23, 58)
    guard.run(late)
    guard.run(late + 150)  # a new month: read again at once
    assert mon.month_calls == 4 and store.get_guard()["mtd"]["month"] == "2026-11"


def test_month_to_date_failures_keep_this_months_estimate():
    guard, store, mon, inv = make_guard(mon=FakeMonitoring(usage=MonthUsage(10, 10, 1.0)))
    guard.run(NOW)
    mon.fail_month = GuardError("monitoring: timed out")
    guard.run(NOW + 1_800)
    assert store.get_guard()["mtd"]["at"] == NOW
    guard.run(NOV + 60)
    assert store.get_guard()["mtd"] is None  # last month's estimate is not this month's


def test_spend_over_the_limit_closes_until_the_first():
    expensive = MonthUsage(1_000_000, 0, 100_000.0)
    assert Pricing().cost(1_000_000, 0, 100_000.0) > LIM.guard_month_usd
    guard, store, mon, inv = make_guard(mon=FakeMonitoring(usage=expensive))
    run = guard.run(NOW)
    assert run.trip == "month_cost" and store.get_guard()["closed_until"] == NOV and not inv.public
    mon.usage = MonthUsage(0, 0, 0.0)
    assert guard.run(NOV + 30).action == "opened" and inv.public


def test_pricing():
    p = Pricing()
    assert p.cost(0, 0, 0.0) == 0
    assert p.cost(1_000_000, None, 0.0) == 0.4
    assert p.cost(0, 2**30, 0.0) == 0.12
    assert p.cost(0, 0, 1_000.0) == round(1_000 * (0.000024 + 0.5 * 0.0000025), 4)
    assert p.cost(1_000, 0, None) == p.cost(1_000, 0, 100.0)  # unknown instance time: 0.1 s a request


def test_a_budget_notification_closes_until_the_period_ends(caplog):
    guard, store, mon, inv = make_guard()
    run = guard.budget(notice(), NOW)
    assert run.trip == "budget" and run.action == "closed" and not inv.public
    saved = store.get_guard()
    assert saved["closed_until"] == utc(2026, 11, 1, 7) and saved["reason"] == "budget"
    assert saved["heartbeat"] is None  # only the poll path's runs beat
    assert saved["budget"]["ratio"] == round(3.8 / 3.75, 4)
    assert "101% of the budget" in caplog.text
    # the poll path keeps it closed, and later notifications under 100% do not reopen it
    assert guard.run(NOW + 120).closed
    assert guard.budget(notice(cost=1.0), NOW + 3_600).closed and not inv.public
    assert guard.run(utc(2026, 11, 1, 7)).action == "opened"


def test_a_budget_notification_under_100_percent_is_recorded_only():
    guard, store, mon, inv = make_guard()
    run = guard.budget(notice(cost=3.0, threshold=0.9), NOW)
    assert run.action is None and inv.public and inv.gets == 0
    assert store.get_guard()["budget"]["threshold"] == 0.9


def test_a_budget_notification_in_dry_run_is_recorded_as_the_dry_runs():
    guard, store, mon, inv = make_guard(mode="dry-run")
    run = guard.budget(notice(), NOW)
    assert run.trip == "budget" and run.action == "would_close" and not run.closed
    assert inv.public and inv.gets == 0
    saved = store.get_guard()
    assert saved["closed_until"] == utc(2026, 11, 1, 7) and saved["applied"] is None
    assert gd.is_dry_closure(saved) and saved["trips"][-1]["dry_run"] is True


def test_a_budget_notification_in_mode_off_is_recorded_only():
    for inv in (None, FakeInvoker()):
        store = MemoryStore()
        run = Guard(store, monitoring=None, invoker=inv, mode="off").budget(notice(), NOW)
        assert run.trip is None and run.action is None and not run.closed
        saved = store.get_guard()
        assert saved["closed_until"] is None and saved["budget"]["ratio"] == round(3.8 / 3.75, 4)
        assert inv is None or (inv.public and inv.gets == 0)


def test_a_budget_notification_that_cannot_be_saved_raises():
    guard, store, mon, inv = make_guard()

    def broken(doc):
        raise RuntimeError("down")

    store.put_guard = broken
    with pytest.raises(RuntimeError):
        guard.budget(notice(), NOW)


def test_the_stored_document_is_storable():
    guard, store, mon, inv = make_guard(mon=FakeMonitoring(five=12_000, usage=MonthUsage(5, None, None)))
    guard.run(NOW)
    guard.budget(notice(), NOW + 60)
    saved = store.get_guard()
    assert set(saved) == set(normalise(None))
    assert isinstance(Decision(saved, True), Decision)


# ---- switching modes ------------------------------------------------------------------------------------------

LOW = Limits(guard_5min=20, guard_1h=200)  # lowered for a dry run, as the operations guide suggests


def manual_close(doc, now, until):
    """guard/state as an operator's manual close writes it (tools/guard_cli.py close)."""
    d = normalise(doc)
    d.update(closed_until=until, reason="manual", applied="closed")
    d["trips"].append({"at": now, "reason": "manual", "until": until})
    return d


def manual_open(doc, now):
    """guard/state as an operator's manual open writes it (tools/guard_cli.py open)."""
    d = normalise(doc)
    d.update(closed_until=None, reason=None, applied="open")
    d["trips"].append({"at": now, "reason": "manual_open", "until": None})
    return d


def test_a_dry_runs_closures_and_strikes_are_never_enforced_after_the_switch():
    """About 5 hours of dry run with lowered limits against ordinary traffic, then enforce with the real limits."""
    store, inv = MemoryStore(), FakeInvoker()
    mon = FakeMonitoring(five=50, hour=400)
    dry, *_ = make_guard(store, mon, inv, mode="dry-run", limits=LOW)
    t, actions = NOW, []
    while t < NOW + 5 * 3_600:
        actions.append(dry.run(t).action)
        t += 120
    saved = store.get_guard()
    assert "would_close" in actions and inv.public and inv.gets == 0
    assert saved["strikes"] == 3 and gd.is_dry_closure(saved) and saved["closed_until"] > t  # still "closed"
    assert saved["trips"] and all(trip["dry_run"] is True for trip in saved["trips"])

    enforce, *_ = make_guard(store, mon, inv)
    run = enforce.run(t)
    assert run.action is None and not run.closed and inv.public and inv.gets == 0
    saved = store.get_guard()
    assert (saved["closed_until"], saved["reason"], saved["strikes"], saved["last_5min_until"]) == (None, None, 0, None)
    assert saved["dry_run"] is None and saved["mode"] == "enforce"

    mon.five = 12_000  # a real flood later starts at 30 minutes, not where the dry run's doubling had got to
    assert enforce.run(t + 120).action == "closed"
    saved = store.get_guard()
    assert (saved["closed_until"], saved["strikes"]) == (t + 120 + 1_800, 1)


def test_a_dry_run_leaves_the_real_doubling_as_it_was():
    store, inv = MemoryStore(), FakeInvoker()
    mon = FakeMonitoring(five=12_000)
    enforce, *_ = make_guard(store, mon, inv)
    assert enforce.run(NOW).action == "closed"  # the first strike: 30 minutes
    mon.five = 0
    assert enforce.run(NOW + 1_800).action == "opened"
    dry, *_ = make_guard(store, mon, inv, mode="dry-run", limits=LOW)
    mon.five = 50
    for k in range(1, 40):
        dry.run(NOW + 1_800 + 120 * k)
    assert store.get_guard()["strikes"] == 3  # the dry run's own doubling
    assert store.get_guard()["dry_run"]["strikes"] == 1  # the real one, kept
    mon.five = 12_000
    t = NOW + 3 * 3_600
    assert enforce.run(t).action == "closed"
    saved = store.get_guard()
    assert (saved["strikes"], saved["closed_until"]) == (2, t + 3_600)  # the real second strike: an hour


def test_a_manual_closure_during_a_dry_run_is_real():
    store, inv = MemoryStore(), FakeInvoker()
    mon = FakeMonitoring(five=50)
    dry, *_ = make_guard(store, mon, inv, mode="dry-run", limits=LOW)
    dry.run(NOW)
    assert gd.is_dry_closure(store.get_guard())
    # an operator closes the API by hand (tools/guard_cli.py close --force): guard/state, then the binding
    store.put_guard(manual_close(store.get_guard(), NOW + 60, NOW + 7_200))
    inv.policy, _ = with_public(inv.policy, False)
    run = dry.run(NOW + 120)
    assert run.closed and run.action is None and inv.gets == 0  # kept as it is, neither extended nor applied
    saved = store.get_guard()
    assert (saved["closed_until"], saved["reason"], saved["applied"]) == (NOW + 7_200, "manual", "closed")
    assert not gd.is_dry_closure(saved)

    enforce, *_ = make_guard(store, mon, inv)
    inv.policy, _ = with_public(inv.policy, True)  # someone adds allUsers back
    assert enforce.run(NOW + 240).action == "closed" and not inv.public
    assert store.get_guard()["reason"] == "manual"


def test_a_dry_run_reopens_a_closure_applied_before_the_switch():
    store, inv = MemoryStore(), FakeInvoker()
    mon = FakeMonitoring(five=12_000)
    enforce, *_ = make_guard(store, mon, inv)
    assert enforce.run(NOW).action == "closed" and not inv.public
    dry, *_ = make_guard(store, mon, inv, mode="dry-run", mtd_every=0)
    mon.five, mon.usage = 0, MonthUsage(1_000_000, 0, 100_000.0)  # over the month's cost limit
    run = dry.run(NOW + 120)
    assert run.closed and run.action == "would_close" and run.trip == "month_cost"
    saved = store.get_guard()
    assert (saved["closed_until"], saved["applied"]) == (NOW + 1_800, "closed")  # not extended to the 1st
    assert saved["trips"][-1] == {"at": NOW + 120, "reason": "month_cost", "until": NOV, "dry_run": True}

    inv.policy, _ = with_public(inv.policy, True)  # reopened by hand: a dry run does not close it again
    gets = inv.gets
    run = dry.run(NOW + 240)
    assert run.action is None and inv.public and inv.gets == gets
    assert len(store.get_guard()["trips"]) == 2  # the same would-be extension is reported once

    inv.policy, _ = with_public(inv.policy, False)
    mon.usage = MonthUsage(0, 0, 0.0)
    run = dry.run(NOW + 1_800)
    assert run.action == "opened" and inv.public
    saved = store.get_guard()
    assert (saved["closed_until"], saved["applied"], saved["dry_run"]["until"]) == (None, "open", None)


def test_mode_off_reopens_a_closure_applied_before_the_switch():
    store, inv = MemoryStore(), FakeInvoker()
    enforce, *_ = make_guard(store, FakeMonitoring(five=12_000), inv)
    enforce.run(NOW)
    assert not inv.public
    off = Guard(store, monitoring=None, invoker=inv, mode="off")
    before = store.get_guard()
    assert off.maybe_run(NOW + 600) is None  # still running: kept, and nothing is written
    assert store.get_guard() == before and not inv.public
    run = off.maybe_run(NOW + 1_800)
    assert run.action == "opened" and inv.public
    saved = store.get_guard()
    assert (saved["applied"], saved["closed_until"], saved["mode"], saved["heartbeat"]) == ("open", None, "off", NOW)
    gets = inv.gets
    assert off.maybe_run(NOW + 2_000) is None and inv.gets == gets  # nothing left to undo


def test_mode_off_drops_a_dry_runs_closure_without_touching_iam():
    store, inv = MemoryStore(), FakeInvoker()
    dry, *_ = make_guard(store, FakeMonitoring(five=50), inv, mode="dry-run", limits=LOW)
    dry.run(NOW)
    off = Guard(store, monitoring=None, invoker=inv, mode="off")
    assert off.run(NOW + 120) is None and inv.gets == 0  # nothing the guard applied
    assert released(store.get_guard(), NOW + 120).doc["closed_until"] is None


# ---- other writers during a run ---------------------------------------------------------------------------------


def test_a_budget_push_on_the_other_instance_during_a_run_is_kept():
    store, inv = MemoryStore(), FakeInvoker()
    a, _, mon, _ = make_guard(store, inv=inv)
    b, *_ = make_guard(store, inv=inv)
    mon.during = lambda: b.budget(notice(cost=4.0), NOW)  # 107%, handled by the other instance
    run = a.run(NOW)
    assert run.closed and not inv.public
    saved = store.get_guard()
    assert (saved["closed_until"], saved["reason"], saved["applied"]) == (utc(2026, 11, 1, 7), "budget", "closed")
    assert saved["heartbeat"] == NOW and saved["budget"]["ratio"] == round(4.0 / 3.75, 4)


def test_a_budget_push_during_the_runs_own_iam_call_is_kept():
    store, inv = MemoryStore(), FakeInvoker()
    a, _, mon, _ = make_guard(store, inv=inv)
    b, *_ = make_guard(store, inv=inv)
    mon.five = 12_000
    a.run(NOW)
    mon.five = 0
    inv.before_set = lambda: b.budget(notice(cost=4.0), NOW + 1_800)  # lands while a reopens the API
    run = a.run(NOW + 1_800)
    assert run.closed and not inv.public  # a reopened it, saw b's closure, and closed it again
    saved = store.get_guard()
    assert (saved["closed_until"], saved["reason"], saved["applied"]) == (utc(2026, 11, 1, 7), "budget", "closed")


def test_an_operator_open_during_a_run_stands():
    store, inv = MemoryStore(), FakeInvoker()
    guard, _, mon, _ = make_guard(store, inv=inv)
    mon.five = 12_000
    guard.run(NOW)
    mon.five = 0
    inv.policy, _ = with_public(inv.policy, True)  # allUsers added back: the next run closes it again...

    def operator_open():  # ...just as an operator opens the API (tools/guard_cli.py open --force)
        store.put_guard(manual_open(store.get_guard(), NOW + 120))
        inv.policy, _ = with_public(inv.policy, True)

    inv.before_set = operator_open
    run = guard.run(NOW + 120)
    assert run.action == "opened" and inv.public
    saved = store.get_guard()
    assert (saved["closed_until"], saved["applied"]) == (None, "open")


def test_a_run_saves_its_last_decision_when_the_state_keeps_changing(caplog):
    guard, store, mon, inv = make_guard()
    stored_get = store.get_guard
    writes = iter(range(1, 100))

    def changing():
        doc = stored_get() or gd.empty_doc()
        doc["error"] = f"someone else's write {next(writes)}"
        return doc

    store.get_guard = changing
    run = guard.run(NOW)
    assert run.doc["heartbeat"] == NOW and stored_get()["heartbeat"] == NOW
    assert "kept changing" in caplog.text
