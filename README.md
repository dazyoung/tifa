# Tifa

Tifa keeps the history of London's rail disruptions. Every minute it records the status of every London
Underground, Elizabeth line, London Overground and DLR line from TfL's open data, turns it into **disruption
episodes** (which line, what kind, when it started and ended, what TfL said, and what caused it), and serves the live
status and the history to everyone through an **open REST API** and a remote **MCP server** for AI assistants.

TfL publishes only the status right now. Tifa remembers it, so you can ask how often the Central line had a signal
failure last month, how much of last week the network ran a good service, or what was disrupted on the night the
clocks went back.

- **Live service:** `<base-url>` (the address will be published here when the service is live)
- **API docs:** `<base-url>/docs` (interactive), [docs/api.md](docs/api.md)
- **MCP:** `<base-url>/mcp`, see [docs/mcp.md](docs/mcp.md)

**Powered by TfL Open Data.** Contains data from Transport for London, re-used under TfL's open data terms (based on
the Open Government Licence v2.0). **Tifa is not affiliated with or endorsed by Transport for London.** It uses no
TfL logos or roundels. Please credit "Powered by TfL Open Data" when you show data you got from Tifa; every API
response and MCP result carries that line.

## Quick start

```sh
# what is disrupted right now
curl -s <base-url>/v1/status | jq '.data.lines[] | select(.good | not) | {name, status, reasons}'

# the Central line's unplanned disruptions in October, with their causes
curl -s '<base-url>/v1/disruptions?line=central&from=2026-10-01&to=2026-10-31&planned=false' \
  | jq '.data.episodes[] | {start, end, status, cause, reason}'

# how the network did over the last 30 days
curl -s '<base-url>/v1/stats?span=30d&end=now' | jq '.data | {good_pct, unplanned, planned, most_disrupted}'
```

Add Tifa to Claude Code (or any MCP client; [docs/mcp.md](docs/mcp.md) covers the others):

```sh
claude mcp add --transport http tifa <base-url>/mcp
```

## What the data means

The full description is in [docs/data-model.md](docs/data-model.md). In short:

- **Episodes.** An episode is a run of polls that saw the same line with the same status severity, planned or not.
  It starts at the first poll that saw it and ends at the first poll that did not, so times are accurate to about a
  minute. Good Service, No Issues and Service Closed (the end of the day) are never episodes. While an episode is
  open its `end` is `null`.
- **The GAP rule.** When no poll succeeded for more than 15 minutes (`GAP` = 900 s), nobody was watching. Every
  episode open at the last poll before the gap ends there, marked `end_estimated: true`, and anything still going on
  starts again at the first poll after it. Shorter outages change nothing.
- **A gap is not good service.** Time Tifa did not watch counts neither as good service nor as disruption. Every
  percentage is of watched time; `/v1/coverage` shows what was watched, the gaps, and each failed poll with its
  reason. Check it before reading a quiet period as a good one.
- **Categories.** Each severity falls in one category: `delays` (minor and severe delays, reduced service),
  `suspended` (suspended, part suspended), `closure` (closed, planned and part closures, not running), `other`
  (special service, diversions, bus service and the rest) and `info` (information, no step-free access). `info`
  episodes are listed but never counted as disruption.
- **Causes.** Each episode's cause is read from TfL's own reason text by 16 rules (signal failure, faulty train,
  track fault, power failure, faulty equipment, customer incident, obstruction, staff shortage, strike, police or
  security, fire alert, weather, engineering works, train cancellations, operational restrictions, Network Rail).
  Planned works are engineering unless they say otherwise. When nothing matches, the cause is `null`: unknown, and
  shown as such.
- **Time.** Times are UTC (ISO 8601 in the API, epoch seconds in month payloads). Months are UTC calendar months.
  Days and hours shown to people are **London** time, so a London day can be 23, 24 or 25 hours long.

## Endpoints

All `GET`, open CORS, JSON in a `{"data": ..., "meta": ...}` envelope with the attribution in `meta`. Details,
parameters and examples: [docs/api.md](docs/api.md).

