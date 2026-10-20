"""The MCP endpoint, driven by the SDK's own client over httpx's in-process ASGI transport.

Most tests replace the views with recording fakes, to check the MCP layer itself: the schemas, the arguments each
tool passes to its view, the envelope and attribution, error mapping, HTTP methods, host checks and the optional
charge hook. The last tests run every tool end to end on the API's read model over a MemoryStore, and through the
API app, whose gate prices each tool call.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.responses import Response

from tifa.api import mcp as tifa_mcp
from tifa.api import views
from tifa.api.readmodel import ReadModel as StoreReadModel
from tifa.config import Limits, Settings
from tifa.core import episodes, months, tfl
from tifa.core.causes import CAUSES, CAUSES_SIG
from tifa.core.stats import SPAN_DAYS
from tifa.core.tfl import CATEGORY_IDS, MODES
from tifa.core.timeutil import month_key
from tifa.store.base import PollWrite, State
from tifa.store.memory import MemoryStore

VIEW_NAMES = (
    "status_view",
    "changes_view",
    "lines_view",
    "line_view",
    "months_index_view",
    "month_view",
    "disruptions_view",
    "stats_view",
    "coverage_view",
    "meta_view",
    "causes_view",
)
ATTRIBUTION = "Powered by TfL Open Data"
NOTICE = "Not affiliated with or endorsed by Transport for London."
TOOLS = {"list_lines", "current_status", "line_status", "disruptions", "period_stats", "coverage", "causes"}
HOST = "tifa.example.org"
JSON_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
STATUS = {
    "lines": [
        {"id": "central", "name": "Central", "mode": "tube", "severity": 6, "good": False, "live": True},
        {"id": "dlr", "name": "DLR", "mode": "dlr", "severity": 10, "good": True, "live": False},
        {"id": "victoria", "name": "Victoria", "mode": "tube", "severity": 10, "good": True, "live": False},
    ],
    "disrupted": 1,
    "total": 3,
    "open": [{"line": "central", "mode": "tube", "severity": 6, "start": 1_760_000_000}],
    "stale": False,
}


class FakeViews:
    """Records each view call as (name, rm, args) and answers with canned data, or raises what `fail` says."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, tuple]] = []
        self.fail: dict[str, Exception] = {}

    def view(self, name: str):
        def view(rm: Any, *args: Any) -> dict:
            self.calls.append((name, rm, tuple(dict(a) if isinstance(a, dict) else a for a in args)))
            if name in self.fail:
                raise self.fail[name]
            return json.loads(json.dumps(STATUS)) if name == "status_view" else {"view": name}

        view.__name__ = name
        return view


@pytest.fixture
def fake(monkeypatch) -> FakeViews:
    fake = FakeViews()
    for name in VIEW_NAMES:
        monkeypatch.setattr(views, name, fake.view(name))
    return fake


class Snapshot:
    last_poll = 1_760_000_040
    store_error = None


class ReadModel:
    """The parts of tifa.api.readmodel.ReadModel the MCP layer uses."""

    public_sources = ("a",)

    def __init__(self) -> None:
        self.refreshes = 0

    def refresh(self, force: bool = False) -> Snapshot:
        self.refreshes += 1
        return Snapshot()

    def snapshot(self) -> Snapshot:
        return Snapshot()

    def now(self) -> int:
        return 1_760_000_100


RM = ReadModel()


class Charges:
    def __init__(self, refuse: Response | None = None) -> None:
        self.costs: list[int] = []
        self.clients: list[Any] = []
        self.refuse = refuse

    def __call__(self, request, cost: int) -> Response | None:
        self.costs.append(cost)
        self.clients.append(request.client)
        return self.refuse


def settings(**kwargs: Any) -> Settings:
    return Settings(**{"allowed_hosts": (HOST,), **kwargs})


def build(charge: Any = None, **kwargs: Any):
    """The app with a recording charge hook (a fresh one unless given)."""
    kwargs.setdefault("settings", settings())
    return tifa_mcp.build_mcp_app(RM, charge=charge or Charges(), **kwargs)


@contextlib.asynccontextmanager
async def http(app, base: str = f"http://{HOST}") -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 50000))
    async with httpx.AsyncClient(transport=transport, base_url=base) as client:
        yield client


