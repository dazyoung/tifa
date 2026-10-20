# MCP server

Tifa serves its data to AI assistants and agents through a remote [Model Context Protocol](https://modelcontextprotocol.io)
server, next to the REST API and from the same data. It is open: no key and no sign-in, under the same fair-use
limits as the REST API.

- **Endpoint:** `<base-url>/mcp`, where `<base-url>` is the API's address (the same one that serves `/v1/...`).
- **Transport:** streamable HTTP, stateless, JSON responses. Every request is one `POST` with one JSON-RPC message
  and gets one JSON response, gzipped when the client sends `Accept-Encoding: gzip` (most clients do). There are
  no sessions and no event streams: `GET` and `DELETE` answer `405`.
- **Tools:** seven read-only tools (below). No resources or prompts.

Data: Powered by TfL Open Data. Tifa is not affiliated with or endorsed by Transport for London. When you show
answers built from Tifa, credit "Powered by TfL Open Data"; every tool result carries that line in its `meta`.

## Adding Tifa to a client

### Claude Code

```sh
claude mcp add --transport http tifa <base-url>/mcp
```

Add `--scope user` to have it in every project, or `--scope project` to share it through the project's `.mcp.json`.
Then ask, for example, "Is the Central line running normally?" or "Which line had the most unplanned disruption
last month?". `claude mcp list` shows whether it connected.

### Claude apps (desktop, web)

Settings, Connectors, "Add custom connector": name it Tifa and give `<base-url>/mcp` as the URL. No authentication
is needed.

### VS Code

In `.vscode/mcp.json` (or your user configuration):

```json
{
  "servers": {
    "tifa": { "type": "http", "url": "<base-url>/mcp" }
  }
}
```

### Cursor and other clients

Most clients take a server entry with a URL, for example in Cursor's `mcp.json`:

```json
{
  "mcpServers": {
    "tifa": { "url": "<base-url>/mcp" }
  }
}
```

A client that only starts local (stdio) servers can reach it through a bridge such as
`npx mcp-remote <base-url>/mcp`.

### MCP Inspector

`npx @modelcontextprotocol/inspector`, then choose the "Streamable HTTP" transport and enter `<base-url>/mcp`.

### From code

With the official Python SDK (`mcp` 1.27 or later, which has `streamable_http_client`):

```python
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main() -> None:
    async with streamable_http_client("<base-url>/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("current_status", {"mode": "tube"})
            print(result.structuredContent["data"]["disrupted"], "tube lines disrupted")


asyncio.run(main())
```

Or by hand: as the server is stateless, a tool call needs no `initialize` first.

```sh
curl -s --compressed <base-url>/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"line_status","arguments":{"line":"victoria"}}}'
```

## Results

A successful result has the data as JSON text (for the model) and as `structuredContent` (for programs), both
`{"data": ..., "meta": ...}`, the same envelope as the REST API:

```json
{
  "data": { "lines": [ ... ], "disrupted": 2, "total": 19, "open": [ ... ], "last_poll": "2026-10-19T08:03:00+00:00", "stale": false },
  "meta": {
    "attribution": "Powered by TfL Open Data",
    "notice": "Not affiliated with or endorsed by Transport for London.",
    "licence": "...",
    "sources": ["a"],
    "generated_at": "2026-10-19T08:04:00+00:00",
    "last_poll": "2026-10-19T08:03:00+00:00",
    "stale": false,
    "api_version": "1"
  }
}
```

Times are ISO 8601 in UTC, except in `period_stats`, which gives its times (`t0`, `t1`, `p0`, starts and ends,
bins, unwatched spans) as epoch seconds (UTC numbers), as `/v1/stats` does; only its `period.first` is ISO 8601.
Days and hours meant for people (busiest day, hour of the day) are London local time. `stale` is true when there
has been no poll for over 5 minutes, so the status may be out of date.

Every tool error is a result with `isError: true` in the same shape: its text is `code (status): message` and its
`structuredContent` is `{"error": {"code", "message", "status"}, "meta": ...}`, with the attribution in `meta`.

| Code | Status | When |
| --- | --- | --- |
| the REST API's (`unknown_line`, `range_too_long`, `bad_range`, `bad_time`, ...) | 400, 404, 503 | Tifa cannot answer: an unknown line, a range that is too long, the history store unavailable |
| `bad_arguments` | 400 | The arguments do not fit the tool's schema (a limit over 100, a category that does not exist, a missing `line`); refused before anything runs. The message names each argument and what is wrong with it |
| `unknown_tool` | 404 | No tool has that name |
| `internal_error` | 500 | Anything unexpected (logged on the server; no detail is shown) |

For example `unknown_line (404): No line foo is known. list_lines gives the line ids.` or
`bad_arguments (400): The arguments do not fit disruptions: limit: Input should be less than or equal to 100.`

A request that is not a JSON-RPC message at all gets an HTTP error with a JSON-RPC error body instead of a result:
`400` with code `-32700` when the body is not JSON, `413` when it is over 64 KiB, `405` for a method other than
`POST`, `421` for a `Host` that is not allowed, `429` or `503` from the fair-use limits.

## Tools

| Tool | Arguments | What it gives |
| --- | --- | --- |
| `list_lines` | none | Every line: `id`, `name`, `mode` (tube, elizabeth-line, overground, dlr), `shared_with`, first and last seen |
| `current_status` | `mode`? | Every line's status now, the disruptions open now, `last_poll` and `stale` |
| `line_status` | `line` | One line in depth: open disruptions, figures over 24 hours to 90 days, latest disruptions with reasons and causes, start times by London hour |
| `disruptions` | `line`?, `from`?, `to`?, `category`?, `cause`?, `planned`?, `limit`? (1-100, default 25), `cursor`? | Recorded disruptions overlapping a range, oldest first, with `total` and `next_cursor` |
| `period_stats` | `span`, `end`? (default `now`), `line`? | Figures for a day to a year, for the network or one line, against the period before |
| `coverage` | `from`?, `to`? | When Tifa was watching: spans, share watched, gaps, polls per UTC day, failed polls |
| `causes` | none | The cause codes and labels |

### `list_lines`

The lines Tifa knows, in display order (mode, then name). Use the `id` values with the other tools (`central`,
`elizabeth`, `hammersmith-city`, `dlr`, `weaver`, ...).

### `current_status`

- `mode` (optional): `tube`, `elizabeth-line`, `overground` or `dlr`, for that mode's lines only (with
  `disrupted` and `total` counted for that mode).

