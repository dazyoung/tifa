"""tools/guard_cli.py: showing, closing and reopening through guard/state and a fake gcloud, and budget messages."""

import calendar
import io
import json
import subprocess
import threading

import pytest

from tifa.config import Limits
from tifa.core.timeutil import iso
from tifa.poller.budget import parse_notification, parse_push
from tifa.poller.guard import Guard, MonthUsage, Readings, decide
from tifa.store.memory import MemoryStore
from tools import guard_cli as gc

NOW = calendar.timegm((2026, 10, 19, 12, 0, 0))
NEXT_MONTH = calendar.timegm((2026, 11, 1, 0, 0, 0))
WHERE = ["--project", "demo-tifa", "--region", "europe-west1"]
NOT_BOUND = (
    "ERROR: (gcloud.run.services.remove-iam-policy-binding) Policy binding with the specified principal, role, and"
    " condition not found!"
)
REMOVE = ["gcloud", "run", "services", "remove-iam-policy-binding", "tifa-api", "--project=demo-tifa",
          "--region=europe-west1", "--member=allUsers", "--role=roles/run.invoker"]  # fmt: skip
POLICY = ["gcloud", "run", "services", "get-iam-policy", "tifa-api", "--project=demo-tifa", "--region=europe-west1",
          "--format=json"]  # fmt: skip


class Gcloud:
    """Stands in for subprocess.run: records each command and keeps one service's allUsers binding.

    get-iam-policy prints the policy; add and remove change it, and remove fails as gcloud does when allUsers is not
    bound. A non-zero `returncode` makes every command fail with "permission denied"; `policy_fails` only the read.
    `on_change` is called after each add or remove."""

    def __init__(self, returncode=0, public=True, policy_fails=False, on_change=None):
        self.calls, self.returncode, self.public = [], returncode, public
        self.policy_fails, self.on_change = policy_fails, on_change

    def verbs(self):
        return [cmd[3] for cmd in self.calls]

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if self.returncode:
            return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr="permission denied")
        verb = cmd[3]
        if verb == "get-iam-policy":
            if self.policy_fails:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="could not read")
            members = ["allUsers"] if self.public else []
            policy = {"bindings": [{"role": "roles/run.invoker", "members": members}], "etag": "BwY"}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(policy), stderr="")
        if verb == "remove-iam-policy-binding" and not self.public:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=NOT_BOUND)
        self.public = verb == "add-iam-policy-binding"
        if self.on_change:
            self.on_change(verb)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class Sleeps:
    """Stands in for time.sleep: records each wait and runs `then` (what happens meanwhile) each time."""

    def __init__(self, then=None):
        self.waits, self.then = [], then

    def __call__(self, seconds):
        self.waits.append(seconds)
        if self.then:
            self.then()


def run(args, store=None, gcloud=None, env=None, sleep=None, inject=True):
    out, err = io.StringIO(), io.StringIO()
    store = store if store is not None else MemoryStore()
    gcloud = gcloud or Gcloud()
    sleep = sleep or Sleeps()
    code = gc.main([str(a) for a in args], store=store if inject else None, run=gcloud, sleep=sleep, now=NOW,
                   env=env or {}, out=out, err=err)  # fmt: skip
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue(), store, gcloud


def no_readings():
    return Readings(requests_5min=0, requests_1h=0, mtd_cost=None)


def closure(reason, until, mode="enforce", applied="closed"):
    trips = [{"at": NOW - 90, "reason": reason, "until": until}]
    return {"schema": 1, "mode": mode, "heartbeat": NOW - 90, "closed_until": until, "reason": reason,
            "applied": applied, "strikes": 1, "trips": trips}  # fmt: skip


def stored(doc):
    store = MemoryStore()
    store.put_guard(doc)
    return store


# ---------------------------------------------------------------- show


def test_show_without_guard_state():
    code, report, *_ = run(["show"])
    assert code == 0 and report["guard"] is None and report["summary"]["closed"] is False


def test_show_summarises_a_closure():
    code, report, *_ = run(["show"], stored(closure("requests_5min", NOW + 1800)))
    summary = report["summary"]
    assert code == 0 and summary["closed"] and summary["decided"] and summary["closed_until"] == iso(NOW + 1800)
    assert summary["reason"] == "requests_5min" and summary["heartbeat_age_s"] == 90 and summary["mode"] == "enforce"
    assert summary["last_trips"] == [{"at": iso(NOW - 90), "reason": "requests_5min", "until": iso(NOW + 1800)}]
    assert "note" not in summary


