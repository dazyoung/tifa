"""Import a local recording of TfL line-status disruptions into a Tifa store, so recording can carry on in Tifa.

    uv run python tools/import_jsonl.py RECORDING_DIR --store firestore [--status STATUS_JSON] [--dry-run]

RECORDING_DIR holds what a local recorder writes after each good poll:

  episodes.jsonl   one closed episode per line: {"line", "name", "mode", "severity", "status", "category",
                   "planned", "start", "end", "end_estimated", "reason"}, times in ISO 8601
  polls.jsonl      one good poll per line: {"at": ISO 8601}
  open.json        {"version": 1, "last_poll": ISO 8601, "open": [open episodes, "start" in ISO 8601]}

STATUS_JSON, optionally, is the current status at the last poll: current()'s {"lines", "disrupted", "total"}, the
same wrapped as {"ok": true, "fetched_at": ..., "data": {...}}, or TfL's raw line-status response.

What it does:

1. Reads and checks the files, open.json first (a recorder writes it last after each poll, so whatever a poll
   during the import adds to the other two files is after its last poll). Lines cut short (a crash mid-write) and
   repeats of an episode are skipped and counted. Polls and closed episodes from after open.json's last poll are
   left out: a closed episode that ends after it, or that open.json still holds as open (a crash between
   appending to episodes.jsonl and saving open.json), is imported as open, as the poller carries it on.
2. Refuses (exit 3) if the store already holds a recording, unless --force; if the last poll is older than
   --max-age seconds (default 300): Tifa's first poll must follow the last imported poll within 15 minutes (GAP)
   for the open episodes to carry on with their start times; or if the store's state is at or after the import's
   last poll and differs from it (a store's state never moves back).
3. Writes, in this order: the poll times into polls/{UTC day}; the closed episodes (source "a", each with its cause)
   and the watched runs into months/{YYYY-MM}, merged with what the months already hold; meta/state (the open
   episodes, last poll and slot, the current watched run, the lines seen, the status); and imports/{id}.
   Polls go in one write per day when the store offers add_polls(day, times), else one transaction each (slow
   on Firestore for a long recording: --polls-days N keeps only the last N days' poll documents; the watched
   runs in the months always come from every poll).

A rerun of the same recording with --force changes nothing: months that already hold its episodes and runs keep
their revisions, poll times already stored are not added again, and an identical state is left as it is.
--dry-run does every read and check and reports what would be written, without writing anything.

The store must be named: --store firestore (or TIFA_STORE=firestore), with --project or GOOGLE_CLOUD_PROJECT and
--database or TIFA_FIRESTORE_DATABASE; the Firestore client honours FIRESTORE_EMULATOR_HOST. --store memory is an
empty store that keeps nothing after the run, for rehearsals; it is never picked by default. Pause the poller's
schedule before importing and resume it straight after.

Exit codes: 0 imported (or a dry run that would import), 1 bad input or a failed write, 2 bad usage, 3 refused.
Prints a JSON report on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from tifa.config import STORES, ConfigError, load_settings
from tifa.core.causes import CAUSES_SIG
from tifa.core.episodes import GAP, clean_open
from tifa.core.months import SOURCE_LIVE, merge_month, new_month, runs_of, split_months
from tifa.core.tfl import CATEGORY, CATEGORY_IDS, current, validate_payload
from tifa.core.timeutil import epoch, iso, month_keys, utc_day
from tifa.store import codec
from tifa.store.base import DUPLICATE, PollWrite, State, Store

EPISODES = "episodes.jsonl"
POLLS = "polls.jsonl"
OPEN = "open.json"
MAX_FILE = 256 * 2**20  # bytes read from any one input file
MAX_AGE = 300  # seconds: the newest last poll an import accepts by default
FUTURE_SLACK = 120  # seconds a last poll may be ahead of this machine's clock
STATUS_SKEW = 300  # seconds between the status file's fetch time and the last poll before the report notes it
SLOW_POLLS = 2000  # polls written one transaction each (no add_polls) above which the import warns

EXIT_OK, EXIT_INPUT, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3
STORE_ENV = ("TIFA_STORE", "GOOGLE_CLOUD_PROJECT", "TIFA_FIRESTORE_DATABASE", "FIRESTORE_EMULATOR_HOST")
COMPARED = ("last_poll", "last_slot", "run", "open", "lines", "status")  # the state fields an import sets


class BadInput(Exception):
    """An input file is missing, unreadable or not in the expected format."""


class Refused(Exception):
    """The import would be unsafe; nothing was written."""


# ---------------------------------------------------------------- store access (shared by the tools)


STORE_HELP = "firestore, or memory for a rehearsal that keeps nothing (default: $TIFA_STORE if it is firestore)"
NO_STORE = (
    "name the store: --store firestore (or TIFA_STORE=firestore) for the real one, or --store memory for a rehearsal"
    " that keeps nothing"
)


def add_store_args(ap: argparse.ArgumentParser, store_help: str = STORE_HELP) -> None:
    g = ap.add_argument_group("store")
    g.add_argument("--store", choices=STORES, help=store_help)
    g.add_argument("--project", help="Google Cloud project ID (default: $GOOGLE_CLOUD_PROJECT)")
    g.add_argument("--database", help="Firestore database ID (default: $TIFA_FIRESTORE_DATABASE, else (default))")


def open_store(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> tuple[Store, str]:
    """The store the arguments and environment name, and its kind. The memory store keeps nothing after the run,
    so it is used only when --store memory says so, never by default or from TIFA_STORE alone (a tool would seem
    to import or record what is thrown away). Raises ConfigError for bad or missing settings."""
    env = os.environ if env is None else env
    picked = {k: env[k] for k in STORE_ENV if env.get(k)}
    flags = (("store", "TIFA_STORE"), ("project", "GOOGLE_CLOUD_PROJECT"), ("database", "TIFA_FIRESTORE_DATABASE"))
    for name, key in flags:
        value = getattr(args, name, None)
        if value:
            picked[key] = value
    settings = load_settings(picked)
    if settings.store == "memory":
        if getattr(args, "store", None) != "memory":
            raise ConfigError(NO_STORE)
        from tifa.store.memory import MemoryStore

        return MemoryStore(), "memory"
    from tifa.store.firestore import FirestoreStore

    return FirestoreStore(project=settings.project, database=settings.firestore_database), "firestore"


def has_recording(state: State | None) -> bool:
    """Whether a state holds a recording (a poll or open episodes), not just the month index an archive import
    leaves."""
    return state is not None and (state.last_poll is not None or state.last_slot is not None or bool(state.open))


def write_json(report: Mapping[str, Any], out: TextIO) -> None:
    out.write(json.dumps(report, indent=1, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- reading a recording


@dataclass
class Recording:
    closed: list[dict[str, Any]]  # closed episodes, epoch times, by (end, start, line, severity)
    open: list[dict[str, Any]]  # open episodes (OPEN_FIELDS), epoch starts
    polls: list[int]  # good poll times, ascending, none after last_poll
    last_poll: int
    status: dict[str, Any] | None = None  # current() at the last poll, if a status file was given
    status_fetched_at: int | None = None
    skipped: dict[str, int] = field(default_factory=dict)
    sha256: dict[str, str] = field(default_factory=dict)  # file name -> sha256 of its bytes

    @property
    def first_poll(self) -> int:
        return self.polls[0]

    def runs(self) -> list[list[int]]:
        return runs_of(self.polls)


def _read(path: Path) -> bytes:
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE + 1)
    except FileNotFoundError:
        raise BadInput(f"{path.name} is missing") from None
    except OSError as err:
        raise BadInput(f"{path.name} could not be read ({type(err).__name__})") from None
    if len(data) > MAX_FILE:
        raise BadInput(f"{path.name} is over {MAX_FILE // 2**20} MB")
    return data


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _time(value: Any) -> int | None:
    """ISO 8601 (a time without an offset is UTC) or epoch seconds -> epoch seconds; None if neither."""
    if isinstance(value, str):
        try:
            return epoch(value)
        except ValueError:
            return None
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return int(value)
    return None


def _planned(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if _is_int(value) and value in (0, 1):
        return bool(value)
    return None


def parse_episode(r: Any) -> dict[str, Any] | None:
    """An episodes.jsonl record as a ClosedEpisode with epoch times, or None if it cannot be one."""
    if not isinstance(r, dict):
        return None
    line, sev, planned = r.get("line"), r.get("severity"), _planned(r.get("planned"))
    start, end = _time(r.get("start")), _time(r.get("end"))
    if not (isinstance(line, str) and line) or not _is_int(sev) or planned is None:
        return None
    if start is None or end is None or end < start:
        return None
    name, mode, status, cat, reason = (r.get(k) for k in ("name", "mode", "status", "category", "reason"))
    return {
        "line": line,
        "name": name if isinstance(name, str) and name else line,
        "mode": mode if isinstance(mode, str) else None,
        "severity": sev,
        "status": status if isinstance(status, str) else None,
        "category": cat if cat in CATEGORY_IDS else CATEGORY(sev),
        "planned": planned,
        "start": start,
        "end": end,
        "end_estimated": r.get("end_estimated") is True,
        "reason": reason if isinstance(reason, str) and reason else None,
    }


def _jsonl(data: bytes) -> Iterable[Any]:
    """Each line's JSON value; a line that does not parse gives the marker _BAD."""
    for text in data.decode("utf-8", errors="replace").splitlines():
        if not text.strip():
            continue
        try:
            yield json.loads(text)
        except ValueError:
            yield _BAD


