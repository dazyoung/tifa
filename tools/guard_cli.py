"""Operator controls for the fair-use guard: see its state, close or reopen the public API, rehearse a budget alert.

    uv run python tools/guard_cli.py show
    uv run python tools/guard_cli.py close [--for 2h | --until TIME] [--reason TEXT] [--shorten] [--force]
    uv run python tools/guard_cli.py open [--force]
    uv run python tools/guard_cli.py budget-message [--percent 100] [--budget 3.75] [--currency GBP] [--push-body]

show, close and open read and write guard/state in Firestore (--project or GOOGLE_CLOUD_PROJECT, --database or
TIFA_FIRESTORE_DATABASE; the client honours FIRESTORE_EMULATOR_HOST). TIFA_STORE is ignored, since the guard's
state lives only in Firestore. --store memory gives an empty store that keeps nothing, for trying the commands
out; --force is refused with it, because the IAM change would be real while the state it records is thrown away.

show prints guard/state and a summary. Its "closed" follows /healthz: a closure that has not ended and that has
been applied (allUsers removed). A dry run's own closure, or one the guard failed to apply, is "decided" but not
"closed".

close and open change two things: guard/state, so the guard's next run keeps the operator's decision, and the API
service's IAM policy (allUsers as roles/run.invoker), through gcloud with the operator's own credentials. gcloud
reads the policy first, so closing an API that is already closed, or opening one that is open, changes nothing
there. Without --force both only print what they would write and the commands, and change nothing.

- close: a closure with reason "manual" until --until, --for from now, or by default the 1st of next month (UTC).
  A real closure already in place that lasts at least as long (a budget or month-to-date cost closure, or an
  earlier manual one) is kept as it is unless --shorten is given; the request is still recorded in the trips. A
  dry run's own closures and doublings are dropped, as the guard does when it enforces.
- open: clears whatever closure guard/state holds.

What the guard does next depends on its mode (TIFA_GUARD_MODE on the poller; show prints the stored one):

- enforce: it keeps a manual closure (removing allUsers again if someone adds it back) and reopens the API when
  the closure ends. After a manual open (or a --shorten) it closes the API again while a limit is still exceeded:
  on its next run for a month-to-date cost estimate over the limit or traffic over a request limit, and at the
  next budget notification at 100% of actual spend for a budget closure.
- dry-run: it reopens the API when a manual closure ends, but does not keep it (allUsers added back by hand
  stays), and never closes the API by itself.
- off: it reopens the API when a manual closure ends only if the poller has its project and region; otherwise run
  `open --force` yourself. It never closes the API by itself.

guard/state has no transactions. A guard run reads it again after its Cloud Monitoring and IAM calls and decides
again on what an operator wrote meanwhile, but a write landing between that read and its save is still lost. With
--force the tool therefore waits --settle seconds (default 30, longer than a guard run may take) after writing,
reads guard/state again, and if its change is gone writes it again and reruns gcloud, up to 3 times. Recording is
never affected: the poller is a separate service.

budget-message never publishes anything. It prints a synthetic budget notification, the gcloud command that would
publish it to the budget topic, and with --push-body the Pub/Sub push body the poller's /internal/budget receives,
for a local rehearsal. At 100% or more the poller closes the public API until the 1st of next month.

Project and region come from --project and --region, or GOOGLE_CLOUD_PROJECT and TIFA_REGION; the service from
--service or TIFA_API_SERVICE (default tifa-api). The service must be a Tifa one, named "tifa-...": the project
may hold other services, which this tool never touches. Printed commands use placeholders for what is not set.

Exit codes: 0 done (or printed), 1 a write or gcloud failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):  # run as a script: make the repository root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tifa.config import DEFAULT_API_SERVICE, ConfigError
from tifa.core.timeutil import epoch, iso, month_index, month_start
from tifa.poller.guard import MODES, TRIPS_KEPT, is_dry_closure, is_public, normalise, real_state
from tifa.store.base import Store
from tools.import_jsonl import add_store_args, open_store, write_json

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
TOPIC = "tifa-budget-alerts"
BUDGET_NAME = "tifa"
THRESHOLDS = (0.5, 0.9, 1.0)
INVOKER = ("--member=allUsers", "--role=roles/run.invoker")
SERVICE_PREFIX = "tifa-"
SETTLE = 30  # seconds: longer than a guard run takes from reading guard/state to writing it (about 12 s at most)
ATTEMPTS = 3  # writes of guard/state before giving up on an overlapping guard
_SERVICE = re.compile(r"tifa-[a-z0-9](?:[a-z0-9-]{0,42}[a-z0-9])?")
_NOT_BOUND = re.compile(r"binding.*not found", re.IGNORECASE | re.DOTALL)  # gcloud's IamPolicyBindingNotFound
_DURATION = re.compile(r"(\d+)\s*([smhd])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
Runner = Callable[..., subprocess.CompletedProcess]


def parse_duration(text: str) -> int:
    """ "90s", "30m", "2h", "1d" or a combination such as "1h30m" -> seconds."""
    text = text.strip().lower()
    parts = _DURATION.findall(text)
    if not parts or _DURATION.sub("", text).strip():
        raise ValueError(f"not a duration: {text!r} (use for example 30m, 2h or 1d)")
    return sum(int(n) * _UNITS[unit] for n, unit in parts)


def parse_time(text: str) -> int:
    text = text.strip()
    return int(text) if text.isdigit() else epoch(text.replace("Z", "+00:00"))


def is_tifa_service(name: str) -> bool:
    return _SERVICE.fullmatch(name) is not None


def iam_command(action: str, service: str, project: str, region: str) -> list[str]:
    verb = "remove-iam-policy-binding" if action == "close" else "add-iam-policy-binding"
    return ["gcloud", "run", "services", verb, service, f"--project={project}", f"--region={region}", *INVOKER]


def policy_command(service: str, project: str, region: str) -> list[str]:
    return [
        "gcloud", "run", "services", "get-iam-policy", service, f"--project={project}", f"--region={region}",
        "--format=json",
    ]  # fmt: skip


def stored_mode(doc: Mapping[str, Any] | None) -> str | None:
    """The guard's mode as it last saved it, or None if it has not (the guard writes it on every run)."""
    mode = doc.get("mode") if isinstance(doc, Mapping) else None
    return mode if mode in MODES else None


