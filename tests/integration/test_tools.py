"""The operator tools against the recorded days: a cutover from a local recording with tools/import_jsonl.py, and a
parallel run checked with tools/verify_parity.py through the real API app.

The local recording stands in for a recorder that polled TfL itself, every two minutes at half past: it samples the
same truth, so its episodes start and end up to 90 seconds later than Tifa's, and it never saw Tifa's outages.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from tests.integration import scenario as sc
from tests.integration.conftest import BASE, make_api, simulate
from tests.tools.recorder import episode_record
from tifa.core.episodes import advance, observe
from tifa.core.tfl import current
from tifa.core.timeutil import iso
from tifa.store.memory import MemoryStore
from tools import import_jsonl, verify_parity

OCTOBER = "2026-10"
LOCAL_EVERY = 120
CUT = sc.ldn(25, 12, 30) + 30  # a local poll: the district closure and Piccadilly's information are open


def local_times(until: int = sc.END, *, outage: bool = False) -> list[int]:
    """The local recorder's polls; with outage, it too got nothing from TfL during Tifa's twenty-minute outage."""
    a, b = sc.LONG_OUTAGE[0] * 60, (sc.LONG_OUTAGE[-1] + 1) * 60
    return [t for t in range(sc.START + 30, until + 1, LOCAL_EVERY) if not (outage and a <= t < b)]


def record_locally(directory: Path, times: list[int]) -> None:
    """A local recording of the scenario's truth at these poll times, in the files import_jsonl reads."""
    directory.mkdir(parents=True)
    state, raw = {"last_poll": None, "open": []}, None
    with open(directory / "episodes.jsonl", "w") as eps, open(directory / "polls.jsonl", "w") as polls:
        for ts in times:
            raw = sc.payload(ts)
            obs, meta = observe(raw)
            state, closed, _ = advance(state, obs, meta, ts)
            for ep in closed:
                eps.write(json.dumps(episode_record(ep)) + "\n")
            polls.write(json.dumps({"at": iso(ts)}) + "\n")
    last = iso(state["last_poll"])
    open_doc = {"version": 1, "last_poll": last, "open": [dict(e, start=iso(e["start"])) for e in state["open"]]}
    (directory / "open.json").write_text(json.dumps(open_doc))
    (directory / "tfl.json").write_text(json.dumps({"ok": True, "fetched_at": last, "data": current(raw)}))


def forward(http: TestClient) -> httpx.MockTransport:
    """An httpx transport that hands each request to the app under test."""

    def handle(request: httpx.Request) -> httpx.Response:
        r = http.request(request.method, request.url.raw_path.decode(), headers={"Accept": "application/json"})
        headers = {k: v for k, v in r.headers.items() if k not in ("content-encoding", "content-length")}
        return httpx.Response(r.status_code, headers=headers, content=r.content)

    return httpx.MockTransport(handle)


def parity(directory: Path, http: TestClient, *args: str) -> tuple[int, dict]:
    out, err = io.StringIO(), io.StringIO()
    argv = [str(directory), "--api", "https://tifa.example.org", *args]
    code = verify_parity.main(argv, transport=forward(http), sleep=lambda s: None, out=out, err=err)
    assert out.getvalue(), err.getvalue()
    return code, json.loads(out.getvalue())


@pytest.fixture(scope="module")
def local(tmp_path_factory) -> Path:
    """A local recording that saw TfL's outage too, as a TfL outage is everyone's."""
    directory = tmp_path_factory.mktemp("local") / "recording"
    record_locally(directory, local_times(outage=True))
    return directory


@pytest.fixture(scope="module")
def local_unbroken(tmp_path_factory) -> Path:
    """A local recording that kept polling while Tifa got nothing (Tifa's outage alone)."""
    directory = tmp_path_factory.mktemp("local") / "recording"
    record_locally(directory, local_times())
    return directory


@pytest.fixture(scope="module")
def tifa(world):
    app, _, _ = make_api(world.store, sc.NOW)
    with TestClient(app, base_url=BASE) as http:
        yield http


