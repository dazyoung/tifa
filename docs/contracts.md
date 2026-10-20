# Tifa internal contracts

Tifa records London TfL line-status disruptions every minute and serves current and historic data through an open REST API and an MCP server.
This file fixes the module layout, the function signatures and the data formats that the parts of Tifa share. Change it in the same commit as any change to a shared interface.

## Layout

```
pyproject.toml            uv project; Python >= 3.12 (image uses 3.13)
src/tifa/
  config.py               Settings from environment variables (see "Configuration")
  main.py                 entry point: TIFA_ROLE=api|poller selects the ASGI app; uvicorn on $PORT
  core/                   pure, stdlib only; no I/O, no clock reads (time is passed in)
    timeutil.py           LONDON tz, iso(), epoch(), London day/hour helpers, UTC month helpers
    tfl.py                TfL constants, clean_reason(), summarise(), current(), validate_payload()
    causes.py             CAUSES, CAUSES_SIG, cause()
    episodes.py           observe(), advance(), GAP, episode shapes
    months.py             month splitting, watched runs, month payload build/merge
    stats.py              window_stats(), parse_period(), period_of(), gather(), cover_of(), line_period(), period_figures(), line_report()
  store/
    base.py               Store protocol (below) and dataclasses
    codec.py              gzip-JSON encode/decode, sha256
    memory.py             in-memory Store (tests, local runs)
    firestore.py          Firestore Store (production)
  poller/
    app.py                private ASGI app: POST /internal/poll, POST /internal/budget, GET /healthz
    tfl_client.py         httpx client for TfL (timeouts, gzip, app_key, redaction)
    poll.py               run_poll(store, client, slot, now_ts) -> PollResult
    guard.py              fair-use guard: Monitoring reads, decide(), close/open the public API (IAM), budget messages
  api/
    app.py                public ASGI app: /v1/*, /healthz, /docs, /openapi.json, mounts MCP at /mcp
    readmodel.py          in-memory read model refreshed from the Store (state every <=30 s, months on revision change)
    views.py              pure functions: read model + query -> response data (shared by REST and MCP)
    ratelimit.py          per-IP token buckets and global limits
    http.py               response envelope, ETag/304, Cache-Control, CORS, errors
    mcp.py                FastMCP server (stateless streamable HTTP, JSON responses), tools call views
tools/                    import_jsonl.py, import_foi.py, verify_parity.py, loadtest.py, guard_cli.py
tests/                    core/, store/, poller/, api/, tools/, integration/, fixtures/
infra/                    bootstrap.sh, post-deploy.sh, env.example (placeholders only)
.github/workflows/        ci.yml, deploy.yml, tfl-canary.yml
```

Time is always integer epoch seconds (UTC) internally, ISO 8601 with `+00:00` at the API, months are UTC calendar months (`YYYY-MM`), and "days" and "hours" shown to people are London local time.

## Core (pure)

### tfl.py
- `URL_PATH = "/Line/Mode/tube,elizabeth-line,overground,dlr/Status"`, `MODES`, `MODE_ORDER = {"tube":0,"elizabeth-line":1,"overground":2,"dlr":3}`.
- `GOOD = {10, 18}`, `NOT_RECORDED = {10, 18, 20}`, `RANK`, `rank(sev)`.
- `CATEGORY(sev) -> "delays"|"suspended"|"closure"|"other"|"info"`; `CATEGORIES = [{"id","label"}]` in display order: delays, suspended, closure, other, info.
- `clean_reason(text, line_name) -> str`.
- `summarise(line_json) -> {id, name, mode, severity, status, reasons, good, live}`; `current(raw) -> {"lines": [...], "disrupted": int, "total": int}` (sorted live problems first, then planned, then good).
- `validate_payload(raw, known_line_ids) -> (ok: bool, code: str|None)`: a JSON list, each item with `id`, `name`, `lineStatuses` list; at least 80% of `known_line_ids` present when that set is non-empty.

### causes.py
- `CAUSES = [(code, label, regex)]` (16 rules, priority order), `CAUSES_SIG` (hash of the rules), `cause(reason, planned, status) -> code|None` (planned works default to `engineering`).