_BAD = object()


def read_episodes(data: bytes, skipped: dict[str, int]) -> list[dict[str, Any]]:
    """Closed episodes from episodes.jsonl, skipping unreadable lines and repeats of an episode already read (a
    crash between appending an episode and saving open.json repeats it)."""
    out, seen = [], set()
    for r in _jsonl(data):
        ep = parse_episode(r) if r is not _BAD else None
        if ep is None:
            skipped["episodes_unreadable"] = skipped.get("episodes_unreadable", 0) + 1
            continue
        key = (ep["line"], ep["severity"], ep["planned"], ep["start"])
        if key in seen:
            skipped["episodes_repeated"] = skipped.get("episodes_repeated", 0) + 1
            continue
        seen.add(key)
        out.append(ep)
    out.sort(key=lambda e: (e["end"], e["start"], e["line"], e["severity"], e["planned"]))
    return out


def read_polls(data: bytes, skipped: dict[str, int]) -> list[int]:
    times = set()
    for r in _jsonl(data):
        ts = _time(r.get("at")) if isinstance(r, dict) else None
        if ts is None:
            skipped["polls_unreadable"] = skipped.get("polls_unreadable", 0) + 1
            continue
        times.add(ts)
    return sorted(times)


def read_open(data: bytes, skipped: dict[str, int]) -> tuple[int, list[dict[str, Any]]]:
    try:
        doc = json.loads(data)
    except ValueError:
        raise BadInput(f"{OPEN} is not JSON") from None
    if not isinstance(doc, dict):
        raise BadInput(f"{OPEN} is not a JSON object")
    last = _time(doc.get("last_poll"))
    if last is None:
        raise BadInput(f"{OPEN} has no last_poll")
    entries = doc.get("open")
    if not isinstance(entries, list):
        raise BadInput(f"{OPEN} has no open list")
    staged = []
    for ep in entries:
        if isinstance(ep, dict):
            ep = dict(ep, start=_time(ep.get("start")))
        staged.append(ep)
    kept, dropped = clean_open(staged)
    later = [ep for ep in kept if ep["start"] > last]
    if dropped or later:
        skipped["open_unreadable"] = dropped + len(later)
    kept = [ep for ep in kept if ep["start"] <= last]
    kept.sort(key=lambda e: (e["start"], e["line"], e["severity"], e["planned"]))
    return last, kept