def test_show_counts_a_closure_as_closed_only_once_applied_as_health_does():
    code, report, *_ = run(["show"], stored(closure("requests_5min", NOW + 1800, mode="dry-run", applied=None)))
    summary = report["summary"]
    assert code == 0 and summary["closed"] is False and summary["decided"] is True and summary["mode"] == "dry-run"
    assert "not applied" in summary["note"] and "dry-run mode" in summary["note"]
    _, report, *_ = run(["show"], stored(closure("manual", NOW - 60)))  # ended, allUsers not back yet
    assert report["summary"]["closed"] is False and "not back yet" in report["summary"]["note"]


# ---------------------------------------------------------------- the store and the service


def test_the_store_is_firestore_unless_memory_is_asked_for():
    for env in ({}, {"TIFA_STORE": "memory"}):  # TIFA_STORE never picks the memory store here
        code, _, err, *_ = run(["show"], env=env, inject=False)
        assert code == gc.EXIT_USAGE and "GOOGLE_CLOUD_PROJECT" in err
    code, report, err, _, gcloud = run(["show", "--store", "memory"], inject=False)
    assert code == 0 and report["guard"] is None and "keeps nothing" in err and gcloud.calls == []


def test_force_is_refused_with_the_memory_store():
    for action in ("close", "open"):
        code, report, err, _, gcloud = run([action, "--force", "--store", "memory", *WHERE], inject=False)
        assert code == gc.EXIT_USAGE and report is None and "--store memory" in err and gcloud.calls == []


def test_only_tifa_services_are_touched():
    cases = ((["--service", "website"], {}), ([], {"TIFA_API_SERVICE": "website-api"}), (["--service", "tifa-"], {}),
             (["--service", "tifa-API"], {}))  # fmt: skip
    for args, env in cases:
        for action in ("close", "open"):
            code, report, err, store, gcloud = run([action, "--force", *WHERE, *args], env=env)
            assert code == gc.EXIT_USAGE and report is None and "not a Tifa service" in err
            assert gcloud.calls == [] and store.get_guard() is None
    assert gc.is_tifa_service("tifa-api") and gc.is_tifa_service("tifa-api-test")


# ---------------------------------------------------------------- close and open


def test_close_without_force_only_prints_the_plan():
    code, plan, err, store, gcloud = run(["close", "--for", "2h", *WHERE])
    assert code == 0 and plan["applied"] is False and "--force" in err
    assert plan["command"] == (
        "gcloud run services remove-iam-policy-binding tifa-api --project=demo-tifa --region=europe-west1"
        " --member=allUsers --role=roles/run.invoker"
    )
    assert plan["check"] == (
        "gcloud run services get-iam-policy tifa-api --project=demo-tifa --region=europe-west1 --format=json"
    )
    assert plan["guard_state"]["closed_until"] == NOW + 7200
    assert store.get_guard() is None and gcloud.calls == []


def test_printed_commands_use_placeholders_for_what_is_not_set():
    _, plan, *_ = run(["open"])
    assert "--project=PROJECT_ID --region=REGION" in plan["command"]
    _, plan, *_ = run(["open"], env={"GOOGLE_CLOUD_PROJECT": "demo-tifa", "TIFA_REGION": "europe-west1",
                                     "TIFA_API_SERVICE": "tifa-api-test"})  # fmt: skip
    assert "add-iam-policy-binding tifa-api-test --project=demo-tifa --region=europe-west1" in plan["command"]


def test_force_needs_a_project_and_region():
    code, _, err, store, gcloud = run(["close", "--force"])
    assert code == gc.EXIT_USAGE and "--project" in err and store.get_guard() is None and gcloud.calls == []


def test_close_with_force_writes_a_manual_closure_the_guard_keeps():
    code, plan, err, store, gcloud = run(["close", "--for", "2h", "--reason", "rehearsal", "--force", *WHERE],
                                         stored(closure("requests_1h", NOW - 10)))  # fmt: skip
    assert code == 0 and plan["applied"] is True and plan["iam"] == "changed" and plan["verified"] is True
    assert gcloud.calls == [POLICY, REMOVE] and gcloud.public is False
    doc = store.get_guard()
    assert doc["closed_until"] == NOW + 7200 and doc["reason"] == "manual" and doc["applied"] == "closed"
    assert doc["trips"][-1] == {"at": NOW, "reason": "manual", "until": NOW + 7200, "note": "rehearsal"}
    assert decide(doc, no_readings(), NOW + 3600, Limits()).closed  # kept by the guard
    assert not decide(doc, no_readings(), NOW + 7200, Limits()).closed  # and ended by it
    assert "waiting 30 s" in err


