"""ETags, 304s, gzip, the response cache and what it does to a request's price."""

from __future__ import annotations

import gzip

import pytest
from starlette.testclient import TestClient

from tests.api.test_routes import NOW, ROOMY, assert_envelope, fixture_store, make_app, open_episodes, poll
from tifa.api import http
from tifa.api.http import Body, Entry, ResponseCache, accepts_gzip, make_body, not_modified, request_key

PATHS = [
    "/v1/status",
    f"/v1/changes?since={NOW - 600}",
    "/v1/lines",
    "/v1/lines/central",
    "/v1/months",
    "/v1/months/2025-10",
    "/v1/disruptions?limit=5",
    "/v1/stats?span=30d&end=now",
    "/v1/coverage",
    "/v1/meta",
]


@pytest.fixture
def setup():
    store = fixture_store()
    app, wall, mono = make_app(store)
    with TestClient(app) as c:
        yield c, store, wall, mono


@pytest.mark.parametrize("path", PATHS)
def test_if_none_match_gives_304(setup, path):
    c = setup[0]
    r = c.get(path)
    tag = r.headers["etag"]
    assert c.get(path).headers["etag"] == tag and c.get(path).content == r.content  # same tag, same bytes
    nm = c.get(path, headers={"If-None-Match": tag})
    assert nm.status_code == 304 and nm.content == b""
    assert nm.headers["etag"] == tag and nm.headers["cache-control"] == r.headers["cache-control"]
    assert nm.headers["access-control-allow-origin"] == "*" and "ratelimit-remaining" in nm.headers
    assert c.get(path, headers={"If-None-Match": '"something-else"'}).status_code == 200


def test_etag_comparison_forms(setup):
    c = setup[0]
    gz = c.get("/v1/months/2025-10", headers={"Accept-Encoding": "gzip"})
    plain = c.get("/v1/months/2025-10", headers={"Accept-Encoding": "identity"})
    assert gz.headers["content-encoding"] == "gzip" and "content-encoding" not in plain.headers
    assert gz.headers["etag"] == plain.headers["etag"][:-1] + '-gz"'  # each representation has its own tag
    assert gz.json() == plain.json()
    # either tag matches either representation, weak or strong, alone or in a list, and "*" matches anything
    for value in (
        plain.headers["etag"],
        gz.headers["etag"],
        "W/" + plain.headers["etag"],
        f'"x", {gz.headers["etag"]}',
        "*",
    ):
        for enc in ("gzip", "identity"):
            r = c.get("/v1/months/2025-10", headers={"If-None-Match": value, "Accept-Encoding": enc})
            assert r.status_code == 304, (value, enc)
            assert r.headers["etag"] == (gz if enc == "gzip" else plain).headers["etag"]


def test_errors_are_never_304(setup):
    c = setup[0]
    r = c.get("/v1/lines/nope")
    assert r.status_code == 404
    assert c.get("/v1/lines/nope", headers={"If-None-Match": "*"}).status_code == 404


def test_gzip_negotiation():
    assert accepts_gzip("gzip, deflate, br") and accepts_gzip("br;q=1.0, gzip;q=0.8") and accepts_gzip("*")
    assert not accepts_gzip("gzip;q=0") and not accepts_gzip("identity") and not accepts_gzip(None)
    assert not accepts_gzip("*;q=0") and not accepts_gzip("gzip;q=0, *")
    small = make_body(b'{"data":1}')
    assert small.gz is None  # not worth compressing
    big = make_body(b'{"data":"' + b"x" * 5000 + b'"}')
    assert gzip.decompress(big.gz) == big.raw and len(big.gz) < len(big.raw)
    assert make_body(big.raw) == big  # the same bytes give the same body, and so the same tags


def test_not_modified():
    body = Body(b"{}", None, "abc")
    assert not_modified('"abc"', body) and not_modified('W/"abc"', body) and not_modified('"abc-gz"', body)
    assert not_modified('"x", "abc"', body) and not_modified("*", body)
    assert not not_modified('"abcd"', body) and not not_modified("", body) and not not_modified(None, body)


def test_an_unchanged_month_keeps_its_tag_across_polls(setup):
    c, store, wall, mono = setup
    past = c.get("/v1/months/2025-10")
    status = c.get("/v1/status")
    index = c.get("/v1/months")
    poll(store, NOW + 60, open_episodes())  # a new poll: the state changes, the past month does not
    wall.advance(60)
    mono.advance(31)  # the read model refreshes
    again = c.get("/v1/months/2025-10")
    assert again.headers["etag"] == past.headers["etag"] and again.content == past.content
    assert again.json()["meta"]["generated_at"] == past.json()["meta"]["generated_at"]
    assert c.get("/v1/months", headers={"If-None-Match": index.headers["etag"]}).status_code == 304
    new_status = c.get("/v1/status")
    assert new_status.headers["etag"] != status.headers["etag"]
    assert new_status.json()["data"]["last_poll"] != status.json()["data"]["last_poll"]