def _check_status(status: Any) -> dict[str, Any]:
    if not isinstance(status, dict) or not isinstance(status.get("lines"), list) or not status["lines"]:
        raise BadInput("the status file has no lines")
    for line in status["lines"]:
        if not isinstance(line, dict) or not isinstance(line.get("id"), str) or not line["id"]:
            raise BadInput("the status file has a line without an id")
        if not isinstance(line.get("name"), str) or not (line.get("mode") is None or isinstance(line["mode"], str)):
            raise BadInput(f"the status file's line {line['id']!r} has no name or a bad mode")
    if not _is_int(status.get("disrupted")) or not _is_int(status.get("total")):
        raise BadInput("the status file has no disrupted and total counts")
    return status


def read_status(data: bytes, name: str = "the status file") -> tuple[dict[str, Any], int | None]:
    """(current()'s status, the fetch time if the file records one) from a status file in any accepted form."""
    try:
        doc = json.loads(data)
    except ValueError:
        raise BadInput(f"{name} is not JSON") from None
    if isinstance(doc, list):  # TfL's raw response
        ok, code = validate_payload(doc)
        if not ok:
            raise BadInput(f"{name} is not a usable TfL response ({code})")
        return current(doc), None
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):  # a wrapper around current()
        if doc.get("ok") is False:
            raise BadInput(f"{name} records a failed fetch, so its status is stale; give a good one or none")
        return _check_status(doc["data"]), _time(doc.get("fetched_at"))
    return _check_status(doc), None