@contextlib.asynccontextmanager
async def session(app, lifespan, base: str = f"http://{HOST}") -> AsyncIterator[ClientSession]:
    async with lifespan(), http(app, base) as client:
        async with streamable_http_client(f"{base}/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as s:
                s.init_result = await s.initialize()
                yield s


def body_of(result) -> dict:
    assert not result.isError, result.content
    text = json.loads(result.content[0].text)
    assert text == result.structuredContent
    return result.structuredContent


def call_body(name: str, arguments: dict | None = None, request_id: int = 2) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


# ---------------------------------------------------------------- protocol and schemas


async def test_initialize_names_the_server_and_asks_for_attribution(fake):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        assert s.init_result.serverInfo.name == "tifa"
        assert ATTRIBUTION in s.init_result.instructions
        assert s.init_result.capabilities.tools is not None


async def test_lists_the_seven_read_only_tools(fake):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert len(tool.description) > 60, tool.name
        assert tool.title
        a = tool.annotations
        assert (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint) == (True, False, True, False)


async def test_schemas_use_the_contract_names_and_the_core_choices(fake):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        schemas = {t.name: t.inputSchema for t in (await s.list_tools()).tools}

    def enum(schema: dict, name: str) -> list:
        prop = schema["properties"][name]
        options = [o for o in prop.get("anyOf", [prop]) if "enum" in o]
        return options[0]["enum"]

    d = schemas["disruptions"]["properties"]
    assert set(d) == {"line", "from", "to", "category", "cause", "planned", "limit", "cursor"}
    assert d["limit"]["maximum"] == 100 and d["limit"]["minimum"] == 1 and d["limit"]["default"] == 25
    assert enum(schemas["disruptions"], "category") == list(CATEGORY_IDS)
    assert enum(schemas["disruptions"], "cause") == [code for code, _, _ in CAUSES] + ["unknown"]
    assert enum(schemas["current_status"], "mode") == list(MODES)
    assert enum(schemas["period_stats"], "span") == list(SPAN_DAYS)
    assert set(schemas["coverage"]["properties"]) == {"from", "to"}
    for schema in (schemas["disruptions"], schemas["coverage"]):
        for name in ("from", "to"):  # epoch seconds as a number, or text
            kinds = {o.get("type") for o in schema["properties"][name]["anyOf"]}
            assert kinds == {"integer", "string", "null"}, name
    assert schemas["line_status"]["required"] == ["line"]
    assert schemas["period_stats"]["required"] == ["span"]
    assert schemas["period_stats"]["properties"]["end"]["default"] == "now"
    assert schemas["list_lines"]["properties"] == {} and schemas["causes"]["properties"] == {}


def test_argument_types_follow_the_core_constants():
    assert get_args(tifa_mcp.Mode) == MODES
    assert get_args(tifa_mcp.Category) == CATEGORY_IDS
    assert get_args(tifa_mcp.Cause) == (*(code for code, _, _ in CAUSES), views.UNKNOWN_CAUSE)
    assert get_args(tifa_mcp.Span) == tuple(SPAN_DAYS)


# ---------------------------------------------------------------- tools -> views


CASES = [
    ("list_lines", {}, "lines_view", ()),
    ("current_status", {}, "status_view", ()),
    ("line_status", {"line": "central"}, "line_view", ("central",)),
    ("line_status", {"line": "  Elizabeth "}, "line_view", ("elizabeth",)),
    ("disruptions", {}, "disruptions_view", ({"limit": "25"},)),
    (
        "disruptions",
        {
            "line": "Northern",
            "from": "2026-10-01",
            "to": "2026-10-19T08:00:00+00:00",
            "category": "suspended",
            "cause": "signal",
            "planned": False,
            "limit": 100,
        },
        "disruptions_view",
        (
            {
                "line": "northern",
                "from": "2026-10-01",
                "to": "2026-10-19T08:00:00+00:00",
                "category": "suspended",
                "cause": "signal",
                "planned": "false",
                "limit": "100",
            },
        ),
    ),
    ("disruptions", {"planned": True, "limit": 1}, "disruptions_view", ({"planned": "true", "limit": "1"},)),
    (
        "disruptions",
        {"cause": "unknown", "cursor": "abc"},
        "disruptions_view",
        ({"cause": "unknown", "limit": "25", "cursor": "abc"},),
    ),
    ("period_stats", {"span": "7d"}, "stats_view", ({"span": "7d", "end": "now"},)),
    (
        "period_stats",
        {"span": "365d", "end": "2026-03-31", "line": "dlr"},
        "stats_view",
        ({"span": "365d", "end": "2026-03-31", "line": "dlr"},),
    ),
    (
        "disruptions",
        {"from": 1_792_396_800, "to": "1792400400"},
        "disruptions_view",
        ({"from": "1792396800", "to": "1792400400", "limit": "25"},),
    ),
    ("coverage", {}, "coverage_view", ({},)),
    (
        "coverage",
        {"from": "2026-10-01", "to": "2026-10-02"},
        "coverage_view",
        ({"from": "2026-10-01", "to": "2026-10-02"},),
    ),
    ("coverage", {"from": 0, "to": 1_792_400_400}, "coverage_view", ({"from": "0", "to": "1792400400"},)),
    ("causes", {}, "causes_view", ()),
]


@pytest.mark.parametrize(("tool", "arguments", "view", "args"), CASES)
async def test_each_tool_calls_its_view_and_wraps_the_data(fake, tool, arguments, view, args):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        result = await s.call_tool(tool, arguments)
    body = body_of(result)
    assert fake.calls == [(view, RM, args)]
    expected = STATUS if view == "status_view" else {"view": view}
    assert body["data"] == expected
    assert body["meta"]["attribution"] == ATTRIBUTION
    assert body["meta"]["notice"] == NOTICE
    assert body["meta"]["api_version"] == "1"
    assert body["meta"]["sources"] == ["a"]
    assert body["meta"]["last_poll"] == "2025-10-09T08:54:00+00:00"
    assert body["meta"]["generated_at"] == "2025-10-09T08:55:00+00:00"
    assert body["meta"]["stale"] is False


async def test_each_call_refreshes_the_read_model_first(fake):
    rm = ReadModel()
    app, lifespan = tifa_mcp.build_mcp_app(rm, settings=settings(), charge=Charges())
    async with session(app, lifespan) as s:
        await s.list_tools()
        assert rm.refreshes == 0
        await s.call_tool("causes", {})
        await s.call_tool("line_status", {"line": "central"})
    assert rm.refreshes == 2


async def test_every_tool_result_carries_the_attribution(fake):
    calls = {
        "list_lines": {},
        "current_status": {"mode": "dlr"},
        "line_status": {"line": "central"},
        "disruptions": {"line": "central"},
        "period_stats": {"span": "1d"},
        "coverage": {},
        "causes": {},
    }
    assert set(calls) == TOOLS
    app, lifespan = build()
    async with session(app, lifespan) as s:
        for tool, arguments in calls.items():
            result = await s.call_tool(tool, arguments)
            meta = body_of(result)["meta"]
            assert (meta["attribution"], meta["notice"]) == (ATTRIBUTION, NOTICE), tool
            assert ATTRIBUTION in result.content[0].text


async def test_current_status_for_one_mode_keeps_its_lines_and_counts(fake):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        tube = body_of(await s.call_tool("current_status", {"mode": "tube"}))["data"]
        dlr = body_of(await s.call_tool("current_status", {"mode": "dlr"}))["data"]
        overground = body_of(await s.call_tool("current_status", {"mode": "overground"}))["data"]
    assert [line["id"] for line in tube["lines"]] == ["central", "victoria"]
    assert (tube["disrupted"], tube["total"], tube["mode"]) == (1, 2, "tube")
    assert [e["line"] for e in tube["open"]] == ["central"]
    assert (dlr["disrupted"], dlr["total"], dlr["open"]) == (0, 1, [])
    assert (overground["lines"], overground["total"]) == ([], 0)
    assert tube["stale"] is False


async def test_meta_can_come_from_the_api(fake):
    app, lifespan = build(meta=lambda rm: {"attribution": ATTRIBUTION, "from": "the api", "rm": rm is RM})
    async with session(app, lifespan) as s:
        body = body_of(await s.call_tool("causes", {}))
    assert body["meta"] == {"attribution": ATTRIBUTION, "from": "the api", "rm": True}


def test_envelope_meta_before_the_first_poll():
    rm = StoreReadModel(MemoryStore(), public_sources=("a", "f"), clock=lambda: 1_760_000_000)
    meta = tifa_mcp.envelope_meta(rm)
    assert meta == {
        "attribution": ATTRIBUTION,
        "notice": NOTICE,
        "licence": views.LICENCE,
        "sources": ["a", "f"],
        "generated_at": "2025-10-09T08:53:20+00:00",
        "last_poll": None,
        "stale": True,
        "api_version": "1",
    }


# ---------------------------------------------------------------- errors


async def test_view_errors_become_tool_errors(fake):
    fake.fail["line_view"] = views.ViewError(404, "unknown_line", "There is no line 'nope'.")
    fake.fail["disruptions_view"] = views.ViewError(400, "range_too_long", "A range is at most 366 days.")
    fake.fail["coverage_view"] = views.ViewError(400, "range_too_long", "A range is at most 90 days.")
    app, lifespan = build()
    async with session(app, lifespan) as s:
        line = await s.call_tool("line_status", {"line": "nope"})
        many = await s.call_tool("disruptions", {"from": "2020-01-01", "to": "2026-01-01"})
        cover = await s.call_tool("coverage", {"from": "2020-01-01"})
    assert line.isError and many.isError and cover.isError
    assert line.content[0].text == "unknown_line (404): There is no line 'nope'. list_lines gives the line ids."
    assert line.structuredContent["error"] == {
        "code": "unknown_line",
        "message": "There is no line 'nope'. list_lines gives the line ids.",
        "status": 404,
    }
    assert line.structuredContent["meta"]["attribution"] == ATTRIBUTION
    assert many.content[0].text == "range_too_long (400): A range is at most 366 days."
    assert many.structuredContent["error"]["status"] == 400
    assert cover.structuredContent["error"]["code"] == "range_too_long"


async def test_a_404_without_a_line_gets_no_line_hint(fake):
    fake.fail["stats_view"] = views.ViewError(404, "no_data", "Nothing recorded yet.")
    app, lifespan = build()
    async with session(app, lifespan) as s:
        result = await s.call_tool("period_stats", {"span": "7d"})
    assert result.isError
    assert result.content[0].text == "no_data (404): Nothing recorded yet."


async def test_the_line_hint_replaces_a_pointer_to_a_rest_route(fake):
    fake.fail["line_view"] = views.ViewError(404, "unknown_line", "No line foo is known. /v1/lines lists them.")
    fake.fail["disruptions_view"] = views.ViewError(404, "unknown_line", "No line foo is known. /v1/lines lists them.")
    app, lifespan = build()
    async with session(app, lifespan) as s:
        one = await s.call_tool("line_status", {"line": "foo"})
        many = await s.call_tool("disruptions", {"line": "foo"})
    for result in (one, many):
        assert result.structuredContent["error"]["message"] == "No line foo is known. list_lines gives the line ids."
        assert "/v1/" not in result.content[0].text


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("No line x is known. /v1/lines lists them.", "No line x is known. See list_lines."),
        ("No line x is known.", "No line x is known. See list_lines."),
        (
            "See /v1/lines for the ids. No line x is known.",
            "See /v1/lines for the ids. No line x is known. See list_lines.",
        ),
    ],
)
def test_tool_message(message, expected):
    hint = "See list_lines."
    assert tifa_mcp._tool_message(message, 404, hint) == expected
    assert tifa_mcp._tool_message(message, 400, hint) == message
    assert tifa_mcp._tool_message(message, 404, "") == message


