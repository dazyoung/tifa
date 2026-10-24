# ADR-0001: Record National Rail operator disruptions from National Rail Enquiries' Knowledgebase feeds

- Status: Proposed
- Date: 2026-10-24
- Builds on: Tifa's TfL recording ([data-model.md](../data-model.md), [contracts.md](../contracts.md))
- Plan: [docs/plan/national-rail.md](../plan/national-rail.md)

## Context

Tifa records the line status of the Underground, the Elizabeth line, London Overground and the DLR every minute and
serves the history through an open API and an MCP server. National Rail is not covered, and nobody publishes a
history of National Rail disruptions as an open API either.

The sources, as checked on 24 October 2026:

| Source | What it gives | Delivery | Terms |
| --- | --- | --- | --- |
| National Rail Enquiries (NRE) Knowledgebase **National Service Indicator** (NSI), v4.0 | Each train operator's status now (`Good service`, `Minor delays on all routes`, ...), its affected service groups (routes such as `Cambridge Route`) and the Knowledgebase incident behind each | XML over HTTPS from the Rail Data Marketplace (RDM); NRE recommends polling every 5 minutes | NRE licence (below) |
| NRE Knowledgebase **Incidents**, v5.0 | Every current incident: id, version, validity periods, planned or not, summary, description, affected operators (two-letter codes), routes affected (free text), priority, cleared | Same | Same |
| TfL Unified API, mode `national-rail` | 25 operators, but every problem is severity 0 "Special Service" in category "Information", with a nationalrail.co.uk link as its only reason; some entries are months old | The API Tifa already polls | TfL open data |
| NRE **Darwin** | Every train's forecasts, delays and cancellations | Push feeds | NRE licence |
| Network Rail **TRUST** and TD | Every train movement against the timetable | STOMP push, at most 1,000 users | Network Rail data feeds licence (Open Government Licence); no Network Rail or partner logos, nothing called "official" |
| Network Rail **historic delay attribution** | Delay minutes by cause and operator, yearly files | Download from RDM | Open Government Licence v3.0 |

The NSI and Incidents schemas are in NRE's Knowledgebase Data Feeds Specification (RSPS5050, P-03-00 Rev A,
18 November 2025), sections 9 and 10.

NRE's licence (NRE Developer Guidelines v05.01) is the Open Government Licence v2.0 with NRE's additions:

- access is personal to the registered user: the key is not passed on;
- the format of the information may be adapted, its content may not be amended;
- attribution: a link to the NRE website and "Powered by National Rail Enquiries" (as a logo, per NRE's brand
  guidelines) alongside the information where it is the main content, or on an attribution page where it is
  combined with other data;
- web services suit light automated use (up to 5,000 requests an hour); push feeds suit bulk capture;
- NRE gives no commitment on availability and may change formats.

RDM sets a licence per product. The Open Rail Data wiki lists both Knowledgebase products as Open; their RDM pages
can only be read with an account, so that is still to be confirmed.

## Decision

1. **Record National Rail at operator level from the NSI and Incidents feeds**, polled every 5 minutes through RDM.
   Not train-level data (Darwin, TRUST), and not TfL's `national-rail` mode.
2. **A second network beside the TfL one, sharing the core.** Operators are not lines, and existing clients read
   `/v1/*` as the four TfL modes, so National Rail gets its own state, months, polls and snapshots in the store and
   its own routes (`/v1/rail/...`) and MCP tools. The episode rules (`advance()`, the GAP rule), month payloads,
   period figures and cause matching are reused, with the gap and the rule set as parameters. The existing `/v1/*`
   routes, MCP tools, month format and `/v1/changes` stream do not change.
3. **Episodes per (operator, level, planned)**, with the same meaning as TfL episodes: an episode starts at the first
   poll that saw it and ends at the first poll that did not.
   - The level comes from the NSI status: minor delays, major delays, or other for anything else. Good service is
     not recorded. The mapping is fixed only after two weeks of recorded statuses, since the specification gives
     examples rather than a list.
   - An episode is planned when every incident linked from the operator's service groups is planned.
   - An episode carries its linked incident ids and the affected service groups; its reason is the linked
     incidents' summaries.
4. **Incidents are recorded in their own right**: first and last seen, validity periods, cleared, planned, priority,
   operators, routes, summary and description **as NRE sent them**, and a cause read from the description by a
   rail rule set (`RAIL_CAUSES`, with its own signature). It keeps the existing cause codes and adds rail ones
   (overhead lines, level crossings, trespass, a person hit by a train, landslips and flooding). TfL's rules and
   their signature stay as they are.
