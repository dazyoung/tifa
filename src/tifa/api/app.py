"""The public API: /v1/*, /healthz, /docs and /openapi.json, with the MCP server at /mcp.

Requests pass through the Gate (an ASGI middleware) before anything else:

1. the read model refreshes if it is due (at most every 30 s), so prices and answers see the same version;
2. OPTIONS gets a CORS preflight answer; methods other than GET and HEAD get 405 (on /mcp the MCP app decides);
3. the request is priced (cached 1, an uncached changes, disruptions, stats or coverage query 5, an MCP tools/call
   3) and admitted or refused by the rate limiter (429 per client, 503 service-wide, both with Retry-After);
4. it goes to the MCP app (/mcp, unchanged path) or to the routes below, and its response gets the CORS and
   RateLimit-* headers on the way out. As the response starts, its bytes (from Content-Length) are charged to the
   client's share and to the service's hourly egress budget; when either is short the response is replaced by a
   refusal (429 or 503 with Retry-After), so no body is sent beyond the budgets. HEAD runs as GET with the body
   dropped.

Routes run in worker threads: views are CPU-bound pure functions over the read model's snapshot. Their responses
are cached per read model version (see tifa.api.http).
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Annotated, Any

import anyio
from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from tifa.api import views
from tifa.api.http import (
    CC_CHANGES,
    CC_LINE,
    CC_LINES,
    CC_LONG,
    CC_MONTH_CURRENT,
    CC_MONTH_PAST,
    CC_MONTHS,
    CC_SHORT,
    CC_STALE,
    CC_STATUS,
    CORS_API,
    CORS_MCP,
    CORS_PREFLIGHT_API,
    CORS_PREFLIGHT_MCP,
    NO_STORE,
    ResponseCache,
    build_entry,
    dumps,
    envelope_bytes,
    error_body,
    error_doc,
    make_body,
    meta,
    request_key,
    respond,
)
from tifa.api.ratelimit import CLIENT_EGRESS_SHARE, Decision, RateLimiter, client_key, client_of
from tifa.api.readmodel import ReadModel
from tifa.api.views import ViewError
from tifa.config import Settings, get_settings
from tifa.core.timeutil import DAY, epoch, iso
from tifa.store.base import Store

log = logging.getLogger("tifa.api")

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
McpFactory = Callable[[ReadModel], Any]

HEALTH_MAX_AGE = 600  # seconds since the last poll before /healthz answers 503
API_METHODS = ("GET", "HEAD")
# Every method but OPTIONS goes on to the MCP app, which answers anything but POST with its own 405 (stateless JSON
# mode has no event stream to open and no session to end).
MCP_METHODS = ("GET", "HEAD", "POST", "DELETE")
MCP_BODY_MAX = 64 * 1024  # bytes of an MCP request read to price it
EXPENSIVE = frozenset({"/v1/changes", "/v1/disruptions", "/v1/stats", "/v1/coverage"})
# the app's own values of these are replaced by the Gate's (a Retry-After of the app's stays: an admitted request
# has none of the Gate's)
_REPLACED = (b"access-control-", b"ratelimit-")
SWAGGER_UI = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"  # an exact version, never a moving one

DESCRIPTION = f"""London TfL line-status disruptions, recorded every minute, as an open REST API and an MCP server.

**{views.ATTRIBUTION}.** {views.NOTICE}

{views.LICENCE}

Every JSON response is `{{"data": ..., "meta": {{...}}}}`; errors are `{{"error": {{"code", "message"}}, "meta": ...}}`.
Times are ISO 8601 in UTC, except month payloads (`/v1/months/{{YYYY-MM}}`, epoch seconds in their established
row format) and the cursor and poll times of `/v1/changes` (epoch seconds). Days and hours shown to people are
London time.