BAD_ARGUMENTS = [
    ("disruptions", {"limit": 101}, "limit"),
    ("disruptions", {"limit": 0}, "limit"),
    ("disruptions", {"category": "bogus"}, "category"),
    ("disruptions", {"cause": "aliens"}, "cause"),
    ("disruptions", {"from": "x" * 70}, "from"),
    ("disruptions", {"from": True}, "from"),
    ("disruptions", {"from": -1}, "from"),
    ("disruptions", {"from": 1.5}, "from"),
    ("disruptions", {"to": 10**11}, "to"),
    ("disruptions", {"cursor": ""}, "cursor"),
    ("coverage", {"from": False}, "from"),
    ("current_status", {"mode": "bus"}, "mode"),
    ("line_status", {}, "line"),
    ("line_status", {"line": ""}, "line"),
    ("period_stats", {"span": "2d"}, "span"),
    ("period_stats", {"span": "7d", "end": "yesterday"}, "end"),
    ("period_stats", {}, "span"),
]


@pytest.mark.parametrize(("tool", "arguments", "name"), BAD_ARGUMENTS)
async def test_bad_arguments_are_refused_before_any_view_runs(fake, tool, arguments, name):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        result = await s.call_tool(tool, arguments)
    assert result.isError
    assert fake.calls == []
    text = result.content[0].text
    assert text.startswith(f"bad_arguments (400): The arguments do not fit {tool}: {name}: "), text
    body = result.structuredContent
    assert body["error"]["code"] == "bad_arguments" and body["error"]["status"] == 400
    assert text == f"bad_arguments (400): {body['error']['message']}"
    assert (body["meta"]["attribution"], body["meta"]["notice"]) == (ATTRIBUTION, NOTICE)
    assert body["meta"]["last_poll"] == "2025-10-09T08:54:00+00:00"  # the read model's meta, as for any result
    assert "pydantic" not in text and "xxxxxxxx" not in text  # no SDK wording, no echo of what was sent


