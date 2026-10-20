"""Import an archive of TfL line-status episodes derived from a Freedom of Information release (source "f").

    uv run python tools/import_foi.py ARCHIVE_JSONL (--coverage COVERAGE_JSON | --from DATE --to DATE)
        --i-have-reuse-permission [--permission-ref TEXT] --store firestore [--dry-run]

LICENCE. TfL released these records in answer to a Freedom of Information request. Disclosure under the Freedom of
Information Act gives access to information; it is not a licence to re-use or publish it. Copyright in the
records stays with TfL, and TfL's open data terms (based on the Open Government Licence) cover what TfL publishes
through its open data feeds, not FOI responses. So re-publishing this archive needs TfL's permission, for example a
re-use licence or written consent. Tifa never publishes source "f" by default (TIFA_PUBLIC_SOURCES defaults to
"a"), and this tool refuses to run unless --i-have-reuse-permission is passed. Pass it only if you hold that
permission for this data and its intended use; --permission-ref records a reference to it in the import's audit
record. Importing puts the archive in the store; the API serves it only once TIFA_PUBLIC_SOURCES includes "f".

ARCHIVE_JSONL has one episode per line, in the recording's episode format: {"line", "name", "mode", "severity",
"status", "category", "planned", "start", "end", "end_estimated", "reason"} (times ISO 8601; other fields ignored).
The time the release covers comes from COVERAGE_JSON ({"sources": [{"from", "to", ...}]}, "to" being its last
second) or from --from and --to (London dates, both included).

Each month the archive touches gets its source "f" episodes (each with its cause, from the current rules) and
watched spans replaced by the archive's; other sources' episodes and spans are kept as they are. Months that hold
source "f" data the archive no longer has lose it. A rerun with the same archive changes nothing. Records
imports/foi-{sha}.

The store must be named: --store firestore (or TIFA_STORE=firestore), with --project or GOOGLE_CLOUD_PROJECT and
--database or TIFA_FIRESTORE_DATABASE. --store memory is an empty store that keeps nothing, for rehearsals.

Exit codes: 0 imported (or a dry run), 1 bad input or a failed write, 2 bad usage, 3 refused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):  # run as a script: make the repository root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tifa.config import ConfigError
from tifa.core.causes import CAUSES_SIG
from tifa.core.months import SOURCE_FOI, merge_month, merge_spans, new_month, split_months
from tifa.core.timeutil import day_bounds, epoch, iso, month_keys
from tifa.store import codec
from tifa.store.base import Store
from tools.import_jsonl import (
    EXIT_INPUT,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    MAX_FILE,
    BadInput,
    add_store_args,
    open_store,
    read_episodes,
    write_json,
)

FLAG = "--i-have-reuse-permission"
SHARED_WITH = {"circle-hammersmith-city": ["circle", "hammersmith-city"]}  # one combined line in the release
LICENCE = __doc__.split("LICENCE. ", 1)[1].split("\n\n", 1)[0]
REFUSAL = (
    f"import_foi refused: this archive comes from an FOI release, and an FOI release is not a licence to re-use "
    f"it. Run with {FLAG} only if you hold TfL's permission to re-use this data (see --help)."
)


def read_archive(path: Path) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    """(episodes, skipped counts, sha256) from the archive JSONL."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE + 1)
    except OSError as err:
        raise BadInput(f"{path.name} could not be read ({type(err).__name__})") from None
    if len(data) > MAX_FILE:
        raise BadInput(f"{path.name} is over {MAX_FILE // 2**20} MB")
    skipped: dict[str, int] = {}
    records = read_episodes(data, skipped)
    if not records:
        raise BadInput(f"{path.name} has no readable episodes")
    return records, skipped, hashlib.sha256(data).hexdigest()


def read_coverage(path: Path) -> list[list[int]]:
    try:
        doc = json.loads(path.read_bytes())
        spans = [[epoch(src["from"]), epoch(src["to"]) + 1] for src in doc["sources"]]  # "to" is its last second
    except (OSError, ValueError, KeyError, TypeError) as err:
        raise BadInput(f"{path.name} is not a coverage file ({type(err).__name__})") from None
    spans = merge_spans(s for s in spans if s[1] > s[0])
    if not spans:
        raise BadInput(f"{path.name} covers no time")
    return spans


def date_span(first: str, last: str) -> list[list[int]]:
    try:
        a, b = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
    except ValueError:
        raise BadInput("--from and --to must be dates (YYYY-MM-DD)") from None
    if b < a:
        raise BadInput("--to is before --from")
    return [[day_bounds(a)[0], day_bounds(b)[1]]]


def without_foi(payload: dict[str, Any]) -> dict[str, Any]:
    watched = dict(payload.get("watched") or {})
    watched[SOURCE_FOI] = []
    rows = [list(r) for r in payload.get("episodes") or () if r[6] != SOURCE_FOI]
    return {**payload, "episodes": rows, "watched": watched}


def has_foi(payload: dict[str, Any]) -> bool:
    return any(r[6] == SOURCE_FOI for r in payload.get("episodes") or ()) or bool(
        (payload.get("watched") or {}).get(SOURCE_FOI)
    )