For each line: `status` (TfL's wording, for example "Minor Delays"), the worst `severity` code, `reasons` (TfL's
messages), `good` (good service) and `live` (a live problem rather than planned works or information). Lines with
live problems come first, then planned works, then good service. `open` lists the disruptions in progress, with
start time and cause.

### `line_status`

- `line` (required): a line id from `list_lines`. Case and surrounding spaces do not matter.

The line's open disruptions; for the last 24 hours, 7, 30 and 90 days the number of disruptions, minutes
disrupted and share of watched time disrupted; its latest disruptions with TfL's reasons and causes; and at which
London hours its unplanned disruptions start.

### `disruptions`

One row per spell of one severity on one line: `start`, `end` (null while it is still going on), `minutes`,
`severity`, `status`, `category`, `planned`, `cause`, `reason` (TfL's message) and `end_estimated` (true when
recording paused and the end is the last time it was seen).

- `line`: one line id.
- `from`, `to`: ISO 8601 times (UTC when there is no offset), epoch seconds (a whole number such as `1792396800`,
  or the same digits as text), or London dates (`YYYY-MM-DD`; a date as `to` includes that day). Default: the 30
  days to now. At most 366 days per call.
- `category`: `delays`, `suspended`, `closure`, `other` or `info` (information only; not counted in the figures).
- `cause`: a code from `causes`, or `unknown` for disruptions whose reason matched no cause.
- `planned`: `true` for planned works only, `false` for unplanned only.
- `limit`: rows per call, 1 to 100 (default 25).
- `cursor`: the `next_cursor` of the previous call, to get the next page (keep the other arguments the same).

