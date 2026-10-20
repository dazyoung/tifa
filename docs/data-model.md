# Data model

What Tifa records, what the numbers mean, and how the history is stored. The API reference is
[api.md](api.md); the internal interfaces are in [contracts.md](contracts.md).

## What is recorded

Once a minute Tifa asks TfL's Unified API for the status of every line of four modes:

```
GET https://api.tfl.gov.uk/Line/Mode/tube,elizabeth-line,overground,dlr/Status
```

That is the Underground, the Elizabeth line, London Overground and the DLR: 19 lines today. TfL keeps no history of
this answer, so each poll records what is in effect at that moment. Buses, trams, river and National Rail are not
covered.

The poll is driven by Cloud Scheduler, once per UTC minute ("slot"). A slot is recorded at most once: a retry of
the same slot, or a late attempt after a later slot, changes nothing. Because the slots are UTC minutes, the London
clock changes have no effect on polling: in October the London hour 01:00-02:00 happens twice and has 120 polls, and
in March the hour London skips is simply not on its clocks, while the polls carry on every UTC minute.

### What one poll sees

A poll's **observations** are the line statuses in effect now:

- a status counts if one of its validity periods says `isNow`, or it has none (works announced for later are left
  out);
- Good Service (10), No Issues (18) and Service Closed (20, the end of the day) are never recorded;
- each observation is keyed by **(line, severity, planned)**, where `planned` means TfL's disruption category is
  `PlannedWork`.

Two statuses with the same key on one line (a Part Closure on two stretches) are one observation, with their distinct
reasons joined. Reasons are TfL's own text, with whitespace collapsed, the leading "Central Line:" style prefix
removed and at most 400 characters kept.

Before anything is recorded, TfL's answer is checked: it must be a list of lines, each with an id, a name and a list
of statuses with integer severities, and it must include at least 80% of the lines seen in the last 24 hours. An
answer that fails (or a timeout, a network error, an HTTP error, an oversized or undecodable body) is recorded as a
**failed slot** with a code (`timeout`, `network`, `http_503`, `too_large`, `bad_json`, `not_list`, `missing_lines`,
and so on) and changes nothing else. So a broken answer can never close every open episode.

## Episodes

An **episode** is a run of polls that saw the same (line, severity, planned):

- it **starts** at the first poll that saw it;
- it **ends** at the first poll that did not;
- while it is open its status text, line name and reason follow the latest poll (a reason is kept while TfL gives
  none).

So start and end are accurate to about a minute, and a line that moves from Severe Delays to Minor Delays has two
episodes, the second starting at the poll where the first ends. Two different statuses at once on one line (planned
works and an unplanned delay) are two episodes that overlap.

| Field | Meaning |
| --- | --- |
| `line`, `name`, `mode` | TfL's line id (`central`, `elizabeth`, `dlr`, `weaver`, ...), its name and mode (`tube`, `elizabeth-line`, `overground`, `dlr`) |
| `severity`, `status` | TfL's status severity code and its description (table below) |
| `category` | `delays`, `suspended`, `closure`, `other` or `info`, from the severity |
| `planned` | TfL called it planned work |
| `start`, `end` | the polls described above; `end` is `null` while the episode is open |
| `end_estimated` | the episode ended at a gap in recording (below), not at a poll that saw it gone |
| `reason` | TfL's reason, cleaned as above; may be `null` |
| `cause` | what the reason puts it down to (below); `null` is unknown |
| `source` | `a` for Tifa's live recording |

### Gaps: the GAP rule

`GAP` is 900 seconds (15 minutes). When two successful polls are more than `GAP` apart, nobody was watching in
between. At the poll after such a gap, every episode that was open ends at the **last poll before the gap**, marked
`end_estimated: true`; anything still in effect then starts a new episode at that poll.

Shorter gaps (a few failed slots) change nothing: episodes carry on across them.

While a gap lasts, nobody knows whether the open episodes are still going on, so the API shows them as of the last
poll: their `minutes` and the figures count only to it. When polling resumes, the rule above ends them there.

The **watched** time of the live recording is the runs of polls no more than `GAP` apart, from the first poll of a
run to its last. The time between runs is unwatched.