def summary(doc: dict[str, Any] | None, now: int) -> dict[str, Any]:
    """What guard/state says: closed or not (as /healthz decides), until when and why, and the guard's mode."""
    if not doc:
        return {
            "closed": False,
            "decided": False,
            "note": "no guard state yet: the guard has not run, and the API's IAM policy alone decides who may call",
        }
    d = normalise(doc)
    until = d["closed_until"]
    decided = until is not None and until > now
    dry = is_dry_closure(d)
    mode = stored_mode(doc)
    out: dict[str, Any] = {
        "closed": decided and d["applied"] == "closed",
        "decided": decided,
        "dry_run_closure": dry,
        "closed_until": iso(until) if until is not None else None,
        "reason": d["reason"],
        "applied": d["applied"],
        "mode": mode,
        "strikes": d["strikes"],
        "heartbeat": iso(d["heartbeat"]) if d["heartbeat"] is not None else None,
        "heartbeat_age_s": now - d["heartbeat"] if d["heartbeat"] is not None else None,
        "last_trips": [
            {k: iso(v) if k in ("at", "until") and isinstance(v, int) else v for k, v in t.items()}
            for t in d["trips"][-5:]
        ],
        "error": d["error"],
    }
    notes = []
    if decided and dry:
        notes.append("the closure is a dry run's: decided and recorded, never applied")
    elif decided and d["applied"] != "closed":
        notes.append("a closure is decided but not applied: allUsers was not removed (see error)")
    if not decided and d["applied"] == "closed":
        notes.append(
            "the last closure has ended but allUsers is not back yet: the guard adds it on its next run (when off,"
            " only if the poller has its project and region); otherwise run open --force"
        )
    if mode is None:
        notes.append("the guard has not saved its mode yet")
    elif mode == "dry-run":
        notes.append("the guard runs in dry-run mode: it records its own closures but never applies them")
    elif mode == "off":
        notes.append("the guard is off: it decides nothing and never closes the API")
    if notes:
        out["note"] = "; ".join(notes)
    return out


