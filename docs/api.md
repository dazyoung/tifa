# REST API

Tifa's REST API is open: no key, no sign-up, no cookies. It is read-only (`GET` and `HEAD`), CORS is open to every
origin, and it is shared fairly between everyone through the [limits below](#fair-use). The same data is available
to AI assistants through the [MCP server](mcp.md).

- **Base URL:** `https://tifa-api-dh52427ooa-ew.a.run.app`
- **Interactive docs:** https://tifa-api-dh52427ooa-ew.a.run.app/docs (Swagger UI; your browser loads its script and stylesheet
  from the jsDelivr CDN).
- **OpenAPI document:** `https://tifa-api-dh52427ooa-ew.a.run.app/openapi.json`, also in the repository as [openapi.json](openapi.json),
  checked in CI.
- **Version:** every route is under `/v1`. Additions (new fields, new routes) can happen within `v1`; a change that
  breaks clients gets a new prefix. Clients should ignore fields they do not know.

What the data means (episodes, the GAP rule, categories, causes, London days) is in [data-model.md](data-model.md).

Data: Powered by TfL Open Data. Tifa is not affiliated with or endorsed by Transport for London.

## Responses

Every JSON response is an envelope:

```json
{
  "data": { ... },
  "meta": {
    "attribution": "Powered by TfL Open Data",
    "notice": "Not affiliated with or endorsed by Transport for London.",
    "licence": "Data: Transport for London open data, re-used under TfL's open data terms (based on the Open Government Licence v2.0). Code: MIT.",
    "sources": ["a"],
    "generated_at": "2026-10-26T08:30:40+00:00",
    "last_poll": "2026-10-26T08:30:06+00:00",
    "stale": false,
    "api_version": "1"
  }
}
```

- `sources`: the sources published (`a`, the live recording).
- `generated_at`: when this body was first built. An unchanged body is reused, so a past month keeps its `ETag`
  (and its `generated_at` and `last_poll`) across polls.
- `stale`: `true` when there has been no poll for over 5 minutes, or the store could not be read (the last good
  data is then served).

When you show data from Tifa, credit "Powered by TfL Open Data".

### Errors

```json
{"error": {"code": "bad_period", "message": "The span must be 1d, 7d, 30d, 90d or 365d."}, "meta": { ... }}
```