5. **Recorded at once, published only once the terms are confirmed in writing.** NRE data is a new source, `n`,
   behind `TIFA_PUBLIC_SOURCES`, as TfL's FOI release (`f`) is. Publishing waits for the RDM product licences and
   for NRE's data feeds team to confirm that republishing the derived history and incident text through an open
   API and MCP server is fine, and how they want it attributed there. Rail answers carry "Powered by National Rail
   Enquiries" with a link to nationalrail.co.uk, and "Not affiliated with or endorsed by National Rail Enquiries or
   the Rail Delivery Group."
6. **Operations:** a second Cloud Scheduler job (`tifa-poll-rail`, every 5 minutes) calls `POST /internal/poll-rail`
   on the existing private poller. The RDM key lives in Secret Manager (`tifa-rdm-api-key`), is mounted into the
   poller as `RDM_API_KEY` and is never logged. The poller parses the XML with `defusedxml` and size limits and
   passes plain dicts to the core, which stays standard-library only.

## Consequences

- Rail times are accurate to about 5 minutes, not 1. The rail GAP is 20 minutes (four missed polls), so one or two
  failed fetches do not end every episode; watched time and figures follow from it as they do for TfL.
- Coarser than TfL: "Minor delays on some routes" covers an operator's whole network. Service groups give some
  detail; episodes per service group can come later without changing the source.
- Incident text is NRE's own. Unlike TfL reasons, it is not trimmed or rewritten: only whitespace is collapsed and
  HTML turned into text, which changes the format, not the content.
- About $0.30 a month more: 8,640 extra poller runs and a second Scheduler job. Firestore stays within its free
  tier. The budget and the guard do not change.
- One new dependency, `defusedxml`.
- The maintainer registers on RDM, and the account has to stay in use: unused Knowledgebase accounts can expire.
- NRE may change the feeds' format and gives no availability commitment. Validation records a failed slot rather
  than closing episodes, as it does for TfL.
- Clients that want rail opt in through the new routes and tools; nothing changes for existing ones.

## Alternatives considered

- **TfL's `national-rail` mode.** No setup, but it says nothing: every problem is "Special Service" with a link.
- **Darwin or TRUST.** The real train-by-train picture (exact delays and cancellations), but they need an always-on
  connection, carry large volumes and take railway knowledge to read. That does not fit a poller that scales to zero
  between Cloud Scheduler calls. A separate decision if wanted.
- **The real-time incidents push feed.** Not offered on RDM, and it needs an always-on connection; 5-minute polling
  is NRE's own recommendation for these feeds.
- **RDM's "NationalRail Disruptions API"** (REST, Knowledgebase and Darwin delays combined). Newer, and its listing
  reports a broken incident lookup. Worth evaluating once registered, as a later or extra source.
- **Operators as a fifth mode inside `/v1/*`.** The least code, but it changes what every existing client gets: the
  network figures, the `/v1/changes` stream, the line list and the meaning of "lines".
- **Network Rail's historic delay attribution.** Open Government Licence v3.0, so unlike TfL's FOI release it could
  be published, giving causes for past years. Left for its own decision.

## Sources

- NRE, [Knowledgebase data feeds](https://www.nationalrail.co.uk/developers/knowledgebase-data-feeds/)
- NRE, [Knowledgebase Data Feeds Specification RSPS5050](https://www.rspaccreditation.org/downloadPublic.php?did=1LY5sWcQiy6zrUqIgjYxYSu4V1RkEy0hMLkNhTAe9DnRjL7egR) (P-03-00 Rev A, 18 November 2025)
- NRE, [Developer Guidelines v05.01](https://assets.nationalrail.co.uk/e8xgegruud3g/7yPy7gHJ7j3QZalp2zKZKJ/e32e5b871465c3a5f920e86cc07900d6/Developer_Guidelines_v_05-01.pdf)
- Rail Data Marketplace, [licensing guide](https://raildata.org.uk/helpAndInformation/guides/licensing)
- Network Rail, [open data feeds](https://www.networkrail.co.uk/who-we-are/transparency-and-ethics/transparency/open-data-feeds/) and [data feeds licence](https://www.networkrail.co.uk/who-we-are/transparency-and-ethics/transparency/open-data-feeds/network-rail-infrastructure-limited-data-feeds-licence)
- TfL Unified API, `GET /Line/Mode/national-rail/Status`, read on 24 October 2026