**A gap is not good service.** Unwatched time counts neither as good service nor as disruption: every percentage
Tifa gives is of watched time, and `/v1/coverage` and each period's `watched` share say how much of a period was
watched. A quiet line during a gap means only that nobody looked.

## Categories

| Category | Severities |
| --- | --- |
| `delays` | 6 Severe Delays, 7 Reduced Service, 9 Minor Delays |
| `suspended` | 2 Suspended, 3 Part Suspended |
| `closure` | 1 Closed, 4 Planned Closure, 5 Part Closure, 11 Part Closed, 16 Not Running |
| `other` | 0 Special Service, 8 Bus Service, 12 Exit Only, 14 Change of frequency, 15 Diverted, 17 Issues Reported, and any code TfL adds later |
| `info` | 13 No Step Free Access, 19 Information |

Not recorded: 10 Good Service, 18 No Issues, 20 Service Closed.

`info` episodes are recorded and listed, but they are **not counted as disruption** anywhere in the figures.

TfL's severity codes are not ordered by badness (0-10 run from Special Service to Good Service, 11-20 are extra
states). Where Tifa sorts by how bad something is (the worst status of a line, the order of `/v1/status`) it uses its
own ranking: suspended and closed first, then not running and planned closures, part suspended, part closures, severe
delays, reduced service and bus service, diversions and changes of frequency, minor delays, special service and
other notices, information, and last any code it does not know yet.

## Causes

Each episode's `cause` is read from TfL's reason by 16 rules, tried in this order; the first whose pattern matches
wins. Specific causes come before general ones, so "minor delays after a signal failure, train cancellations"
is a signal failure.

| Code | Label |
| --- | --- |
| `signal` | Signal failure |
| `train` | Faulty train |
| `track` | Track fault |
| `power` | Power failure |
| `equipment` | Faulty equipment |
| `customer` | Customer incident |
| `obstruction` | Obstruction on the track |
| `staff` | Staff shortage |
| `strike` | Strike |
| `security` | Police or security |
| `fire` | Fire alert |
| `weather` | Weather |
| `engineering` | Engineering works |
| `cancellations` | Train cancellations |
| `restrictions` | Operational restrictions |
| `network-rail` | Network Rail |

- Planned works are `engineering` unless the reason says otherwise (a strike, staff).
- For unplanned episodes, sentences about planned engineering work elsewhere on the line are ignored, unless nothing
  else matches.
- With no matching reason, the status itself is tried (Special Service reads as `restrictions`).
- Otherwise the cause is `null`: **unknown**. The API's `cause=unknown` filter finds these, and the period figures
  always list Unknown among the top causes when it is there.

The rules were refined against TfL's 2020-2025 status messages. `/v1/meta` and `/v1/months` list the codes and
labels; the MCP `causes` tool lists them too, with a `signature` that changes whenever the rules do.

## Months

The history is kept and served by UTC calendar month (`YYYY-MM`). A month payload:

```json
{
  "month": "2026-10",
  "episodes": [
    ["central", 1792824066, 1792828625, 9, "delays", 0, "a", "signal", "Minor Delays",
     "Minor delays while we fix a signal failure at Liverpool Street.", 0]
  ],
  "watched": {"a": [[1792796407, 1792940941], [1792942201, 1793059144]]}
}
```

Each episode row is `[line, start, end, severity, category, planned01, source, cause, status, reason,
end_estimated01]`, times in epoch seconds:

- the first 8 fields are the established month format, so a client that reads only those keeps working;
- an episode is in **every month it overlaps**: one that runs over a month end is in both (an episode ending exactly
  at a month's start is not in that month);
- rows are sorted by start; stored months hold closed episodes, and the API adds the open ones with `end: null`;
- `watched` holds, per published source, the spans of the month that source watched.

## Time

- Internally every time is an integer of **epoch seconds** (UTC).
- The API shows times as **ISO 8601 in UTC** (`2026-10-25T01:40:03+00:00`), except month payloads and the cursor and
  poll times of `/v1/changes`, which stay epoch seconds.
- Months are **UTC** calendar months.
- **Days and hours shown to people are London time.** A London day runs from midnight to midnight and is 23, 24 or
  25 hours long: Sunday 25 October 2026 has 25 hours, because 01:00-02:00 happens twice. A date given to the API
  (`2026-10-25`) means that London day. `starts_by_hour` counts London hours.

## Period figures

`/v1/stats` (and the MCP `period_stats` tool) gives figures for a period of 1, 7, 30, 90 or 365 days, ending now or
on a London date (whole London days).

- **The period before** has the same length and ends where this one starts. Changes against it are given only when
  both were at least 80% watched (`comparable`).
- **Watched**: the share of the period each source watched, and the unwatched spans.
- A line counts when it was watched in the period (`lines_watched`). The live recording watches every line it polls.
- **Line-time disrupted** (`disrupted_s`): per line, the time with at least one non-`info` episode, inside watched time
  (two at once count once), added up over the lines.
- **Good service** (`good_pct`): 100% minus line-time disrupted as a share of line-time watched.
- **Unplanned** and **planned**: how many non-`info` episodes overlap the period.
- **Most disrupted line**: the highest share of its watched time disrupted.
- **Longest unplanned**: the longest unplanned episode overlapping the period (its whole length).
- **Busiest day** (periods of 7 days or more): the London day with the most unplanned line-time.
- **Causes**: the six commonest by number of episodes (then time), with Unknown always shown when present, and the
  rest together.
- **Bins**: line-time disrupted per London day (up to 30 days) or per week starting Monday (longer), split by
  category and planned.
- **Per line**: watched and disrupted time, good service now and before, counts by category, average and median
  length, longest unplanned episode, the share of disrupted time in TfL's weekday core hours (07:00-09:30 and
  16:30-19:00 London), the days with unplanned disruption, and the top cause.

An episode still open runs to the last watched time. Shares are 0 to 1, good service is a percentage, times are
epoch seconds and durations seconds.

`/v1/lines/{id}` (MCP `line_status`) gives one line's figures over the last 24 hours, 7, 30 and 90 days, from the
live recording, with the share of each window watched.

## Sources and licences

| Source | What | Published |
| --- | --- | --- |
| `a` | Tifa's live recording, from its first poll | yes |
| `f` | TfL's freedom-of-information release of its own status records, 2020 to February 2025 | **no** |

Information released under the Freedom of Information Act is released to the person who asked; the Act gives access,
not a licence to re-use or publish, and TfL's release grants no such permission. So Tifa publishes only its own
recording.
`TIFA_PUBLIC_SOURCES` defaults to `a`, rows and watched spans of any other source are dropped when the API loads a
month, and `tools/import_foi.py` refuses to run without `--i-have-reuse-permission`. Should TfL grant re-use in
writing, setting `TIFA_PUBLIC_SOURCES=a,f` publishes it. (The FOI release reports Circle and Hammersmith & City as
one shared line, `circle-hammersmith-city`; that line appears only if `f` is published.)

The live recording is TfL open data, re-used under TfL's open data terms (based on the Open Government Licence v2.0):
"Powered by TfL Open Data". Tifa is not affiliated with or endorsed by Transport for London.

## Storage

Firestore, in one database:

| Document | Holds |
| --- | --- |
| `meta/state` | the last poll and slot, the current watched run, the open episodes, the lines seen (first and last seen), the current status (gzipped), the month revision index, TfL auth state |
| `months/{YYYY-MM}` | the month payload, gzipped, with its revision, size and SHA-256 |
| `polls/{YYYY-MM-DD}` | per UTC day: the poll times and the failed slots with their codes |
| `snapshots/{epoch}` | the parts of TfL's answer the recording reads, kept only when the status changed or an episode opened or closed |
| `guard/state` | the fair-use guard's state |
| `imports/{id}` | an audit record of each import |

Each poll is one transaction: the state, the months it touched (at most two), the poll time and any snapshot commit
together or not at all. The snapshots and poll times are enough to recompute every episode, for example after a
change to the rules.

The API never reads Firestore per request: it serves a copy in memory, refreshed from `meta/state` at most every 30
seconds and reloading only the months whose revision changed.
