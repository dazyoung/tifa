"""Load generator for Tifa's fair-use limits: the per-IP rate, spoofed X-Forwarded-For, the global ceilings and the
egress allowance.

    uv run python tools/loadtest.py --base-url http://127.0.0.1:8080 --mode single-ip [--requests 200] [--rate 50]

Modes:

- single-ip: every request as one client. Behind Google's front end that is this machine's address; against a
  local server started with TIFA_PROXY_HEADERS=1, --client-ip ADDR sends X-Forwarded-For: ADDR in its place.
- spoofed-xff: every request sends a different made-up X-Forwarded-For entry. Google's front end appends the real
  address after it and the API keys on the right-most entry, so the limiter must still see one client. Against a
  local server pass --client-ip to append an address as the front end would.
- global: every request as a different client (a new IPv6 /64 as the right-most X-Forwarded-For entry), so the
  per-IP limit never bites and the service-wide ones do. Which one refused is read from each 503's message and
  counted ("global" requests per second, "uncached" computations per second, "egress"); --expect-503 REASON
  requires that one. For the uncached ceiling every request must be a different query: a path may hold {n}, the
  request's number from 1, e.g. --path '/v1/disruptions?limit={n}' (up to 500 requests) at a --rate between
  TIFA_UNCACHED_PER_S and TIFA_GLOBAL_RPS.
- egress: as global, for the hourly egress allowance: start the server with a small TIFA_EGRESS_PER_HOUR, pass the
  same value as --egress-per-hour, and keep --rate under TIFA_GLOBAL_RPS so the request ceiling does not bite first.

global and egress are only meaningful against a local server with TIFA_PROXY_HEADERS=1: on Cloud Run the front end
appends the real address, which makes them single-ip again.

Made-up addresses come from the documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32).

Safety: --requests may not exceed --hard-cap (default 20,000), and a target other than localhost, 127.0.0.1, ::1 or
a *.localhost name needs --i-understand-this-costs-money, since every request to a deployment is billed and counts
towards the guard, which may close the public API for everyone.

Checks (exit 1 when one fails; --no-check only reports):
- single-ip and spoofed-xff: at least one 429, each with Retry-After, and no more successes than the bucket plus its
  refill over the run allow (--bucket, --refill: the API's defaults);
- global: at least one 503, each with Retry-After, and with --expect-503 at least one for that reason;
- egress: at least one 503 for egress, each 503 with Retry-After, and no more body bytes in successful answers than
  the allowance, its refill over the run and one answer per request in flight (bytes are charged after they are
  sent, so answers already admitted may overshoot).

Prints a JSON report. Exit codes: 0 checks passed, 1 a check failed, 2 bad usage, 3 refused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx

MODES = ("single-ip", "spoofed-xff", "global", "egress")
REASONS = ("global", "uncached", "egress")
HARD_CAP = 20_000
COST_FLAG = "--i-understand-this-costs-money"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RATE_HEADERS = ("ratelimit-limit", "ratelimit-remaining", "ratelimit-reset", "ratelimit-policy", "ratelimit")
DOC_NETS = ("192.0.2", "198.51.100", "203.0.113")
USER_AGENT = "tifa-tools/loadtest"
BUCKET, REFILL = 60, 1.0  # the API's per-IP defaults (tifa.config.Limits)
EGRESS_PER_HOUR = 10 * 1024 * 1024  # bytes, the API's default (tifa.config.Limits)
# words in the API's 503 messages that tell which service-wide limit refused
REASON_HINTS = (("egress", "data allowance"), ("uncached", "uncached"), ("global", "request limit"))
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3


@dataclass
class Result:
    index: int
    status: int | None  # None: no response (a transport error)
    retry_after: str | None
    rate_headers: dict[str, str]
    latency: float  # seconds
    at: float  # seconds after the run began
    error: str | None = None
    nbytes: int = 0  # body bytes as sent (before any decompression)
    reason: str | None = None  # for a 503: which service-wide limit refused, from its message


def is_local(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def spoofed(rng: random.Random) -> str:
    return f"{rng.choice(DOC_NETS)}.{rng.randrange(1, 255)}"


def distinct_client(i: int) -> str:
    """A different IPv6 /64 for each i, inside 2001:db8::/32."""
    return f"2001:db8:{(i >> 16) & 0xFFFF:x}:{i & 0xFFFF:x}::1"


def headers_for(mode: str, i: int, client_ip: str | None, rng: random.Random) -> dict[str, str]:
    if mode == "single-ip":
        return {"X-Forwarded-For": client_ip} if client_ip else {}
    if mode == "spoofed-xff":
        fake = spoofed(rng)
        return {"X-Forwarded-For": f"{fake}, {client_ip}" if client_ip else fake}
    return {"X-Forwarded-For": f"{spoofed(rng)}, {distinct_client(i)}"}


def path_for(template: str, i: int) -> str:
    """The path for request i: {n} becomes its number from 1, so each request can be a different query."""
    return template.replace("{n}", str(i + 1))


def busy_reason(body: bytes) -> str:
    """Which service-wide limit a 503 is for, from the message in Tifa's error envelope; "other" if none."""
    try:
        doc = json.loads(body)
        message = str(doc["error"]["message"]).lower()
    except (ValueError, KeyError, TypeError):
        return "other"
    return next((reason for reason, hint in REASON_HINTS if hint in message), "other")