def read_recording(directory: Path, status_path: Path | None = None) -> Recording:
    """A recording directory's episodes, polls and open episodes, checked. Raises BadInput."""
    directory = Path(directory)
    if not directory.is_dir():
        raise BadInput(f"{directory.name or directory} is not a directory")
    skipped: dict[str, int] = {}
    # open.json first: a recorder appends to episodes.jsonl and polls.jsonl, then saves open.json, so anything a
    # poll adds while these are read comes after open.json's last poll and is left out below
    raw = {name: _read(directory / name) for name in (OPEN, EPISODES, POLLS)}
    sha = {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()}
    last, open_eps = read_open(raw[OPEN], skipped)
    closed = read_episodes(raw[EPISODES], skipped)
    polls = read_polls(raw[POLLS], skipped)
    later = [ts for ts in polls if ts > last]
    if later:
        skipped["polls_after_last_poll"] = len(later)
    polls = sorted({ts for ts in polls if ts <= last} | {last})  # open.json's last poll was a good poll
    still_open = {(ep["line"], ep["severity"], ep["planned"], ep["start"]) for ep in open_eps}
    kept = [
        ep
        for ep in closed
        if ep["end"] <= last and (ep["line"], ep["severity"], ep["planned"], ep["start"]) not in still_open
    ]
    if len(kept) < len(closed):  # closed by a poll after open.json's last poll: still open as far as it goes
        skipped["episodes_after_last_poll"] = len(closed) - len(kept)
    closed = kept
    status = fetched = None
    if status_path is not None:
        data = _read(Path(status_path))
        status, fetched = read_status(data, Path(status_path).name)
        sha[Path(status_path).name] = hashlib.sha256(data).hexdigest()
    return Recording(closed, open_eps, polls, last, status, fetched, skipped, sha)


# ---------------------------------------------------------------- building what the store gets