| Status | When | Codes |
| --- | --- | --- |
| 400 | A parameter could not be read | `bad_parameter`, `repeated_parameter`, `bad_time`, `bad_range`, `range_too_long`, `bad_line`, `bad_month`, `bad_category`, `bad_cause`, `bad_source`, `bad_limit`, `bad_cursor`, `bad_period`, `bad_since` |
| 404 | No such route, line or month | `not_found`, `unknown_line`, `unknown_month` |
| 405 | A method the route does not take: the REST routes take `GET`, `HEAD` and `OPTIONS`, `/mcp` takes `POST` and `OPTIONS` (see [mcp.md](mcp.md)) | `method_not_allowed` |
| 410 | A `/v1/changes` cursor more than 35 days old | `cursor_expired` |
| 413 | An MCP request body over 64 KiB | `too_large` |
| 429 | This client is over its request or data limit (`Retry-After`) | `rate_limited` |
| 500 | Something went wrong in Tifa | `internal` |
| 503 | The service is at a limit, or `/v1/coverage` has read too many days of history just now (both with `Retry-After`); the store cannot be read; or the MCP server is not running | `over_capacity`, `store_unavailable`, `mcp_unavailable` |
| 403 | The public API is closed by the fair-use guard (Google's front end answers, without the envelope) | |

Errors are never cached (`no-store`). Those a route answers (400, 404, 410, and 503 when the store cannot be read or
too much history was read) carry an `ETag` like any other answer. Those the service raises before a route is
reached (405, 413, 429, 500, `mcp_unavailable`, and `over_capacity` for the service-wide limits) do not.

Unknown query parameters are ignored. A parameter given twice is a 400.

### Times and parameters

- Times in responses are ISO 8601 in UTC (`2026-10-25T01:40:03+00:00`), except month payloads and the `cursor` and
  `polls` of `/v1/changes`, which are epoch seconds. Figures from `/v1/stats` give times as epoch seconds.
- `from` and `to` take epoch seconds, ISO 8601 (UTC when there is no offset; `Z` is fine) or a London date
  `YYYY-MM-DD`. A date as `from` is the start of that London day, as `to` the end of it, so `from=2026-10-24&to=
  2026-10-26` covers three whole London days. Ranges are `[from, to)`.
- Times before 2000 or from 2101 on, in any form, are a 400 (`bad_time`).
- Lists are comma-separated (`line=central,victoria`), at most 20 values.
- Line ids are TfL's: `bakerloo`, `central`, `circle`, `district`, `hammersmith-city`, `jubilee`, `metropolitan`,
  `northern`, `piccadilly`, `victoria`, `waterloo-city`, `elizabeth`, `liberty`, `lioness`, `mildmay`,
  `suffragette`, `weaver`, `windrush`, `dlr`. `/v1/lines` lists them.

### Caching

Every answer from a route has a strong `ETag` (the refusals and errors listed [above](#errors) as raised before a
route is reached have none). Send it back as `If-None-Match` and an unchanged response is a `304` with no body. Bodies of 1 KiB or more are gzipped when the request accepts gzip (the gzipped variant has its own tag, ending
`-gz`; `If-None-Match` matches either). `HEAD` works on every route.

| Route | `Cache-Control` |
| --- | --- |
| `/v1/status` | `public, max-age=30, stale-while-revalidate=30` |
| `/v1/changes` | `public, max-age=15` |
| `/v1/lines` | `public, max-age=3600` |
| `/v1/lines/{id}` | `public, max-age=60` |
| `/v1/months` | `public, max-age=60` |
| `/v1/months/{YYYY-MM}` | a past month `public, max-age=86400`; the current month `public, max-age=60` |
| `/v1/disruptions` | `public, max-age=3600` when `to` is over a day ago, else `public, max-age=60` |
| `/v1/stats` | `public, max-age=3600` for a past date, `public, max-age=60` for `end=now` |
| `/v1/coverage` | `public, max-age=60` |
| `/v1/meta` | `public, max-age=3600` |
| `/health`, errors | `no-store` |

While the store cannot be read, everything else is `public, max-age=15`.

## Fair use

Tifa runs on a small budget, so requests are shared out:

- **Per client** (the client address Google's front end reports; IPv6 addresses grouped by /64): a bucket of
  **60 tokens**, refilled at **1 token per second**. A request costs **1**; an uncached `/v1/changes`,
  `/v1/disruptions`, `/v1/stats` or `/v1/coverage` query costs **5** (the same query answered in the last 30
  seconds, with no new data since, is cached and costs 1); an MCP tool call costs **3**. Over the limit: **429**
  with `Retry-After`.
- **Data per client:** each client may receive an eighth of the service's hourly allowance, **1.25 MiB an hour**
  (all at once, then refilled at that rate; `/v1/meta` gives the figure in force). Ask for gzip and send
  `If-None-Match` to use less of it. Over: **429** (`rate_limited`) with `Retry-After`.
- **For the whole service:** **20 requests per second** sustained (bursts to 100), at most **10 uncached
  computations per second**, and **10 MiB of responses per hour**. Over: **503** with `Retry-After` (60 seconds, or
  longer while the hourly allowance recovers).
- A refused request costs nothing: a 429 takes none of the service's capacity, a 503 none of the client's tokens.
  A response is sent only when both the client's share and the service's allowance have room for it.
- Every response carries `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (seconds until the bucket is
  full) and `RateLimit-Policy` (`60;w=60`).
- Behind these, a guard closes the public API (403 for everyone) if traffic runs away: more than 9,000 requests in 5
  minutes closes it for 30 minutes (doubling on repeats, up to a day), more than 36,000 in an hour for 2 hours, and
  an estimated month's cost over $2, or the budget being spent, until the 1st of next month. Recording carries on
  throughout.

Polling `/v1/status` or `/v1/changes` once a minute is well within the limits and is all a copy needs: the data
changes at most once a minute. Use `If-None-Match`.

## Routes

### `GET /v1/status`

Every line's status at the last poll, the episodes open now, and whether this is stale.

```json
{
  "lines": [
    {"id": "victoria", "name": "Victoria", "mode": "tube", "severity": 2, "status": "Suspended",
     "reasons": ["Suspended due to a fire alert at Oxford Circus. Tickets are being accepted on London Buses."],
     "good": false, "live": true},
    {"id": "bakerloo", "name": "Bakerloo", "mode": "tube", "severity": 10, "status": "Good Service",
     "reasons": [], "good": true, "live": false}
  ],
  "disrupted": 2,
  "total": 19,
  "open": [
    {"line": "victoria", "name": "Victoria", "mode": "tube", "start": "2026-10-26T08:15:05+00:00", "end": null,
     "minutes": 16, "severity": 2, "status": "Suspended", "category": "suspended", "planned": false,
     "cause": "fire", "reason": "Suspended due to a fire alert at Oxford Circus. Tickets are being accepted on London Buses.",
     "source": "a"}
  ],
  "last_poll": "2026-10-26T08:30:06+00:00",
  "stale": false
}
```

- `lines` come live problems first, then planned works, then good service. `severity` is the line's worst status,
  `status` every status it has (comma-separated), `good` that all are Good Service or No Issues, `live` that a
  problem is happening now rather than planned works or information.
- `disrupted` counts the lines not on good service (information included).
- `minutes` of an open episode count to now. While the recording has a gap (no poll for over 15 minutes) they count
  to the last poll: nobody is watching, and the [GAP rule](data-model.md#gaps-the-gap-rule) ends the episode there
  when polling resumes.

### `GET /v1/changes?since=<epoch>`

For keeping a copy of the history in sync. `since` is required: epoch seconds, normally the `cursor` of the previous
answer.

```json
{
  "status": { "lines": [ ... ], "disrupted": 2, "total": 19 },
  "open": [ ... ],
  "closed": [
    {"line": "dlr", "name": "DLR", "mode": "dlr", "start": "2026-10-26T06:30:05+00:00",
     "end": "2026-10-26T07:20:06+00:00", "end_estimated": false, "minutes": 50, "severity": 7,
     "status": "Reduced Service", "category": "delays", "planned": false, "cause": "staff",
     "reason": "Reduced service due to a shortage of staff.", "source": "a"}
  ],
  "polls": [1792999145, 1792999206],
  "last_poll": "2026-10-26T07:20:06+00:00",
  "cursor": 1792999206,
  "stale": false
}
```

(the answer to `since=1792999085`, the poll at 07:18:05)

- `closed`: the episodes that closed at or after `since`, in the order they closed. An episode ended by a gap ends at
  the poll before the gap, which can be your cursor, so closed episodes ending exactly at `since` are included: merge
  on (line, severity, planned, start) and a repeat is harmless.
- `polls`: every poll time in (`since`, `cursor`] (epoch seconds), so a client that follows the cursor gets each
  poll exactly once. With the episodes, they rebuild the watched time.
- `cursor`: pass it as `since` next time.
- A **503** `store_unavailable` means the history could not be read in full just now (nothing is left out
  silently): retry from the same cursor.
- `since` more than 35 days ago is a **410**: reload the months (below) instead. A `since` more than a day in the
  future is a 400.

To keep a copy:

1. Load the history once: `/v1/months`, then `/v1/months/{YYYY-MM}` for each month you need. Take the cursor from
   `/v1/changes?since=<the latest end or poll you have>`.
2. Every minute, `GET /v1/changes?since=<cursor>` with `If-None-Match`. Add `closed` (a closed copy replaces an open
   one), replace your open episodes with `open`, add `polls`, and keep `cursor`.
3. On a 410, go back to step 1.

### `GET /v1/lines`

Every line: id, name, mode, the member lines of a shared line (`shared_with`, otherwise `[]`) and when the live
recording first and last saw it.

```json
{"lines": [{"id": "bakerloo", "name": "Bakerloo", "mode": "tube", "shared_with": [],
            "first_seen": "2026-10-23T23:00:07+00:00", "last_seen": "2026-10-26T08:30:06+00:00"}, ...]}
```

### `GET /v1/lines/{id}`

One line in depth, from the live recording:

- `now`: its open episodes, worst first;
- `stats`: over the last `24h`, `7d`, `30d` and `90d`: `episodes`, `minutes_disrupted`, `disrupted_pct` (of watched
  time), `longest_min`, and `by_category` (information is listed there but not counted in the totals);
- `coverage_pct`: the share of each window watched;
- `recent`: its 25 latest episodes, newest first, with reasons and causes;
- `starts_by_hour`: how many unplanned disruptions started in each London hour (0-23) over the last 90 days.

```json
{"id": "victoria", "name": "Victoria", "mode": "tube",
 "now": [{"severity": 2, "status": "Suspended", "category": "suspended", "planned": false,
          "start": "2026-10-26T08:15:05+00:00", "minutes": 16, "reason": "Suspended due to a fire alert ...",
          "cause": "fire"}],
 "stats": {"24h": {"episodes": 1, "minutes_disrupted": 16, "disrupted_pct": 1.1, "longest_min": 16,
                   "by_category": {"suspended": {"episodes": 1, "minutes": 16, "longest_min": 16,
                                                 "by_status": {"Suspended": 1}}}}, "7d": { ... }, ...},
 "coverage_pct": {"24h": 98.5, "7d": 34.0, "30d": 7.9, "90d": 2.6},
 "recent": [ ... ], "starts_by_hour": [0, 0, 0, 0, 0, 0, 0, 0, 1, 0, ...], "shared_with": [], "source": "a"}
```

An unknown line is a 404.

### `GET /v1/months`

The months index: the months with published data, the start of the history, the lines, the categories and the
causes.

```json
{"months": ["2026-10"], "first": "2026-10-23T23:00:07+00:00",
 "lines": [{"id": "bakerloo", "name": "Bakerloo", "mode": "tube", "shared_with": []}, ...],
 "categories": ["delays", "suspended", "closure", "other", "info"],
 "causes": [{"code": "signal", "label": "Signal failure"}, ...]}
```

### `GET /v1/months/{YYYY-MM}`

One UTC month in the month format ([data-model.md](data-model.md#months)): every episode overlapping it as a row
`[line, start, end, severity, category, planned01, source, cause, status, reason, end_estimated01]` (epoch seconds;
`end` is `null` while open), and the spans each published source watched.

```json
{"month": "2026-10",
 "episodes": [["district", 1792796407, 1792940941, 5, "closure", 1, "a", "engineering", "Part Closure",
               "No service between Earl's Court and Wimbledon due to planned engineering work. Replacement buses operate.", 1],
              ...],
 "watched": {"a": [[1792796407, 1792940941], [1792942201, 1793003406]]}}
```

A month in the future, or one with no data, is a 404.

### `GET /v1/disruptions`

Episodes overlapping `[from, to)`, oldest first, filtered and paged.

| Parameter | Default | |
| --- | --- | --- |
| `from` | 30 days before `to` | at most 366 days before `to` |
| `to` | now | |
| `line` | all | line ids |
| `category` | all | `delays`, `suspended`, `closure`, `other`, `info` |
| `cause` | all | cause codes (see `/v1/meta`), or `unknown` for none |
| `planned` | both | `true` for planned works only, `false` for unplanned |
| `source` | all published | `a` |
| `limit` | 100 | 1 to 500 per page |
| `cursor` | | `next_cursor` of the previous page |

```json
{"from": "2026-10-26T00:00:00+00:00", "to": "2026-10-26T08:30:40+00:00",
 "episodes": [{"line": "district", "name": "District", "mode": "tube", "start": "2026-10-25T15:30:01+00:00",
               "end": "2026-10-26T04:30:04+00:00", "end_estimated": false, "minutes": 780, "severity": 5,
               "status": "Part Closure", "category": "closure", "planned": true, "cause": "engineering",
               "reason": "No service between Earl's Court and Wimbledon due to planned engineering work. ...",
               "source": "a"}, ...],
 "count": 2, "total": 4, "limit": 2, "next_cursor": "WzE3OTI5NDIyMDEsInBpY2NhZGlsbHkiLDE5LDAsImEiXQ"}
```

`total` is how many match; follow `next_cursor` (with the same other parameters) until it is `null`. Open episodes
are included with `end: null`.

### `GET /v1/stats`

Figures for a period, for the network or one line, against the period before it ([what they
mean](data-model.md#period-figures)).

| Parameter | Default | |
| --- | --- | --- |
| `span` | `7d` | `1d`, `7d`, `30d`, `90d` or `365d` |
| `end` | `now` | `now`, or a London date: the period is the whole London days ending with it; today's date means `now` (the span up to this moment) |
| `line` | the network | one line id |

```json
{"span": "1d", "end": "2026-10-25", "t0": 1792882800.0, "t1": 1792972800.0, "p0": 1792796400.0,
 "watched": {"f": 0.0, "a": 0.986, "any": 0.986, "before": 0.9999},
 "unwatched": [[1792940941, 1792942201]], "comparable": true, "lines_watched": 19,
 "good_pct": 93.88, "unplanned": 3, "planned": 3, "disrupted_s": 103208.0,
 "before": {"good_pct": 88.17, "unplanned": 4, "planned": 3, "disrupted_s": 194267.0},
 "most_disrupted": {"line": "district", "name": "District", "share": 1.0, "disrupted_s": 88740.0},
 "longest_unplanned": {"line": "jubilee", "name": "Jubilee", "status": "Severe Delays", "severity": 6,
                       "category": "delays", "cause": "customer", "source": "a", "start": 1792885806.0,
                       "end": 1792892404.0, "duration_s": 6598.0, "ongoing": false},
 "busiest_day": null,
 "causes": [{"code": "engineering", "label": "Engineering works", "count": 3, "seconds": 90547.0}, ...],
 "other_causes": null, "bins": null,
 "lines": [{"id": "bakerloo", "name": "Bakerloo", "mode": "tube", "shared": false, "watched_s": 88740.0,
            "disrupted_s": 0, "good_pct": 100.0, "good_pct_before": 100.0, "unplanned": 0, "planned": 0,
            "by_category": {"delays": 0, "suspended": 0, "closure": 0, "other": 0}, "avg_s": null,
            "median_s": null, "longest_unplanned": null, "core_share": null, "days": 0, "days_in_period": 1,
            "top_cause": null}, ...],
 "line": null,
 "period": {"label": "Sun 25 Oct 2026", "before": "Sat 24 Oct 2026", "prev": "2026-10-24", "next": "now",
            "today": "2026-10-26", "first": "2026-10-23T23:00:07+00:00"}}
```

This Sunday was the day the clocks went back, so `t1 - t0` is 25 hours. `period.prev` and `period.next` are the
`end` to pass for the period before or after (`""` at either end of the history). Shares are 0 to 1, `good_pct`
is a percentage, times are epoch seconds and durations seconds. `bins` (periods of 7 days or more) give line-time
disrupted per London day, or per week from 90 days, split by category (`delays`, `delays-planned`, ...).

### `GET /v1/coverage`

When the published sources were watching, over `[from, to)`: default the 7 days to now, at most 90 days.

```json
{"from": "2026-10-24T23:00:00+00:00", "to": "2026-10-26T00:00:00+00:00",
 "sources": {"a": {"watched": [["2026-10-24T23:00:00+00:00", "2026-10-25T15:09:01+00:00"],
                               ["2026-10-25T15:30:01+00:00", "2026-10-26T00:00:00+00:00"]],
                   "watched_s": 88740, "coverage_pct": 98.6}},
 "coverage_pct": 98.6,
 "gaps": [{"start": "2026-10-25T15:09:01+00:00", "end": "2026-10-25T15:30:01+00:00", "minutes": 21}],
 "days": [{"date": "2026-10-24", "polls": 60, "expected": 1440, "failed": []},
          {"date": "2026-10-25", "polls": 1420, "expected": 1440,
           "failed": [{"slot": 29882350, "at": "2026-10-25T15:10:00+00:00", "code": "timeout"}, ...]}]}
```

`gaps` are the spans no source watched (the GAP rule decides what a gap is). `days` are UTC days: the polls
recorded and the minute slots that failed, with why (`timeout`, `http_503`, `missing_lines`, ...). Check coverage
before reading a quiet period as a good one.

### `GET /v1/meta`

About the API: `name`, `api_version`, the published `sources`, the `categories` and `causes` with their labels, the
`licence`, `attribution` and `notice`, the `fair_use` limits in force, and `links`.

### `GET /health`

The last poll and its age, the TfL key state (`tfl_auth`: `ok`, `degraded` when TfL refused the key and requests
went out anonymously, or `anonymous`), the store, the guard's heartbeat and whether it has the API closed, and
whether MCP is running. **503** when the last poll is more than 10 minutes old. (It isn't `/healthz`: Cloud Run
reserves some paths that end in `z`.)

```json
{"status": "ok", "last_poll": "2026-10-26T08:30:06+00:00", "last_poll_age_s": 34, "tfl_auth": "ok",
 "store": "ok", "store_error_since": null, "read_model_age_s": 0,
 "guard": {"heartbeat": "2026-10-26T08:29:04+00:00", "heartbeat_age_s": 96, "closed": false, "closed_until": null,
           "mode": "enforce"},
 "mcp": true}
```

### `POST /mcp`

The MCP server: streamable HTTP, stateless, JSON responses. See [mcp.md](mcp.md).

## Examples

```sh
curl -s https://tifa-api-dh52427ooa-ew.a.run.app/v1/status | jq '.data.lines[] | select(.good | not) | {name, status}'
curl -s 'https://tifa-api-dh52427ooa-ew.a.run.app/v1/disruptions?line=central&from=2026-10-01&to=2026-10-31&planned=false' | jq '.data.total'
curl -s 'https://tifa-api-dh52427ooa-ew.a.run.app/v1/stats?span=30d&end=now' | jq '.data | {good_pct, unplanned, most_disrupted}'
curl -s -H 'If-None-Match: "<etag>"' -o /dev/null -w '%{http_code}\n' https://tifa-api-dh52427ooa-ew.a.run.app/v1/months/2026-10
```
