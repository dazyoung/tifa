"""tifa.core.causes: the 16 rules, their signature and what each message is put down to."""

import functools
import json
import re
from pathlib import Path

import pytest

from tifa.core import causes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "core"


@functools.cache
def golden() -> dict:
    return json.loads((FIXTURES / "golden_causes.json").read_text(encoding="utf-8"))


def test_rules_and_signature_are_the_references():
    g = golden()
    assert [list(c) for c in causes.CAUSES] == g["rules"]
    assert causes.PLANNED_WORKS.pattern == g["planned_works"]
    assert causes.CAUSES_SIG == g["sig"]


def test_every_case_matches_the_reference():
    cases = golden()["cases"]
    assert len(cases) > 500
    for reason, planned, status, expected in cases:
        assert causes.cause(reason, planned, status) == expected, (reason, planned, status)
    codes = {c for *_, c in cases}
    assert {code for code, _, _ in causes.CAUSES} | {None} == codes  # every rule is exercised


# ---- the reference's own cases ------------------------------------------------------------------------------

MESSAGES = [  # as TfL words them, with what each should be put down to
    (
        "Minor delays between White City and Ealing Broadway while we fix a signal failure at North Acton. "
        "GOOD SERVICE on the rest of the line",
        "signal",
    ),
    ("Severe delays due to an earlier signalling problem at Baker Street.", "signal"),
    ("Minor delays due to an earlier defective train at Blackhorse Road.", "train"),
    ("No service clockwise while we deal with a faulty train at Victoria.", "train"),
    (
        "No service between Baker Street and Aldgate due to emergency engineering work to fix a track fault at "
        "Euston Square.",
        "track",
    ),
    (
        "No service while we fix a points failure at Clapton. London Underground are accepting tickets via any "
        "reasonable route.",
        "track",
    ),
    ("No service between Wembley Park and Aldgate due to an earlier power failure.", "power"),
    ("Minor delays while we fix faulty communications equipment.", "equipment"),
    ("Severe delays while we help a person ill on a train at Victoria.", "customer"),
    ("Minor delays due to an earlier trespasser on the track at South Harrow.", "customer"),
    ("No service while emergency services deal with a casualty on the track.", "customer"),
    ("Minor delays between High Barnet and Camden Town while we remove an obstruction from the track.", "obstruction"),
    ("Severe delays due to a tree on the track in the Eastcote area.", "obstruction"),
    ("No service due to the temporary unavailability of control room staff.", "staff"),
    ("Minor delays due to a shortage of train operators.", "staff"),
    ("No service due to strike action by the RMT union.", "strike"),
    ("No service between Tooting Broadway and Morden due to a security alert at Colliers Wood.", "security"),
    ("Minor delays while police respond to an incident at Edgware.", "security"),
    ("Minor delays due to an earlier fire alert at Baker Street.", "fire"),
    ("No service between Stonebridge Park and Harrow & Wealdstone due to flooding.", "weather"),
    (
        "No service between Rayners Lane and Uxbridge due to difficult track conditions caused by significant "
        "leaf fall.",
        "weather",
    ),
    ("Severe delays following a late finish of engineering work at Ealing Broadway.", "engineering"),
    ("Minor delays due to train cancellations.", "cancellations"),
    ("Minor delays due to a shortage of trains.", "cancellations"),
    ("A 7 minute service is operating due to operational restrictions.", "restrictions"),
    ("No service between Queen's Park and Harrow & Wealdstone due to a Network Rail issue.", "network-rail"),
    ("Service will resume later this morning.", None),
    ("", None),
]


@pytest.mark.parametrize(("text", "expected"), MESSAGES)
def test_messages(text, expected):
    assert causes.cause(text) == expected


def test_no_reason_and_any_case():
    assert causes.cause(None) is None
    assert causes.cause("MINOR DELAYS DUE TO A SIGNAL FAILURE.") == "signal"


def test_order_and_planned_works_elsewhere():
    both = (
        "No service between Kennington and Moorgate due to planned engineering work. MINOR DELAYS between "
        "Acton Town and Uxbridge due to train cancellations."
    )
    assert causes.cause(both) == "cancellations"  # the delays: not the closure elsewhere
    assert causes.cause(both, planned=True) == "engineering"  # the closure itself
    assert causes.cause("No service due to planned engineering work.") == "engineering"  # nothing else
    assert causes.cause("Minor delays due to train cancellations after a signal failure.") == "signal"
    assert causes.cause("Minor delays due to a bridge strike at Putney.") == "obstruction"  # not a strike


def test_planned_and_status_fallbacks():
    assert causes.cause("Service will resume at 06:00 on Monday.", planned=True) == "engineering"
    assert causes.cause("No service due to strike action.", planned=True) == "strike"
    assert (
        causes.cause("A 15 minute service is operating between Baker Street and Watford.", False, "Special Service")
        == "restrictions"
    )
    assert causes.cause("Service resumes later this morning.", False, "Severe Delays") is None


def test_every_code_has_a_label_and_compiles():
    codes = [c for c, _, _ in causes.CAUSES]
    assert len(codes) == len(set(codes)) == 16
    assert all(label for _, label, _ in causes.CAUSES)
    assert codes[:3] == ["signal", "train", "track"]
    assert codes[-3:] == ["cancellations", "restrictions", "network-rail"]
    assert all(re.compile(rx) for _, _, rx in causes.CAUSES)
    assert list(causes.CAUSE_LABELS) == codes
    assert len(causes.CAUSES_SIG) == 12