def build_lines(rec: Recording) -> dict[str, dict[str, Any]]:
    """{line: {name, mode, first_seen, last_seen}} from the episodes, the open episodes and the status's lines (seen at
    every poll of the recording, so from its first poll to its last). The latest sighting's name and mode win."""
    lines: dict[str, dict[str, Any]] = {}
    latest: dict[str, int] = {}

    def see(line: str, name: Any, mode: Any, first: int, last: int) -> None:
        have = lines.get(line)
        if have is None:
            have = lines[line] = {"name": line, "mode": None, "first_seen": first, "last_seen": last}
        have["first_seen"] = min(have["first_seen"], first)
        have["last_seen"] = max(have["last_seen"], last)
        if last >= latest.get(line, -1):
            latest[line] = last
            have["name"] = name if isinstance(name, str) and name else have["name"]
            have["mode"] = mode if isinstance(mode, str) else have["mode"]

    for ep in rec.closed:
        see(ep["line"], ep["name"], ep["mode"], ep["start"], ep["end"])
    for ep in rec.open:
        see(ep["line"], ep["name"], ep["mode"], ep["start"], rec.last_poll)
    for line in (rec.status or {}).get("lines") or ():
        see(line["id"], line.get("name"), line.get("mode"), rec.first_poll, rec.last_poll)
    return dict(sorted(lines.items()))


def build_state(rec: Recording, now: int) -> State:
    runs = rec.runs()
    status = rec.status or {}
    return State(
        last_poll=rec.last_poll,
        last_slot=rec.last_poll // 60,
        run=list(runs[-1]),
        open=[dict(ep) for ep in rec.open],
        lines=build_lines(rec),
        status=status,
        status_etag=codec.digest(status) if status else "",
        causes_sig=CAUSES_SIG,
        updated_at=now,
    )


