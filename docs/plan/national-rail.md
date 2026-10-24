# Plan: National Rail operator disruptions

How to build [ADR-0001](../decisions/ADR-0001-national-rail-operator-disruptions.md): record National Rail
operators' status and incidents from National Rail Enquiries' (NRE) Knowledgebase feeds every 5 minutes, beside the
TfL recording, and publish them under `/v1/rail/...` once NRE's terms are confirmed.

Each phase ends in a pull request that leaves `main` deployable. Nothing is published before phase 5.

## 1. Access and terms (maintainer)

- [ ] Register on the Rail Data Marketplace (RDM) and subscribe to the Knowledgebase **National Service Indicator**
      and **Incidents** products.
- [ ] Note each product's licence, any fair-use limit, the endpoint and the authentication header from its RDM page.
      The endpoints and header go into the poller's configuration notes in this plan; nothing about the account
      goes in the repository.
- [ ] Add the key: `infra/bootstrap.sh rdm-key` (new, built like `tfl-key`: hidden prompt, stdin to Secret Manager
      as `tifa-rdm-api-key`, version printed, never echoed). Set `RDM_API_KEY_VERSION` like `TFL_APP_KEY_VERSION`.
- [ ] Write to NRE's data feeds team (address in the Developer Guidelines) with the questions below. Recording can
      start without the answer; publishing cannot.

Draft:

> Subject: Republishing Knowledgebase NSI and Incidents history through an open API
>
> Tifa (github.com/dazyoung/tifa, MIT) records London's TfL line status every minute and publishes the history as
> an open, rate-limited REST API and MCP server. We would like to add National Rail: polling the Knowledgebase
> National Service Indicator and Incidents feeds from the Rail Data Marketplace every 5 minutes and publishing
> (a) when each operator's status changed, with the linked incidents, and (b) each incident's summary and
> description unchanged, with derived figures such as time disrupted per operator and a cause read from the text.
>
> 1. Is republishing that history, and the incident text, through a public API and MCP server within the licence?
> 2. The API has no screen of its own. Is "Powered by National Rail Enquiries" with a link in every response's
>    metadata, in the README and in the API documentation the right attribution, and should we ask API users to
>    show the logo per your brand guidelines?
> 3. Is a 5-minute poll of each feed the right rate for this use?
>
> We would say clearly that Tifa is not affiliated with or endorsed by National Rail Enquiries or the Rail
> Delivery Group.

**Done:** the key is in Secret Manager, the licences are noted, the email is sent. Phase 5 waits for the reply.

## 2. Core (pure, no I/O)

Test fixtures are written by hand from the specification's examples until NRE confirms republishing: recorded feed
samples are not committed before then.

- `src/tifa/core/rail.py`:
  - constants: `GAP_RAIL = 1200`, `SLOT_RAIL = 300`, the level table (`minor`, `major`, `other`) as a function
    `level_of(status: str) -> str | None` (`None` for good service), conservative until phase 4;
  - `validate_nsi(nsi, known_operators) -> (ok, code)` and `validate_incidents(incidents) -> (ok, code)`, in the
    spirit of `validate_payload()`: the right shape, and at least 80% of the operators seen in the last day;
  - `observe_rail(nsi, incidents) -> (obs, meta)`: `obs = {(operator, level, planned): {"status", "reason",
    "incidents", "groups"}}`, where `reason` joins the linked incidents' summaries and `planned` is true when every
    linked incident is planned;
  - `incident_rows(incidents, seen, now_ts)`: each incident's first and last seen, validity periods, cleared,
    planned, priority, operators, routes, summary, description and cause;
  - `text_of(html) -> str`: tags to text and whitespace collapsed; no other change to NRE's words.
- `src/tifa/core/episodes.py`: `advance(..., gap=GAP)`, so rail passes `GAP_RAIL`; open episodes may carry extra
  fields (`incidents`, `groups`) that `advance()` keeps.
- `src/tifa/core/causes.py`: `RAIL_CAUSES` and `RAIL_CAUSES_SIG`, `rail_cause(text, planned)`. The existing codes
  plus `overhead-lines`, `level-crossing`, `trespass`, `person-hit` ("a person hit by a train"), `landslip` and
  `flooding`; planned incidents default to `engineering`. `CAUSES` and `CAUSES_SIG` do not change.
- `src/tifa/core/months.py` and `stats.py`: the entity name and source as parameters, so the same month payloads
  and `period_figures()` serve operators. A rail month payload adds `"incidents": [[id, first_seen, last_seen,
  cleared01, planned01, priority, operators, cause, summary, description, routes], ...]`; the episode rows keep the
  11 fields, with the operator code in the line field.
- `docs/contracts.md`: these interfaces, in the same pull request.

Tests: golden fixtures for `observe_rail()`, `incident_rows()`, `level_of()` and `text_of()`; `advance()` with
`gap=GAP_RAIL`; TfL golden fixtures unchanged (`CAUSES_SIG` too); a status the table does not know gives `other`,
never an error.

