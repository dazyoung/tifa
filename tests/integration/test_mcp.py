"""The MCP server as the API app mounts it (the default factory, with the app's own settings), driven by the MCP
SDK's client over the recorded days: every tool answers, with the same data as the matching REST route."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.integration import scenario as sc
from tests.integration.conftest import BASE, make_api
from tifa.core.causes import CAUSES

ATTRIBUTION = "Powered by TfL Open Data"
NOTICE = "Not affiliated with or endorsed by Transport for London."


@contextlib.asynccontextmanager
async def connected(world) -> AsyncIterator[tuple[ClientSession, httpx.AsyncClient]]:
    app, _, _ = make_api(world.store, sc.NOW, mcp_factory="default")
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 50000))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url=BASE) as http:
        async with streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session, http


def body(result) -> dict:
    assert not result.isError, result.content
    assert json.loads(result.content[0].text) == result.structuredContent
    meta = result.structuredContent["meta"]
    assert meta["attribution"] == ATTRIBUTION and meta["notice"] == NOTICE and meta["sources"] == ["a"]
    return result.structuredContent["data"]


async def rest(http: httpx.AsyncClient, path: str, **params) -> dict:
    r = await http.get(path, params=params or None)
    assert r.status_code == 200, r.text[:300]
    return r.json()["data"]


async def test_every_tool_gives_what_the_rest_api_gives(world):
    async with connected(world) as (s, http):
        tools = {t.name for t in (await s.list_tools()).tools}
        assert tools == {
            "list_lines",
            "current_status",
            "line_status",
            "disruptions",
            "period_stats",
            "coverage",
            "causes",
        }

        assert body(await s.call_tool("list_lines", {})) == await rest(http, "/v1/lines")
        assert body(await s.call_tool("current_status", {})) == await rest(http, "/v1/status")
        assert body(await s.call_tool("line_status", {"line": "Central"})) == await rest(http, "/v1/lines/central")
        window = {"from": "2026-10-24", "to": "2026-10-26"}
        assert body(await s.call_tool("disruptions", {**window, "limit": 100})) == await rest(
            http, "/v1/disruptions", **window, limit=100
        )
        for span, end in (("1d", "2026-10-25"), ("7d", "2026-10-26"), ("1d", "now")):
            assert body(await s.call_tool("period_stats", {"span": span, "end": end})) == await rest(
                http, "/v1/stats", span=span, end=end
            )
        assert body(await s.call_tool("coverage", window)) == await rest(http, "/v1/coverage", **window)
        causes = body(await s.call_tool("causes", {}))["causes"]
        assert causes == [{"code": c, "label": label} for c, label, _ in CAUSES]

        # the host list came from the app's settings: its own name is served, any other is refused
        init = {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        r = await http.post("/mcp", json=init, headers={**headers, "Host": "rebound.example"})
        assert r.status_code == 421


async def test_the_tools_answer_questions_about_the_three_days(world):
    async with connected(world) as (s, _):
        now = body(await s.call_tool("current_status", {"mode": "elizabeth-line"}))
        assert [x["id"] for x in now["lines"]] == ["elizabeth"] and now["disrupted"] == 1
        assert [(e["line"], e["planned"], e["cause"]) for e in now["open"]] == [("elizabeth", True, "engineering")]

        sunday = body(await s.call_tool("period_stats", {"span": "1d", "end": "2026-10-25"}))
        assert sunday["t1"] - sunday["t0"] == 25 * 3600
        assert sunday["longest_unplanned"]["line"] == "jubilee"

        unknown = body(await s.call_tool("disruptions", {"from": "2026-10-24", "to": "2026-10-26", "cause": "unknown"}))
        assert [e["line"] for e in unknown["episodes"]] == ["liberty"]

        pages, cursor = [], None
        while True:
            args = {"from": "2026-10-24", "to": "2026-10-26", "limit": 7, **({"cursor": cursor} if cursor else {})}
            page = body(await s.call_tool("disruptions", args))
            pages += page["episodes"]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(pages) == page["total"] == 18

        coverage = body(await s.call_tool("coverage", {"from": "2026-10-25", "to": "2026-10-25"}))
        assert [g["minutes"] for g in coverage["gaps"]] == [21]  # the twenty failed minutes, poll to poll

        missing = await s.call_tool("line_status", {"line": "circle-hammersmith-city"})
        assert missing.isError and missing.structuredContent["error"]["status"] == 404
