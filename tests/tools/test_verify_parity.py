"""tools/verify_parity.py: a local recording against Tifa's store or API, with matching and failing cases."""

import io
import json

import httpx
import pytest

from tests.tools import recorder as r
from tifa.core.months import merge_month, month_episode
from tifa.core.timeutil import iso, month_bounds
from tifa.store.memory import MemoryStore
from tools import import_jsonl as ij
from tools import verify_parity as vp

MIN = 60
END = r.T0 + 300 * MIN
LOCAL = r.schedule(r.T0, END, 120)  # a recorder polling every two minutes
TIFA = r.schedule(r.T0 + 30, END + 30, 60)  # Tifa polling every minute, half a minute later
FROM = iso(r.T0 + 60)


def imported(directory, polls):
    """A store holding a recording of the truth at these polls, as Tifa would have recorded it."""
    r.record(directory, polls)
    store = MemoryStore()
    out, err = io.StringIO(), io.StringIO()
    args = [str(directory), "--status", str(directory / "tfl.json"), "--max-age", "0"]
    assert ij.main(args, store=store, out=out, err=err) == 0, err.getvalue()
    return store


@pytest.fixture
def local(tmp_path):
    directory = tmp_path / "local"
    r.record(directory, LOCAL)
    return directory


@pytest.fixture
def tifa(tmp_path):
    return imported(tmp_path / "tifa", TIFA)


def run(args, **kw):
    out, err = io.StringIO(), io.StringIO()
    code = vp.main([str(a) for a in args], out=out, err=err, **kw)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


def edit_month(store, month, fn):
    doc = store.get_month(month)
    store.put_month(month, fn(doc.payload))


def test_parity_holds_for_two_recordings_of_the_same_minutes(local, tifa):
    code, report, _ = run([local, "--from", FROM], store=tifa)
    assert code == 0 and report["ok"], report
    assert report["checks"] == {"episodes": True, "causes": True, "coverage": True}
    assert (
        report["matched"] == report["local"]["episodes"] == report["tifa"]["episodes"] == 4
    )  # central, northern, district, jubilee
    assert report["unmatched_local"] == report["unmatched_tifa"] == [] and report["cause_mismatch_count"] == 0
    assert 0 < report["max_start_diff_s"] <= 90 and report["max_end_diff_s"] <= 90
    assert report["tifa"]["coverage_pct"] == 100.0 and report["tifa"]["source"] == "store"
    assert report["window"]["to"] == iso(END - vp.TOLERANCE)  # the earlier last poll, less the tolerance


def test_a_missing_episode_fails_and_is_listed(local, tifa):
    edit_month(tifa, "2026-09", lambda p: {**p, "episodes": [e for e in p["episodes"] if e[0] != "central"]})
    code, report, _ = run([local, "--from", FROM], store=tifa)
    assert code == vp.EXIT_FAILED and report["checks"]["episodes"] is False
    assert [(e["line"], e["near_gap"]) for e in report["unmatched_local"]] == [("central", False)]
    assert report["unmatched_count"] == {"local": 1, "tifa": 0}
    assert run([local, "--from", FROM, "--max-unmatched", "1"], store=tifa)[0] == 0


def test_an_end_beyond_the_tolerance_does_not_match(local, tifa):
    def later(p):
        rows = [list(e) for e in p["episodes"]]
        for e in rows:
            if e[0] == "central":
                e[2] += vp.TOLERANCE + 60
        return {**p, "episodes": rows}

    edit_month(tifa, "2026-09", later)
    code, report, _ = run([local, "--from", FROM], store=tifa)
    assert code == 1 and report["unmatched_count"] == {"local": 1, "tifa": 1}
    assert run([local, "--from", FROM, "--tolerance", "400"], store=tifa)[0] == 0


def test_a_different_cause_fails(local, tifa):
    def recause(p):
        return {**p, "episodes": [[*e[:7], "power", *e[8:]] if e[0] == "central" else e for e in p["episodes"]]}

    edit_month(tifa, "2026-09", recause)
    code, report, _ = run([local, "--from", FROM], store=tifa)
    assert code == 1 and report["checks"] == {"episodes": True, "causes": False, "coverage": True}
    assert [(m["line"], m["local_cause"], m["tifa_cause"]) for m in report["cause_mismatches"]] == [
        ("central", "signal", "power")
    ]