async def run_load(
    base_url: str,
    mode: str,
    requests: int,
    *,
    rate: float,
    concurrency: int,
    paths: Sequence[str],
    client_ip: str | None = None,
    timeout: float = 10.0,
    seed: int = 1,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> tuple[list[Result], float]:
    """Send `requests` GETs paced at `rate` per second with at most `concurrency` in flight; (results, seconds)."""
    rng = random.Random(seed)
    plan = [(i, path_for(paths[i % len(paths)], i), headers_for(mode, i, client_ip, rng)) for i in range(requests)]
    results: list[Result | None] = [None] * requests
    gate = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        transport=transport,
        limits=limits,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as client:
        t0 = clock()

        async def one(i: int, path: str, headers: dict[str, str]) -> None:
            delay = t0 + i / rate - clock()
            if delay > 0:
                await sleep(delay)
            async with gate:
                began = clock()
                try:
                    resp = await client.get(path, headers=headers)
                except httpx.HTTPError as exc:
                    results[i] = Result(i, None, None, {}, clock() - began, began - t0, type(exc).__name__)
                    return
                seen = {k: resp.headers[k] for k in RATE_HEADERS if k in resp.headers}
                results[i] = Result(
                    i,
                    resp.status_code,
                    resp.headers.get("retry-after"),
                    seen,
                    clock() - began,
                    began - t0,
                    nbytes=resp.num_bytes_downloaded,
                    reason=busy_reason(resp.content) if resp.status_code == 503 else None,
                )

        await asyncio.gather(*(one(*item) for item in plan))
        elapsed = clock() - t0
    return [r for r in results if r is not None], elapsed


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[k]


def summarise(
    results: list[Result],
    elapsed: float,
    mode: str,
    *,
    bucket: int = BUCKET,
    refill: float = REFILL,
    egress_per_hour: int = EGRESS_PER_HOUR,
    concurrency: int = 1,
    expect: str | None = None,
) -> dict[str, Any]:
    statuses = Counter("error" if r.status is None else str(r.status) for r in results)
    limited = [r for r in results if r.status == 429]
    busy = [r for r in results if r.status == 503]
    reasons = Counter(r.reason or "other" for r in busy)
    ok = [r for r in results if r.status is not None and 200 <= r.status < 400]
    latencies = [r.latency * 1000 for r in results if r.status is not None]
    allowed = math.floor(bucket + refill * elapsed) + max(5, math.ceil(0.05 * bucket))
    ok_bytes = sum(r.nbytes for r in ok)
    egress_allowed = math.floor(egress_per_hour * (1 + elapsed / 3600)) + concurrency * max(
        (r.nbytes for r in ok), default=0
    )
    if mode == "egress":
        expect = "egress"
    checks: dict[str, bool] = {}
    if mode in ("single-ip", "spoofed-xff"):
        checks["limited"] = bool(limited)
        checks["retry_after_on_429"] = all(r.retry_after for r in limited)
        checks["successes_within_bucket"] = len(ok) <= allowed
    else:
        checks["global_limited"] = bool(busy)
        checks["retry_after_on_503"] = all(r.retry_after for r in busy)
        if expect:
            checks[f"{expect}_limited"] = reasons.get(expect, 0) > 0
        if mode == "egress":
            checks["bytes_within_egress"] = ok_bytes <= egress_allowed

    def first(rs: list[Result]) -> int | None:
        return min((r.index for r in rs), default=None)

    sample = next((r.rate_headers for r in reversed(results) if r.rate_headers), {})
    report: dict[str, Any] = {
        "mode": mode,
        "requests": len(results),
        "elapsed_s": round(elapsed, 3),
        "achieved_rps": round(len(results) / elapsed, 1) if elapsed > 0 else None,
        "status": dict(sorted(statuses.items())),
        "successes": len(ok),
        "successes_allowed": allowed if mode in ("single-ip", "spoofed-xff") else None,
        "first_429_at": first(limited),
        "first_503_at": first(busy),
        "busy_reasons": dict(sorted(reasons.items())),
        "first_503_by_reason": {k: first([r for r in busy if (r.reason or "other") == k]) for k in sorted(reasons)},
        "retry_after": sorted({r.retry_after for r in limited + busy if r.retry_after})[:10],
        "closed_403": statuses.get("403", 0),
        "bytes": {"ok": ok_bytes, "all": sum(r.nbytes for r in results)},
        "errors": dict(Counter(r.error for r in results if r.error)),
        "latency_ms": {
            "p50": _round(percentile(latencies, 0.5)),
            "p95": _round(percentile(latencies, 0.95)),
            "max": _round(max(latencies) if latencies else None),
        },
        "ratelimit_headers": sample,
        "checks": checks,
    }
    if mode == "egress":
        report["bytes"]["ok_allowed"] = egress_allowed
    return report


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="loadtest",
        description="Exercise Tifa's fair-use limits (see the module docstring for the modes and checks).",
        epilog="Exit codes: 0 checks passed, 1 a check failed, 2 bad usage, 3 refused.",
    )
    ap.add_argument("--base-url", required=True, help="the API's base URL, e.g. http://127.0.0.1:8080")
    ap.add_argument("--mode", choices=MODES, default="single-ip")
    ap.add_argument("--requests", type=int, default=200, help="requests to send (default 200)")
    ap.add_argument("--rate", type=float, default=50.0, help="requests per second to aim for (default 50)")
    ap.add_argument("--concurrency", type=int, default=20, help="requests in flight at most (default 20)")
    ap.add_argument(
        "--path",
        action="append",
        dest="paths",
        help="path to request, repeatable, in turn; {n} is the request's number from 1 (default /v1/status)",
    )
    ap.add_argument("--client-ip", help="append this address to X-Forwarded-For, standing in for Google's front end")
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds per request (default 10)")
    ap.add_argument("--seed", type=int, default=1, help="seed for the made-up addresses (default 1)")
    ap.add_argument(
        "--bucket", type=int, default=BUCKET, help=f"the per-IP bucket the checks assume (default {BUCKET})"
    )
    ap.add_argument("--refill", type=float, default=REFILL, help=f"its refill per second (default {REFILL})")
    ap.add_argument(
        "--egress-per-hour",
        type=int,
        default=EGRESS_PER_HOUR,
        help=f"the server's TIFA_EGRESS_PER_HOUR, for the egress check (default {EGRESS_PER_HOUR})",
    )
    ap.add_argument(
        "--expect-503", choices=REASONS, help="global mode: require a 503 for this limit (egress mode: egress)"
    )
    ap.add_argument(
        "--hard-cap", type=int, default=HARD_CAP, help=f"--requests may not exceed this (default {HARD_CAP})"
    )
    ap.add_argument(COST_FLAG, dest="costs_ok", action="store_true", help="required for a target that is not local")
    ap.add_argument("--no-check", action="store_true", help="report only; exit 0 whatever the results")
    return ap