@pytest.mark.parametrize("value", ["[" * 60_000, "1" * 5_000])
async def test_argument_text_the_sdk_cannot_read_is_a_bad_argument(fake, value, caplog):
    # FastMCP reads argument text that looks like JSON before validating it: deep nesting raises RecursionError
    # there and an over-long integer ValueError, neither a JSONDecodeError
    app, lifespan = build()
    with caplog.at_level(logging.WARNING):
        async with session(app, lifespan) as s:
            result = await s.call_tool("disruptions", {"line": value})
    assert result.isError and fake.calls == []
    assert result.structuredContent["error"] == {
        "code": "bad_arguments",
        "message": "The arguments of disruptions could not be read.",
        "status": 400,
    }
    assert result.structuredContent["meta"]["attribution"] == ATTRIBUTION
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_unknown_tool_is_an_error_in_the_envelope(fake):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        result = await s.call_tool("delete_everything", {})
        long = await s.call_tool("x" * 500, {})
    assert result.isError
    assert result.content[0].text == "unknown_tool (404): Unknown tool: delete_everything. tools/list lists them."
    assert result.structuredContent["error"]["code"] == "unknown_tool"
    assert result.structuredContent["meta"]["attribution"] == ATTRIBUTION
    assert "x" * 70 not in long.content[0].text  # the name is clipped
    assert fake.calls == []


async def test_a_failure_before_the_view_is_an_internal_error(fake, monkeypatch, caplog):
    def broken(line: str) -> str:
        raise RuntimeError("internal detail /var/state")

    monkeypatch.setattr(tifa_mcp, "_line", broken)
    app, lifespan = build()
    with caplog.at_level(logging.ERROR, logger="tifa.api.mcp"):
        async with session(app, lifespan) as s:
            result = await s.call_tool("line_status", {"line": "central"})
    assert result.isError and result.content[0].text.startswith("internal_error (500):")
    assert result.structuredContent["meta"]["attribution"] == ATTRIBUTION
    assert "internal detail" not in json.dumps(result.model_dump(mode="json"))
    assert any("line_status" in r.getMessage() and r.exc_info for r in caplog.records)


async def test_a_failing_meta_hook_still_gives_the_attribution(fake):
    def meta(rm):
        raise RuntimeError("no meta")

    app, lifespan = build(meta=meta)
    async with session(app, lifespan) as s:
        result = await s.call_tool("disruptions", {"limit": 0})
    assert result.structuredContent["error"]["code"] == "bad_arguments"
    assert result.structuredContent["meta"] == tifa_mcp.STATIC_META


def test_instructions_and_period_stats_say_its_times_are_epoch_seconds():
    assert "period_stats, whose times are epoch seconds" in tifa_mcp.INSTRUCTIONS.replace("\n", " ")


