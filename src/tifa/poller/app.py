"""The private poller service (contracts: "Poller").

- `POST /internal/poll`: Cloud Scheduler, every minute, with OIDC (Cloud Run IAM keeps everyone else out). The slot
  is `floor(X-CloudScheduler-ScheduleTime / 60)`, so Scheduler's retries of one run share a slot; without the
  header (or with one that cannot be right) it is the current minute. 200 for a recorded or duplicate slot, 502
  when TfL failed or sent a bad payload (Scheduler retries within the minute), 503 when the store failed. Never
  4xx. After the poll, the guard runs if it is due; its failures never change the answer.
- `POST /internal/budget`: budget notifications pushed by Pub/Sub. 200 once handled, and for pushes that are not
  budget notifications (so Pub/Sub does not redeliver them); 503 if the guard's state could not be saved.
- `GET /healthz`: the last poll, TfL auth and the guard; 503 when the last poll is over 10 minutes old.

The app's parts (store, TfL client, guard, clock) are built from the settings at start-up, or passed to
`create_app()` by tests.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tifa.config import Settings, get_settings
from tifa.core.timeutil import epoch, iso
from tifa.poller.budget import MAX_BODY, BudgetMessageError, parse_notification, parse_push
from tifa.poller.guard import Guard, build_guard, is_dry_closure, normalise
from tifa.poller.poll import FAILED, Fetcher, PollResult, run_poll
from tifa.poller.tfl_client import TflClient
from tifa.store.base import Store

log = logging.getLogger("tifa.poller")

SCHEDULE_HEADER = "x-cloudscheduler-scheduletime"
STALE_AFTER = 600  # /healthz answers 503 when the last poll is older than this
FUTURE_SLOTS = 2  # a schedule time more than this many minutes ahead of the clock is not believed
NO_STORE = {"Cache-Control": "no-store"}


@dataclass
class Parts:
    store: Store
    client: Fetcher
    guard: Guard
    clock: Callable[[], float]
    closers: list[Callable[[], None]] = field(default_factory=list)


def make_store(settings: Settings) -> Store:
    if settings.store == "memory":
        from tifa.store.memory import MemoryStore

        return MemoryStore()
    from tifa.store.firestore import FirestoreStore

    return FirestoreStore(project=settings.project, database=settings.firestore_database)


def slot_from_header(value: str | None, now_ts: int) -> int:
    """The minute slot of a Scheduler run: its schedule time // 60, else the current minute."""
    current = now_ts // 60
    if not value:
        return current
    try:
        ts = epoch(value.strip())
    except (ValueError, OverflowError):
        log.warning("poll: unreadable %s header; using the current minute", SCHEDULE_HEADER)
        return current
    slot = ts // 60
    if slot > current + FUTURE_SLOTS:  # believing it would refuse every real slot until then
        ahead = slot - current
        log.warning("poll: %s is %d minutes ahead of the clock; using the current minute", SCHEDULE_HEADER, ahead)
        return current
    return slot


def create_app(
    *,
    settings: Settings | None = None,
    store: Store | None = None,
    client: Fetcher | None = None,
    guard: Guard | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    lock = threading.Lock()
    built: list[Parts] = []

    def parts() -> Parts:
        """Build the parts once (at start-up through the lifespan, or on first use without one)."""
        if built:
            return built[0]
        with lock:
            if not built:
                s = settings or get_settings()
                st = store if store is not None else make_store(s)
                closers: list[Callable[[], None]] = []
                cl = client
                if cl is None:
                    tfl = TflClient(s.tfl_app_key, s.tfl_base_url)
                    closers.append(tfl.close)
                    cl = tfl
                gd = guard if guard is not None else build_guard(s, st)
                built.append(Parts(store=st, client=cl, guard=gd, clock=clock, closers=closers))
        return built[0]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        parts()  # a broken configuration fails start-up, so the revision never takes traffic
        yield
        for close in parts().closers:
            try:
                close()
            except Exception:  # shutting down anyway
                pass

    app = FastAPI(
        title="Tifa poller",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.parts = parts

    @app.post("/internal/poll")
    def poll(request: Request) -> JSONResponse:
        p = parts()
        now = int(p.clock())
        slot = slot_from_header(request.headers.get(SCHEDULE_HEADER), now)
        try:
            result = run_poll(p.store, p.client, slot, now)
        except Exception:  # run_poll handles TfL and store failures itself; this is a bug
            log.exception("poll slot %d failed unexpectedly", slot)
            result = PollResult(FAILED, slot, code="store")
        try:
            p.guard.maybe_run(int(p.clock()))
        except Exception:  # maybe_run never raises; belt and braces, as recording must not depend on it
            log.exception("guard failed")
        return JSONResponse(result.as_dict(), status_code=result.http_status, headers=NO_STORE)

    @app.post("/internal/budget")
    async def budget(request: Request) -> JSONResponse:
        p = parts()
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                break
        try:
            notice = parse_notification(parse_push(bytes(body)))
        except BudgetMessageError as exc:
            log.warning("budget push ignored: %s", exc)
            return JSONResponse({"ok": False, "ignored": str(exc)}, headers=NO_STORE)
        try:
            run = await run_in_threadpool(p.guard.budget, notice, int(p.clock()))
        except Exception as exc:
            log.error("budget notification not recorded (%s); Pub/Sub will redeliver it", type(exc).__name__)
            return JSONResponse({"ok": False, "error": "store"}, status_code=503, headers=NO_STORE)
        until = run.doc.get("closed_until")
        return JSONResponse(
            {
                "ok": True,
                "ratio": round(notice.ratio, 4),
                "closed_until": iso(until) if until else None,
                "reason": run.doc.get("reason"),
                "action": run.action,
            },
            headers=NO_STORE,
        )

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz() -> JSONResponse:
        p = parts()
        now = int(p.clock())
        try:
            state = p.store.get_state()
            guard_doc = p.store.get_guard() or {}
        except Exception as exc:
            log.error("healthz: store unreadable (%s)", type(exc).__name__)
            return JSONResponse({"ok": False, "role": "poller", "error": "store"}, status_code=503, headers=NO_STORE)
        last = state.last_poll if state else None
        age = now - last if last is not None else None
        ok = age is not None and age <= STALE_AFTER
        if state is not None:
            auth = state.tfl_auth
        else:
            auth = "ok" if getattr(p.client, "has_key", False) else "anonymous"
        body: dict[str, Any] = {
            "ok": ok,
            "role": "poller",
            "last_poll": iso(last) if last is not None else None,
            "last_poll_age_s": age,
            "last_slot": state.last_slot if state else None,
            "open": len(state.open) if state else 0,
            "tfl_auth": auth,
            "guard": _guard_view(guard_doc, p.guard.mode),
        }
        return JSONResponse(body, status_code=200 if ok else 503, headers=NO_STORE)

    return app


def _guard_view(doc: dict[str, Any], mode: str) -> dict[str, Any]:
    """The guard document for /healthz; `dry_run` is true when the closure shown is a dry run's (never applied)."""
    d = normalise(doc)

    def at(key: str) -> str | None:
        value = d[key]
        return iso(value) if value is not None else None

    return {
        "mode": mode,
        "heartbeat": at("heartbeat"),
        "closed_until": at("closed_until"),
        "reason": d["reason"],
        "applied": d["applied"],
        "dry_run": is_dry_closure(d),
        "error": d["error"],
    }


app = create_app()