def test_close_by_default_lasts_until_the_first_of_next_month():
    _, plan, *_ = run(["close"])
    assert plan["guard_state"]["closed_until"] == NEXT_MONTH
    _, plan, *_ = run(["close", "--until", "2026-10-20T08:00:00Z"])
    assert plan["guard_state"]["closed_until"] == calendar.timegm((2026, 10, 20, 8, 0, 0))
    assert run(["close", "--until", "2026-10-19T11:00:00Z"])[0] == gc.EXIT_USAGE  # already over
    assert run(["close", "--for", "soon"])[0] == gc.EXIT_USAGE


def test_close_keeps_a_longer_closure_unless_told_to_shorten_it():
    budget = closure("budget", NEXT_MONTH)
    code, plan, err, store, gcloud = run(["close", "--for", "2h", "--force", *WHERE], stored(budget),
                                         gcloud=Gcloud(public=False))  # fmt: skip
    doc = store.get_guard()
    assert code == 0 and plan["closure"] == "kept" and "--shorten" in err
    assert doc["closed_until"] == NEXT_MONTH and doc["reason"] == "budget" and doc["applied"] == "closed"
    assert doc["trips"][-1] == {"at": NOW, "reason": "manual", "until": NOW + 7200}  # the request is recorded
    assert decide(doc, no_readings(), NOW + 7200, Limits()).closed  # the budget closure still holds

    code, plan, err, store, _ = run(["close", "--for", "2h", "--shorten", "--force", *WHERE], stored(budget))
    doc = store.get_guard()
    assert code == 0 and plan["closure"] == "shortened" and "next budget notification" in err
    assert doc["closed_until"] == NOW + 7200 and doc["reason"] == "manual"
    _, plan, *_ = run(["close", "--until", str(NEXT_MONTH + 86400)], stored(budget))  # a longer one replaces it
    assert "closure" not in plan and plan["guard_state"]["closed_until"] == NEXT_MONTH + 86400


def test_open_with_force_clears_the_closure():
    store = stored({"schema": 1, "mode": "enforce", "closed_until": NOW + 3600, "reason": "requests_1h",
                    "applied": "closed", "strikes": 2})  # fmt: skip
    code, plan, err, store, gcloud = run(["open", "--force", *WHERE], store, gcloud=Gcloud(public=False))
    assert code == 0 and gcloud.verbs() == ["get-iam-policy", "add-iam-policy-binding"] and gcloud.public
    doc = store.get_guard()
    assert doc["closed_until"] is None and doc["reason"] is None and doc["applied"] == "open"
    assert doc["strikes"] == 2  # repeat trips still count up
    assert not decide(doc, no_readings(), NOW + 60, Limits()).closed
    assert "request limit is still over" in err


def test_open_during_a_month_cost_closure_warns_that_the_guard_closes_it_again():
    code, _, err, store, _ = run(["open", "--force", *WHERE], stored(closure("month_cost", NEXT_MONTH)),
                                 gcloud=Gcloud(public=False))  # fmt: skip
    assert code == 0 and "TIFA_GUARD_MONTH_USD" in err
    again = decide(store.get_guard(), Readings(0, 0, Limits().guard_month_usd + 1), NOW + 120, Limits())
    assert again.closed and again.trip == "month_cost"


def test_an_api_already_closed_or_open_is_left_as_it_is():
    code, plan, _, _, gcloud = run(["close", "--force", *WHERE], gcloud=Gcloud(public=False))
    assert code == 0 and plan["iam"] == "unchanged" and gcloud.verbs() == ["get-iam-policy"]
    code, plan, _, _, gcloud = run(["open", "--force", *WHERE], gcloud=Gcloud(public=True))
    assert code == 0 and plan["iam"] == "unchanged" and gcloud.verbs() == ["get-iam-policy"]


def test_a_binding_already_gone_is_not_a_failure_even_unread():
    code, plan, err, store, gcloud = run(["close", "--force", *WHERE], gcloud=Gcloud(public=False, policy_fails=True))
    assert code == 0 and plan["applied"] is True and plan["iam"] == "unchanged" and "could not read" in err
    assert gcloud.verbs() == ["get-iam-policy", "remove-iam-policy-binding"]
    assert store.get_guard()["applied"] == "closed"


def test_a_gcloud_failure_is_reported_after_guard_state_is_written():
    code, plan, err, store, _ = run(["close", "--force", *WHERE], gcloud=Gcloud(returncode=1))
    assert code == gc.EXIT_FAILED and plan["applied"] is False and "permission denied" in err
    assert store.get_guard()["applied"] == "closed"  # the guard enforces it on its next run


