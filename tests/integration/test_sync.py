"""Keeping a copy in sync with /v1/changes while the three days are recorded.

The world fixture's sync client visits /v1/changes every 97 minutes (and just after the clock change and the long
outage), each time from the cursor of the answer before, and merges what it gets as a copy kept elsewhere would:
polls by time, closed episodes on (line, severity, planned, start). Its copy must end up equal to the history.

That client is alone and moves the API's monotonic clock 60 s per visit, so every cache in the API expires between
two visits. The last tests run every clock as time passes, with other clients' requests between the visits, so the
read model refreshes at moments the sync client does not choose: each poll must still reach each copy once.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from tests.integration import scenario as sc
from tests.integration.conftest import BASE, make_api
from tifa.core.timeutil import iso


def test_every_visit_is_answered_from_its_cursor(world):
    assert len(world.visits) > 40
    for v in world.visits:
        assert v.status == 200, v
        last = max(t for t in world.polls if t <= v.at)
        assert v.data["cursor"] == last and v.data["last_poll"] == iso(last)
        window = [t for t in world.polls if v.since < t <= last]
        assert v.data["polls"] == sorted(set(v.data["polls"])) and set(v.data["polls"]) <= set(window)
        assert all(e["end"] >= iso(v.since) for e in v.data["closed"])
        assert v.data["stale"] is False


def test_the_copy_ends_up_equal_to_the_history(world, api):
    closed = {}
    for v in world.visits:
        for e in v.data["closed"]:
            closed[(e["line"], e["severity"], e["planned"], e["start"])] = e
    history = api.get("/v1/disruptions", params={"from": "2026-10-24", "to": "2026-10-26", "limit": 500}).json()
    want = [e for e in history["data"]["episodes"] if e["end"] is not None]
    assert sorted(closed.values(), key=lambda e: (e["start"], e["line"], e["severity"])) == want
    final = world.visits[-1].data
    assert [(e["line"], e["start"]) for e in final["open"]] == [
        (e["line"], iso(e["start"])) for e in world.expected.open
    ]


def test_the_copy_gets_every_poll_once(world):
    polls = [t for v in world.visits for t in v.data["polls"]]
    assert polls == world.polls  # each poll once, none missing, across the clock change and UTC midnights


def test_a_poll_just_before_utc_midnight_reaches_the_next_visit():
    from tests.integration.conftest import RecordingStore, simulate

    store, client = RecordingStore(), sc.FakeTfl()
    midnight = sc.ldn(25, 1)  # 00:00 UTC on 25 October
    attempts = [a for a in sc.attempts() if midnight - 600 <= a.slot * 60 < midnight + 120]
    app, wall, mono = make_api(store, attempts[0].at)
    with TestClient(app, base_url=BASE) as http:
        before = [a for a in attempts if a.slot * 60 < midnight - 60]
        simulate(store, client, before)
        wall.t, mono.t = before[-1].at + 30, mono.t + 60
        cursor = http.get("/v1/changes", params={"since": before[0].at}).json()["data"]["cursor"]
        simulate(store, client, [a for a in attempts if a not in before])  # 23:59 and 00:00 and 00:01 UTC
        wall.t, mono.t = attempts[-1].at + 30, mono.t + 60
        data = http.get("/v1/changes", params={"since": cursor}).json()["data"]
    assert data["polls"] == [a.at for a in attempts if a.at > cursor]


def test_the_visit_after_the_clock_change_has_every_minute(world):
    (visit,) = [v for v in world.visits if v.since < sc.CLOCKS_BACK < v.at]
    minutes = [t // 60 for t in visit.data["polls"]]
    assert minutes == list(range(minutes[0], minutes[-1] + 1))


def test_the_visit_after_the_long_outage_gets_the_episodes_it_ended(world):
    last_before = max(t for t in world.polls if t < sc.LONG_OUTAGE[0] * 60)
    (visit,) = [v for v in world.visits if v.since <= last_before < v.at]
    estimated = {(e["line"], e["end"]) for e in visit.data["closed"] if e["end_estimated"]}
    assert estimated == {("district", iso(last_before)), ("piccadilly", iso(last_before))}


def test_an_unchanged_answer_is_304_even_once_recomputed(world):
    app, wall, mono = make_api(world.store, sc.NOW)
    with TestClient(app, base_url=BASE) as http:
        params = {"since": world.polls[-1]}
        first = http.get("/v1/changes", params=params)
        assert first.status_code == 200 and first.headers["cache-control"] == "public, max-age=15"
        etag = first.headers["etag"]
        assert http.get("/v1/changes", params=params, headers={"If-None-Match": etag}).status_code == 304
        mono.t += 120  # the read model and the response cache both expire; the content has not changed
        again = http.get("/v1/changes", params=params, headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.headers["etag"] == etag


def test_a_poll_is_not_lost_when_another_client_refreshed_the_read_model_first():
    """Mid-day, with a second client. The read model refreshes at most every 30 s, on whichever request comes first,
    and today's poll times are kept for 30 s from the request that read them: the two clocks need not agree."""
    from tests.integration.conftest import RecordingStore, simulate

    store, client = RecordingStore(), sc.FakeTfl()
    attempts = [a for a in sc.attempts() if sc.ldn(24, 11) <= a.slot * 60 < sc.ldn(24, 11, 5)]
    simulate(store, client, attempts[:3])
    t = attempts[3].at - 20
    app, wall, _ = make_api(store, t - 40, linked=True)
    seen: list[int] = []
    with TestClient(app, base_url=BASE) as http:
        wall.t = t
        assert http.get("/v1/status").status_code == 200  # another client: the read model refreshes
        wall.t = t + 15  # the sync client: today's poll times are read and kept
        data = http.get("/v1/changes", params={"since": attempts[0].at - 1}).json()["data"]
        seen += data["polls"]
        simulate(store, client, attempts[3:4])  # a poll lands at t + 20
        for at in (t + 35, t + 95):  # the read model refreshes at t + 35 and t + 95
            if at > attempts[4].at:
                simulate(store, client, attempts[4:5])
            wall.t = at
            data = http.get("/v1/changes", params={"since": data["cursor"]}).json()["data"]
            seen += data["polls"]
    assert seen == [a.at for a in attempts]