async def test_period_stats_description_says_its_times_are_epoch_seconds(fake):
    app, lifespan = build()
    async with session(app, lifespan) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert "epoch seconds" in tools["period_stats"].description
    assert "epoch seconds" not in tools["disruptions"].description  # its times are ISO 8601


async def test_unexpected_failures_are_logged_and_not_shown(fake, caplog):
    fake.fail["causes_view"] = RuntimeError("internal detail /var/state")
    app, lifespan = build()
    with caplog.at_level(logging.ERROR, logger="tifa.api.mcp"):
        async with session(app, lifespan) as s:
            result = await s.call_tool("causes", {})
    assert result.isError
    assert result.content[0].text.startswith("internal_error (500):")
    assert result.structuredContent["meta"]["attribution"] == ATTRIBUTION
    assert "internal detail" not in json.dumps(result.model_dump(mode="json"))
    assert any("causes_view" in r.getMessage() and r.exc_info for r in caplog.records)


# ---------------------------------------------------------------- HTTP


async def test_get_is_405_and_opens_no_event_stream(fake):
    app, lifespan = build()
    async with lifespan(), http(app) as client:
        r = await client.get("/mcp", headers={"Accept": "text/event-stream"})
        assert r.status_code == 405
        assert r.headers["allow"] == "POST"
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["error"]["message"].startswith("Method Not Allowed")
        for method in ("DELETE", "PUT", "HEAD"):
            assert (await client.request(method, "/mcp")).status_code == 405


async def test_post_answers_in_json_without_a_session(fake):
    app, lifespan = build()
    async with lifespan(), http(app) as client:
        r = await client.post("/mcp", json=INIT, headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "mcp-session-id" not in r.headers
        assert r.json()["result"]["serverInfo"]["name"] == "tifa"
        # stateless: a tool call needs no initialize first
        r = await client.post("/mcp", json=call_body("causes"), headers=JSON_HEADERS)
        assert r.status_code == 200
        assert r.json()["result"]["structuredContent"]["data"] == {"view": "causes_view"}


async def test_mounts_on_the_api_app_as_its_docstring_says(fake):
    from fastapi import FastAPI

    mcp_app, mcp_lifespan = build()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_lifespan(app):
            yield

    app = FastAPI(lifespan=lifespan)
    app.add_route("/mcp", mcp_app, include_in_schema=False)
    async with app.router.lifespan_context(app), http(app) as client:
        r = await client.post("/mcp", json=INIT, headers=JSON_HEADERS)
        assert r.status_code == 200  # not a redirect to /mcp/
        assert r.json()["result"]["serverInfo"]["name"] == "tifa"
        assert (await client.get("/mcp")).status_code == 405
    assert "/mcp" not in app.openapi()["paths"]


async def test_tool_errors_for_data_that_is_not_strict_json(fake, monkeypatch):
    monkeypatch.setattr(views, "causes_view", lambda rm: {"x": float("nan")})
    app, lifespan = build()
    async with session(app, lifespan) as s:
        result = await s.call_tool("causes", {})
    assert result.isError and result.content[0].text.startswith("internal_error")


async def test_not_running_without_the_lifespan(fake):
    app, _ = build()
    async with http(app) as client:
        r = await client.post("/mcp", json=INIT, headers=JSON_HEADERS)
    assert r.status_code == 503


async def test_the_lifespan_can_run_again(fake):
    app, lifespan = build()
    for _ in range(2):
        async with session(app, lifespan) as s:
            assert body_of(await s.call_tool("causes", {}))["data"] == {"view": "causes_view"}


UNREADABLE = [
    pytest.param(b"[" * 60_000, id="nested-arrays"),
    pytest.param(b'{"a":' * 12_000, id="nested-objects"),
    pytest.param(b'{"a": "\xc3\x28"}', id="bad-utf8"),
    pytest.param(b"1" * 5_000, id="huge-integer"),
    pytest.param(b"{bad", id="malformed"),
    pytest.param(b"", id="empty"),
]


@pytest.mark.parametrize("body", UNREADABLE)
async def test_bodies_that_are_not_json_get_a_parse_error_uncharged_and_unlogged(fake, body, caplog):
    charges = Charges()
    app, lifespan = build(charges)
    with caplog.at_level(logging.WARNING):
        async with lifespan(), http(app) as client:
            r = await client.post("/mcp", content=body, headers=JSON_HEADERS)
            after = await client.post("/mcp", json=call_body("causes"), headers=JSON_HEADERS)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700  # JSON-RPC parse error
    assert charges.costs == [3] and [c[0] for c in fake.calls] == ["causes_view"]  # only the good call
    assert after.status_code == 200
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


@pytest.mark.parametrize("body", [b"[" * 60_000, b'"\xc3\x28"', b"1" * 5_000, b"{", b'{"method": "tools/call"}'])
def test_parse_never_raises(body):
    message = tifa_mcp._parse(body)
    assert tifa_mcp._is_tool_call(message) is (message is not tifa_mcp._UNREADABLE)


def big_answer(rm: Any) -> dict:
    return {"rows": [{"line": "central", "reason": f"Minor delays, row {i}."} for i in range(400)]}


@pytest.mark.parametrize(
    ("accept", "gzipped"),
    [("gzip, deflate", True), ("br;q=1, gzip;q=0.5", True), ("gzip;q=0", False), ("identity", False), (None, False)],
)
async def test_large_answers_are_gzipped_when_accepted(fake, monkeypatch, accept, gzipped):
    monkeypatch.setattr(views, "causes_view", big_answer)
    app, lifespan = build()
    headers = {**JSON_HEADERS, "Accept-Encoding": accept} if accept else JSON_HEADERS
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 50000))
    async with lifespan(), httpx.AsyncClient(transport=transport, base_url=f"http://{HOST}") as client:
        if accept is None:
            client.headers.pop("accept-encoding", None)
        r = await client.post("/mcp", json=call_body("causes"), headers=headers)
    assert r.status_code == 200
    assert r.json()["result"]["structuredContent"]["data"] == big_answer(None)
    sent = int(r.headers["content-length"])
    if gzipped:
        assert r.headers["content-encoding"] == "gzip"
        assert sent * 5 < len(r.content)
    else:
        assert "content-encoding" not in r.headers
        assert sent == len(r.content)