def test_a_changed_month_gets_a_new_tag(setup):
    c, store, wall, mono = setup
    before = c.get("/v1/months/2025-11")
    doc = store.get_month("2025-11")
    payload = dict(doc.payload, watched={"a": doc.payload["watched"]["a"] + [[NOW + 60, NOW + 120]], "f": []})
    store.put_month("2025-11", payload)
    mono.advance(31)
    after = c.get("/v1/months/2025-11")
    assert after.headers["etag"] != before.headers["etag"]
    assert after.json()["data"]["watched"]["a"][-1] == [NOW + 60, NOW + 120]


def remaining(r) -> int:
    return int(r.headers["ratelimit-remaining"])


def test_uncached_queries_cost_more_until_cached(setup):
    c, store, wall, mono = setup
    start = remaining(c.get("/v1/meta"))
    steps = []
    last = start
    for path in (
        "/v1/stats?span=90d&end=now",  # computed: 5
        "/v1/stats?span=90d&end=now",  # cached: 1
        "/v1/stats?end=now&span=90d",  # the same query: 1
        "/v1/stats?span=90d&end=now&_=123",  # unknown parameters do not dodge the cache: 1
        "/v1/stats?span=30d&end=now",  # another query: 5
        "/v1/lines/central",  # never priced as uncached: 1
    ):
        now = remaining(c.get(path))
        steps.append(last - now)
        last = now
    assert steps == [5, 1, 1, 1, 5, 1]
    mono.advance(http.CACHE_TTL)  # the cached answers expire (and the bucket refills 30 tokens)
    before = remaining(c.get("/v1/meta"))
    assert before - remaining(c.get("/v1/stats?span=90d&end=now")) == 5


def test_the_uncached_limit_through_the_app():
    app, _, _ = make_app(env={**ROOMY, "TIFA_UNCACHED_PER_S": "2"})
    with TestClient(app) as c:
        assert c.get("/v1/stats?span=1d&end=now").status_code == 200
        assert c.get("/v1/stats?span=7d&end=now").status_code == 200
        r = c.get("/v1/stats?span=30d&end=now")
        assert r.status_code == 503 and r.headers["retry-after"] == "60"
        assert assert_envelope(r, error=True)["error"]["code"] == "over_capacity"
        assert c.get("/v1/stats?span=1d&end=now").status_code == 200  # cached: no computation needed
        assert c.get("/v1/status").status_code == 200


def test_errors_are_cached_too(setup):
    c = setup[0]
    first = remaining(c.get(f"/v1/changes?since={NOW - 40 * 86400}"))
    second = remaining(c.get(f"/v1/changes?since={NOW - 40 * 86400}"))
    assert first - second == 1  # the same refused query is not computed again


# ---- the cache itself ------------------------------------------------------------------------------------------


class Mono:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def entry(version: str, size: int, at: float = 0.0) -> Entry:
    return Entry(version, at, 200, "d", Body(b"x" * size, None, "t"), "no-store")


def test_cache_freshness_and_bounds():
    clock = Mono()
    cache = ResponseCache(ttl=30, max_bytes=10_000, max_entries=3, clock=clock)
    cache.put("a", entry("v1", 100))
    assert cache.fresh("a", "v1") and not cache.fresh("a", "v2") and not cache.fresh("b", "v1")
    clock.t = 30
    assert not cache.fresh("a", "v1")  # too old
    for k in "bcd":
        cache.put(k, entry("v1", 100, at=30))
    assert len(cache) == 3 and cache.peek("a") is None  # the oldest went
    cache.put("e", entry("v1", 9000, at=30))  # too big to keep (over a quarter of the cache)
    assert cache.peek("e") is None and len(cache) == 3
    by_bytes = ResponseCache(ttl=30, max_bytes=10_000, max_entries=100, clock=clock)
    for k in "fghij":
        by_bytes.put(k, entry("v1", 2000, at=30))  # 2,200 bytes each with the overhead
    assert [k for k in "fghij" if by_bytes.peek(k)] == list("ghij")  # held under max_bytes, oldest out first


def test_request_key():
    assert request_key("/v1/stats", b"span=7d&end=now") == request_key("/v1/stats", b"end=now&span=7d&utm=x")
    assert request_key("/v1/stats", b"") == "/v1/stats"
    assert request_key("/v1/status", b"anything=1") == "/v1/status"
    assert request_key("/v1/disruptions", b"line=a&line=b") == "/v1/disruptions?line=a&line=b"
    assert request_key("/v1/disruptions", b"line=b&line=a") != request_key("/v1/disruptions", b"line=a&line=b")