def test_copies_kept_beside_other_clients_get_every_poll_once():
    """Three hours across UTC midnight on one API instance, every clock running as time passes. Other clients ask
    for /v1/status every 20 s, /healthz every 45 s and /v1/coverage every 40 s, and two copies are kept in sync,
    visiting /v1/changes every 61 and every 53 s. The read model refreshes on whichever request comes first, so it
    does so at ever different moments relative to the polls and the visits."""
    from tests.integration.conftest import RecordingStore, poll

    store, client = RecordingStore(), sc.FakeTfl()
    a, b = sc.ldn(24, 23), sc.ldn(25, 1)  # 22:00 UTC on 24 October to 01:00 UTC on 25 October (01:00 BST)
    attempts = [x for x in sc.attempts() if a <= x.slot * 60 < b]
    end = b + 300  # the copies keep visiting for five minutes after the last poll
    events = [(x.at, 0, x) for x in attempts]
    events += [(t, 1, "/v1/status") for t in range(a + 11, end, 20)]
    events += [(t, 2, "/healthz") for t in range(a + 7, end, 45)]
    events += [(t, 3, "/v1/coverage") for t in range(a + 23, end, 40)]
    events += [(t, 4, 0) for t in range(a + 30, end, 61)]
    events += [(t, 5, 1) for t in range(a + 2, end, 53)]
    copies = [{"cursor": a - 60, "polls": [], "closed": {}} for _ in range(2)]
    app, wall, _ = make_api(store, a - 60, linked=True)
    with TestClient(app, base_url=BASE) as http:
        for at, _, what in sorted(events, key=lambda e: e[:2]):
            wall.t = at
            if isinstance(what, sc.Attempt):
                poll(store, client, what)
            elif isinstance(what, str):
                assert http.get(what).status_code == 200, what
            else:
                copy = copies[what]
                r = http.get("/v1/changes", params={"since": copy["cursor"]})
                assert r.status_code == 200, r.text
                data = r.json()["data"]
                assert all(copy["cursor"] < t <= data["cursor"] for t in data["polls"]), (at, copy["cursor"], data)
                copy["polls"] += data["polls"]
                copy["closed"].update(((e["line"], e["start"]), e["end"]) for e in data["closed"])
                copy["cursor"] = data["cursor"]
    recorded = [x.at for x in attempts if x.expect == "applied"]
    closed = {(e["line"], iso(e["start"])): iso(e["end"]) for e in sc.expected(recorded).closed}
    for copy in copies:
        assert copy["polls"] == recorded  # each once, in order, none missing
        assert copy["closed"] == closed