async def test_small_answers_and_errors_are_not_gzipped(fake):
    app, lifespan = build()
    async with lifespan(), http(app) as client:
        small = await client.post("/mcp", json=call_body("causes"), headers=JSON_HEADERS)
        refused = await client.get("/mcp", headers={"Accept-Encoding": "gzip"})
    assert small.status_code == 200 and "content-encoding" not in small.headers
    assert refused.status_code == 405 and "content-encoding" not in refused.headers


async def test_large_bodies_are_refused_uncharged(fake):
    charges = Charges()
    app, lifespan = build(charges)
    big = call_body("disruptions", {"line": "x" * (70 * 1024)})
    async with lifespan(), http(app) as client:
        r = await client.post("/mcp", content=json.dumps(big), headers=JSON_HEADERS)
    assert r.status_code == 413
    assert charges.costs == [] and fake.calls == []


# ---------------------------------------------------------------- hosts and origins


@pytest.mark.parametrize(
    ("base", "status"),
    [
        (f"http://{HOST}", 200),
        (f"https://{HOST}", 200),
        (f"https://{HOST}:443", 200),
        ("http://localhost:8080", 200),
        ("http://127.0.0.1", 200),
        ("http://TIFA.Example.ORG", 200),
        ("http://evil.example", 421),
        (f"http://{HOST}.evil.example", 421),
        (f"http://evil.{HOST}", 421),
    ],
)
async def test_host_allow_list(fake, base, status):
    app, lifespan = build()
    async with lifespan(), http(app, base) as client:
        r = await client.post("/mcp", json=INIT, headers=JSON_HEADERS)
    assert r.status_code == status


async def test_on_cloud_run_only_the_configured_hosts(fake):
    app, lifespan = build(settings=settings(service="tifa-api"))
    async with lifespan():
        for base, status in ((f"https://{HOST}", 200), ("http://localhost:8080", 421), ("http://127.0.0.1", 421)):
            async with http(app, base) as client:
                r = await client.post("/mcp", json=INIT, headers=JSON_HEADERS)
            assert r.status_code == status, base


async def test_no_configured_hosts_on_cloud_run_refuses_all(fake, caplog):
    with caplog.at_level(logging.WARNING, logger="tifa.api.mcp"):
        app, lifespan = build(settings=Settings(service="tifa-api"))
    assert any("TIFA_ALLOWED_HOSTS" in r.getMessage() for r in caplog.records)
    async with lifespan(), http(app, "https://anything.example") as client:
        assert (await client.post("/mcp", json=INIT, headers=JSON_HEADERS)).status_code == 421


@pytest.mark.parametrize("origin", [f"https://{HOST}", "http://localhost:6274", "https://elsewhere.example"])
async def test_any_origin_is_served_as_the_rest_api_is(fake, origin):
    app, lifespan = build()
    async with lifespan(), http(app) as client:
        r = await client.post("/mcp", json=INIT, headers={**JSON_HEADERS, "Origin": origin})
    assert r.status_code == 200


async def test_a_bad_host_is_refused_before_the_method_or_the_charge(fake):
    charges = Charges()
    app, lifespan = build(charges)
    async with lifespan(), http(app, "http://evil.example") as client:
        assert (await client.get("/mcp")).status_code == 421
        r = await client.post("/mcp", json=call_body("causes"), headers=JSON_HEADERS)
    assert r.status_code == 421
    assert r.json()["error"]["message"] == "Invalid Host header"
    assert charges.costs == [] and fake.calls == []


def test_allowed_hosts_lists():
    s = Settings(allowed_hosts=("a.example", "b.example:8443", "c.example:*"))
    assert tifa_mcp.allowed_hosts(s) == [
        "a.example",
        "a.example:*",
        "b.example:8443",
        "c.example:*",
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    ]
    cloud = Settings(allowed_hosts=("a.example",), service="tifa-api")
    assert tifa_mcp.allowed_hosts(cloud) == ["a.example", "a.example:*"]


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("a.example", True),
        ("A.Example", True),
        ("a.example:443", True),
        ("b.example:8443", True),
        ("b.example", False),
        ("b.example:8444", False),
        ("c.example:9000", True),
        ("c.example", False),
        ("a.example.evil", False),
        ("xa.example", False),
        ("", False),
        (None, False),
    ],
)
def test_host_matching(host, ok):
    allowed = tifa_mcp.allowed_hosts(
        Settings(allowed_hosts=("a.example", "b.example:8443", "c.example:*"), service="tifa-api")
    )
    assert tifa_mcp.host_allowed(host, allowed) is ok