`total` is how many disruptions match; `next_cursor` is null on the last page.

### `period_stats`

- `span` (required): `1d`, `7d`, `30d`, `90d` or `365d`.
- `end`: `now` (default), or a London date the period ends with (the whole day included).
- `line`: one line id, for that line's figures only.

Watched coverage, the share of time with good service, unplanned and planned disruption counts, line-time
disrupted, the change against the period before (only when both periods were at least 80% watched), the most
disrupted line, the longest unplanned disruption, the busiest day, the top causes, per-day (or per-week) bins and a
per-line table. `period.prev` and `period.next` give the `end` for the period before and after. Times in this
result are epoch seconds (see [Results](#results)).

### `coverage`

- `from`, `to`: as for `disruptions`. Default: the 7 days to now. At most 90 days.

For each published source, the spans it watched and the share of the range; the gaps nothing watched; and for
each UTC day the polls recorded (of 1,440) and the polls that failed. A quiet period with poor coverage was not
watched, not necessarily good.

### `causes`

The causes Tifa puts disruptions down to, read from TfL's reason text (signal failures, faulty trains, track
faults, power, customer incidents, strikes, weather, planned engineering and so on), in the order the rules are
tried. A disruption whose reason matched none has a null cause.

## Fair use

The MCP endpoint shares the REST API's limits (see `/v1/meta` for the live numbers). Each client address has a
bucket of 60 tokens refilled at 1 a second; a tool call costs 3, so a client can make up to about 20 tool calls a
minute sustained, more in a burst. Over the limit the endpoint answers HTTP `429` with `Retry-After` (seconds);
when the whole service is busy or has sent its hourly data allowance, `503` with `Retry-After: 60`.

- Accept gzip (`Accept-Encoding: gzip`): answers are several times smaller, and the data allowance counts the
  bytes sent.
- Prefer one `period_stats` or `disruptions` call over many small ones, and ask `disruptions` for the rows you
  need (`limit`, filters) rather than 100 at a time.
- The limits are per client address. A hosted connector, such as a custom connector added in the Claude apps,
  connects from its provider's servers rather than from your machine, so its users may share one allowance and
  meet `429` sooner. A client on your own machine (Claude Code, VS Code, Cursor, a bridge) has its own.

## For operators

- The `Host` header must be one of `TIFA_ALLOWED_HOSTS` (the deploy workflow puts the service's own `run.app`
  host names there, plus any custom domains from the repository variable), else `421`. A name without a port also
  matches with any explicit port. Away from Cloud Run, `localhost`, `127.0.0.1` and `[::1]` are accepted as well.
  This is the protection against DNS rebinding.
- Any `Origin` is accepted: the endpoint is public and read-only and takes no credentials, like the REST API,
  whose CORS is open.
- Request bodies over 64 KiB get `413`. A body that is not JSON (malformed, bad UTF-8, nested too deeply for the
  parser, an integer too long to convert) gets `400` with a JSON-RPC parse error from Tifa itself and never
  reaches the SDK, so it logs nothing.
- Answers of 1 KiB or more are gzipped for clients that accept gzip (level 6), as the REST API's are, so the
  hourly egress allowance counts the compressed bytes.
- To try it locally (an empty in-memory store unless you import data): `TIFA_ROLE=api uv run tifa`, then
  `claude mcp add --transport http tifa-local http://localhost:8080/mcp`.
- The server is built with the official `mcp` Python SDK (FastMCP, 1.x) in `src/tifa/api/mcp.py`; the tools call
  the same views as the REST routes (`src/tifa/api/views.py`).
