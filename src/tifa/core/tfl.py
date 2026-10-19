"""TfL line status: constants, severity ranks and categories, reasons, the current status and payload checks.

Severity codes come from https://api.tfl.gov.uk/Line/Meta/Severity and are not ordered by badness: 0-10 run from
Special Service to Good Service (lower is worse), but 11-20 are extra states such as Part Closed (11), Not Running
(16) and Service Closed (20). RANK orders them; lower is worse.
"""

import re
from collections.abc import Iterable, Iterator, Mapping

API_BASE = "https://api.tfl.gov.uk"
MODES = ("tube", "elizabeth-line", "overground", "dlr")
URL_PATH = "/Line/Mode/tube,elizabeth-line,overground,dlr/Status"
MODE_ORDER = {"tube": 0, "elizabeth-line": 1, "overground": 2, "dlr": 3}

# fmt: off
SEVERITY_NAMES = {
    0: "Special Service", 1: "Closed", 2: "Suspended", 3: "Part Suspended", 4: "Planned Closure",
    5: "Part Closure", 6: "Severe Delays", 7: "Reduced Service", 8: "Bus Service", 9: "Minor Delays",
    10: "Good Service", 11: "Part Closed", 12: "Exit Only", 13: "No Step Free Access", 14: "Change of frequency",
    15: "Diverted", 16: "Not Running", 17: "Issues Reported", 18: "No Issues", 19: "Information",
    20: "Service Closed",
}
GOOD = frozenset({10, 18})               # Good Service, No Issues
NOT_RECORDED = frozenset({10, 18, 20})   # and Service Closed (the end of the day): never an episode
RANK = {
    2: 0, 1: 0, 20: 0,     # Suspended, Closed, Service Closed
    16: 1, 4: 1,           # Not Running, Planned Closure
    3: 2,                  # Part Suspended
    5: 3, 11: 3,           # Part Closure, Part Closed
    6: 4,                  # Severe Delays
    7: 5, 8: 5,            # Reduced Service, Bus Service
    15: 6, 14: 6,          # Diverted, Change of frequency
    9: 7,                  # Minor Delays
    0: 8, 17: 8, 12: 8,    # Special Service, Issues Reported, Exit Only
    13: 9, 19: 9,          # No Step Free Access, Information
    10: 20, 18: 20,        # Good Service, No Issues
}
# fmt: on
UNKNOWN_RANK = 10  # a code TfL adds later: shown as disrupted, below the known problems


def rank(severity) -> int:
    """How bad a severity is; lower is worse."""
    return RANK.get(severity, UNKNOWN_RANK) if isinstance(severity, int) else UNKNOWN_RANK


class _Categories(Mapping[int, str]):
    """Severity -> category, read-only. Every code has one: CATEGORY(sev), CATEGORY[sev] and CATEGORY.get(sev) all
    give "other" for a code not listed (so .get's default is never used). len(), iteration and `in` cover the listed
    codes only. Use dict(CATEGORY) for a plain copy."""

    __slots__ = ("_known",)

    def __init__(self, known: Mapping[int, str]) -> None:
        self._known = dict(known)

    def __getitem__(self, severity) -> str:
        try:
            return self._known.get(severity, "other")
        except TypeError:  # unhashable: not a severity code
            return "other"

    def __contains__(self, severity) -> bool:
        try:
            return severity in self._known
        except TypeError:
            return False

    def __iter__(self) -> Iterator[int]:
        return iter(self._known)

    def __len__(self) -> int:
        return len(self._known)

    def __call__(self, severity) -> str:
        return self[severity]

    def __repr__(self) -> str:
        return f"CATEGORY({self._known!r})"


# fmt: off
CATEGORY = _Categories({
    9: "delays", 6: "delays", 7: "delays",                                   # Minor, Severe Delays, Reduced Service
    2: "suspended", 3: "suspended",                                          # Suspended, Part Suspended
    1: "closure", 4: "closure", 5: "closure", 11: "closure", 16: "closure",  # Closed, Planned/Part Closure, ...
    13: "info", 19: "info",                                                  # No Step Free Access, Information
})
# fmt: on
CATEGORIES = [  # in display order
    {"id": "delays", "label": "Delays"},
    {"id": "suspended", "label": "Suspended"},
    {"id": "closure", "label": "Closures"},
    {"id": "other", "label": "Other"},
    {"id": "info", "label": "Information"},
]
CATEGORY_IDS = tuple(c["id"] for c in CATEGORIES)


def category(severity) -> str:
    """The category of a severity code (the same as CATEGORY(severity))."""
    return CATEGORY(severity)


_PREFIX = re.compile(r"([^:\d]{1,40}):\s*")  # "Central Line:", "DOCKLANDS LIGHT RAILWAY:", "Waterloo & City line:"


def clean_reason(text, line_name: str) -> str:
    """Collapse whitespace and drop a leading "Bakerloo Line:" style prefix that repeats the line name."""
    text = " ".join(str(text).split())
    m = _PREFIX.match(text)
    if m:
        prefix = m.group(1).strip().casefold()
        if prefix == line_name.casefold() or prefix.endswith((" line", " railway")):
            text = text[m.end() :]
    return text[:1].upper() + text[1:]


