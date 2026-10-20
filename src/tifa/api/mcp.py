"""The MCP server: read-only tools over the same views as the REST API.

FastMCP (mcp 1.x) in stateless streamable HTTP with JSON responses: every POST carries one JSON-RPC message and
gets one JSON body back, with no session to keep and no stream left open, which suits a request-billed service
that scales to zero. GET (the optional server-to-client event stream) and DELETE (ending a session) answer 405:
there are no sessions and nothing for the server to push.

Each tool calls one function of ``tifa.api.views`` and returns ``{"data": ..., "meta": ...}`` like the REST
envelope, so every result carries the attribution and the notice. Every tool error (``isError``) is
``{"error": {"code", "message", "status"}, "meta": ...}`` as well: a ``ViewError`` (bad input, unknown id) with its
code and status, arguments that do not fit a tool's schema (refused before any view runs) as ``bad_arguments``
(400), an unknown tool as ``unknown_tool`` (404) and anything unexpected as ``internal_error`` (500, logged, with no
detail shown).

Fair use: in the API app the gate in front of every route prices each ``tools/call`` at ``Limits.cost_mcp_call``
tokens (other MCP messages as any request) before it reaches this app, so nothing is charged here by default. An app
without such a gate passes ``charge``, which is then called for each tools/call with that cost before it runs.
Answers are gzipped when the client accepts it (as the REST API's are), so the gate's egress count sees the bytes
actually sent. A body that is not JSON at all (bad UTF-8, nesting too deep for the parser, an integer too long) gets
a JSON-RPC parse error (400) here and never reaches the SDK, whose handler would log a traceback for some of them.

Host check (DNS rebinding protection): the Host header must be one of ``TIFA_ALLOWED_HOSTS`` (the service's
run.app host names and any custom domain), else 421. Entries match as the SDK's ``TransportSecuritySettings`` do
("host", "host:port", or "host:*" for any port), ignoring case, and a name given without a port also matches it
with an explicit port. Away from Cloud Run, ``localhost``, ``127.0.0.1`` and ``[::1]`` on any port are accepted
too, as the SDK does for a local server. Any Origin is accepted: the endpoint is public and read-only and takes no
credentials, like the REST API (CORS open), so a page elsewhere gains nothing it could not read anyway; a rebound
DNS name still fails the Host check. The SDK's own check is therefore off, and this one runs instead.

The app is path-agnostic. To mount it on another Starlette or FastAPI app::

    mcp_app, mcp_lifespan = build_mcp_app(read_model)
    app.add_route("/mcp", mcp_app, include_in_schema=False)  # a Route, so POST /mcp is not redirected to /mcp/
    # and enter `async with mcp_lifespan(app):` inside the app's own lifespan
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, MutableMapping
from typing import Annotated, Any, Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import INVALID_REQUEST, PARSE_ERROR, CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, StrictInt, ValidationError
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import Response

from tifa.api import http as api_http
from tifa.api import views
from tifa.api.views import API_VERSION, ATTRIBUTION, NOTICE, UNKNOWN_CAUSE
from tifa.config import Settings, get_settings
from tifa.core.causes import CAUSES
from tifa.core.stats import SPAN_DAYS
from tifa.core.tfl import CATEGORY_IDS, MODES

log = logging.getLogger("tifa.api.mcp")

SERVER_NAME = "tifa"
MAX_BODY = 64 * 1024  # bytes; a tool call's arguments are a few hundred
LIMIT_DEFAULT = 25  # disruptions per call when no limit is given
LIMIT_MAX = 100
TEXT_MAX = 64  # characters of a line id or a time
EPOCH_MAX = 10**11 - 1  # the views read epoch seconds of up to 11 digits
GZIP_MIN = api_http.GZIP_MIN  # bytes: smaller answers are sent as they are, as by the REST API
GZIP_LEVEL = 6
PROBLEMS_MAX = 400  # characters of the argument problems quoted back in a bad_arguments error
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

INSTRUCTIONS = f"""\
Tifa records the status of every London Underground, Elizabeth line, London Overground and DLR line once a minute
and keeps the history of disruptions. Use current_status for now, line_status for one line in depth, disruptions
to search the history, period_stats for figures over a day to a year, coverage to check whether a quiet period was
recorded at all, and list_lines and causes for the ids the other tools take.
Times in results are ISO 8601 in UTC, except in period_stats, whose times are epoch seconds (UTC). Days and hours
shown to people are London local time. Every result has the data under "data" and its provenance under "meta".
When you present the data, say "{ATTRIBUTION}"; Tifa is not affiliated with Transport for London.
"""

# Argument types. Literal choices come from the core constants, so the schemas list exactly what the views take.
Mode = Literal[MODES]  # type: ignore[valid-type]
Category = Literal[CATEGORY_IDS]  # type: ignore[valid-type]
Cause = Literal[(*(code for code, _, _ in CAUSES), UNKNOWN_CAUSE)]  # type: ignore[valid-type]
Span = Literal[tuple(SPAN_DAYS)]  # type: ignore[valid-type]
LineId = Annotated[str, Field(min_length=1, max_length=TEXT_MAX)]
# A time: epoch seconds as a whole number (strict, so true is not 1), or text (epoch seconds, ISO 8601 or a London
# date), passed to the view as text either way.
Time = Annotated[StrictInt, Field(ge=0, le=EPOCH_MAX)] | Annotated[str, Field(min_length=1, max_length=TEXT_MAX)]
LINE = "A line id from list_lines, such as central, elizabeth, northern, dlr or weaver."
FROM = (
    "Start of the range: an ISO 8601 time (UTC when it has no offset), epoch seconds (a whole number, or the "
    "same as text), or a London date YYYY-MM-DD (from the start of that day)."
)
TO = (
    "End of the range (exclusive): an ISO 8601 time, epoch seconds, or a London date YYYY-MM-DD (to the end of "
    "that day, so the day is included). Default: now."
)

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
Charge = Callable[[Request, int], Response | None | Awaitable[Response | None]]
Meta = Callable[[Any], Mapping[str, Any]]
Lifespan = Callable[..., contextlib.AbstractAsyncContextManager[None]]


def build_mcp_app(
    rm: Any,
    *,
    settings: Settings | None = None,
    charge: Charge | None = None,
    meta: Meta | None = None,
) -> tuple[ASGIApp, Lifespan]:
    """The MCP endpoint as an ASGI app, and the lifespan that must be running while it serves.

    - ``rm``: the API's ReadModel. Each tool call runs ``rm.refresh()`` (a no-op until a refresh is due) and its
      view in a worker thread, as the store may be read.
    - ``settings``: default ``get_settings()``; gives the allowed hosts and the call cost.
    - ``charge(request, cost)``: an optional fair-use hook for each tools/call (default: none, as the API's gate
      prices calls). It returns None to let the call run, or a response (429 or 503 with Retry-After) to send
      instead; it may be a coroutine function.
    - ``meta(rm)``: the ``meta`` part of each result; default :func:`envelope_meta`, the REST envelope's meta.

    The app gzips answers of ``GZIP_MIN`` bytes or more for clients that accept gzip. ``lifespan`` takes an
    optional app argument (FastAPI's lifespan shape) and may be entered again after it exits: each run gets a new
    session manager, as the SDK allows only one run per manager.
    """
    settings = settings or get_settings()
    hosts = allowed_hosts(settings)
    if not hosts:
        log.warning("TIFA_ALLOWED_HOSTS is empty: the MCP endpoint will refuse every request (421)")
    # The endpoint checks Host itself (see the module docstring); without this FastMCP would turn the SDK's check on
    # for localhost only.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    meta_of = meta or envelope_meta
    _quiet_sdk_logs()

    def make_server() -> FastMCP:
        return _server(rm, security, meta_of)

    endpoint = _Endpoint(make_server, charge, settings.limits.cost_mcp_call, hosts)
    return _Gzip(endpoint), endpoint.lifespan


def envelope_meta(rm: Any) -> dict[str, Any]:
    """The REST envelope's meta (attribution, notice, licence, published sources, freshness) for the read model's
    current snapshot."""
    snap, now = rm.snapshot(), rm.now()
    return api_http.meta(rm.public_sources, snap.last_poll, now, views.is_stale(snap, now))


def allowed_hosts(settings: Settings) -> list[str]:
    """Host header values the endpoint accepts, in the SDK's form ("host", "host:port" or "host:*"), lower case."""
    out: list[str] = []
    for host in settings.allowed_hosts:
        host = host.lower()
        out.append(host)
        if ":" not in host:  # a client may send the default port explicitly
            out.append(f"{host}:*")
    if not settings.on_cloud_run:
        for host in LOCAL_HOSTS:
            out += [host, f"{host}:*"]
    return list(dict.fromkeys(out))


def host_allowed(host: str | None, allowed: Iterable[str]) -> bool:
    """Whether a Host header value matches one of `allowed` (exactly, or "name:*" for name with any port)."""
    if not host:
        return False
    host = host.lower()
    return any(host == a or (a.endswith(":*") and host.startswith(a[:-1])) for a in allowed)


# ---------------------------------------------------------------- tools


class _Server(FastMCP):
    """FastMCP whose refused calls are tool errors in the envelope too. The SDK's own are bare text ("Error
    executing tool disruptions: 1 validation error for ...") with no code, no status and no attribution."""

    def __init__(self, error_meta: Callable[[], Awaitable[Mapping[str, Any]]], **kwargs: Any) -> None:
        self.tool_names: set[str] = set()
        self._error_meta = error_meta
        super().__init__(**kwargs)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            return await self._refused(name, exc)

    async def _refused(self, name: str, exc: ToolError) -> CallToolResult:
        cause = exc.__cause__
        if name not in self.tool_names:
            message = f"Unknown tool: {_clip(name, TEXT_MAX)}. tools/list lists them."
            return _error_result(404, "unknown_tool", message, await self._error_meta())
        if isinstance(cause, ValidationError):
            message = f"The arguments do not fit {name}: {_problems(cause)}."
        elif isinstance(cause, ValueError | RecursionError):
            # FastMCP reads argument text that looks like JSON (a list sent as a string) before validating it
            message = f"The arguments of {name} could not be read."
        else:  # the tools catch their own failures, so this is unexpected
            log.error("MCP tool %s failed before its view ran", name, exc_info=cause or exc)
            return _internal_error()
        return _error_result(400, "bad_arguments", message, await self._error_meta())


def _server(rm: Any, security: TransportSecuritySettings, meta_of: Meta) -> FastMCP:
    async def error_meta() -> Mapping[str, Any]:
        try:
            return await anyio.to_thread.run_sync(meta_of, rm)
        except Exception:
            log.exception("MCP meta failed")
            return STATIC_META

    server = _Server(
        error_meta,
        name=SERVER_NAME,
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        transport_security=security,
        # No max_request_body_size: _Endpoint reads at most MAX_BODY bytes itself (413 above that), and older 1.x
        # releases do not take the argument.
        log_level="WARNING",  # only used when nothing has configured logging yet
    )

    def run(view: Callable[..., dict], args: tuple, hint: str) -> CallToolResult:
        rm.refresh()
        try:
            data = view(rm, *args)
        except views.ViewError as exc:
            return _view_error(exc, meta_of(rm), hint)
        return _result(data, meta_of(rm))

    async def call(view: Callable[..., dict], *args: Any, hint: str = "") -> CallToolResult:
        try:
            return await anyio.to_thread.run_sync(run, view, args, hint)
        except Exception:
            log.exception("MCP tool %s failed", getattr(view, "__name__", view))
            return _internal_error()

    def tool(name: str, title: str, description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        server.tool_names.add(name)
        return server.tool(
            name=name,
            title=title,
            description=description,
            annotations=ToolAnnotations(title=title, **READ_ONLY),
        )

    @tool(
        "list_lines",
        "List lines",
        "Every line Tifa records, with its id, name and mode (tube, elizabeth-line, overground or dlr) and the lines "
        "it shares track with. The ids are what line_status, disruptions and period_stats take.",
    )
    async def list_lines() -> CallToolResult:
        return await call(views.lines_view)

    @tool(
        "current_status",
        "Status now",
        "The status of every line right now, refreshed from TfL each minute: status text, worst severity, TfL's "
        "reasons, whether it is good service and whether a problem is live or planned works; lines with live "
        "problems first, then planned works, then good service. Also the disruptions open now (with their start "
        "times and causes), the time of the last poll, and `stale`: true when there has been no poll for over 5 "
        "minutes (or the history cannot be read), so the status may be out of date.",
    )
    async def current_status(
        mode: Annotated[Mode | None, Field(description="Only lines of this mode. Omit for every line.")] = None,
    ) -> CallToolResult:
        if mode is None:
            return await call(views.status_view)
        return await call(_status_of_mode, mode)

    @tool(
        "line_status",
        "One line",
        "One line in depth: its disruptions open now; the number of disruptions, minutes disrupted and share of "
        "watched time disrupted over the last 24 hours, 7, 30 and 90 days; its latest disruptions with TfL's "
        "reasons and their causes; and the London hours of the day at which its unplanned disruptions start.",
    )
    async def line_status(line: Annotated[LineId, Field(description=LINE)]) -> CallToolResult:
        return await call(views.line_view, _line(line), hint=_LINE_HINT)

    @tool(
        "disruptions",
        "Search disruptions",
        "Recorded disruptions overlapping a time range (default: the 30 days to now; at most 366 days), oldest "
        "first. One row per spell of one severity on one line: start, end (null while it is still going on), "
        "minutes, severity, status text, category, planned or not, cause and TfL's reason, and whether the end is "
        "estimated (recording was paused). Filter by line, category, cause and planned. `total` is how many match; "
        "when `next_cursor` is not null, call again with it (and the same other arguments) for the next page.",
    )
    @_from_alias
    async def disruptions(
        line: Annotated[LineId | None, Field(description=f"Only this line. {LINE}")] = None,
        from_: Annotated[Time | None, Field(alias="from", description=FROM)] = None,
        to: Annotated[Time | None, Field(description=TO)] = None,
        category: Annotated[
            Category | None,
            Field(description="Only this kind: delays, suspended, closure, other or info (information only)."),
        ] = None,
        cause: Annotated[
            Cause | None, Field(description="Only this cause (codes and labels from causes); unknown: no cause found.")
        ] = None,
        planned: Annotated[
            bool | None, Field(description="true: planned works only; false: unplanned only. Omit for both.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX, description="Most rows to return.")] = LIMIT_DEFAULT,
        cursor: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=views.MAX_PARAM,
                description="next_cursor from the previous call, for the next page.",
            ),
        ] = None,
    ) -> CallToolResult:
        params = _params(
            line=_line(line) if line is not None else None,
            to=_text(to),
            category=category,
            cause=cause,
            planned=None if planned is None else ("true" if planned else "false"),
            limit=str(limit),
            cursor=cursor,
            **{"from": _text(from_)},
        )
        return await call(views.disruptions_view, params, hint=_LINE_HINT if line else "")

    @tool(
        "period_stats",
        "Period figures",
        "Figures for a period, for the network or one line: watched coverage, share of time with good service, "
        "unplanned and planned disruption counts, line-time disrupted, the change against the period before (when "
        "both were at least 80% watched), the most disrupted line, the longest unplanned disruption, the busiest "
        "day, the top causes, per-day (or per-week) bins and a per-line table. `period` names the period and "
        "gives `prev` and `next`: the end to pass for the period before or after. Unlike the other tools, times "
        "here (t0, t1, p0, starts and ends, bins, unwatched spans) are epoch seconds (UTC) as numbers; only "
        "period.first is ISO 8601.",
    )
    async def period_stats(
        span: Annotated[Span, Field(description="Length of the period: 1d, 7d, 30d, 90d or 365d.")],
        end: Annotated[
            str,
            Field(
                pattern=r"^(now|[0-9]{4}-[0-9]{2}-[0-9]{2})$",
                description="now (the period ends now), or a London date YYYY-MM-DD the period ends with.",
            ),
        ] = "now",
        line: Annotated[LineId | None, Field(description=f"Only this line; omit for the network. {LINE}")] = None,
    ) -> CallToolResult:
        params = _params(span=span, end=end, line=_line(line) if line is not None else None)
        return await call(views.stats_view, params, hint=_LINE_HINT if line else "")

    @tool(
        "coverage",
        "Coverage",
        "When Tifa was watching, over a time range (default: the 7 days to now; at most 90 days): the spans each "
        "source covered, the share of the range watched, the gaps nothing watched, and per UTC day the polls "
        "recorded (of 1440) and the polls that failed. Check it before reading a quiet period as a good one.",
    )
    @_from_alias
    async def coverage(
        from_: Annotated[Time | None, Field(alias="from", description=FROM)] = None,
        to: Annotated[Time | None, Field(description=TO)] = None,
    ) -> CallToolResult:
        return await call(views.coverage_view, _params(to=_text(to), **{"from": _text(from_)}))

    @tool(
        "causes",
        "Causes",
        "The causes Tifa puts disruptions down to (read from TfL's reason text), as codes with labels, in the order "
        "the rules are tried. The codes are what disruptions takes as `cause`; a disruption with a null cause is "
        "unknown (cause=unknown finds those).",
    )
    async def causes() -> CallToolResult:
        return await call(views.causes_view)

    return server


_LINE_HINT = "list_lines gives the line ids."


def _status_of_mode(rm: Any, mode: str) -> dict:
    """status_view for one mode: its lines and open disruptions, with the counts of that mode."""
    data = dict(views.status_view(rm))
    lines = [line for line in data.get("lines") or [] if isinstance(line, Mapping) and line.get("mode") == mode]
    data["lines"] = lines
    data["disrupted"] = sum(not line.get("good") for line in lines)
    data["total"] = len(lines)
    if isinstance(data.get("open"), list):
        data["open"] = [e for e in data["open"] if isinstance(e, Mapping) and e.get("mode") == mode]
    data["mode"] = mode
    return data


def _line(line: str) -> str:
    return line.strip().lower()


def _params(**values: str | None) -> dict[str, str]:
    return {k: v for k, v in values.items() if v is not None}


def _text(value: int | str | None) -> str | None:
    """A time argument as the views read it: text (epoch seconds given as a number become their digits)."""
    return None if value is None else str(value)


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def _problems(err: ValidationError) -> str:
    """What is wrong with the arguments, per argument, without echoing the values sent."""
    found: dict[str, list[str]] = {}
    for e in err.errors(include_url=False, include_input=False, include_context=False):
        loc = e.get("loc") or ()
        msgs = found.setdefault(str(loc[0]) if loc else "arguments", [])
        if e["msg"] not in msgs:
            msgs.append(e["msg"])
    return _clip("; ".join(f"{k}: {', or '.join(v)}" for k, v in found.items()), PROBLEMS_MAX)


def _from_alias(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Take the argument "from" (not a Python name) as ``from_``. FastMCP reads the schema from ``fn`` (through
    ``__wrapped__``), where ``from_`` has the alias "from", and passes aliased arguments by their alias."""

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        if "from" in kwargs:
            kwargs["from_"] = kwargs.pop("from")
        return await fn(**kwargs)

    return wrapper


def _dumps(value: Any) -> str:
    """Compact strict JSON, as the REST API sends (NaN and infinities are refused)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _result(data: Any, meta: Mapping[str, Any]) -> CallToolResult:
    body = {"data": data, "meta": dict(meta)}
    return CallToolResult(content=[TextContent(type="text", text=_dumps(body))], structuredContent=body)


STATIC_META = {"attribution": ATTRIBUTION, "notice": NOTICE, "api_version": API_VERSION}  # no read model needed


def _view_error(exc: views.ViewError, meta: Mapping[str, Any], hint: str = "") -> CallToolResult:
    status = int(exc.status)
    return _error_result(status, str(exc.code), _tool_message(str(exc.message), status, hint), meta)


def _tool_message(message: str, status: int, hint: str) -> str:
    """A view's message for a tool user: on a 404 the hint replaces a closing pointer to a REST route ("No line x
    is known. /v1/lines lists them." becomes "No line x is known. list_lines gives the line ids.")."""
    if not hint or status != 404:
        return message
    head, sep, last = message.rstrip().rpartition(". ")
    if sep and "/v1/" in last:
        message = f"{head}."
    return f"{message} {hint}"


def _error_result(status: int, code: str, message: str, meta: Mapping[str, Any]) -> CallToolResult:
    body = {"error": {"code": code, "message": message, "status": status}, "meta": dict(meta)}
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=f"{code} ({status}): {message}")],
        structuredContent=body,
    )