# ---------------------------------------------------------------- the guard's mode


def test_without_guard_state_the_mode_is_left_unknown_and_the_operator_told():
    code, _, err, store, _ = run(["close", "--for", "1h", "--force", *WHERE])
    assert code == 0 and store.get_guard()["mode"] is None and "mode is unknown" in err


def test_a_guard_in_dry_run_or_off_mode_is_reported():
    for mode, said in (("dry-run", "does not keep it closed"), ("off", "only if the poller has its project")):
        code, _, err, store, _ = run(["close", "--for", "1h", "--force", *WHERE], stored(closure("manual", None, mode)))
        assert code == 0 and said in err and store.get_guard()["mode"] == mode
        code, _, err, *_ = run(["open", *WHERE], stored(closure("manual", NOW + 60, mode)))
        assert code == 0 and "will not close the API again" in err


class Flood:
    """Monitoring that reports a flood, and an Invoker that keeps a Gcloud's binding."""

    def __init__(self, gcloud, readings=None):
        self.gcloud, self.readings = gcloud, readings or (Limits().guard_5min + 1, 100)

    def recent_requests(self, now_ts, deadline=None):
        return self.readings

    def month_usage(self, now_ts, deadline=None):
        return MonthUsage(100, 1000, 10.0)

    def is_public(self, deadline=None):
        return self.gcloud.public

    def set_public(self, public, deadline=None):
        changed, self.gcloud.public = self.gcloud.public != public, public
        return changed


def test_a_dry_runs_own_closure_is_neither_closed_nor_kept_and_a_manual_one_is_real():
    gcloud = Gcloud(public=True)
    store = MemoryStore()
    flood = Flood(gcloud)
    Guard(store, monitoring=flood, invoker=flood, mode="dry-run").run(NOW - 60)
    _, report, *_ = run(["show"], store)
    assert report["summary"]["decided"] and report["summary"]["dry_run_closure"] and not report["summary"]["closed"]
    assert "a dry run's" in report["summary"]["note"] and gcloud.public  # the dry run applied nothing

    code, plan, _, store, _ = run(["close", "--for", "10m", "--force", *WHERE], store, gcloud=gcloud)
    doc = store.get_guard()
    assert code == 0 and "closure" not in plan  # not kept: the dry run's 30 minutes were never real
    assert doc["closed_until"] == NOW + 600 and doc["reason"] == "manual" and doc["dry_run"] is None
    assert gcloud.public is False
    run_ = Guard(store, monitoring=flood, invoker=flood, mode="dry-run").run(NOW + 120)
    assert run_.closed and store.get_guard()["closed_until"] == NOW + 600  # the dry run leaves the manual one be
    Guard(store, monitoring=Flood(gcloud, (0, 0)), invoker=flood, mode="dry-run").run(NOW + 700)
    assert gcloud.public and store.get_guard()["applied"] == "open"  # and puts allUsers back when it ends


# ---------------------------------------------------------------- an overlapping guard run


def test_a_change_an_overlapping_guard_run_put_back_is_written_again():
    store = stored(closure("requests_1h", NOW - 10))
    stale = store.get_guard()
    sleeps = Sleeps(then=lambda: store.put_guard(stale) if len(sleeps.waits) == 1 else None)
    gcloud = Gcloud()
    code, plan, err, store, _ = run(["close", "--for", "2h", "--force", *WHERE], store, gcloud=gcloud, sleep=sleeps)
    assert code == 0 and plan["verified"] is True and plan["attempts"] == 2 and "overwrote" in err
    assert sleeps.waits == [30, 30] and store.get_guard()["closed_until"] == NOW + 7200
    assert gcloud.verbs() == ["get-iam-policy", "remove-iam-policy-binding", "get-iam-policy"]


def test_a_change_lost_every_time_is_a_failure():
    store = MemoryStore()
    sleeps = Sleeps(then=lambda: store.put_guard(closure("requests_1h", NOW - 10)))
    code, plan, err, *_ = run(["close", "--force", "--settle", "5", *WHERE], store, sleep=sleeps)
    assert code == gc.EXIT_FAILED and plan["verified"] is False and plan["attempts"] == gc.ATTEMPTS
    assert sleeps.waits == [5] * gc.ATTEMPTS and "check it with show" in err


def test_settle_zero_skips_the_check():
    sleeps = Sleeps()
    code, plan, *_ = run(["open", "--force", "--settle", "0", *WHERE], sleep=sleeps)
    assert code == 0 and plan["verified"] is None and sleeps.waits == []
    assert run(["open", "--settle", "-1"])[0] == gc.EXIT_USAGE