### episodes.py
- `GAP = 900` seconds. Polls further apart break continuity.
- `observe(raw) -> (obs, meta)`: `obs = {(line_id, severity, planned): {"status": str, "reason": str}}` for statuses in effect now (any `validityPeriods[].isNow`, or none) excluding `NOT_RECORDED`; `meta = {line_id: (name, mode)}`.
- `advance(state, obs, meta, now_ts) -> (state, closed, opened)`, pure:
  - `state = {"last_poll": int|None, "open": [OpenEpisode]}`.
  - If `now_ts - last_poll > GAP`, every open episode closes at `last_poll` with `end_estimated: true` before this poll is applied.
  - A key missing from `obs` closes its episode at `now_ts`; a new key opens an episode at `now_ts`; reasons update while open.
- `OpenEpisode = {line, name, mode, severity, status, category, planned, start, reason}`.
- `ClosedEpisode = OpenEpisode + {end, end_estimated}`. `cause` is computed with `causes.cause(reason, planned, status)` when stored.

### months.py
- Month payload (stored gzipped; served as JSON):
  `{"month": "YYYY-MM", "episodes": [[line, start, end, severity, category, planned01, source, cause, status, reason, end_estimated01], ...], "watched": {"a": [[s, e], ...], "f": [[s, e], ...]}}`
  - Closed episodes that OVERLAP the month (an episode spanning months appears in each), sorted by start.
  - `source`: `"a"` live recording, `"f"` TfL's FOI release (only when published).
  - `watched`: spans in the month the source covered. Live coverage: runs of polls no more than `GAP` apart.
- The API adds open episodes from state with `end: null`. Clients that only read the first 8 fields get the established month format.
- `split_months(start, end) -> ["YYYY-MM", ...]`, `clip_spans(spans, a, b)`, `runs_of(poll_times, gap=GAP) -> [[s, e]]`, `month_episode(ep, source) -> list`, `merge_month(payload, add_episodes, add_watched, source) -> payload` (dedupe on `(line, severity, planned, start)`; a closed copy beats an open one).

### stats.py
- `window_stats(episodes, start, now_ts, covered_s)`: episodes, minutes disrupted (overlaps counted once, info excluded), `disrupted_pct` of watched time, longest, per category.
- `parse_period(query: dict, today) -> ((span, end) | None, error|None)`: `span` in `1d|7d|30d|90d|365d`, `end` `now` or `YYYY-MM-DD` (London day) between 2000-01-01 and today.
- `period_of(span, end, now_ts, first_ts) -> Period` (t0, t1, previous p0..t0, labels, prev/next).
- `gather(months, index, a, b, now_ts)`, `cover_of(...)`, `line_period(...)`, and `period_figures(...) -> dict` (network figures: coverage per source, good service %, unplanned and planned counts, line-time disrupted, the change against the previous period when both are >= 80% watched, most disrupted line, longest unplanned, busiest day, top causes, per-day/per-week bins, per-line table rows).
- `line_report(...)` builds one line's entry: now, stats per window (24h/7d/30d/90d), recent episodes with reasons, starts by hour.

## Store (store/base.py)

```python
class Store(Protocol):
    def get_state(self) -> State | None
    def poll_transaction(self, slot: int, fn: Callable[[State | None], PollWrite | None]) -> str   # "applied" | "duplicate"
    def get_month(self, month: str) -> MonthDoc | None          # MonthDoc(month, rev, payload: dict)
    def list_months(self) -> dict[str, int]                     # month -> rev
    def put_month(self, month: str, payload: dict) -> int       # outside polls (imports); returns new rev
    def get_polls(self, day: str) -> PollsDoc | None            # day = UTC "YYYY-MM-DD"
    def add_snapshot(self, ts: int, payload: dict) -> None
    def get_guard(self) -> dict | None
    def put_guard(self, doc: dict) -> None
    def record_import(self, import_id: str, doc: dict) -> None
```

