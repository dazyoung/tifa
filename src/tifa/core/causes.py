"""What a disruption was put down to, from TfL's own words.

An episode's cause is the first rule below whose pattern its reason matches, case-insensitively. The order is also
the order in which causes are listed. It matters when a message names several things: the specific causes come
before the general ones (cancellations, restrictions, Network Rail). Unplanned episodes ignore sentences about
planned engineering work elsewhere on the line ("No service A to B due to planned engineering work. MINOR DELAYS
... due to a signal failure") unless nothing else matches; with no reason that matches, the status itself is tried
("Special Service": restrictions). Planned works are engineering unless they say otherwise (a strike, staff).
None means unknown. The rules were refined against TfL's 2020-2025 status messages.
"""

import functools
import hashlib
import re

CAUSES = (
    ("signal", "Signal failure", r"signal"),
    (
        "train",
        "Faulty train",
        r"fault(?:y)? (?:engineering |engineer's |engineers' |freight |empty )?trains?\b|"
        r"defective (?:freight )?trains?|train (?:fault|failure)|failed train|"
        r"broken[- ]down train|disabled train|derail\w*",
    ),
    (
        "track",
        "Track fault",
        r"track (?:fault|defect|failure|inspection|repairs?)|faulty track|fault with the track|"
        r"broken rails?|rail defect|points? (?:failure|fault|problem)|faulty points|"
        r"damaged track",
    ),
    (
        "power",
        "Power failure",
        r"power (?:failure|supply|cut|outage|problem|fault)|loss (?:of )?power|"
        r"electrical (?:fault|failure|problem)|overhead (?:wire|line)",
    ),
    (
        "equipment",
        "Faulty equipment",
        r"communications? (?:failure|equipment|fault|problem|system)|"
        r"faulty (?:communications|platform|tunnel|station|equipment|infrastructure)|"
        r"platform (?:edge )?doors?|platform equipment|ventilation|"
        r"equipment (?:fault|failure)|structural|lineside cable|escalators?|\blifts?\b",
    ),
    (
        "customer",
        "Customer incident",
        r"customer incident|(?:customer|passenger|public) action|"
        r"(?:person|passenger|customer)s? (?:who is |who has been |taken )?"
        r"(?:ill|unwell)\b|taken ill|(?:customer|person|people|passenger)s? on the track|"
        r"trespass\w*|casualt|causalit|injured (?:customer|person|passenger)|"
        r"medical emergency|person under a train|"
        r"(?:help|assist) a (?:customer|passenger|person)",
    ),
    (
        "obstruction",
        "Obstruction on the track",
        r"obstruct\w*|\btrees?\b|debris|\banimals?\b|"
        r"objects? (?:on|from) the track|items? (?:on|blowing)|fallen branch|"
        r"branch(?:es)? on|bridge strike|hitting a bridge",
    ),
    (
        "staff",
        "Staff shortage",
        r"(?:shortage|unavailab\w*|lack|availability|absence) of (?:available )?(?:\w+ ){0,3}?"
        r"(?:staff|train operators|drivers|operators|controllers)|"
        r"staff (?:shortage|availability|unavailability)",
    ),
    ("strike", "Strike", r"\bstrikes?\b|industrial action"),
    (
        "security",
        "Police or security",
        r"police|security (?:alert|incident|issue)|suspicious (?:item|package|object)|"
        r"emergency services|unattended (?:item|bag)|vandal\w*|assault|gas leak",
    ),
    ("fire", "Fire alert", r"\bfire\b|smoke"),
    (
        "weather",
        "Weather",
        r"flood\w*|weather|\bheat\b|high temperatures?|\bsnow|\bice\b|\bicy\b|frost|leaf fall|"
        r"\bleaves\b|high winds|strong winds|\bstorm|lightning|difficult track conditions|"
        r"poor rail conditions|adhesion|slippery rails?",
    ),
    (
        "engineering",
        "Engineering works",
        r"engineering|improvement works|upgrade works|planned works|maintenance|"
        r"planned closure",
    ),
    (
        "cancellations",
        "Train cancellations",
        r"trains? cancell?ations?|cancell?ed trains?|shortage of trains|"
        r"fewer trains",
    ),
    (
        "restrictions",
        "Operational restrictions",
        r"operational restrictions?|operating restrictions?|reduced service|"
        r"special service|speed restrictions?",
    ),
    ("network-rail", "Network Rail", r"network rail|national rail"),
)
CAUSE_LABELS = {code: label for code, label, _ in CAUSES}
PLANNED_WORKS = re.compile(r"[^.]*\bplanned (?:engineering )?(?:work|works|closure)s?\b[^.]*\.?", re.I)
_CAUSE_RX = tuple((code, re.compile(rx, re.I)) for code, _, rx in CAUSES)
_ENGINEERING = dict(_CAUSE_RX)["engineering"]
# changes when the rules do, so that stored causes can be computed again
CAUSES_SIG = hashlib.sha256(repr((CAUSES, PLANNED_WORKS.pattern)).encode()).hexdigest()[:12]


def first_cause(text: str) -> str | None:
    """The code of the first rule that matches text, or None."""
    for code, rx in _CAUSE_RX:
        if rx.search(text):
            return code
    return None


@functools.lru_cache(maxsize=4096)
def cause(reason, planned: bool = False, status=None) -> str | None:
    """The cause code of an episode from its reason, whether it was planned and its status; None if unknown."""
    text = reason if isinstance(reason, str) else ""
    if planned:
        return "engineering" if _ENGINEERING.search(text) else first_cause(text) or "engineering"
    found = (first_cause(PLANNED_WORKS.sub(" ", text)) or first_cause(text)) if text else None
    if found is None and isinstance(status, str):
        found = first_cause(status)
    return found