# ---------------------------------------------------------------- fair use


async def test_only_tool_calls_are_charged(fake):
    charges = Charges()
    app, lifespan = build(charges)
    async with session(app, lifespan) as s:
        await s.list_tools()
        assert charges.costs == []
        await s.call_tool("causes", {})
        await s.call_tool("line_status", {"line": "central"})
    assert charges.costs == [3, 3]
    assert charges.clients[0] == ("192.0.2.10", 50000)


async def test_the_cost_comes_from_the_limits(fake):
    charges = Charges()
    app, lifespan = build(charges, settings=settings(limits=Limits(cost_mcp_call=7)))
    async with session(app, lifespan) as s:
        await s.call_tool("causes", {})
    assert charges.costs == [7]


async def test_a_refused_charge_answers_for_the_call(fake):
    charges = Charges(refuse=Response("slow down", status_code=429, headers={"Retry-After": "7"}))
    app, lifespan = build(charges)
    async with lifespan(), http(app) as client:
        r = await client.post("/mcp", json=INIT, headers=JSON_HEADERS)
        assert r.status_code == 200  # initialize is not a tool call
        r = await client.post("/mcp", json=call_body("period_stats", {"span": "365d"}), headers=JSON_HEADERS)
    assert (r.status_code, r.headers["retry-after"]) == (429, "7")
    assert charges.costs == [3]
    assert fake.calls == []


async def test_an_async_charge_is_awaited(fake):
    costs = []

    async def charge(request, cost):
        costs.append(cost)
        return Response(status_code=503, headers={"Retry-After": "60"}) if len(costs) > 1 else None

    app, lifespan = build(charge)
    async with lifespan(), http(app) as client:
        first = await client.post("/mcp", json=call_body("causes"), headers=JSON_HEADERS)
        second = await client.post("/mcp", json=call_body("causes", request_id=3), headers=JSON_HEADERS)
    assert (first.status_code, second.status_code) == (200, 503)
    assert costs == [3, 3]
    assert [c[0] for c in fake.calls] == ["causes_view"]


async def test_without_a_charge_hook_nothing_is_charged_here(fake):
    app, lifespan = tifa_mcp.build_mcp_app(RM, settings=settings())
    async with session(app, lifespan) as s:
        body_of(await s.call_tool("causes", {}))
    assert [c[0] for c in fake.calls] == ["causes_view"]


# ---------------------------------------------------------------- end to end on a MemoryStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "core"
T0 = 1_792_396_800  # 2026-10-19 08:00 UTC


def _all_good(raw: list) -> list:
    """The payload with every line on Good Service."""
    good = json.loads(json.dumps(raw))
    for line in good:
        line["lineStatuses"] = [{"statusSeverity": 10, "statusSeverityDescription": "Good Service"}]
    return good