class SlowSave(MemoryStore):
    """A store whose guard thread's save waits until `go` is set: the operator's write then lands after the guard's
    last read of guard/state and before its save, the window the guard's own re-reads cannot close."""

    def __init__(self):
        super().__init__()
        self.thread = None
        self.saving, self.go = threading.Event(), threading.Event()

    def put_guard(self, doc):
        if threading.current_thread() is self.thread:
            self.saving.set()
            assert self.go.wait(5)
        super().put_guard(doc)


def overlap(doc, gcloud):
    """A store holding doc and a real Guard run in a thread, stopped just before it saves."""
    store = SlowSave()
    store.put_guard(doc)
    gcloud.on_change = lambda verb: store.go.set()  # the guard saves once the operator's gcloud change is made
    flood = Flood(gcloud, (10, 100))
    store.thread = threading.Thread(target=Guard(store, monitoring=flood, invoker=flood).maybe_run, args=(NOW,))
    store.thread.start()
    assert store.saving.wait(5)
    return store, flood


def finished(store):
    store.thread.join(5)
    assert not store.thread.is_alive()


def test_a_close_survives_a_guard_run_that_saves_over_it():
    gcloud = Gcloud(public=True)
    store, flood = overlap({"schema": 1, "mode": "enforce", "heartbeat": NOW - 200, "applied": "open"}, gcloud)
    code, plan, err, store, _ = run(["close", "--for", "2h", "--force", *WHERE], store, gcloud=gcloud,
                                    sleep=Sleeps(then=lambda: finished(store)))  # fmt: skip
    doc = store.get_guard()
    assert code == 0 and plan["verified"] is True and plan["attempts"] == 2 and "overwrote" in err
    assert doc["closed_until"] == NOW + 7200 and doc["reason"] == "manual" and doc["applied"] == "closed"
    assert doc["heartbeat"] == NOW and gcloud.public is False  # the guard's run is kept, and the API closed
    assert Guard(store, monitoring=flood, invoker=flood).run(NOW + 600).closed  # and the guard keeps the closure


def test_an_open_survives_a_guard_run_that_saves_over_it():
    gcloud = Gcloud(public=False)
    store, flood = overlap(closure("manual", NOW + 3600) | {"heartbeat": NOW - 200}, gcloud)
    code, plan, err, store, _ = run(["open", "--force", *WHERE], store, gcloud=gcloud,
                                    sleep=Sleeps(then=lambda: finished(store)))  # fmt: skip
    doc = store.get_guard()
    assert code == 0 and plan["verified"] is True and plan["attempts"] == 2
    assert doc["closed_until"] is None and doc["applied"] == "open" and gcloud.public is True
    run_ = Guard(store, monitoring=flood, invoker=flood).run(NOW + 600)
    assert not run_.closed and gcloud.public  # the guard leaves it open


# ---------------------------------------------------------------- budget messages


@pytest.mark.parametrize("percent,over", [(100, True), (120, True), (90, False), (40, False)])
def test_budget_message_is_printed_never_published(percent, over):
    gcloud = Gcloud()
    code, report, err, _, _ = run(["budget-message", "--percent", percent, "--push-body", "--project", "demo-tifa"],
                                  gcloud=gcloud)  # fmt: skip
    assert code == 0 and report["published"] is False and gcloud.calls == [] and "nothing was published" in err
    assert report["command"].startswith("gcloud pubsub topics publish tifa-budget-alerts --project=demo-tifa")
    notice = parse_notification(parse_push(json.dumps(report["push_body"]).encode()))
    assert notice.over is over and notice.currency == "GBP" and notice.budget == 3.75
    assert notice.period_end(NOW) == NEXT_MONTH
    assert report["push_body"]["message"]["attributes"]["billingAccountId"] == "BILLING_ACCOUNT_ID"


def test_budget_message_thresholds_and_checks():
    msg = gc.budget_message(95, 3.75, "GBP", "tifa", NOW)
    assert msg["alertThresholdExceeded"] == 0.9 and msg["costAmount"] == 3.56
    assert "alertThresholdExceeded" not in gc.budget_message(10, 3.75, "GBP", "tifa", NOW)
    assert run(["budget-message", "--currency", "pounds"])[0] == gc.EXIT_USAGE


def test_parse_duration():
    assert [gc.parse_duration(t) for t in ("90s", "30m", "2h", "1d", "1h30m")] == [90, 1800, 7200, 86400, 5400]
    with pytest.raises(ValueError):
        gc.parse_duration("2 weeks")
