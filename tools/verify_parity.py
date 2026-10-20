"""Compare a local recording with what Tifa recorded over the same window: the check for a parallel run.

    uv run python tools/verify_parity.py RECORDING_DIR (--api BASE_URL | --store firestore) [--from T] [--to T]

RECORDING_DIR is a local recording (episodes.jsonl, polls.jsonl, open.json; see import_jsonl.py). Tifa is read
from its public API (GET /v1/status for the last poll and the open episodes, then GET /v1/months/{YYYY-MM} for the
month payloads) or straight from a store (meta/state and months/{YYYY-MM}). Only source "a", the live recording,
is compared.

The window defaults to the 48 hours before the earlier of the two sides' last polls, less the tolerance, so both
sides have polled through all of it; --from and --to take ISO 8601 or epoch seconds. An episode is in the window
if it overlaps it. Each one on either side must have a twin on the other: the same (line, severity, planned), a
start within --tolerance seconds (default 150), and an end within it too. An open episode matches a closed one if
the open side last polled no later than the closed end plus the tolerance (it has not polled since the end).

Parity holds when no episode is unmatched (--max-unmatched), the matched pairs have the same cause
(--max-cause-mismatches), and Tifa watched at least --min-coverage percent of the window (default 99.5).
Unmatched episodes near a gap in either side's watched time are flagged, since a gap ends episodes early.

Prints a JSON report. Exit codes: 0 parity holds, 1 it does not, 2 bad usage or a side could not be read.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx

if __package__ in (None, ""):  # run as a script: make the repository root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tifa.config import ConfigError
from tifa.core.causes import cause
from tifa.core.months import SOURCE_LIVE, clip_spans, episode_key, merge_spans, month_episode
from tifa.core.timeutil import epoch, iso, month_keys
from tifa.store.base import Store
from tools.import_jsonl import BadInput, Recording, add_store_args, open_store, read_recording, write_json

TOLERANCE = 150  # seconds
MIN_COVERAGE = 99.5  # percent of the window Tifa must have watched
WINDOW = 48 * 3600
LIST_MAX = 50
RETRIES = 3
RETRY_MAX_S = 60
EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2
USER_AGENT = "tifa-tools/verify-parity"


@dataclass
class Side:
    name: str
    episodes: list[dict[str, Any]]  # line, severity, planned, start, end (None: open), cause, end_estimated
    watched: list[list[int]]
    last_poll: int | None


def _ep(line, severity, planned, start, end, code, estimated=False) -> dict[str, Any]:
    return {
        "line": line,
        "severity": severity,
        "planned": bool(planned),
        "start": start,
        "end": end,
        "cause": code,
        "end_estimated": bool(estimated),
    }


def local_side(rec: Recording) -> Side:
    eps = [
        _ep(
            e["line"],
            e["severity"],
            e["planned"],
            e["start"],
            e["end"],
            cause(e["reason"], e["planned"], e["status"]),
            e["end_estimated"],
        )
        for e in rec.closed
    ]
    eps += [
        _ep(e["line"], e["severity"], e["planned"], e["start"], None, cause(e["reason"], e["planned"], e["status"]))
        for e in rec.open
    ]
    return Side("local", eps, merge_spans(r for r in rec.runs() if r[1] > r[0]), rec.last_poll)


def months_side(
    name: str, payloads: Iterable[Mapping[str, Any]], open_eps: Iterable[Mapping[str, Any]], last_poll: int | None
) -> Side:
    """A side from month payloads (rows; an open one ends None) and open episodes given apart (dicts)."""
    rows: dict[tuple, list] = {}
    watched: list[list[int]] = []

    def add(row: list) -> None:
        if row[6] != SOURCE_LIVE:
            return
        key = episode_key(row)
        have = rows.get(key)
        if have is None or (have[2] is None and row[2] is not None):  # a closed copy beats an open one
            rows[key] = row

    for payload in payloads:
        for row in payload.get("episodes") or ():
            add(list(row))
        for ep in payload.get("open") or ():  # in case the open episodes come as a list of their own
            add(month_episode(dict(ep, end=None), SOURCE_LIVE))
        watched += [list(s) for s in (payload.get("watched") or {}).get(SOURCE_LIVE) or ()]
    for ep in open_eps:
        add(month_episode(dict(ep, end=None), SOURCE_LIVE))
    eps = [_ep(r[0], r[3], r[5], r[1], r[2], r[7], r[10] if len(r) > 10 else False) for r in rows.values()]
    return Side(name, eps, merge_spans(watched), last_poll)


def store_reads(store: Store) -> tuple[int | None, list[dict[str, Any]], Callable[[list[str]], list[dict]]]:
    """(last poll, open episodes, a reader of month payloads) from a store."""
    state = store.get_state()

    def months(names: list[str]) -> list[dict[str, Any]]:
        return [doc.payload for doc in (store.get_month(m) for m in names) if doc is not None]

    return (state.last_poll if state else None), (list(state.open) if state else []), months


def _get(client: httpx.Client, path: str, sleep: Callable[[float], None]) -> httpx.Response:
    for attempt in range(RETRIES + 1):
        resp = client.get(path)
        if resp.status_code not in (429, 503) or attempt == RETRIES:
            return resp
        try:
            wait = float(resp.headers.get("Retry-After", "5"))
        except ValueError:
            wait = 5.0
        sleep(min(max(wait, 1.0), RETRY_MAX_S))
    return resp


def _body(resp: httpx.Response, path: str) -> dict[str, Any]:
    if resp.status_code != 200:
        raise BadInput(f"GET {path} answered HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise BadInput(f"GET {path} did not return JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise BadInput(f"GET {path} returned no data")
    return body


def _last_poll(body: Mapping[str, Any]) -> int | None:
    """The last poll an API response reports: in its data (/v1/status) or in its meta (every response)."""
    for where in (body.get("data"), body.get("meta")):
        polled = where.get("last_poll") if isinstance(where, Mapping) else None
        if isinstance(polled, str):
            return epoch(polled)
    return None


def api_reads(
    client: httpx.Client, sleep: Callable[[float], None] = time.sleep
) -> tuple[int | None, list[dict[str, Any]], Callable[[list[str]], list[dict]]]:
    """(last poll, open episodes, a reader of month payloads) from the API: /v1/status, then /v1/months/{m}."""
    body = _body(_get(client, "/v1/status", sleep), "/v1/status")
    open_eps = body["data"].get("open") or []
    if not isinstance(open_eps, list) or not all(isinstance(e, dict) for e in open_eps):
        raise BadInput("GET /v1/status returned no open episode list")

    def months(names: list[str]) -> list[dict[str, Any]]:
        out = []
        for month in names:
            path = f"/v1/months/{month}"
            resp = _get(client, path, sleep)
            if resp.status_code == 404:
                continue
            data = _body(resp, path)["data"]
            if not isinstance(data.get("episodes"), list):
                raise BadInput(f"GET {path} returned no month payload")
            out.append(data)
        return out

    return _last_poll(body), open_eps, months


# ---------------------------------------------------------------- comparing


def overlaps(ep: Mapping[str, Any], a: int, b: int) -> bool:
    return ep["start"] < b and (ep["end"] is None or ep["end"] > a or (ep["end"] == ep["start"] >= a))


def ends_agree(x: Mapping, y: Mapping, last_x: int | None, last_y: int | None, tol: int) -> int | None:
    """How far apart two episodes' ends are (0 when one is still open), or None if they disagree."""
    ex, ey = x["end"], y["end"]
    if ex is None and ey is None:
        return 0
    if ex is not None and ey is not None:
        return abs(ex - ey) if abs(ex - ey) <= tol else None
    open_last, closed_end = (last_x, ey) if ex is None else (last_y, ex)
    return 0 if open_last is not None and open_last <= closed_end + tol else None