def memory_store() -> MemoryStore:
    """A store after four polls a minute apart of the sample TfL payload, the second with every line good: some
    episodes closed (in the month), others open (in the state)."""
    raw = json.loads((FIXTURES / "tfl_status_sample.json").read_text())
    store, state, closed, lines = MemoryStore(), episodes.empty_state(), [], {}
    month = month_key(T0)
    for i, payload in enumerate((raw, _all_good(raw), raw, raw)):
        ts = T0 + 60 * i
        obs, meta = episodes.observe(payload)
        state, ended, _ = episodes.advance(state, obs, meta, ts)
        closed += ended
        for lid, (name, mode) in meta.items():
            lines.setdefault(lid, {"name": name, "mode": mode, "first_seen": ts})["last_seen"] = ts
        body = months.merge_month(months.new_month(month), closed, [[T0, ts]])
        write = PollWrite(
            state=State(
                last_poll=ts,
                last_slot=ts // 60,
                run=[T0, ts],
                open=state["open"],
                lines=json.loads(json.dumps(lines)),
                status=tfl.current(payload),
                status_etag=f"etag-{i}",
                causes_sig=CAUSES_SIG,
                tfl_auth="ok",
                updated_at=ts,
            ),
            months={month: body},
            poll_at=ts,
        )
        assert store.poll_transaction(ts // 60, lambda current, write=write: write) == "applied"
    assert closed and state["open"]
    return store


async def test_every_tool_against_a_memory_store():
    rm = StoreReadModel(memory_store(), clock=lambda: T0 + 240)
    app, lifespan = tifa_mcp.build_mcp_app(rm, settings=settings(), charge=Charges())
    async with session(app, lifespan) as s:
        lines = body_of(await s.call_tool("list_lines", {}))["data"]
        ids = [line["id"] for line in lines["lines"]]
        assert "central" in ids and "dlr" in ids
        now = body_of(await s.call_tool("current_status", {}))
        assert now["data"]["total"] == 19
        assert now["meta"]["last_poll"] == "2026-10-19T08:03:00+00:00"
        tube = body_of(await s.call_tool("current_status", {"mode": "tube"}))["data"]
        assert tube["lines"] and {line["mode"] for line in tube["lines"]} == {"tube"}
        for tool, arguments in (
            ("line_status", {"line": "central"}),
            ("disruptions", {"limit": 5}),
            ("disruptions", {"from": "2026-10-19", "to": "2026-10-20", "planned": False, "limit": 100}),
            ("period_stats", {"span": "1d"}),
            ("period_stats", {"span": "7d", "line": "central"}),
            ("coverage", {}),
            ("coverage", {"from": T0, "to": T0 + 240}),
            ("causes", {}),
        ):
            body = body_of(await s.call_tool(tool, arguments))
            assert (body["meta"]["attribution"], body["meta"]["notice"]) == (ATTRIBUTION, NOTICE), tool
        causes = body_of(await s.call_tool("causes", {}))["data"]
        assert [c["code"] for c in causes["causes"]] == [code for code, _, _ in CAUSES]
        first = body_of(await s.call_tool("disruptions", {"limit": 1}))["data"]
        assert first["count"] == 1 and first["total"] > 1 and first["next_cursor"]
        second = body_of(await s.call_tool("disruptions", {"limit": 1, "cursor": first["next_cursor"]}))["data"]
        assert second["episodes"] and second["episodes"] != first["episodes"]
        by_epoch = body_of(await s.call_tool("disruptions", {"from": T0, "to": T0 + 240, "limit": 100}))["data"]
        by_iso = body_of(
            await s.call_tool("disruptions", {"from": "2026-10-19T08:00:00Z", "to": str(T0 + 240), "limit": 100})
        )["data"]
        assert by_epoch == by_iso and by_epoch["total"] == first["total"]
        missing = await s.call_tool("line_status", {"line": "no-such-line"})
        bad = await s.call_tool("disruptions", {"from": "2026-10-20", "to": "2026-10-19"})
    assert missing.isError and "(404)" in missing.content[0].text
    assert missing.content[0].text.endswith("is known. list_lines gives the line ids.")  # the REST hint replaced
    assert "/v1/" not in missing.content[0].text
    assert bad.isError and "(400)" in bad.content[0].text


def test_memory_store_fixture_holds_open_and_closed_episodes():
    store = memory_store()
    state = store.get_state()
    doc = store.get_month(month_key(T0))
    assert state.last_poll == T0 + 180 and state.open
    assert doc.payload["episodes"] and all(row[2] == T0 + 60 for row in doc.payload["episodes"])
    assert doc.payload["watched"]["a"] == [[T0, T0 + 180]]
    assert store.get_polls("2026-10-19").at == [T0, T0 + 60, T0 + 120, T0 + 180]


async def test_through_the_api_app_with_its_gate_pricing_tool_calls():
    from tifa.api.app import create_app

    limits = Limits(ip_bucket=11, ip_refill=0.0001)
    s = settings(limits=limits)
    api = create_app(
        s,
        store=memory_store(),
        clock=lambda: T0 + 240,
        mcp_factory=lambda rm: tifa_mcp.build_mcp_app(rm, settings=s),
    )
    async with api.router.lifespan_context(api), http(api) as client:
        r = await client.get("/mcp", headers={"Accept": "text/event-stream"})
        assert r.status_code == 405 and "POST" in r.headers["allow"]  # no event stream
        remaining = []
        for message in (INIT, call_body("causes"), call_body("line_status", {"line": "central"}, 3)):
            r = await client.post("/mcp", json=message, headers=JSON_HEADERS)
            assert r.status_code == 200, r.text
            remaining.append(int(r.headers["ratelimit-remaining"]))
        result = r.json()["result"]
        assert not result["isError"]
        assert result["structuredContent"]["meta"]["attribution"] == ATTRIBUTION
        assert result["structuredContent"]["data"]["id"] == "central"
        assert remaining == [9, 6, 3]  # initialize 1, each tool call 3
        r = await client.post("/mcp", json=call_body("period_stats", {"span": "7d"}, 4), headers=JSON_HEADERS)
        assert r.status_code == 200 and int(r.headers["ratelimit-remaining"]) == 0
        r = await client.post("/mcp", json=call_body("causes", request_id=5), headers=JSON_HEADERS)
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


async def test_through_the_api_app_egress_counts_the_gzipped_bytes():
    from tifa.api.app import create_app

    s = settings()
    api = create_app(
        s,
        store=memory_store(),
        clock=lambda: T0 + 240,
        monotonic=lambda: 1000.0,  # no refill during the test
        mcp_factory=lambda rm: tifa_mcp.build_mcp_app(rm, settings=s),
    )
    limiter = api.state.tifa.limiter
    async with api.router.lifespan_context(api), http(api) as client:
        before = limiter.egress_left()
        r = await client.post(
            "/mcp",
            json=call_body("period_stats", {"span": "7d"}),
            headers={**JSON_HEADERS, "Accept-Encoding": "gzip"},
        )
        used = before - limiter.egress_left()
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip"
    assert not r.json()["result"]["isError"]
    sent = int(r.headers["content-length"])
    assert sent * 3 < len(r.content)
    assert sent <= used < sent + 2048  # the compressed body and the headers