def main(
    argv: list[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    parts = urlsplit(args.base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query:
        print("loadtest: --base-url must be an http(s) URL without a query", file=err)
        return EXIT_USAGE
    if args.requests < 1 or args.rate <= 0 or args.concurrency < 1 or args.hard_cap < 1 or args.timeout <= 0:
        print("loadtest: --requests, --rate, --concurrency, --hard-cap and --timeout must be positive", file=err)
        return EXIT_USAGE
    if args.egress_per_hour < 1:
        print("loadtest: --egress-per-hour must be positive", file=err)
        return EXIT_USAGE
    if args.expect_503 and args.mode != "global":
        print("loadtest: --expect-503 goes with --mode global (egress mode always expects egress)", file=err)
        return EXIT_USAGE
    paths = args.paths or ["/v1/status"]
    if any(not p.startswith("/") for p in paths):
        print("loadtest: each --path must start with /", file=err)
        return EXIT_USAGE
    if args.requests > args.hard_cap:
        print(f"loadtest refused: --requests {args.requests} is over the hard cap of {args.hard_cap}", file=err)
        return EXIT_REFUSED
    local = is_local(args.base_url)
    if not local and not args.costs_ok:
        print(
            f"loadtest refused: {parts.hostname} is not local; every request is billed and counts towards the guard,"
            f" which may close the public API. Pass {COST_FLAG} to go ahead.",
            file=err,
        )
        return EXIT_REFUSED
    if args.mode in ("global", "egress") and not local:
        print(f"loadtest: {args.mode} mode only spreads clients locally; on Cloud Run it is one client", file=err)
    if args.mode == "spoofed-xff" and local and not args.client_ip:
        print(
            "loadtest: locally, pass --client-ip so the right-most entry is fixed as the front end makes it", file=err
        )

    results, elapsed = asyncio.run(
        run_load(
            args.base_url.rstrip("/"),
            args.mode,
            args.requests,
            rate=args.rate,
            concurrency=args.concurrency,
            paths=paths,
            client_ip=args.client_ip,
            timeout=args.timeout,
            seed=args.seed,
            transport=transport,
        )
    )
    report = summarise(
        results,
        elapsed,
        args.mode,
        bucket=args.bucket,
        refill=args.refill,
        egress_per_hour=args.egress_per_hour,
        concurrency=args.concurrency,
        expect=args.expect_503,
    )
    report["target"] = {"host": parts.hostname, "local": local, "paths": paths}
    report["ok"] = all(report["checks"].values())
    out.write(json.dumps(report, indent=1) + "\n")
    return EXIT_OK if report["ok"] or args.no_check else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