| Route | What |
| --- | --- |
| `/v1/status` | Every line's status at the last poll, and the episodes open now |
| `/v1/changes?since=<epoch>` | What changed since a cursor: for keeping a copy in sync |
| `/v1/lines`, `/v1/lines/{id}` | The lines; one line in depth (now, 24 hours to 90 days, recent episodes, hours of the day) |
| `/v1/months`, `/v1/months/{YYYY-MM}` | The months index; one month's episodes and watched time |
| `/v1/disruptions` | Episodes filtered by line, time, category, cause and planned, paged |
| `/v1/stats?span=&end=&line=` | Figures for 1, 7, 30, 90 or 365 days, against the period before |
| `/v1/coverage` | When Tifa was watching: spans, gaps, polls and failed polls per day |
| `/v1/meta` | Sources, categories, causes, licence and the fair-use limits |
| `/health` | The last poll's age and the service's health |
| `/docs`, `/openapi.json` | Interactive documentation and the OpenAPI document |
| `/mcp` | The MCP server (streamable HTTP, stateless, JSON responses) |

Every answer from a route, errors included, has an `ETag` (send `If-None-Match` for a `304`), `Cache-Control` set
per route, and gzip when asked. Refusals by the fair-use limits (429, and 503 for the service-wide limits) and other
errors the service raises before reaching a route (405, 413, 500) are `no-store` and carry no `ETag`.

## MCP

The MCP server at `/mcp` has seven read-only tools over the same data as the REST API: `list_lines`,
`current_status`, `line_status`, `disruptions`, `period_stats`, `coverage` and `causes`. No key or sign-in is
needed. Setup for Claude Code, the Claude apps, VS Code, Cursor and others, and the tool reference, are in
[docs/mcp.md](docs/mcp.md).

## Fair use

Tifa is free to use and runs on a small budget, so requests are shared out:

- **Per client address** (IPv6 by /64): a bucket of **60 tokens**, refilled at **1 per second**. A request costs 1,
  an uncached changes, disruptions, stats or coverage query 5, an MCP tool call 3. Each address may also receive
  1.25 MiB an hour (an eighth of the service's allowance), so ask for gzip and send `If-None-Match`. Over either:
  **429** with `Retry-After`.
- **For the whole service:** 20 requests a second (bursts to 100), 10 uncached computations a second and 10 MiB of
  responses an hour. Over: **503** with `Retry-After` (60 seconds, or longer while the hourly allowance recovers).
- Responses carry `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` and `RateLimit-Policy`.
- If traffic still runs away, a guard closes the public API for a while (more than 9,000 requests in 5 minutes: 30
  minutes, doubling on repeats up to a day; more than 36,000 in an hour: 2 hours; the month's estimated cost over
  $2, or the budget spent: until the 1st). Recording never stops.

Polling once a minute is plenty: the data changes at most once a minute. Use `If-None-Match`, and `/v1/changes` to
keep a copy.

## Sources and licences

| Source | What | Published |
| --- | --- | --- |
| `a` | Tifa's own live recording of TfL's line status, from its first poll | yes |
| `f` | TfL's freedom-of-information release of its status records, 2020 to February 2025 | no |

Historic data from TfL's FOI release is **not published**. Information released under the Freedom of Information Act
is released to the person who asked: the Act gives access, not a licence to re-use or publish, and TfL's release
grants no such permission. So the API publishes only Tifa's own recording (`TIFA_PUBLIC_SOURCES=a`, the default
everywhere), and `tools/import_foi.py` refuses to run unless told `--i-have-reuse-permission`. If TfL grants re-use
in writing, the history back to 2020 can be added.

- **Code:** MIT ([LICENSE](LICENSE)).
- **Data:** TfL open data, under TfL's open data terms (based on the Open Government Licence v2.0), with the
  attribution "Powered by TfL Open Data". Tifa is not affiliated with or endorsed by Transport for London.

## Privacy

- No accounts, no keys, no cookies, no analytics, no tracking.
- The REST API, `/openapi.json` and `/mcp` load nothing from anywhere else. The interactive docs page, `/docs`, is
  Swagger UI: when you open it in a browser, your browser fetches Swagger UI's script and stylesheet (an exact,
  pinned version) from the jsDelivr CDN, so the CDN sees your address too. Nothing else on the page asks a third
  party. Read [docs/api.md](docs/api.md) or `/openapi.json` instead to avoid that.
- Your IP address is used only for the per-client rate limit. It is held in the API's memory (at most 10,000
  addresses, the least recently seen dropped first) and never written to the database or to Tifa's own logs.
- Cloud Run's request logs for responses below HTTP 500 are excluded from Cloud Logging, so ordinary requests leave
  no log entry with your address. Request logs of server errors (5xx) are kept for diagnosis, as Cloud Logging
  records them.
- The fair-use guard reads only aggregate request counts.
- Tifa's TfL app key is kept in Secret Manager and never logged.

## How it works