def test_a_parallel_run_holds_parity_through_the_api(local, tifa):
    # the whole three days (to the earlier last poll, less the tolerance); the 21 minutes TfL was down for both
    # sides are under 1% of them
    code, report = parity(local, tifa, "--from", iso(sc.START + 600), "--min-coverage", "99")
    assert code == 0 and report["ok"], report
    assert report["checks"] == {"episodes": True, "causes": True, "coverage": True}
    assert report["matched"] == report["local"]["episodes"] == report["tifa"]["episodes"] == 18
    assert report["max_start_diff_s"] <= 90 + 7 and report["max_end_diff_s"] <= 90 + 7
    assert report["tifa"]["coverage_pct"] > 99.4 and report["tifa"]["source"] == "api"


def test_parity_fails_across_an_outage_only_tifa_had_and_says_why(local_unbroken, tifa):
    code, report = parity(local_unbroken, tifa, "--from", iso(sc.ldn(25, 12)), "--to", iso(sc.ldn(25, 20)))
    assert code == 1 and not report["ok"]
    assert report["checks"]["coverage"] is False and report["checks"]["episodes"] is False
    assert report["tifa"]["coverage_pct"] == pytest.approx(100 * (1 - 21 / 480), abs=0.05)
    # Tifa's gap split the district closure and Piccadilly's information in two; the recording kept each whole
    assert {(e["line"], e["end_estimated"]) for e in report["unmatched_tifa"]} == {
        ("district", True),
        ("district", False),
        ("piccadilly", True),
        ("piccadilly", False),
    }
    assert all(e["near_gap"] for e in report["unmatched_tifa"])
    assert {e["line"] for e in report["unmatched_local"]} == {"district", "piccadilly"}
    assert len(report["gaps"]["tifa"]) == 1 and report["gaps"]["local"] == []


def test_a_cutover_from_a_local_recording_carries_on_without_a_gap(tmp_path, world):
    directory = tmp_path / "recording"
    record_locally(directory, local_times(CUT))
    store = MemoryStore()
    out, err = io.StringIO(), io.StringIO()
    args = [str(directory), "--status", str(directory / "tfl.json")]
    assert import_jsonl.main(args, store=store, now=CUT + 20, out=out, err=err) == 0, err.getvalue()
    report = json.loads(out.getvalue())
    assert report["ok"] and report["recording"]["last_poll"] == iso(CUT) and report["state"]["open"] == 2
    assert store.get_import(report["import_id"])["last_poll"] == CUT

    # Tifa's poller takes over from the next minute
    simulate(store, sc.FakeTfl(), [a for a in sc.attempts() if a.at > CUT])
    after, whole = store.get_month(OCTOBER).payload, world.store.get_month(OCTOBER).payload

    # the recording's open episodes carried on with the start times it gave them
    district = [r for r in after["episodes"] if r[0] == "district"]
    assert (
        district[0][1] == sc.START + 30
        and district[0][2:] == [r for r in whole["episodes"] if r[0] == "district"][0][2:]
    )
    # episodes that began after the cutover are exactly those of three days of Tifa
    assert [r for r in after["episodes"] if r[1] > CUT] == [r for r in whole["episodes"] if r[1] > CUT]
    # and those before it are the recording's, a poll or so later than Tifa's
    before = [r for r in after["episodes"] if r[1] <= CUT]
    twins = [r for r in whole["episodes"] if r[1] <= CUT]
    assert [(r[0], r[3]) for r in before] == [(r[0], r[3]) for r in twins]
    assert all(0 <= a[1] - b[1] <= 97 for a, b in zip(before, twins, strict=True))
    # one watched run from the recording's first poll to Tifa's long outage: the cutover left no gap
    assert after["watched"]["a"] == [[sc.START + 30, world.expected.runs[0][1]], world.expected.runs[1]]
    state = store.get_state()
    assert state.last_poll == world.polls[-1] and state.open == world.store.get_state().open