def episode_months(closed: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    by_month: dict[str, list[Mapping[str, Any]]] = {}
    for ep in closed:
        for month in split_months(ep["start"], ep["end"]):
            by_month.setdefault(month, []).append(ep)
    return by_month


def plan_months(
    rec: Recording, get_month: Callable[[str], dict[str, Any] | None]
) -> dict[str, tuple[dict[str, Any], bool]]:
    """{month: (payload, changed)}: each month the recording touches, merged into what the store holds."""
    by_month = episode_months(rec.closed)
    runs = [r for r in rec.runs() if r[1] > r[0]]
    names = set(by_month)
    for a, b in runs:
        names.update(month_keys(a, b))
    out = {}
    for month in sorted(names):
        old = get_month(month)
        payload = merge_month(old or new_month(month), by_month.get(month, ()), runs, SOURCE_LIVE)
        out[month] = (payload, old is None or codec.digest(old) != codec.digest(payload))
    return out


def plan_polls(rec: Recording, get_polls: Callable[[str], Any], since: int | None = None) -> dict[str, list[int]]:
    """{UTC day: poll times the store's polls/{day} lacks}, for polls at or after since."""
    days: dict[str, list[int]] = {}
    for ts in rec.polls:
        if since is None or ts >= since:
            days.setdefault(utc_day(ts), []).append(ts)
    missing = {}
    for day, times in sorted(days.items()):
        doc = get_polls(day)
        have = set(doc.at) if doc is not None else set()
        add = [ts for ts in times if ts not in have]
        if add:
            missing[day] = add
    return missing


def same_state(a: State | None, b: State) -> bool:
    if a is None:
        return False
    da, db = a.to_dict(), b.to_dict()
    return all(codec.dumps(da.get(k)) == codec.dumps(db.get(k)) for k in COMPARED)


def write_polls(store: Store, missing: Mapping[str, list[int]]) -> str:
    """Add poll times to polls/{day}: in one write per day when the store offers add_polls(day, times), else one
    transaction per poll (slow on Firestore; fine for small imports). Returns which way it went."""
    bulk = getattr(store, "add_polls", None)
    if callable(bulk):
        for day, times in sorted(missing.items()):
            bulk(day, list(times))
        return "bulk"
    for times in missing.values():
        for ts in times:
            result = store.poll_transaction(ts // 60, lambda _state, ts=ts: PollWrite(poll_at=ts))
            if result == DUPLICATE:
                raise RuntimeError(f"the store refused the poll at {iso(ts)} as a duplicate slot")
    return "per-poll"


def import_id(rec: Recording) -> str:
    digest = hashlib.sha256("".join(f"{k}:{v};" for k, v in sorted(rec.sha256.items())).encode()).hexdigest()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(rec.last_poll))
    return f"jsonl-{stamp}-{digest[:12]}"


# ---------------------------------------------------------------- the import


def run_import(
    store: Store,
    rec: Recording,
    *,
    now: int,
    max_age: int = MAX_AGE,
    force: bool = False,
    dry_run: bool = False,
    polls_days: int = 0,
    store_kind: str = "memory",
    log: TextIO = sys.stderr,
) -> dict[str, Any]:
    """Check and import a recording; returns the report. Raises Refused before writing anything when unsafe."""
    state = build_state(rec, now)
    report: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "store": store_kind,
        "recording": {
            "episodes": len(rec.closed),
            "open": len(rec.open),
            "polls": len(rec.polls),
            "first_poll": iso(rec.first_poll),
            "last_poll": iso(rec.last_poll),
            "runs": len(rec.runs()),
            "skipped": dict(sorted(rec.skipped.items())),
            "status": rec.status is not None,
        },
    }
    if rec.status is not None and rec.status_fetched_at is not None:
        skew = rec.status_fetched_at - rec.last_poll
        report["recording"]["status_skew_s"] = skew
        if abs(skew) > STATUS_SKEW:
            print(f"import: the status was fetched {skew:+d} s from the last poll", file=log)

    age = now - rec.last_poll
    report["age_s"] = age
    if age < -FUTURE_SLACK:
        raise Refused(f"the last poll is {-age} s in the future; check this machine's clock")
    if max_age > 0 and age > max_age:
        raise Refused(
            f"the last poll is {age} s old, over --max-age {max_age} s: import right after a poll, so Tifa's first "
            f"poll follows within {GAP // 60} minutes and the open episodes carry on"
        )

    existing = store.get_state()
    unchanged_state = same_state(existing, state)
    if has_recording(existing) and not force:
        raise Refused(
            f"the store already holds a recording (last poll {iso(existing.last_poll) if existing.last_poll else '-'});"
            " pass --force to import over it"
        )
    if existing is not None and existing.last_slot is not None and existing.last_slot >= state.last_slot:
        if not unchanged_state:
            raise Refused(
                f"the store's state is at slot {existing.last_slot}, at or after the import's {state.last_slot}, and"
                " differs from it; a store's state never moves back"
            )
    since = rec.last_poll - polls_days * 86400 if polls_days > 0 else None
    missing = plan_polls(rec, store.get_polls, since)
    if missing and not callable(getattr(store, "add_polls", None)) and existing is not None:
        if existing.last_slot is not None and min(min(t) for t in missing.values()) // 60 <= existing.last_slot:
            raise Refused("this store cannot add polls before its state's last slot (it has no add_polls)")

    def get_payload(month: str) -> dict[str, Any] | None:
        doc = store.get_month(month)
        return doc.payload if doc is not None else None

    months = plan_months(rec, get_payload)
    run_from, run_to = state.run
    report["months"] = {
        m: {
            "episodes": sum(1 for row in p["episodes"] if row[6] == SOURCE_LIVE),
            "watched_s": sum(b - a for a, b in p["watched"].get(SOURCE_LIVE, ())),
            "changed": changed,
        }
        for m, (p, changed) in months.items()
    }
    added = sum(len(t) for t in missing.values())
    bulk = callable(getattr(store, "add_polls", None))
    report["polls"] = {"days": len(missing), "added": added, "write": "bulk" if bulk else "per-poll"}
    if not bulk and added > SLOW_POLLS:
        print(
            f"import: this store has no add_polls(), so {added} polls take one transaction each; on Firestore that"
            " can outlast the 15 minutes Tifa's first poll has. Limit them with --polls-days.",
            file=log,
        )
    report["state"] = {
        "last_slot": state.last_slot,
        "run": [iso(run_from), iso(run_to)],
        "open": len(state.open),
        "lines": len(state.lines),
        "changed": not unchanged_state,
    }
    report["import_id"] = import_id(rec)
    if dry_run:
        report["ok"] = True
        return report

    if missing:
        write_polls(store, missing)
    for month, (payload, changed) in months.items():
        if changed:
            report["months"][month]["rev"] = store.put_month(month, payload)
    if not unchanged_state:
        result = store.poll_transaction(state.last_slot, lambda _current: PollWrite(state=state))
        if result == DUPLICATE:
            raise RuntimeError("the store refused the state as a duplicate slot (did a poll run during the import?)")
    revs = store.list_months()
    store.record_import(
        report["import_id"],
        {
            "kind": "jsonl",
            "imported_at": now,
            "forced": force,
            "last_poll": rec.last_poll,
            "last_slot": state.last_slot,
            "first_poll": rec.first_poll,
            "episodes": len(rec.closed),
            "open": len(rec.open),
            "polls": len(rec.polls),
            "polls_added": report["polls"]["added"],
            "skipped": dict(rec.skipped),
            "months": {m: revs.get(m, 0) for m in months},
            "inputs": dict(sorted(rec.sha256.items())),
            "status": rec.status is not None,
            "causes_sig": CAUSES_SIG,
        },
    )
    report["ok"] = True
    return report


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="import_jsonl",
        description="Import a local recording (episodes.jsonl, polls.jsonl, open.json) into a Tifa store.",
        epilog="Exit codes: 0 imported, 1 bad input or failed write, 2 bad usage, 3 refused (nothing written).",
    )
    ap.add_argument("recording", type=Path, help="the recording directory")
    ap.add_argument("--status", type=Path, help="the current status at the last poll (current() JSON, or TfL's raw)")
    ap.add_argument(
        "--max-age",
        type=int,
        default=MAX_AGE,
        help=f"refuse if the last poll is older than this many seconds (default {MAX_AGE}; 0 turns the check off)",
    )
    ap.add_argument("--force", action="store_true", help="import even though the store already holds a recording")
    ap.add_argument("--dry-run", action="store_true", help="read and check everything, write nothing")
    ap.add_argument(
        "--polls-days",
        type=int,
        default=0,
        help="only write polls/{day} for the last N days (default 0: all); watched runs always use every poll",
    )
    add_store_args(ap)
    return ap