def _base(doc: dict[str, Any] | None) -> dict[str, Any]:
    """guard/state as the operator's change starts from: the real state (a dry run's own closure and doublings
    dropped, as the guard drops them when it enforces), with the mode the guard last saved (None before its first
    run; the guard sets it on every run)."""
    d = real_state(doc)
    d["mode"] = stored_mode(doc)
    return d


def _active(d: Mapping[str, Any], now: int) -> int | None:
    until = d.get("closed_until")
    return until if isinstance(until, int) and until > now else None


def closed_doc(
    doc: dict[str, Any] | None, now: int, until: int, note: str | None, *, shorten: bool = False
) -> tuple[dict[str, Any], str]:
    """(guard/state after a manual close, "set" | "kept" | "shortened"). The closure becomes reason "manual" until
    `until`, which the guard ends by itself, except that a real closure in place that lasts at least as long is
    kept as it is ("kept") unless `shorten`. Either way the request is recorded as a trip."""
    d = _base(doc)
    current = _active(d, now)
    trip: dict[str, Any] = {"at": now, "reason": "manual", "until": until}
    if note:
        trip["note"] = note[:200]
    if current is not None and current >= until and not shorten:
        outcome = "kept"
        d["applied"] = "closed"
    else:
        outcome = "shortened" if current is not None and current > until else "set"
        d.update(closed_until=until, reason="manual", applied="closed")
    d["trips"] = (d["trips"] + [trip])[-TRIPS_KEPT:]
    return d, outcome


def opened_doc(doc: dict[str, Any] | None, now: int) -> dict[str, Any]:
    """guard/state after a manual open: no closure, so the guard leaves the binding alone until a new trip."""
    d = _base(doc)
    d.update(closed_until=None, reason=None, applied="open")
    d["trips"] = (d["trips"] + [{"at": now, "reason": "manual_open", "until": None}])[-TRIPS_KEPT:]
    return d


def has_trip(doc: Mapping[str, Any] | None, trip: Mapping[str, Any]) -> bool:
    """Whether guard/state still records this tool's trip. A guard run whose save follows its last read of
    guard/state but precedes the tool's write puts back a document without it; the guard's later runs keep it."""
    return any(t.get("at") == trip["at"] and t.get("reason") == trip["reason"] for t in normalise(doc)["trips"])


def warnings(action: str, doc: dict[str, Any] | None, now: int, outcome: str, until: int | None) -> list[str]:
    """What the operator should know about how the guard will treat this close or open."""
    d = real_state(doc)
    mode = stored_mode(doc)
    active = _active(d, now) is not None
    out = []
    if mode is None:
        out.append(
            "the guard has not run yet, so its mode is unknown: only in enforce mode does it keep a manual closure"
            " (removing allUsers again if someone adds it back)"
        )
    elif mode == "dry-run" and action == "close":
        out.append(
            "the guard runs in dry-run mode: it reopens the API when this closure ends, but does not keep it closed"
            " if allUsers is added back"
        )
    elif mode == "off" and action == "close":
        out.append(
            "the guard is off: it reopens the API when this closure ends only if the poller has its project and"
            " region; otherwise run `guard_cli open --force` then"
        )
    elif mode != "enforce":
        out.append(f"the guard runs in {mode} mode: it will not close the API again by itself")
    if outcome == "kept" and until is not None:
        out.append(
            f"a closure that lasts at least as long ({d['reason']}, until {iso(d['closed_until'])}) is in place and is"
            f" kept; pass --shorten to end it at {iso(until)} instead"
        )
    if active and mode in (None, "enforce") and (action == "open" or outcome == "shortened"):
        reason = d["reason"]
        if reason == "month_cost":
            out.append(
                "that closure was for the month-to-date cost estimate: the guard closes the API again on its next run"
                " while the estimate is over the limit (TIFA_GUARD_MONTH_USD)"
            )
        elif reason == "budget":
            out.append(
                "that closure was for the budget: the next budget notification at 100% of actual spend closes the API"
                " again"
            )
        elif reason in ("requests_5min", "requests_1h"):
            out.append("that closure was for traffic: the guard closes the API again if a request limit is still over")
    return out