def gaps(watched: list[list[int]], a: int, b: int) -> list[list[int]]:
    out, reach = [], a
    for s, e in clip_spans(watched, a, b):
        if s > reach:
            out.append([reach, s])
        reach = max(reach, e)
    if reach < b:
        out.append([reach, b])
    return out


def near(ep: Mapping[str, Any], spans: list[list[int]], tol: int) -> bool:
    times = [ep["start"]] + ([ep["end"]] if ep["end"] is not None else [])
    return any(g0 - tol <= t <= g1 + tol for t in times for g0, g1 in spans)


def show(ep: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "line": ep["line"],
        "severity": ep["severity"],
        "planned": ep["planned"],
        "start": iso(ep["start"]),
        "end": iso(ep["end"]) if ep["end"] is not None else None,
        "end_estimated": ep["end_estimated"],
        "cause": ep["cause"],
        **extra,
    }


def compare(
    local: Side,
    tifa: Side,
    a: int,
    b: int,
    *,
    tol: int = TOLERANCE,
    min_coverage: float = MIN_COVERAGE,
    max_unmatched: int = 0,
    max_cause_mismatches: int = 0,
    list_max: int = LIST_MAX,
) -> dict[str, Any]:
    by_key: dict[str, dict[tuple, list[int]]] = {"local": {}, "tifa": {}}
    for side in (local, tifa):
        for i, ep in enumerate(side.episodes):
            by_key[side.name].setdefault((ep["line"], ep["severity"], ep["planned"]), []).append(i)
    used = {"local": set(), "tifa": set()}
    pairs: list[tuple[int, int, int, int]] = []  # (local index, tifa index, start diff, end diff)

    def match(src: Side, dst: Side, i: int) -> bool:
        ep, best = src.episodes[i], None
        for j in by_key[dst.name].get((ep["line"], ep["severity"], ep["planned"]), ()):
            if j in used[dst.name]:
                continue
            other = dst.episodes[j]
            ds = abs(other["start"] - ep["start"])
            de = ends_agree(ep, other, src.last_poll, dst.last_poll, tol)
            if ds <= tol and de is not None and (best is None or ds + de < best[0]):
                best = (ds + de, j, ds, de)
        if best is None:
            return False
        used[src.name].add(i)
        used[dst.name].add(best[1])
        pairs.append((i, best[1], best[2], best[3]) if src is local else (best[1], i, best[2], best[3]))
        return True

    in_local = sorted(
        (i for i, e in enumerate(local.episodes) if overlaps(e, a, b)), key=lambda i: local.episodes[i]["start"]
    )
    in_tifa = sorted(
        (i for i, e in enumerate(tifa.episodes) if overlaps(e, a, b)), key=lambda i: tifa.episodes[i]["start"]
    )
    for i in in_local:
        match(local, tifa, i)
    for j in in_tifa:
        if j not in used["tifa"]:
            match(tifa, local, j)
    lost_local = [i for i in in_local if i not in used["local"]]
    lost_tifa = [j for j in in_tifa if j not in used["tifa"]]
    mismatched = [(i, j) for i, j, _, _ in pairs if local.episodes[i]["cause"] != tifa.episodes[j]["cause"]]
    length = max(1, b - a)
    coverage = {
        s.name: round(100 * sum(e - s0 for s0, e in clip_spans(s.watched, a, b)) / length, 3) for s in (local, tifa)
    }
    gap_spans = gaps(local.watched, a, b) + gaps(tifa.watched, a, b)
    unmatched = len(lost_local) + len(lost_tifa)
    checks = {
        "episodes": unmatched <= max_unmatched,
        "causes": len(mismatched) <= max_cause_mismatches,
        "coverage": coverage["tifa"] >= min_coverage,
    }
    in_pairs = [p for p in pairs if p[0] in set(in_local) or p[1] in set(in_tifa)]
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "window": {"from": iso(a), "to": iso(b), "hours": round((b - a) / 3600, 2)},
        "tolerance_s": tol,
        "local": {
            "episodes": len(in_local),
            "last_poll": iso(local.last_poll) if local.last_poll else None,
            "coverage_pct": coverage["local"],
        },
        "tifa": {
            "episodes": len(in_tifa),
            "last_poll": iso(tifa.last_poll) if tifa.last_poll else None,
            "coverage_pct": coverage["tifa"],
            "min_coverage_pct": min_coverage,
        },
        "matched": len(in_pairs),
        "match_pct": round(100 * len(in_pairs) / max(1, len(in_pairs) + unmatched), 3),
        "max_start_diff_s": max((p[2] for p in in_pairs), default=0),
        "max_end_diff_s": max((p[3] for p in in_pairs), default=0),
        "unmatched_local": [
            show(local.episodes[i], near_gap=near(local.episodes[i], gap_spans, tol)) for i in lost_local[:list_max]
        ],
        "unmatched_tifa": [
            show(tifa.episodes[j], near_gap=near(tifa.episodes[j], gap_spans, tol)) for j in lost_tifa[:list_max]
        ],
        "unmatched_count": {"local": len(lost_local), "tifa": len(lost_tifa)},
        "cause_mismatches": [
            {
                **show(local.episodes[i]),
                "local_cause": local.episodes[i]["cause"],
                "tifa_cause": tifa.episodes[j]["cause"],
            }
            for i, j in mismatched[:list_max]
        ],
        "cause_mismatch_count": len(mismatched),
        "gaps": {
            "local": [[iso(x), iso(y)] for x, y in gaps(local.watched, a, b)][:list_max],
            "tifa": [[iso(x), iso(y)] for x, y in gaps(tifa.watched, a, b)][:list_max],
        },
    }