- `poll_transaction` runs `fn` inside one transaction against the current state; `fn` returns `None` for a duplicate or late slot, else a `PollWrite` with the new state, the months to upsert (`{month: payload}`), the poll time to add, an optional failed-slot record and an optional snapshot. Everything commits atomically or not at all. Retries of the same slot are no-ops (`state.last_slot >= slot`).
- `State`:
  `{schema: 1, last_poll: int|None, last_slot: int|None, run: [s, e]|None, open: [OpenEpisode], lines: {id: {name, mode, first_seen, last_seen}}, status: dict (current()), status_etag: str, months: {"YYYY-MM": {"rev": int, "n": int}}, causes_sig: str, tfl_auth: "ok"|"degraded"|"anonymous", updated_at: int}`
  (Firestore stores `status` gzipped.)
- Firestore documents: `meta/state`, `months/{YYYY-MM}` (`{month, rev, payload: Bytes(gzip JSON), bytes, sha256, updated_at}`), `polls/{YYYY-MM-DD}` (`{at: [epochs], n, failed: [{slot, code}]}`), `snapshots/{epoch}` (`{hash, payload: Bytes}`; written only when statuses change), `guard/state`, `imports/{id}`. No composite indexes; single-field indexing disabled on blob and array fields.

## Poller

- `POST /internal/poll` (Cloud Scheduler with OIDC; IAM makes the service private). Slot = `floor(X-CloudScheduler-ScheduleTime / 60)` (fallback: now // 60).
  - Fetch TfL outside the transaction (connect 3 s, read 12 s, total 15 s; gzip; app_key; never log the URL or key).
  - Invalid or failed fetch: record a failed slot (`code`), keep state; return 502 so Scheduler retries within the minute.
  - Valid: `observe` → `advance` → months touched by closed episodes and by the watched run → one `poll_transaction`. Return 200 for applied or duplicate. Never 4xx to Scheduler.
  - TfL 401/403 with a key: one anonymous request; `tfl_auth = "degraded"`.
- `POST /internal/budget` (Pub/Sub push with OIDC): budget notification → guard (close the public API until month end at >= 100%).
- Guard runs at most every 2 minutes from the poll path: reads `run.googleapis.com/request_count` (and egress estimates) for the public service from Cloud Monitoring, applies the trip rules, and closes the public API by removing `allUsers` from its `run.invoker` IAM binding (Cloud Run Admin API v2 get/setIamPolicy); reopens when the closure ends. State in `guard/state`. Recording never stops.

## API (public, GET/HEAD/OPTIONS only, CORS `*`)

Envelope: `{"data": ..., "meta": {"attribution": "Powered by TfL Open Data", "notice": "Not affiliated with or endorsed by Transport for London.", "licence": "...", "sources": [...], "generated_at": iso, "last_poll": iso|null, "api_version": "1"}}`.
Errors: `{"error": {"code", "message"}, "meta": {...}}` with 400/404/410/429/503.

| Route | Data | Cache-Control |
| --- | --- | --- |
| `/v1/status` | `current()` plus `open` episodes and `stale` (last poll > 5 min) | `public, max-age=30, stale-while-revalidate=30` |
| `/v1/changes?since=<epoch>` | `{status, open, closed: [ClosedEpisode+cause] closed after since, polls: [epochs > since], last_poll, cursor}`; 410 if since is older than 35 days | `public, max-age=15` |
| `/v1/lines`, `/v1/lines/{id}` | line list; one line's `line_report()` | 3600 / 60 |
| `/v1/months`, `/v1/months/{YYYY-MM}` | index (`months`, `first`, `lines`, `categories`, `causes`); one month payload plus open episodes | 60; past months 86400, current 60 |
| `/v1/disruptions?line&from&to&category&cause&planned&source&limit&cursor` | episodes, at most 366 days per request, at most 500 per page | 3600 if `to` is over a day ago, else 60 |
| `/v1/stats?span&end[&line]` | `period_figures()` | 3600 for a past date, 60 for now |
| `/v1/coverage?from&to` | watched spans per source, coverage %, gaps, polls per day, failed slots (at most 90 days) | 60 |
| `/v1/meta` | sources, categories, causes, licence, fair-use limits | 3600 |
| `/healthz` | last poll age, guard heartbeat, TfL auth state; 503 when the last poll is over 10 min old | no-store |

All responses: gzip, strong ETag, `If-None-Match` → 304, `RateLimit-*` headers.

## Fair use (api/ratelimit.py)

- Client IP = the right-most `X-Forwarded-For` entry (set by Google's front end); IPv6 grouped by /64; LRU of 10,000 buckets; memory only.
- Per IP: bucket of 60 tokens, refill 1/s. Cost: cached GET 1, uncached stats/disruptions/changes 5, MCP `tools/call` 3. Over: 429 with `Retry-After`.
- Global (one API instance, so effectively global): 20 req/s sustained, burst 100; at most 10 uncached computations per second; egress at most 10 MiB/hour. Over: 503 with `Retry-After: 60`.
- Guard trips: > 9,000 requests in 5 min → closed 30 min (doubling on repeats up to 24 h); > 36,000 in 1 h → 2 h; estimated month-to-date API cost > $2.00 → closed until the 1st; budget >= 100% → closed until the 1st.

## MCP (/mcp)

Official `mcp` SDK FastMCP, `stateless_http=True`, `json_response=True`, allowed hosts = the service's run.app host(s) plus any custom domain. Read-only tools sharing `views.py`: `list_lines`, `current_status(mode?)`, `line_status(line)`, `disruptions(line?, from?, to?, category?, cause?, planned?, limit<=100)`, `period_stats(span, end, line?)`, `coverage(from?, to?)`, `causes()`. Every result includes the attribution.

## Configuration (environment; nothing committed)

`TIFA_ROLE` (api|poller), `GOOGLE_CLOUD_PROJECT`, `TIFA_REGION`, `TIFA_API_SERVICE` (name of the public service, for the guard), `TFL_APP_KEY` (from Secret Manager), `TIFA_PUBLIC_SOURCES` (default `a`; `a,f` once the FOI data may be published), `TIFA_ALLOWED_HOSTS`, `TIFA_STORE` (`firestore`|`memory`), limits overridable for tests (`TIFA_GUARD_5MIN`, etc.).

## Licences and attribution

Code: MIT. Data: TfL open data, re-used under TfL's open data terms (based on the Open Government Licence v2.0); every response and the README carry "Powered by TfL Open Data" and "Not affiliated with or endorsed by Transport for London". No TfL logos or roundels.

## Changes agreed during the build

- `merge_month` dedupes on `(line, severity, planned, start, source)`; `split_months` takes an optional `now_ts` for open episodes; an observation's `reason` may be `None`; `CAUSES` is a tuple. Watched runs are merged with the run-segment helper, so a poll touches at most two months.
- `validate_payload` is strict (non-empty `lineStatuses` of dicts with an integer `statusSeverity`; `modeName` a string; `disruption` a dict when present). The poller passes only line ids seen in the last 24 hours as `known_line_ids`.
- `/v1/months` index: `{months, first, lines: [{id, name, mode, shared_with}], categories, causes: [{code, label}]}`. Stats use `gather(..., sources=public_sources)`.
- Views (`api/views.py`), shared by REST and MCP: `status_view`, `changes_view`, `lines_view`, `line_view`, `months_index_view`, `month_view`, `disruptions_view`, `stats_view`, `coverage_view`, `meta_view`, `causes_view`, raising `ViewError(status, code, message)`. MCP is built by `api/mcp.py: build_mcp_app(rm, settings=...)` and charged through `ratelimit.charge(request, cost)`.
- `/v1/coverage` and the MCP coverage tool clamp their window to the recorded history and now, so a request cannot force reads of days that were never polled. The read model caches a UTC day's polls as final only if the day was over when it was read.
- Guard: dry-run decisions are recorded separately (`dry_run` fields; `trips[].dry_run`) and are never enforced by a later switch to enforce mode; dry-run and off still reopen a closure the guard applied. The poller `/healthz` reports `dry_run`.
- Deployment identifiers (`GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA`) are repository secrets so they are masked in public logs; `TIFA_DEPLOY=true` (a variable) enables the deploy job.