def _brief(text: str | None) -> str:
    return (text or "").strip()[:500]


def read_public(run: Runner, service: str, project: str, region: str) -> tuple[bool | None, str | None]:
    """(whether allUsers may invoke the service now, None), or (None, why) if gcloud could not read the policy."""
    try:
        done = run(policy_command(service, project, region), check=False, capture_output=True, text=True)
    except OSError as exc:
        return None, f"could not run gcloud ({type(exc).__name__})"
    if done.returncode != 0:
        return None, f"get-iam-policy failed ({done.returncode}): {_brief(done.stderr)}"
    try:
        policy = json.loads(done.stdout or "{}")
    except ValueError:
        return None, "get-iam-policy did not print JSON"
    if not isinstance(policy, dict):
        return None, "get-iam-policy did not print a policy"
    return is_public(policy), None


def apply_iam(
    action: str, run: Runner, service: str, project: str, region: str, err: TextIO
) -> tuple[str | None, str | None]:
    """Make allUsers' invoker binding match the action: ("changed" | "unchanged", None), or (None, what failed)."""
    public, why = read_public(run, service, project, region)
    if public is not None and public == (action == "open"):
        return "unchanged", None
    if why:
        print(f"guard_cli: could not read the policy first ({why}); changing it anyway", file=err)
    try:
        done = run(iam_command(action, service, project, region), check=False, capture_output=True, text=True)
    except OSError as exc:
        return None, f"could not run gcloud ({type(exc).__name__})"
    if done.returncode == 0:
        return "changed", None
    if action == "close" and _NOT_BOUND.search(done.stderr or ""):
        return "unchanged", None  # allUsers was not bound: the API is already closed
    return None, f"gcloud failed ({done.returncode}): {_brief(done.stderr)}"


def budget_message(percent: float, budget: float, currency: str, name: str, now: int) -> dict[str, Any]:
    """A budget notification in Cloud Billing's format, as if spend had reached `percent` of the budget."""
    t = time.gmtime(now)
    msg: dict[str, Any] = {
        "budgetDisplayName": name,
        "costAmount": round(budget * percent / 100, 2),
        "costIntervalStart": iso(calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0))).replace("+00:00", "Z"),
        "budgetAmount": budget,
        "budgetAmountType": "SPECIFIED_AMOUNT",
        "currencyCode": currency,
    }
    crossed = [x for x in THRESHOLDS if x <= percent / 100]
    if crossed:
        msg["alertThresholdExceeded"] = crossed[-1]
    return msg


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="guard_cli",
        description="See the guard's state, close or reopen the public API, rehearse a budget alert.",
        epilog="Exit codes: 0 done or printed, 1 a write or gcloud failed, 2 bad usage.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        add_store_args(  # --store, --project, --database
            p, store_help="firestore (the default: guard/state lives there) or memory (empty; refused with --force)"
        )
        p.add_argument("--region", help="Cloud Run region (default: $TIFA_REGION)")
        p.add_argument(
            "--service",
            help=f"the public API service, named tifa-... (default: $TIFA_API_SERVICE, else {DEFAULT_API_SERVICE})",
        )

    def forcing(p: argparse.ArgumentParser) -> None:
        p.add_argument("--force", action="store_true", help="write guard/state and run gcloud")
        p.add_argument(
            "--settle",
            type=int,
            default=SETTLE,
            help=f"seconds to wait before checking that no guard run overwrote the change (default {SETTLE}; 0 skips"
            " the check)",
        )

    show = sub.add_parser("show", help="print guard/state")
    common(show)

    close = sub.add_parser("close", help="close the public API (prints the plan unless --force)")
    when = close.add_mutually_exclusive_group()
    when.add_argument(
        "--for", dest="duration", help="how long, e.g. 30m, 2h, 1d (default: until the 1st of next month)"
    )
    when.add_argument("--until", help="until when, ISO 8601 or epoch seconds")
    close.add_argument("--reason", help="a short note kept in guard/state")
    close.add_argument(
        "--shorten", action="store_true", help="replace a closure in place even if it lasts longer than this one"
    )
    forcing(close)
    common(close)

    reopen = sub.add_parser("open", help="reopen the public API (prints the plan unless --force)")
    forcing(reopen)
    common(reopen)

    budget = sub.add_parser("budget-message", help="print a synthetic budget alert and how to publish it")
    budget.add_argument("--percent", type=float, default=100.0, help="spend as a percentage of the budget (100)")
    budget.add_argument("--budget", type=float, default=3.75, help="the budget amount (default 3.75)")
    budget.add_argument("--currency", default="GBP", help="the billing account's currency (default GBP)")
    budget.add_argument("--name", default=BUDGET_NAME, help=f"the budget's display name (default {BUDGET_NAME})")
    budget.add_argument("--topic", default=TOPIC, help=f"the budget's Pub/Sub topic (default {TOPIC})")
    budget.add_argument("--push-body", action="store_true", help="also print the push body /internal/budget gets")
    budget.add_argument(
        "--project", help="Google Cloud project ID for the printed command (default: $GOOGLE_CLOUD_PROJECT)"
    )
    return ap


