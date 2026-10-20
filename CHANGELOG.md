# Changelog

All notable changes to Tifa are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The REST API is versioned separately (`/v1`); breaking API changes get a new prefix.

## Unreleased

### Added

- Project scaffold: uv project for Python 3.12+, MIT licence, security policy.
- `tifa.config`: settings from environment variables with safe local defaults, Cloud Run defaults and validation;
  fair-use and guard limits overridable through `TIFA_*` variables.
- `tifa.main`: one entry point for both services; `TIFA_ROLE` picks the API or the poller, uvicorn serves it on
  `$PORT`, and client addresses come from the right-most `X-Forwarded-For` entry. JSON logs for Cloud Logging with
  app keys redacted.
- Container image: Python 3.13 slim pinned by digest, locked dependencies installed with uv, non-root user.
- CI: Ruff, pytest on Python 3.12 and 3.13, every Firestore emulator test in the suite, image build and start-up
  check, gitleaks over the full history, workflow and shell script linting.
- Deployment on push to `main`: keyless Workload Identity Federation, one image pushed to Artifact Registry and
  deployed as two Cloud Run services (`tifa-api`, public; `tifa-poller`, private).
- Daily TfL canary that checks the shape of TfL's line-status response and opens an issue when it changes.
- `infra/bootstrap.sh` and `infra/post-deploy.sh`: idempotent Google Cloud setup and post-deploy wiring (Firestore,
  Artifact Registry, service accounts, Workload Identity Federation, Secret Manager, Pub/Sub, Cloud Scheduler,
  budget, log exclusion) and smoke tests.
- Secret and privacy scanning with gitleaks (pre-commit and CI) using custom rules for this public repository
  (including Google Cloud project IDs and private network addresses); private terms come from a CI secret rather
  than the public rules, and pull requests are also scanned with the base branch's rules and ignore list.
- `docs/operations.md`: bootstrap, deploy, secrets rotation, guard and budget behaviour, rollback and costs.
- The poller (`tifa.poller`), private: `POST /internal/poll` records TfL's line status once per Cloud Scheduler
  minute slot, at most once per slot, in one Firestore transaction that touches at most two months. TfL's answer is
  checked first (against the lines seen in the last day), so a broken answer records a failed slot instead of
  closing every episode; a failure answers 502 so Scheduler retries within the minute. TfL requests have connect,
  read and total timeouts, ask for gzip and never log the app key; when TfL refuses the key, the poller asks
  anonymously for 15 minutes and reports `tfl_auth: degraded`. Snapshots of TfL's answer are kept only when
  something changed, so the history can be recomputed. `POST /internal/budget` takes budget notifications from
  Pub/Sub; `GET /healthz` answers 503 when the last poll is over 10 minutes old.
- The fair-use guard: from the poll path at most every 2 minutes, it reads the public service's request counts and
  month-to-date usage from Cloud Monitoring and closes the public API (removing `allUsers` from its invoker binding,
  nothing else) on a flood, on an estimated month's cost over $2, or when the budget is spent; it reopens the API
  when the closure ends. `TIFA_GUARD_MODE=dry-run` decides and logs only. Recording never stops.
- The public API (`tifa.api`): `/v1/status`, `/v1/changes`, `/v1/lines`, `/v1/lines/{id}`, `/v1/months`,
  `/v1/months/{YYYY-MM}`, `/v1/disruptions`, `/v1/stats`, `/v1/coverage`, `/v1/meta`, `/healthz`, `/docs` and
  `/openapi.json` (also checked in as `docs/openapi.json`). Every response is a `{data, meta}` envelope carrying
  "Powered by TfL Open Data" and the not-affiliated notice, with a strong `ETag` and 304s, gzip, per-route
  `Cache-Control`, open CORS and `RateLimit-*` headers. It serves an in-memory copy of the store, refreshed at most
  every 30 seconds, and publishes only the sources in `TIFA_PUBLIC_SOURCES`. `/v1/changes` gives every poll in
  (`since`, `cursor`], so a client following the cursor gets each poll exactly once however many other clients the
  instance serves. `/docs` loads a pinned Swagger UI from one CDN and nothing else from third parties.
- Fair use in the API: a per-client token bucket (60 tokens, 1 a second; the right-most `X-Forwarded-For` entry,
  IPv6 by /64, addresses held in memory only), higher prices for uncached queries and MCP tool calls, a per-client
  share of the hourly data allowance (an eighth), and global limits on requests, uncached computations and egress
  (429 or 503 with `Retry-After`). A response is sent only when both data budgets have room for it, and a refused
  request takes nothing from the other limits.
- The MCP server at `/mcp` (MCP SDK 1.x, stateless streamable HTTP with JSON responses): `list_lines`,
  `current_status`, `line_status`, `disruptions`, `period_stats`, `coverage` and `causes`, sharing the REST views and
  carrying the attribution in every result; a Host check against `TIFA_ALLOWED_HOSTS` guards against DNS rebinding.
- Operator tools: `tools/import_jsonl.py` (import a local recording for a cutover without a gap),
  `tools/import_foi.py` (TfL's FOI release; refuses to run without `--i-have-reuse-permission`),
  `tools/verify_parity.py` (compare a local recording with Tifa's API or store), `tools/loadtest.py` (check the
  fair-use limits) and `tools/guard_cli.py` (show, close or reopen the public API by hand).
- `infra/first-deploy.sh` and `infra/lib.sh`: the first deploy, which creates both services with the operator's own
  credentials, and the resource names and service settings shared by the scripts and the Deploy workflow.
- Integration tests (`tests/integration/`): three simulated days of minute polls, including the night the clocks go
  back, and the night a UTC month ends (an episode in both months, the watched run split at the 1st, a TfL outage
  across midnight), through the real poller into an in-memory store, read back through the REST API, the MCP
  server, the operator tools and sync clients following `/v1/changes`, alone and beside other clients whose
  requests refresh the API's read model at other moments; an optional differential check against an independent
  implementation of the recorder (`TIFA_REFERENCE_DIR`).
- Documentation: the README, `docs/api.md`, `docs/data-model.md` and `docs/mcp.md`.

### Changed

- Tifa runs inside an existing Google Cloud project: every resource it creates is named `tifa-*` and labelled
  `app=tifa`, and nothing else is changed. Renamed: the image repository (`tifa-images`), the TfL key secret
  (`tifa-tfl-app-key`), the budget topic (`tifa-budget-alerts`) and its subscription, the guard's custom role
  (`tifa_guard`) and the Workload Identity pool (`tifa-github`).
- The CI deploy identity holds no project-wide role: it may only update the two Tifa services, push to
  `tifa-images` and run the services as their own accounts. The first deploy is the operator's
  (`infra/first-deploy.sh`); the Deploy workflow only updates the services and makes no IAM changes.
- The budget counts only resources labelled `app=tifa`. The scripts read their settings only from `infra/.env`
  (variables exported in the shell are ignored, unknown names are an error), and the `TIFA_API_SA` and
  `TIFA_POLLER_SA` repository variables are gone.
- TfL's FOI release is not published: its terms do not grant re-use, so `TIFA_PUBLIC_SOURCES` defaults to `a` (the
  live recording) everywhere and the scripts warn if it includes `f`.

### Fixed

- `infra/post-deploy.sh`: the smoke tests no longer stop silently part-way when a response header is missing.
- `/healthz` on the API reports the API as closed only once the guard has applied the closure, not while a dry run
  merely decides one.
- The MCP app mounted by the API takes its allowed hosts and tool-call cost from the settings the API app was built
  with, rather than from the process environment.