Fair use: each client address has a bucket of 60 tokens refilled at one per second. A request costs 1, an
uncached changes, disruptions, stats or coverage query 5, an MCP tool call 3. Each address may also receive
1/{CLIENT_EGRESS_SHARE} of the service's hourly data allowance per hour (see `/v1/meta`): ask for gzip and send
`If-None-Match`.
Over a limit the API answers 429 with `Retry-After`; when the whole service is at its limit, 503 with
`Retry-After` (60 seconds, or longer while the hourly data allowance recovers). Responses carry `ETag` (send
`If-None-Match` for a 304), `Cache-Control` and `RateLimit-*` headers, and are gzipped when asked.

The MCP server (streamable HTTP, stateless, JSON responses) is at `/mcp`."""


# ---- documentation models (the handlers build their responses themselves) ---------------------------------


class Meta(BaseModel):
    attribution: str = Field(examples=[views.ATTRIBUTION])
    notice: str = Field(examples=[views.NOTICE])
    licence: str
    sources: list[str] = Field(description='The sources published: "a" is the live recording.')
    generated_at: str = Field(description="When this body was generated (ISO 8601, UTC).")
    last_poll: str | None = Field(description="The latest poll when this body was generated.")
    stale: bool = Field(description="No poll for over 5 minutes, or the store could not be read.")
    api_version: str = Field(examples=[views.API_VERSION])


class Envelope(BaseModel):
    data: dict[str, Any]
    meta: Meta


class ErrorDetail(BaseModel):
    code: str = Field(examples=["bad_request"])
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
    meta: Meta


def _responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    words = {
        400: "Bad request",
        404: "Not found",
        410: "The cursor is too old",
        429: "Too many requests from this client (Retry-After)",
        503: "The service is at a limit, or the store cannot be read (Retry-After)",
    }
    out: dict[int | str, dict[str, Any]] = {200: {"model": Envelope, "description": "The data, in the envelope"}}
    out[304] = {"description": "Not modified (If-None-Match matched the ETag)"}
    for code in sorted({*codes, 429, 503}):
        out[code] = {"model": ErrorEnvelope, "description": words[code]}
    return out


# ---- services ------------------------------------------------------------------------------------------------


def make_store(settings: Settings) -> Store:
    if settings.store == "firestore":
        from tifa.store.firestore import FirestoreStore

        return FirestoreStore(project=settings.project, database=settings.firestore_database)
    from tifa.store.memory import MemoryStore

    return MemoryStore()


def default_mcp_factory() -> McpFactory | None:
    """tifa.api.mcp.build_mcp_app, or None while that module is missing."""
    try:
        from tifa.api.mcp import build_mcp_app
    except ModuleNotFoundError as exc:
        if exc.name == "tifa.api.mcp":
            log.warning("tifa.api.mcp is missing; /mcp is not served")
            return None
        raise
    return build_mcp_app


@dataclass
class Services:
    settings: Settings
    rm: ReadModel
    limiter: RateLimiter
    cache: ResponseCache
    trust_forwarded: bool
    mcp_factory: McpFactory | None
    mcp_app: ASGIApp | None = None
    openapi_body: Any = None
    docs_body: Any = None
    started: list[str] = field(default_factory=list)


def _split_mcp(built: Any) -> tuple[ASGIApp, Any]:
    if isinstance(built, tuple | list) and len(built) == 2:
        return built[0], built[1]
    return built, None


def _as_context(lifespan: Any, app: Any) -> contextlib.AbstractAsyncContextManager:
    """An async context manager from what build_mcp_app returned as the lifespan: a context manager, a function
    of no arguments returning one (session_manager.run), or a Starlette lifespan taking the app."""
    if hasattr(lifespan, "__aenter__"):
        return lifespan
    if callable(lifespan):
        try:
            sig = inspect.signature(lifespan)
            required = [
                p
                for p in sig.parameters.values()
                if p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
        except (TypeError, ValueError):
            required = []
        return lifespan(app) if required else lifespan()
    raise TypeError("build_mcp_app must return (asgi_app, lifespan) with an async context manager as lifespan")


# ---- the gate --------------------------------------------------------------------------------------------------


class _TooLarge(Exception):
    pass


async def _read_body(receive: Receive, limit: int) -> bytes:
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body") or b""
        size += len(chunk)
        if size > limit:
            raise _TooLarge
        chunks.append(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def recv() -> MutableMapping[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return recv


def mcp_tool_calls(body: bytes) -> int:
    """How many JSON-RPC tools/call requests an MCP POST body holds."""
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):  # deeply nested JSON must not escape the gate
        return 0
    msgs = msg if isinstance(msg, list) else [msg]
    return sum(1 for m in msgs if isinstance(m, dict) and m.get("method") == "tools/call")


REFUSALS = {
    "client": ("rate_limited", "Too many requests from this address. Retry after {n} seconds."),
    "client_egress": (
        "rate_limited",
        "This address has received its share of the hourly data allowance. Ask for gzip (Accept-Encoding: gzip) "
        "and send If-None-Match to use less of it. Retry after {n} seconds.",
    ),
    "global": ("over_capacity", "The service is at its request limit. Retry in a minute."),
    "egress": ("over_capacity", "The service has sent its hourly data allowance. Retry after {n} seconds."),
    "uncached": ("over_capacity", "Too many uncached queries at once. Retry in a minute."),
}


def _content_length(headers: list) -> int | None:
    for k, v in headers:
        if k.lower() == b"content-length":
            try:
                return max(0, int(v))
            except ValueError:
                return None
    return None


class Gate:
    """Pricing, rate limits, CORS, HEAD and the /mcp dispatch, in front of the FastAPI routes."""

    def __init__(self, app: ASGIApp, services: Services) -> None:
        self.app = app
        self.svc = services

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        svc = self.svc
        path = scope.get("path") or "/"
        method = str(scope.get("method", "GET")).upper()
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        cors = CORS_MCP if is_mcp else CORS_API
        allowed = MCP_METHODS if is_mcp else API_METHODS
        client = client_key(client_of(scope, svc.trust_forwarded))
        limiter = svc.limiter

        if svc.rm.due():
            await anyio.to_thread.run_sync(svc.rm.refresh)

        if method == "OPTIONS" or method not in allowed:
            decision = limiter.admit(client, limiter.cost("cached"))
            if not decision.allowed:
                await self._refuse(scope, send, decision, cors, client)
            elif method == "OPTIONS":
                preflight = CORS_PREFLIGHT_MCP if is_mcp else CORS_PREFLIGHT_API
                allow = "POST, OPTIONS" if is_mcp else ", ".join((*allowed, "OPTIONS"))
                headers = [*preflight, ("Allow", allow), *decision.headers()]
                await self._emit(Response(status_code=204), scope, send, headers, head=False, client=client)
            else:
                allow = "POST, OPTIONS" if is_mcp else ", ".join((*allowed, "OPTIONS"))
                resp = self._error(405, "method_not_allowed", f"{method} is not allowed here; use {allow}.")
                headers = [*cors, ("Allow", allow), *decision.headers()]
                await self._emit(resp, scope, send, headers, head=False, client=client)
            return

        body: bytes | None = None
        kind, uncached, calls = "cached", False, 0
        if is_mcp and method == "POST":
            length = Headers(scope=scope).get("content-length")
            try:
                if length is not None and length.isdigit() and int(length) > MCP_BODY_MAX:
                    raise _TooLarge
                body = await _read_body(receive, MCP_BODY_MAX)
            except _TooLarge:
                decision = limiter.admit(client, limiter.cost("cached"))
                resp = self._error(413, "too_large", f"MCP requests are at most {MCP_BODY_MAX} bytes.")
                await self._emit(resp, scope, send, [*cors, *decision.headers()], head=False, client=client)
                return
            calls = mcp_tool_calls(body)
            if calls:
                kind, uncached = "mcp_call", True
        elif path in EXPENSIVE:
            key = request_key(path, scope.get("query_string", b""))
            if svc.cache.fresh(key, svc.rm.version) is None:
                kind, uncached = "uncached", True
        cost = limiter.cost(kind) * max(1, calls)
        decision = limiter.admit(client, cost, uncached=uncached)
        if not decision.allowed:
            await self._refuse(scope, send, decision, cors, client)
            return

        head = method == "HEAD"
        inner = {**scope, "method": "GET"} if head else scope
        target = self.app
        if is_mcp:
            if svc.mcp_app is None:
                resp = self._error(503, "mcp_unavailable", "The MCP server is not running.")
                await self._emit(resp, scope, send, [*cors, *decision.headers()], head=head, client=client)
                return
            target = svc.mcp_app
        extra = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (*cors, *decision.headers())]
        started = False
        replaced = False  # the app's response was refused for want of egress and a refusal sent instead
        charged = False  # the body's bytes were charged up front (its length was known)

        async def send_out(message: MutableMapping[str, Any]) -> None:
            nonlocal started, replaced, charged
            if replaced:
                return  # the rest of a response that was refused
            if message["type"] == "http.response.start":
                hdrs = [h for h in message.get("headers") or [] if not h[0].lower().startswith(_REPLACED)]
                hdrs += extra
                length = 0 if head else _content_length(hdrs)
                charged = length is not None
                refusal = limiter.spend(client, sum(len(k) + len(v) + 4 for k, v in hdrs) + 17 + (length or 0))
                started = True
                if refusal is not None:
                    replaced = True
                    await self._refuse(scope, send, refusal, cors, client)
                    return
                message = {**message, "headers": hdrs}
            elif message["type"] == "http.response.body":
                if head:
                    message = {**message, "body": b""}
                elif not charged:
                    limiter.record_egress(len(message.get("body") or b""), client)
            await send(message)

        try:
            await target(inner, _replay(body, receive) if body is not None else receive, send_out)
        except Exception:
            log.exception("unhandled error serving %s", path)
            if replaced:
                return  # a refusal went out already
            if started:
                raise
            resp = self._error(500, "internal", "Something went wrong on our side.")
            await self._emit(resp, scope, send, [*cors, *decision.headers()], head=head, client=client)

    def _error(self, status: int, code: str, message: str) -> Response:
        snap, now = self.svc.rm.peek(), self.svc.rm.now()
        body = error_body(status, code, message, snap.public_sources, snap.last_poll, views.is_stale(snap, now), now)
        resp = Response(content=body.raw, status_code=status, media_type=body.media_type)
        resp.headers["Cache-Control"] = NO_STORE
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    async def _refuse(self, scope: Scope, send: Send, decision: Decision, cors: list, client: str) -> None:
        code, text = REFUSALS[decision.reason or "client"]
        resp = self._error(decision.status, code, text.format(n=decision.retry_after))
        head = scope.get("method") == "HEAD"
        await self._emit(resp, scope, send, [*cors, *decision.headers()], head=head, client=client)

    async def _emit(
        self, resp: Response, scope: Scope, send: Send, headers: list, *, head: bool, client: str | None = None
    ) -> None:
        """Send a response of the Gate's own (refusals, errors, preflights), its bytes counted after the fact."""
        for k, v in headers:
            resp.headers[k] = v
        limiter = self.svc.limiter

        async def out(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                limiter.record_egress(sum(len(k) + len(v) + 4 for k, v in message.get("headers") or []) + 17, client)
            elif message["type"] == "http.response.body":
                if head:
                    message = {**message, "body": b""}
                else:
                    limiter.record_egress(len(message.get("body") or b""), client)
            await send(message)

        async def nothing() -> MutableMapping[str, Any]:
            return {"type": "http.disconnect"}

        await resp(scope, nothing, out)


# ---- the app ---------------------------------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    store: Store | Callable[[], Store] | None = None,
    clock: Callable[[], float] | None = None,
    monotonic: Callable[[], float] | None = None,
    mcp_factory: McpFactory | None | str = "default",
    trust_forwarded: bool | None = None,
) -> FastAPI:
    """Build the public ASGI app. Nothing touches the store until the first request (or startup).

    `store` defaults to the settings' (Firestore or in memory); `clock` (wall clock) and `monotonic` are for tests;
    `mcp_factory` is called with the read model at startup and returns (asgi_app, lifespan); "default" imports
    tifa.api.mcp.build_mcp_app, None serves no MCP. `trust_forwarded` (default: settings.proxy_headers) takes the
    client address from the right-most X-Forwarded-For entry."""
    settings = settings or get_settings()
    extra_clock = {"clock": clock} if clock else {}
    mono = {"monotonic": monotonic} if monotonic else {}
    rm = ReadModel(
        store if store is not None else (lambda: make_store(settings)),
        public_sources=settings.public_sources,
        limits=settings.limits,
        **extra_clock,
        **mono,
    )
    limiter = RateLimiter(settings.limits, **({"clock": monotonic} if monotonic else {}))
    cache = ResponseCache(**({"clock": monotonic} if monotonic else {}))
    factory = mcp_factory
    if mcp_factory == "default":
        build = default_mcp_factory()
        # the MCP app takes its allowed hosts and call cost from the same settings as the rest of the app
        factory = None if build is None else (lambda rm: build(rm, settings=settings))
    svc = Services(
        settings=settings,
        rm=rm,
        limiter=limiter,
        cache=cache,
        trust_forwarded=settings.proxy_headers if trust_forwarded is None else trust_forwarded,
        mcp_factory=factory,  # type: ignore[arg-type]
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            await anyio.to_thread.run_sync(rm.refresh)  # warm the read model (never raises for store errors)
            if svc.mcp_factory is not None:
                mcp_app, mcp_lifespan = _split_mcp(svc.mcp_factory(rm))
                if mcp_lifespan is not None:
                    await stack.enter_async_context(_as_context(mcp_lifespan, mcp_app))
                svc.mcp_app = mcp_app
                svc.started.append("mcp")
            try:
                yield
            finally:
                svc.mcp_app = None

    app = FastAPI(
        title="Tifa",
        version=views.API_VERSION,
        summary="London TfL line-status disruptions: an open REST API and MCP server.",
        description=DESCRIPTION,
        license_info={"name": "Code: MIT. Data: TfL open data (OGL v2.0 based terms)"},
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.tifa = svc

    def openapi_schema() -> dict[str, Any]:
        # FastAPI documents a 422 for every route with parameters; this API answers bad input with a 400 envelope
        if app.openapi_schema is None:
            schema = FastAPI.openapi(app)
            for ops in schema.get("paths", {}).values():
                for op in ops.values():
                    op.get("responses", {}).pop("422", None)
            for name in ("HTTPValidationError", "ValidationError"):
                schema.get("components", {}).get("schemas", {}).pop(name, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi_schema  # type: ignore[method-assign]
    app.add_middleware(Gate, services=svc)
    _add_handlers(app, svc)
    _add_routes(app, svc)
    return app


def _add_handlers(app: FastAPI, svc: Services) -> None:
    def error(request: Request, status: int, code: str, message: str) -> Response:
        snap, now = svc.rm.peek(), svc.rm.now()
        body = error_body(status, code, message, snap.public_sources, snap.last_poll, views.is_stale(snap, now), now)
        return respond(body, status=status, cache_control=NO_STORE, headers=request.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return error(request, 404, "not_found", "There is nothing here. /docs lists the routes.")
        if exc.status_code == 405:
            return error(request, 405, "method_not_allowed", "Use GET.")
        return error(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> Response:
        names = [str(e.get("loc", ["", "?"])[-1]) for e in exc.errors()]
        what = ", ".join(dict.fromkeys(names)) or "a parameter"
        return error(request, 400, "bad_parameter", f"Check {what}: it could not be read.")


def _serve(
    request: Request,
    svc: Services,
    compute: Callable[[], dict[str, Any]],
    policy: Callable[[dict[str, Any], int], str],
) -> Response:
    """Answer from the response cache, or compute, encode and cache."""
    rm = svc.rm
    snap = rm.snapshot()
    key = request_key(request.url.path, request.scope.get("query_string", b""))
    entry = svc.cache.get(key, snap.version)
    if entry is None:
        now = rm.now()
        retry: int | None = None
        try:
            data = compute()
            status, payload, cc = 200, data, policy(data, now)
        except ViewError as exc:
            status, payload, cc, retry = exc.status, error_doc(exc.code, exc.message), NO_STORE, exc.retry_after
        if snap.store_error is not None and cc not in (NO_STORE, CC_MONTH_PAST):
            cc = CC_STALE
        if status >= 500:  # the store could not be read, or a limit: try again on the next request
            sources, last, stale = snap.public_sources, snap.last_poll, views.is_stale(snap, now)
            body = error_body(status, payload["code"], payload["message"], sources, last, stale, now)
            extra = [("Retry-After", str(retry))] if retry is not None else []
            return respond(body, status=status, cache_control=NO_STORE, headers=request.headers, extra=extra)
        entry = build_entry(
            svc.cache,
            key,
            snap.version,
            status=status,
            payload=payload,
            meta_inputs=(snap.public_sources, snap.last_poll, views.is_stale(snap, now)),
            cache_control=cc,
            now=now,
        )
    return respond(entry.body, status=entry.status, cache_control=entry.cache_control, headers=request.headers)


def _fixed(cc: str) -> Callable[[dict[str, Any], int], str]:
    return lambda data, now: cc


def _guard_summary(doc: Any, now: int) -> dict[str, Any]:
    """What /healthz shows of the guard document: when it last ran and whether it has the API closed."""
    out: dict[str, Any] = {"heartbeat": None, "heartbeat_age_s": None, "closed": None, "closed_until": None}
    if not isinstance(doc, dict):
        return out

    def ts(name: str) -> int | None:
        v = doc.get(name)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    beat = next((t for t in map(ts, ("heartbeat", "checked_at", "last_run", "updated_at")) if t is not None), None)
    until = ts("closed_until")
    closed = doc.get("closed")
    if not isinstance(closed, bool):
        closed = until is not None and until > now
        if "applied" in doc:  # the guard's own document: closed only once it has applied the closure (not in dry-run)
            closed = closed and doc.get("applied") == "closed"
    out["heartbeat"] = iso(beat) if beat is not None else None
    out["heartbeat_age_s"] = now - beat if beat is not None else None
    out["closed"] = closed
    out["closed_until"] = iso(until) if until is not None else None
    mode = doc.get("mode")
    if mode in ("enforce", "dry-run", "off"):
        out["mode"] = mode
    return out


def _add_routes(app: FastAPI, svc: Services) -> None:
    rm = svc.rm

    @app.get("/v1/status", tags=["live"], summary="Every line's status now", responses=_responses())
    def status(request: Request) -> Response:
        """TfL's status of every line at the last poll (live problems first, then planned works, then good
        service), the episodes open now, the last poll time and `stale` (no poll for over 5 minutes)."""
        return _serve(request, svc, lambda: views.status_view(rm), _fixed(CC_STATUS))

    @app.get("/v1/changes", tags=["live"], summary="What changed since a cursor", responses=_responses(400, 410))
    def changes(
        request: Request,
        since: Annotated[
            int | None,
            Query(description="Epoch seconds: the `cursor` of the previous answer (at most 35 days ago)."),
        ] = None,
    ) -> Response:
        """For keeping a copy in sync: the status now, the open episodes, the episodes that closed at or after
        `since` (an episode closed at a gap ends at the poll before it, so repeats can come: merge on line,
        severity, planned and start), the poll times after `since` and the next `cursor`. 410 when `since` is
        more than 35 days ago: reload the months instead."""

        def compute() -> dict[str, Any]:
            if since is None:
                raise ViewError(400, "bad_since", "since (epoch seconds) is required.")
            return views.changes_view(rm, since)

        return _serve(request, svc, compute, _fixed(CC_CHANGES))

    @app.get("/v1/lines", tags=["lines"], summary="The lines", responses=_responses())
    def lines(request: Request) -> Response:
        """Every known line with its id, name and mode, the member lines of a shared line, and when the live
        recording first and last saw it."""
        return _serve(request, svc, lambda: views.lines_view(rm), _fixed(CC_LINES))

    @app.get("/v1/lines/{line_id}", tags=["lines"], summary="One line's report", responses=_responses(400, 404))
    def line(
        request: Request,
        line_id: Annotated[str, Path(description="A line id from /v1/lines, such as central.")],
    ) -> Response:
        """What is open on the line now, its figures over the last 24 hours, 7, 30 and 90 days, its latest
        episodes with reasons and causes, and how many unplanned disruptions started in each London hour."""
        return _serve(request, svc, lambda: views.line_view(rm, line_id), _fixed(CC_LINE))

    @app.get("/v1/months", tags=["history"], summary="The months index", responses=_responses())
    def months(request: Request) -> Response:
        """The months with published data, the start of the history, the lines (with `shared_with`), the
        categories and the causes."""
        return _serve(request, svc, lambda: views.months_index_view(rm), _fixed(CC_MONTHS))

    @app.get("/v1/months/{month}", tags=["history"], summary="One month's episodes", responses=_responses(400, 404))
    def month(
        request: Request,
        month: Annotated[str, Path(description="A UTC month, YYYY-MM.", examples=["2025-11"])],
    ) -> Response:
        """Every episode overlapping the UTC month as rows `[line, start, end, severity, category, planned01,
        source, cause, status, reason, end_estimated01]` (epoch seconds; `end` null while open), and the spans
        each source watched. The first 8 fields are the established month format."""

        def policy(data: dict[str, Any], now: int) -> str:
            return CC_MONTH_PAST if views.month_is_past(data["month"], now) else CC_MONTH_CURRENT

        return _serve(request, svc, lambda: views.month_view(rm, month), policy)

    @app.get(
        "/v1/disruptions", tags=["history"], summary="Episodes, filtered and paged", responses=_responses(400, 404)
    )
    def disruptions(
        request: Request,
        line: Annotated[str | None, Query(description="Line ids, comma-separated.")] = None,
        from_: Annotated[
            str | None,
            Query(alias="from", description="Epoch seconds, ISO 8601 or a London date (default: 30 days before to)."),
        ] = None,
        to: Annotated[
            str | None, Query(description="Epoch seconds, ISO 8601 or a London date, included (default: now).")
        ] = None,
        category: Annotated[
            str | None, Query(description="delays, suspended, closure, other or info; comma-separated.")
        ] = None,
        cause: Annotated[
            str | None, Query(description="Cause codes (see /v1/meta) or unknown; comma-separated.")
        ] = None,
        planned: Annotated[str | None, Query(description="true for planned works only, false for unplanned.")] = None,
        source: Annotated[str | None, Query(description="Published sources, comma-separated.")] = None,
        limit: Annotated[str | None, Query(description="Episodes per page, 1-500 (default 100).")] = None,
        cursor: Annotated[str | None, Query(description="next_cursor from the previous page.")] = None,
    ) -> Response:
        """Episodes overlapping [from, to), oldest first: at most 366 days per request and 500 per page. Follow
        `next_cursor` for the next page."""

        def policy(data: dict[str, Any], now: int) -> str:
            return CC_LONG if now - epoch(data["to"]) > DAY else CC_SHORT

        return _serve(request, svc, lambda: views.disruptions_view(rm, request.query_params), policy)

    @app.get("/v1/stats", tags=["history"], summary="Figures for a period", responses=_responses(400, 404))
    def stats(
        request: Request,
        span: Annotated[str | None, Query(description="1d, 7d, 30d, 90d or 365d (default 7d).")] = None,
        end: Annotated[str | None, Query(description="now, or a London date YYYY-MM-DD (default now).")] = None,
        line: Annotated[str | None, Query(description="One line id, for that line's figures only.")] = None,
    ) -> Response:
        """Good service, unplanned and planned disruptions, line-time disrupted, the most disrupted line, the
        longest unplanned disruption, the busiest day, the top causes, chart bins and one row per line, against
        the period before when both were watched enough. Shares are 0-1, times epoch seconds."""

        def policy(data: dict[str, Any], now: int) -> str:
            return CC_LONG if views.stats_is_past(request.query_params, now) else CC_SHORT

        return _serve(request, svc, lambda: views.stats_view(rm, request.query_params), policy)

    @app.get("/v1/coverage", tags=["history"], summary="When the sources watched", responses=_responses(400))
    def coverage(
        request: Request,
        from_: Annotated[
            str | None, Query(alias="from", description="Epoch seconds, ISO 8601 or a London date (default: 7 days).")
        ] = None,
        to: Annotated[str | None, Query(description="Epoch seconds, ISO 8601 or a London date (default now).")] = None,
    ) -> Response:
        """The spans each published source watched in [from, to) (at most 90 days), the share watched, the gaps,
        and per UTC day the polls recorded and the slots that failed."""
        return _serve(request, svc, lambda: views.coverage_view(rm, request.query_params), _fixed(CC_SHORT))

    @app.get("/v1/meta", tags=["about"], summary="About this API", responses=_responses())
    def meta_route(request: Request) -> Response:
        """The sources published, the categories and causes, the licence and attribution, and the fair-use
        limits."""
        return _serve(request, svc, lambda: views.meta_view(rm), _fixed(CC_LONG))

    @app.get("/healthz", tags=["about"], summary="Service health", responses={**_responses(), 503: {"model": Envelope}})
    def healthz(request: Request) -> Response:
        """The last poll and its age, the guard's heartbeat and the TfL key state. 503 when the last poll is
        more than 10 minutes old."""
        snap, now = rm.snapshot(), rm.now()
        last = snap.last_poll
        age = now - last if last is not None else None
        ok = age is not None and age <= HEALTH_MAX_AGE
        data = {
            "status": "ok" if ok else "stale",
            "last_poll": iso(last) if last is not None else None,
            "last_poll_age_s": age,
            "tfl_auth": snap.tfl_auth,
            "store": "ok" if snap.store_error is None else "error",
            "store_error_since": iso(snap.error_since) if snap.error_since else None,
            "read_model_age_s": int(now - snap.loaded_at) if snap.loaded_at else None,
            "guard": _guard_summary(rm.guard(), now),
            "mcp": svc.mcp_app is not None,
        }
        stale = views.is_stale(snap, now)
        body = make_body(envelope_bytes(dumps(data), meta(snap.public_sources, last, now, stale)))
        return respond(body, status=200 if ok else 503, cache_control=NO_STORE, headers=request.headers)

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> Response:
        data = {
            "name": "Tifa",
            "description": "London TfL line-status disruptions, recorded every minute.",
            "links": {"docs": "/docs", "openapi": "/openapi.json", "mcp": "/mcp", "meta": "/v1/meta"},
        }
        return _serve(request, svc, lambda: data, _fixed(CC_LONG))

    @app.get("/openapi.json", include_in_schema=False)
    def openapi(request: Request) -> Response:
        if svc.openapi_body is None:
            svc.openapi_body = make_body(dumps(app.openapi()))
        return respond(svc.openapi_body, cache_control=CC_LONG, headers=request.headers)

    @app.get("/docs", include_in_schema=False)
    def docs(request: Request) -> Response:
        if svc.docs_body is None:
            # Swagger UI at an exact version, with no favicon and no validator badge: the page asks no third party
            # but the CDN, and only for those two files
            page = get_swagger_ui_html(
                openapi_url="/openapi.json",
                title="Tifa API",
                swagger_js_url=f"{SWAGGER_UI}/swagger-ui-bundle.js",
                swagger_css_url=f"{SWAGGER_UI}/swagger-ui.css",
                swagger_favicon_url="data:,",
                swagger_ui_parameters={"validatorUrl": None},
            )
            svc.docs_body = make_body(bytes(page.body), "text/html; charset=utf-8")
        return respond(svc.docs_body, cache_control=CC_LONG, headers=request.headers)


app = create_app()