def main(
    argv: list[str] | None = None,
    *,
    store: Store | None = None,
    now: int | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    if args.max_age < 0 or args.polls_days < 0:
        print("import: --max-age and --polls-days must not be negative", file=err)
        return EXIT_USAGE
    kind = "given"
    if store is None:
        try:
            store, kind = open_store(args)
        except ConfigError as exc:
            print(f"import: {exc}", file=err)
            return EXIT_USAGE
        if kind == "memory" and not args.dry_run:
            print("import: --store memory keeps nothing after this run; this is a rehearsal", file=err)
    try:
        rec = read_recording(args.recording, args.status)
        report = run_import(
            store,
            rec,
            now=int(time.time()) if now is None else now,
            max_age=args.max_age,
            force=args.force,
            dry_run=args.dry_run,
            polls_days=args.polls_days,
            store_kind=kind,
            log=err,
        )
    except BadInput as exc:
        print(f"import: {exc}", file=err)
        return EXIT_INPUT
    except Refused as exc:
        print(f"import refused: {exc}", file=err)
        write_json({"ok": False, "refused": str(exc)}, out)
        return EXIT_REFUSED
    except Exception as exc:  # a store or network error part-way: report it; a rerun with --force completes it
        print(f"import failed: {type(exc).__name__}: {exc}", file=err)
        return EXIT_INPUT
    write_json(report, out)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