**Done:** the core suite passes on Python 3.12 and 3.13, and every existing golden test still passes byte for byte.

## 3. Poller, record only

- `src/tifa/poller/rdm_client.py`: httpx, timeouts as for TfL (connect 3 s, read 12 s, total 15 s), gzip, the key in
  a header and never logged, a size cap (8 MiB), `defusedxml` parsing to dicts. Add `defusedxml` to
  `pyproject.toml`.
- `src/tifa/poller/poll_rail.py`: `run_rail_poll(store, client, slot, now_ts)`, slot = 5-minute UTC slot. Both feeds
  are fetched; if either fails or fails validation, a failed slot is recorded and nothing else changes.
- Store: the same `Store` protocol with a namespace: `rail/state`, `rail_months/{YYYY-MM}`, `rail_polls/{YYYY-MM-DD}`,
  `rail_snapshots/{epoch}` (kept only when a feed's content hash changes). One transaction per poll, as for TfL.
- Poller app: `POST /internal/poll-rail`; `/health` gains a `rail` block with its last poll age. A stale rail poll
  does not make `/health` answer 503, so it never trips TfL's alerting.
- Settings: `RDM_API_KEY`, `RDM_NSI_URL`, `RDM_INCIDENTS_URL`; `SOURCES` gains `n`, not in the default
  `TIFA_PUBLIC_SOURCES`.
- Infra: `infra/bootstrap.sh` creates `tifa-poll-rail` (every 5 minutes, Etc/UTC, OIDC as `tifa-scheduler`, paused)
  and the secret; `infra/post-deploy.sh` points it at the poller. `docs/operations.md`: the job, the key, its
  rotation, the cost line.
- The API serves nothing new.

Tests: the poller against recorded-shape fixtures and an in-memory store; Firestore emulator tests for the namespace;
a failed or invalid fetch records a failed slot and closes nothing; a duplicate slot changes nothing.

**Done:** deployed with the job resumed, polling every 5 minutes, nothing public.

## 4. Two weeks of recording, then calibrate

- List every NSI status, status image and service group seen; fix the level table and document it in
  `docs/data-model.md`.
- Run `RAIL_CAUSES` over every recorded incident description. Unplanned incidents with no cause should be under a
  quarter; read the unknowns and add rules where a cause is common.
- Check ten incidents by hand against NRE's status pages: times within one poll, the right operators, the right
  planned flag.
- If the mapping or the rules change, rebuild the rail months from `rail_snapshots` (a `tools/recompute_rail.py`
  that runs the core over the snapshots, as the TfL history can be rebuilt).
- Coverage: rail watched time at least 99% over the two weeks.

**Done:** the level table and rules are fixed, documented and tested against anonymised examples.

## 5. Publish (after NRE's reply)

- Routes, mirroring the TfL ones under `/v1/rail/`: `status`, `changes?since=`, `operators`, `operators/{code}`,
  `months`, `months/{YYYY-MM}`, `disruptions`, `incidents`, `stats`, `coverage`. `/v1/meta` lists the networks and
  each one's attribution.
- Rail envelopes: `"attribution": "Powered by National Rail Enquiries"`, `"attribution_url":
  "https://www.nationalrail.co.uk/"`, `"notice": "Not affiliated with or endorsed by National Rail Enquiries or the
  Rail Delivery Group."`, and the licence line NRE asks for.
- MCP: `rail_status`, `rail_operator_status`, `rail_disruptions`, `rail_incidents`, `rail_period_stats` and
  `rail_coverage`, sharing `views.py` as the TfL tools do. Their descriptions say the times are accurate to about
  5 minutes and the status is per operator. The existing tools do not change.
- Fair use: the same buckets and costs; the guard counts the new routes with the rest.
- Docs: `api.md`, `mcp.md`, `data-model.md` (a National Rail section: levels, incidents, causes, GAP),
  `openapi.json`, the README's coverage and attribution, and the CHANGELOG.
- Set `TIFA_PUBLIC_SOURCES=a,n`.

**Done:** live, the integration suite covers the rail routes and tools, and a week's costs are within the estimate.

## Later, as their own decisions

- Episodes per service group (route), not only per operator.
- Network Rail's historic delay attribution (Open Government Licence v3.0) as a published history of causes.
- RDM's "NationalRail Disruptions API", or Darwin, for train-level punctuality and cancellations.

## Risks

| Risk | What happens | Mitigation |
| --- | --- | --- |
| NRE does not agree to republishing | Recorded but never public | Source `n` stays unpublished, as `f` does; the recording is still there if terms change |
| NSI statuses are free text | The level mapping misreads a status | Unknown statuses become `other`; phase 4 fixes the table from real data; snapshots allow a rebuild |
| A feed changes format | Polls fail validation | Failed slots, never closed episodes; `/health` shows rail staleness |
| The RDM account lapses | The key stops working | The poller keeps it in use; `/health` and the failed-slot codes show an auth failure |
| Incident text holds personal details | Published as received | NRE writes for the public; phase 4 reads a sample; incidents can be withheld by id if needed |