# ---------------------------------------------------------------- command line


def parse_time(text: str) -> int:
    text = text.strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return epoch(text.replace("Z", "+00:00"))


def check_base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise ValueError("--api must be an http(s) base URL without a query")
    return url.rstrip("/")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="verify_parity",
        description="Compare a local recording with Tifa (its API or its store) over a window.",
        epilog="Exit codes: 0 parity holds, 1 it does not, 2 bad usage or unreadable input.",
    )
    ap.add_argument("recording", type=Path, help="the local recording directory")
    ap.add_argument("--api", metavar="BASE_URL", help="Tifa's API base URL, e.g. https://tifa.example.org")
    ap.add_argument(
        "--from", dest="start", metavar="TIME", help="window start, ISO 8601 or epoch (default: 48 h before --to)"
    )
    ap.add_argument(
        "--to", dest="end", metavar="TIME", help="window end (default: the earlier last poll less the tolerance)"
    )
    ap.add_argument(
        "--tolerance", type=int, default=TOLERANCE, help=f"seconds starts and ends may differ (default {TOLERANCE})"
    )
    ap.add_argument(
        "--min-coverage",
        type=float,
        default=MIN_COVERAGE,
        help=f"percent of the window Tifa must have watched (default {MIN_COVERAGE})",
    )
    ap.add_argument("--max-unmatched", type=int, default=0, help="unmatched episodes allowed (default 0)")
    ap.add_argument(
        "--max-cause-mismatches", type=int, default=0, help="matched pairs with different causes allowed (default 0)"
    )
    ap.add_argument(
        "--list",
        type=int,
        default=LIST_MAX,
        help=f"at most this many of each kind of mismatch listed (default {LIST_MAX})",
    )
    ap.add_argument("--timeout", type=float, default=20.0, help="seconds per API request (default 20)")
    add_store_args(ap, store_help="read Tifa from this store instead of --api: firestore (memory is always empty)")
    return ap