def _internal_error() -> CallToolResult:
    return _error_result(500, "internal_error", "Something went wrong on the server. Try again later.", STATIC_META)


# ---------------------------------------------------------------- HTTP


class _Endpoint:
    """The ASGI app: the Host check, POST only, the optional charge for tool calls, then the SDK's session
    manager."""

    def __init__(self, make_server: Callable[[], FastMCP], charge: Charge | None, cost: int, hosts: list[str]) -> None:
        self._make_server = make_server
        self._spare: FastMCP | None = make_server()  # built now, so a bad tool definition fails at startup
        self._charge = charge
        self._cost = cost
        self._hosts = tuple(hosts)
        self._manager: StreamableHTTPSessionManager | None = None

    @contextlib.asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        server, self._spare = self._spare or self._make_server(), None
        server.streamable_http_app()  # creates the server's session manager
        manager = server.session_manager
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        if not host_allowed(_header(scope, b"host"), self._hosts):
            await _jsonrpc_error(421, "Invalid Host header")(scope, receive, send)
            return
        if scope["method"] != "POST":
            # No sessions, so no event stream to open (GET) and none to end (DELETE).
            await _jsonrpc_error(405, "Method Not Allowed: this endpoint only takes POST", {"Allow": "POST"})(
                scope, receive, send
            )
            return
        manager = self._manager
        if manager is None:
            await _jsonrpc_error(503, "The MCP server is not running")(scope, receive, send)
            return

        body = await _read_body(scope, receive)
        if body is None:
            await _jsonrpc_error(413, "Request body too large")(scope, receive, send)
            return
        message = _parse(body)
        if message is _UNREADABLE:
            await _jsonrpc_error(400, "Parse error: the body is not JSON", code=PARSE_ERROR)(scope, receive, send)
            return
        if self._charge is not None and _is_tool_call(message):
            refusal = self._charge(Request(scope), self._cost)
            if inspect.isawaitable(refusal):
                refusal = await refusal
            if refusal is not None:
                await refusal(scope, receive, send)
                return

        sent = False

        async def replay() -> MutableMapping[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await manager.handle_request(scope, replay, send)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def _read_body(scope: Scope, receive: Receive) -> bytes | None:
    """The whole request body, or None when it is over MAX_BODY."""
    length = _header(scope, b"content-length")
    if length is not None and length.strip().isdigit() and int(length) > MAX_BODY:
        return None
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


_UNREADABLE = object()


def _parse(body: bytes) -> Any:
    """The body as JSON, or _UNREADABLE. Besides malformed JSON, json.loads raises UnicodeDecodeError (a ValueError)
    on bad UTF-8, ValueError on an integer of more digits than Python converts, and RecursionError on deep nesting
    ("[" * 60000); none of these may escape."""
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return _UNREADABLE


def _is_tool_call(message: Any) -> bool:
    """Whether a parsed body is a tools/call request. The SDK takes one JSON-RPC message per POST (no batches)."""
    return isinstance(message, dict) and message.get("method") == "tools/call"


def _jsonrpc_error(
    status: int, message: str, headers: Mapping[str, str] | None = None, *, code: int = INVALID_REQUEST
) -> Response:
    """An HTTP error with a JSON-RPC error body, as the SDK's transport sends its own."""
    body = {"jsonrpc": "2.0", "id": "server-error", "error": {"code": code, "message": message}}
    return Response(_dumps(body), status_code=status, media_type="application/json", headers=dict(headers or {}))


class _Gzip:
    """Starlette's GZipMiddleware, applied only when the client accepts gzip by the REST API's reading of
    Accept-Encoding (which honours q=0; Starlette looks for the word "gzip"). Behind the API's gate the egress
    budget then counts the compressed bytes, as it does for REST answers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._gzip = GZipMiddleware(app, minimum_size=GZIP_MIN, compresslevel=GZIP_LEVEL)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and api_http.accepts_gzip(_header(scope, b"accept-encoding")):
            await self._gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _quiet_sdk_logs() -> None:
    """The SDK logs every request at INFO, and every call of an unknown tool at WARNING. Keep its warnings (errors
    only for the request handler), unless something has set these loggers' levels already."""
    for name, level in (("mcp", logging.WARNING), ("mcp.server.lowlevel.server", logging.ERROR)):
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET:
            logger.setLevel(level)