def test_a_gap_in_tifa_fails_coverage_and_flags_the_episodes_it_split(local, tmp_path):
    gap = (r.T0 + 100 * MIN, r.T0 + 125 * MIN)  # 25 minutes: over GAP, so open episodes end early
    tifa = imported(tmp_path / "gappy", r.schedule(r.T0 + 30, END + 30, 60, skip=[gap]))
    code, report, _ = run([local, "--from", FROM], store=tifa)
    assert code == 1 and report["checks"]["coverage"] is False and report["tifa"]["coverage_pct"] < 99.5
    assert report["unmatched_tifa"] and all(e["near_gap"] for e in report["unmatched_tifa"])
    assert report["gaps"]["tifa"] and report["gaps"]["local"] == []


def test_an_open_episode_matches_one_closed_after_the_open_sides_last_poll(local, tmp_path):
    # Tifa stopped earlier than the local recorder: what it still has open, the local side closed later
    tifa = imported(tmp_path / "early", r.schedule(r.T0 + 30, r.T0 + 100 * MIN + 30, 60))
    code, report, _ = run([local, "--from", FROM, "--to", iso(r.T0 + 100 * MIN), "--min-coverage", "0"], store=tifa)
    assert code == 0, report
    assert report["matched"] == 3  # central (closed later locally), district, northern


def test_ends_agree_rules():
    closed = {"end": 1000}
    assert vp.ends_agree({"end": None}, {"end": None}, 1, 1, 150) == 0
    assert vp.ends_agree({"end": 1100}, closed, 1, 1, 150) == 100
    assert vp.ends_agree({"end": 1200}, closed, 1, 1, 150) is None
    assert vp.ends_agree({"end": None}, closed, 1100, 2000, 150) == 0  # open side has not polled since the end
    assert vp.ends_agree({"end": None}, closed, 1200, 2000, 150) is None  # it has, and should have closed it


# ---------------------------------------------------------------- the API


def api_transport(store, calls=None, busy=0):
    """Tifa's API as far as verify_parity reads it, served from a store: /v1/status and /v1/months/{m}."""
    state = store.get_state()
    meta = {"attribution": "Powered by TfL Open Data", "last_poll": iso(state.last_poll)}
    opens = [month_episode(dict(e, end=None), "a") for e in state.open]
    left = [busy]

    def handler(request):
        if calls is not None:
            calls.append(request.url.path)
        if left[0] > 0:
            left[0] -= 1
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"code": "rate_limited"}})
        if request.url.path == "/v1/status":
            data = {**state.status, "open": [{**e, "start": iso(e["start"])} for e in state.open],
                    "last_poll": iso(state.last_poll), "stale": False}  # fmt: skip
            return httpx.Response(200, json={"data": data, "meta": meta})
        month = request.url.path.rsplit("/", 1)[-1]
        doc = store.get_month(month)
        if doc is None:
            return httpx.Response(404, json={"error": {"code": "unknown_month"}, "meta": meta})
        _, b = month_bounds(month)
        payload = merge_month(doc.payload, [o for o in opens if o[1] < b], (), "a")
        return httpx.Response(200, json={"data": payload, "meta": meta})

    return httpx.MockTransport(handler)


def test_the_api_gives_the_same_result_as_the_store(local, tifa):
    calls = []
    code, report, _ = run([local, "--from", FROM, "--api", "https://tifa.example.org"],
                          transport=api_transport(tifa, calls))  # fmt: skip
    _, from_store, _ = run([local, "--from", FROM], store=tifa)
    assert code == 0 and report["tifa"]["source"] == "api"
    report["tifa"].pop("source"), from_store["tifa"].pop("source")
    assert report == from_store
    assert calls == ["/v1/status", "/v1/months/2026-09", "/v1/months/2026-10"]


def test_the_api_is_retried_after_429(local, tifa):
    slept = []
    code, _, _ = run([local, "--from", FROM, "--api", "https://tifa.example.org/"],
                     transport=api_transport(tifa, busy=2), sleep=slept.append)  # fmt: skip
    assert code == 0 and slept == [2.0, 2.0]


def test_an_api_error_is_reported(local):
    transport = httpx.MockTransport(lambda request: httpx.Response(500, text="no"))
    code, report, err = run([local, "--api", "https://tifa.example.org"], transport=transport)
    assert code == vp.EXIT_ERROR and report is None and "HTTP 500" in err


@pytest.mark.parametrize(
    "args",
    [
        [],  # neither --api nor --store
        ["--api", "https://tifa.example.org", "--store", "memory"],
        ["--api", "ftp://tifa.example.org"],
        ["--api", "https://tifa.example.org", "--from", "yesterday"],
        ["--store", "memory", "--from", iso(END), "--to", iso(r.T0)],
    ],
)
def test_usage_errors(local, args):
    assert run([local, *args])[0] == vp.EXIT_ERROR