def main(
    argv: list[str] | None = None,
    *,
    store: Store | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    use_store = store is not None or args.store is not None
    if bool(args.api) == use_store:
        print("verify_parity: give either --api or --store", file=err)
        return EXIT_ERROR
    if args.tolerance < 0 or args.list < 0 or args.max_unmatched < 0 or args.max_cause_mismatches < 0:
        print("verify_parity: numbers must not be negative", file=err)
        return EXIT_ERROR
    tol = args.tolerance
    try:
        start = parse_time(args.start) if args.start else None
        end = parse_time(args.end) if args.end else None
        base = check_base_url(args.api) if args.api else None
    except ValueError as exc:
        print(f"verify_parity: {exc}", file=err)
        return EXIT_ERROR
    client = None
    try:
        local = local_side(read_recording(args.recording))
        if base is not None:
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            client = httpx.Client(base_url=base, timeout=args.timeout, headers=headers, transport=transport)
            last_poll, open_eps, read_months = api_reads(client, sleep)
        else:
            if store is None:
                store, _ = open_store(args)
            last_poll, open_eps, read_months = store_reads(store)
        if end is None:
            end = min(t for t in (local.last_poll, last_poll) if t is not None) - tol
        if start is None:
            start = end - WINDOW
        if end <= start:
            print("verify_parity: the window is empty (--to is not after --from)", file=err)
            return EXIT_ERROR
        payloads = read_months(month_keys(start - tol, end + tol + 1))
        tifa = months_side("tifa", payloads, open_eps, last_poll)
    except (BadInput, ConfigError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        print(f"verify_parity: {type(exc).__name__}: {exc}", file=err)
        return EXIT_ERROR
    finally:
        if client is not None:
            client.close()
    report = compare(
        local,
        tifa,
        start,
        end,
        tol=tol,
        min_coverage=args.min_coverage,
        max_unmatched=args.max_unmatched,
        max_cause_mismatches=args.max_cause_mismatches,
        list_max=args.list,
    )
    report["tifa"]["source"] = "api" if base is not None else "store"
    write_json(report, out)
    return EXIT_OK if report["ok"] else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