class PayloadError(RuntimeError):
    """A TfL response that does not look like a real answer."""


def summarise(line: dict) -> dict:
    """One line's status now: {id, name, mode, severity (the worst), status, reasons, good, live}."""
    line_id = line.get("id")
    name = line.get("name") or line_id
    if not isinstance(line_id, str) or not line_id or not isinstance(name, str):
        raise PayloadError("TfL API returned a line without an id or name")
    statuses = [s for s in (line.get("lineStatuses") or []) if isinstance(s, dict)]
    if not statuses:  # not seen in practice; show the line as unknown rather than failing
        return {
            "id": line_id,
            "name": name,
            "mode": line.get("modeName"),
            "severity": None,
            "status": "No status reported",
            "reasons": [],
            "good": False,
            "live": False,
        }

    statuses.sort(key=lambda s: rank(s.get("statusSeverity")))
    descriptions, reasons, seen = [], [], set()
    for s in statuses:
        desc = " ".join(str(s.get("statusSeverityDescription") or "").split())
        if desc and desc not in descriptions:
            descriptions.append(desc)
        if s.get("reason"):
            text = clean_reason(s["reason"], name)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                reasons.append(text)
    worst = statuses[0].get("statusSeverity")
    return {
        "id": line_id,
        "name": name,
        "mode": line.get("modeName"),
        "severity": worst if isinstance(worst, int) else None,
        "status": ", ".join(descriptions) or "Unknown",
        "reasons": reasons,
        "good": all(s.get("statusSeverity") in GOOD for s in statuses),
        # a live problem (delays now) rather than planned works or information
        "live": any(
            ((s.get("disruption") or {}).get("category") == "RealTime") and s.get("statusSeverity") not in GOOD
            for s in statuses
        ),
    }


def current(raw: list) -> dict:
    """Every line's status now: {"lines", "disrupted", "total"}, live problems first, then planned works, then
    good service."""
    lines = [summarise(line) for line in raw if isinstance(line, dict)]
    if not lines:
        raise PayloadError("TfL API returned no usable lines")
    lines.sort(
        key=lambda s: (
            s["good"],
            not s["live"],
            rank(s["severity"]) if not s["good"] else 0,
            MODE_ORDER.get(s["mode"], 9),
            s["name"].casefold(),
        )
    )
    disrupted = sum(not s["good"] for s in lines)
    return {"lines": lines, "disrupted": disrupted, "total": len(lines)}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


LINE_RECENT = 86400  # seconds: lines seen this recently are the ones a response must mostly include


def recent_line_ids(lines: Mapping[str, Mapping], now_ts: int, within: int = LINE_RECENT) -> set[str]:
    """The ids of lines last seen no more than `within` seconds before now_ts, from {id: {"last_seen": epoch, ...}}
    (the state's line list): the known_line_ids for validate_payload(). A line TfL retires or renames drops out of
    the check after a day instead of failing every later poll."""
    out = set()
    for line_id, info in lines.items():
        seen = info.get("last_seen") if isinstance(info, Mapping) else None
        if _is_int(seen) and now_ts - seen <= within:
            out.add(line_id)
    return out


def _good_status(s) -> bool:
    """A lineStatuses entry that observe() and summarise() can read as TfL means it."""
    if not isinstance(s, dict) or not _is_int(s.get("statusSeverity")):
        return False
    disruption, periods = s.get("disruption"), s.get("validityPeriods")
    if disruption is not None and not isinstance(disruption, dict):
        return False
    return periods is None or (isinstance(periods, list) and all(isinstance(p, dict) for p in periods))


def validate_payload(raw, known_line_ids: Iterable[str] = ()) -> tuple[bool, str | None]:
    """Whether a decoded TfL response may be recorded: (True, None), or (False, code) with code one of

    - "not_list": not a JSON list; "empty": an empty list;
    - "bad_line": an item that is not an object with a non-empty string id and name, and a string (or no) modeName;
    - "bad_status": a line whose lineStatuses is not a non-empty list of objects, each with an integer
      statusSeverity, a disruption object or none, and a list of validity period objects or none;
    - "missing_lines": under 80% of known_line_ids present, when that set is not empty.

    A response that passes can be given to current() and observe() without error, and every line in it has a
    status, so a broken response cannot close every open episode. Pass recent_line_ids() as known_line_ids."""
    if not isinstance(raw, list):
        return False, "not_list"
    if not raw:
        return False, "empty"
    ids = set()
    for line in raw:
        if not isinstance(line, dict):
            return False, "bad_line"
        line_id, name, mode = line.get("id"), line.get("name"), line.get("modeName")
        named = isinstance(line_id, str) and line_id and isinstance(name, str) and name
        if not named or not (mode is None or isinstance(mode, str)):
            return False, "bad_line"
        statuses = line.get("lineStatuses")
        if not (isinstance(statuses, list) and statuses and all(_good_status(s) for s in statuses)):
            return False, "bad_status"
        ids.add(line_id)
    known = set(known_line_ids or ())
    if known and len(known & ids) * 5 < len(known) * 4:
        return False, "missing_lines"
    return True, None