def plan(store: Store, records: list[dict[str, Any]], spans: list[list[int]]) -> dict[str, tuple[dict, bool]]:
    """{month: (payload, changed)} for every month the archive touches or that holds source "f" data."""
    by_month: dict[str, list[dict[str, Any]]] = {}
    for ep in records:
        for month in split_months(ep["start"], ep["end"]):
            by_month.setdefault(month, []).append(ep)
    names = set(by_month)
    for a, b in spans:
        names.update(month_keys(a, b))
    out = {}
    for month in sorted(names | set(store.list_months())):
        doc = store.get_month(month)
        old = doc.payload if doc is not None else None
        if month not in names:
            if old is not None and has_foi(old):
                out[month] = (without_foi(old), True)  # the archive no longer has anything here
            continue
        payload = merge_month(without_foi(old or new_month(month)), by_month.get(month, ()), spans, SOURCE_FOI)
        out[month] = (payload, old is None or codec.digest(old) != codec.digest(payload))
    return out


def lines_of(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for ep in records:
        seen.setdefault(
            ep["line"],
            {"id": ep["line"], "name": ep["name"], "mode": ep["mode"], "shared_with": SHARED_WITH.get(ep["line"])},
        )
    return [seen[k] for k in sorted(seen)]


def run_import(
    store: Store,
    records: list[dict[str, Any]],
    spans: list[list[int]],
    *,
    sha256: str,
    skipped: dict[str, int],
    now: int,
    dry_run: bool = False,
    permission_ref: str | None = None,
    store_kind: str = "given",
) -> dict[str, Any]:
    months = plan(store, records, spans)
    report: dict[str, Any] = {
        "ok": False,
        "dry_run": dry_run,
        "store": store_kind,
        "source": SOURCE_FOI,
        "archive": {
            "episodes": len(records),
            "skipped": dict(sorted(skipped.items())),
            "from": iso(spans[0][0]),
            "to": iso(spans[-1][1]),
            "lines": len(lines_of(records)),
        },
        "months": {
            m: {
                "episodes": sum(1 for r in p["episodes"] if r[6] == SOURCE_FOI),
                "watched_s": sum(b - a for a, b in p["watched"].get(SOURCE_FOI, ())),
                "changed": changed,
            }
            for m, (p, changed) in months.items()
        },
        "import_id": f"foi-{sha256[:16]}",
    }
    if dry_run:
        report["ok"] = True
        return report
    for month, (payload, changed) in months.items():
        if changed:
            report["months"][month]["rev"] = store.put_month(month, payload)
    revs = store.list_months()
    doc = {
        "kind": "foi",
        "source": SOURCE_FOI,
        "imported_at": now,
        "input_sha256": sha256,
        "episodes": len(records),
        "skipped": dict(skipped),
        "coverage": [{"from": a, "to": b} for a, b in spans],
        "lines": lines_of(records),
        "months": {m: revs.get(m, 0) for m in months},
        "causes_sig": CAUSES_SIG,
        "reuse_permission": True,
    }
    if permission_ref:
        doc["permission_ref"] = permission_ref
    store.record_import(report["import_id"], doc)
    report["ok"] = True
    return report


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="import_foi",
        description=(
            'Import an FOI-derived archive of TfL line-status episodes into a Tifa store as source "f".\n\n'
            f"LICENCE: {LICENCE}"
        ),
        epilog="Exit codes: 0 imported, 1 bad input or failed write, 2 bad usage, 3 refused (nothing written).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("archive", type=Path, help="the archive JSONL, one episode per line")
    ap.add_argument("--coverage", type=Path, help='coverage JSON: {"sources": [{"from", "to"}]}, "to" its last second')
    ap.add_argument("--from", dest="first", metavar="DATE", help="first London day the release covers (YYYY-MM-DD)")
    ap.add_argument("--to", dest="last", metavar="DATE", help="last London day the release covers (YYYY-MM-DD)")
    ap.add_argument(
        FLAG,
        dest="permission",
        action="store_true",
        help="confirm that you hold TfL's permission to re-use this data (required; see LICENCE above)",
    )
    ap.add_argument("--permission-ref", help="a short reference to that permission, kept in the import record")
    ap.add_argument("--dry-run", action="store_true", help="read and check everything, write nothing")
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
    if not args.permission:
        print(REFUSAL, file=err)
        write_json({"ok": False, "refused": "no re-use permission confirmed"}, out)
        return EXIT_REFUSED
    dates = (args.first, args.last)
    if (args.coverage is not None and any(dates)) or (args.coverage is None and not all(dates)):
        print("import_foi: give either --coverage, or both --from and --to", file=err)
        return EXIT_USAGE
    kind = "given"
    if store is None:
        try:
            store, kind = open_store(args)
        except ConfigError as exc:
            print(f"import_foi: {exc}", file=err)
            return EXIT_USAGE
        if kind == "memory" and not args.dry_run:
            print("import_foi: --store memory keeps nothing after this run; this is a rehearsal", file=err)
    try:
        records, skipped, sha = read_archive(args.archive)
        spans = read_coverage(args.coverage) if args.coverage else date_span(args.first, args.last)
        report = run_import(
            store,
            records,
            spans,
            sha256=sha,
            skipped=skipped,
            now=int(time.time()) if now is None else now,
            dry_run=args.dry_run,
            permission_ref=args.permission_ref,
            store_kind=kind,
        )
    except BadInput as exc:
        print(f"import_foi: {exc}", file=err)
        return EXIT_INPUT
    except Exception as exc:  # a store error part-way: a rerun completes it
        print(f"import_foi failed: {type(exc).__name__}: {exc}", file=err)
        return EXIT_INPUT
    write_json(report, out)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