```
Cloud Scheduler (every minute, UTC) ──► tifa-poller (private)
                                         ├─ GET TfL line status
                                         ├─ check the answer, observe, advance: one Firestore transaction
                                         └─ guard: Cloud Monitoring every 2 minutes; can close tifa-api
Budget alerts ─► Pub/Sub ─push─────────► tifa-poller /internal/budget
Anyone ─────────────────────────────────► tifa-api (public): /v1/*, /mcp, /docs, /health
                                           served from memory, refreshed from Firestore every 30 s
```

- Two Cloud Run services from one image (`TIFA_ROLE=api|poller`), so a flood on the public API can never stall
  recording. The poller is private (IAM); the API is public and scales to zero.
- Each minute slot is recorded at most once, in one transaction, so retries are harmless. TfL's answer is checked
  before anything changes, so a broken answer cannot close every episode.
- The API serves a copy held in memory, so database reads do not grow with traffic.

## Self-hosting

Everything needed to run your own Tifa on Google Cloud is in this repository; nothing identifying a real deployment
is. Tifa can share an existing project: every resource it creates is named `tifa-*` and labelled `app=tifa`, its CI
identity may update only its two services, and its budget counts only its own resources.

1. `cp infra/env.example infra/.env` and fill it in (gitignored; never commit it).
2. `infra/bootstrap.sh --dry-run`, then `infra/bootstrap.sh`: APIs, Firestore, Artifact Registry, service accounts,
   Workload Identity Federation for GitHub Actions, the TfL key secret, Pub/Sub and a paused Scheduler job.
3. In the GitHub repository's settings, set the Actions variables the bootstrap prints (`--github` sets them with
   `gh`), and create the environment `production`, limited to the `main` branch. The Deploy workflow runs in that
   environment, so without it every deploy from CI fails.
4. Optionally register for a free TfL app key on TfL's API portal and store it with `infra/bootstrap.sh tfl-key`
   (a hidden prompt), then set `TFL_APP_KEY_VERSION` to the version it prints. Without a key, Tifa polls
   anonymously.
5. `infra/first-deploy.sh`: builds the image and creates both services with your own credentials, and lets CI update
   them (and nothing else).
6. `infra/post-deploy.sh`: who may call what, the budget and its push, the log exclusion, then smoke tests. From
   then on every push to `main` deploys through `.github/workflows/deploy.yml`.
7. Resume the Scheduler job, and recording starts.

[docs/operations.md](docs/operations.md) has the details: settings, deploying, rotating the key, the guard and the
budget, rolling back and troubleshooting. The services' settings are environment variables, listed in
[docs/contracts.md](docs/contracts.md#configuration-environment-nothing-committed).

## Cost

Expected, in US dollars a month, assuming the free tiers are already used by something else:

| Item | Expected |
| --- | --- |
| Poller: 43,800 polls a month of about 3.5 s at 0.25 vCPU | 1.06 |
| API: about 100,000 requests | 0.36 |
| Cloud Scheduler: one job | 0.10 |
| Egress, Artifact Registry, Secret Manager | 0.17 |
| Firestore, Logging, Monitoring, Pub/Sub (inside their free tiers) | 0 |
| **Total** | **about 1.70** |

Under abuse, the fair-use limits, the guard and a monthly budget (£3.75, about $5, by default) hold the worst case
to about the budget. Nothing is ever deleted and billing is never detached; recording carries on while the public
API is closed.

## Development

```sh
uv sync                      # Python 3.12+; the image uses 3.13
uv run pytest -q             # the whole suite; Firestore emulator tests run when FIRESTORE_EMULATOR_HOST is set
uv run ruff check .
TIFA_ROLE=api uv run tifa    # the API on http://localhost:8080 with an empty in-memory store
```

`tests/integration/` runs three simulated days of minute polls (including the night the clocks go back) and the
night a month ends through the real poller into an in-memory store, and reads them back through the REST API, the
MCP server, clients keeping a copy in sync and the operator tools.

## Documentation

- [docs/api.md](docs/api.md): the REST API
- [docs/mcp.md](docs/mcp.md): the MCP server
- [docs/data-model.md](docs/data-model.md): episodes, gaps, categories, causes, months and the period figures
- [docs/operations.md](docs/operations.md): setting up, deploying and running it
- [docs/contracts.md](docs/contracts.md): the internal interfaces
- [CHANGELOG.md](CHANGELOG.md), [SECURITY.md](SECURITY.md), [LICENSE](LICENSE)