def _budget(args: argparse.Namespace, project: str | None, now: int, out: TextIO, err: TextIO) -> int:
    if args.percent < 0 or args.budget <= 0 or not re.fullmatch(r"[A-Z]{3}", args.currency):
        print("guard_cli: --percent >= 0, --budget > 0 and a three-letter --currency please", file=err)
        return EXIT_USAGE
    msg = budget_message(args.percent, args.budget, args.currency, args.name, now)
    data = json.dumps(msg, separators=(",", ":"))
    attrs = "billingAccountId=BILLING_ACCOUNT_ID,budgetId=BUDGET_ID,schemaVersion=1.0"
    cmd = ["gcloud", "pubsub", "topics", "publish", args.topic, f"--project={project or 'PROJECT_ID'}"]
    cmd += [f"--message={data}", f"--attribute={attrs}"]
    report: dict[str, Any] = {"published": False, "message": msg, "command": shlex.join(cmd)}
    if args.push_body:
        report["push_body"] = {
            "message": {
                "data": base64.b64encode(data.encode()).decode(),
                "attributes": dict(a.split("=", 1) for a in attrs.split(",")),
                "messageId": "1",
                "publishTime": iso(now).replace("+00:00", "Z"),
            },
            "subscription": "projects/PROJECT_ID/subscriptions/tifa-budget-push",
        }
    write_json(report, out)
    print("guard_cli: nothing was published; run the command yourself to send it", file=err)
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    store: Store | None = None,
    run: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    now: int | None = None,
    env: dict[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    env = dict(os.environ if env is None else env)
    args = build_parser().parse_args(argv)
    now = int(time.time()) if now is None else now
    project = args.project or env.get("GOOGLE_CLOUD_PROJECT")
    if args.command == "budget-message":
        return _budget(args, project, now, out, err)

    region = args.region or env.get("TIFA_REGION")
    service = args.service or env.get("TIFA_API_SERVICE") or DEFAULT_API_SERVICE
    force = getattr(args, "force", False)
    if args.command != "show":
        if not is_tifa_service(service):
            print(
                f"guard_cli: {service!r} is not a Tifa service (named {SERVICE_PREFIX}...); this tool never changes"
                " other services in the project",
                file=err,
            )
            return EXIT_USAGE
        if args.settle < 0:
            print("guard_cli: --settle must not be negative", file=err)
            return EXIT_USAGE
    if force and (not project or not region):
        print("guard_cli: --force needs --project and --region (or GOOGLE_CLOUD_PROJECT and TIFA_REGION)", file=err)
        return EXIT_USAGE

    if store is None:
        args.store = args.store or "firestore"  # never TIFA_STORE: guard/state lives only in Firestore
        if args.store == "memory" and force:
            print(
                "guard_cli: --force is refused with --store memory: the IAM change would be real while guard/state"
                " is thrown away, so the guard would undo it",
                file=err,
            )
            return EXIT_USAGE
        try:
            store, kind = open_store(args, env)
        except ConfigError as exc:
            print(f"guard_cli: {exc}", file=err)
            return EXIT_USAGE
        if kind == "memory":
            print("guard_cli: --store memory is an empty store that keeps nothing; this is a rehearsal", file=err)
    try:
        doc = store.get_guard()
    except Exception as exc:
        print(f"guard_cli: could not read guard/state: {type(exc).__name__}: {exc}", file=err)
        return EXIT_FAILED

    if args.command == "show":
        write_json({"guard": doc, "summary": summary(doc, now)}, out)
        return EXIT_OK

    until: int | None = None
    if args.command == "close":
        until = month_start(month_index(now) + 1)
        try:
            if args.duration:
                until = now + parse_duration(args.duration)
            elif args.until:
                until = parse_time(args.until)
        except ValueError as exc:
            print(f"guard_cli: {exc}", file=err)
            return EXIT_USAGE
        if until <= now:
            print("guard_cli: the closure would already be over", file=err)
            return EXIT_USAGE

    def build(current: dict[str, Any] | None) -> tuple[dict[str, Any], str]:
        if until is not None:
            return closed_doc(current, now, until, args.reason, shorten=args.shorten)
        return opened_doc(current, now), "set"

    new, outcome = build(doc)
    trip = new["trips"][-1]
    for line in warnings(args.command, doc, now, outcome, until):
        print(f"guard_cli: {line}", file=err)
    where = (service, project or "PROJECT_ID", region or "REGION")
    plan: dict[str, Any] = {
        "action": args.command,
        "guard_state": new,
        "check": shlex.join(policy_command(*where)),
        "command": shlex.join(iam_command(args.command, *where)),
        "applied": False,
    }
    if outcome != "set":
        plan["closure"] = outcome
    if not force:
        write_json(plan, out)
        print("guard_cli: nothing changed; pass --force to write guard/state and run the command", file=err)
        return EXIT_OK
    assert project is not None and region is not None

    for attempt in range(1, ATTEMPTS + 1):
        plan["attempts"] = attempt
        try:
            store.put_guard(new)
        except Exception as exc:
            write_json(plan, out)
            print(f"guard_cli: could not write guard/state: {type(exc).__name__}: {exc}", file=err)
            return EXIT_FAILED
        iam, problem = apply_iam(args.command, run, service, project, region, err)
        if problem is not None:
            plan["applied"] = False
            write_json(plan, out)
            print(f"guard_cli: {problem}", file=err)
            print("guard_cli: guard/state is written; fix the cause and rerun with --force", file=err)
            return EXIT_FAILED
        plan["applied"], plan["iam"] = True, iam
        if args.settle == 0:
            plan["verified"] = None
            break
        print(f"guard_cli: waiting {args.settle} s to check that no overlapping guard run put back its copy", file=err)
        sleep(args.settle)
        try:
            current = store.get_guard()
        except Exception as exc:
            write_json(plan, out)
            print(f"guard_cli: could not read guard/state back: {type(exc).__name__}: {exc}; run show", file=err)
            return EXIT_FAILED
        if has_trip(current, trip):
            plan["verified"] = True
            break
        print(f"guard_cli: an overlapping guard run overwrote guard/state (attempt {attempt})", file=err)
        if attempt < ATTEMPTS:
            new, outcome = build(current)  # the operator's decision again, on top of what the guard wrote
            plan["guard_state"] = new
            if outcome != "set":
                plan["closure"] = outcome
    else:
        plan["verified"] = False
        write_json(plan, out)
        print(f"guard_cli: guard/state still lost the change after {ATTEMPTS} attempts; check it with show", file=err)
        return EXIT_FAILED
    write_json(plan, out)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
