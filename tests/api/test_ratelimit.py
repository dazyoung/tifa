"""Fair use: the token-bucket maths, client addresses (spoofing, IPv6 /64), the LRU and the service-wide limits,
directly and through the app."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tests.api.test_routes import ROOMY, Clock, assert_envelope, make_app
from tifa.api.ratelimit import GLOBAL_RETRY_AFTER, RateLimiter, TokenBucket, client_key, client_of
from tifa.config import Limits, load_settings


def limiter(**overrides) -> tuple[RateLimiter, Clock]:
    clock = Clock(0.0)
    big = {"global_burst": 10**9, "global_rps": 10**9, "uncached_per_s": 10**9}
    return RateLimiter(Limits(**{**big, **overrides}), clock=clock), clock


# ---- the maths ----------------------------------------------------------------------------------------------


def test_token_bucket():
    b = TokenBucket(60, 1.0, now=0.0)
    assert b.take(60, 0.0) and not b.take(1, 0.0)
    assert b.wait(5, 0.0) == 5.0 and b.wait(5, 2.0) == 3.0
    assert b.level(30.0) == 30.0 and b.level(1000.0) == 60.0  # never over capacity
    b.charge(100, 1000.0)
    assert b.level(1000.0) == -40.0 and b.wait(1, 1000.0) == 41.0


def test_bucket_of_60_refills_one_a_second():
    rl, clock = limiter()
    for i in range(60):
        d = rl.admit("192.0.2.1", 1)
        assert d.allowed and d.remaining == 59 - i
    d = rl.admit("192.0.2.1", 1)
    assert (d.allowed, d.status, d.reason, d.retry_after, d.remaining) == (False, 429, "client", 1, 0)
    assert d.reset == 60
    clock.advance(1)
    assert rl.admit("192.0.2.1", 1).allowed
    assert not rl.admit("192.0.2.1", 1).allowed
    assert rl.admit("192.0.2.2", 1).allowed  # another client has its own bucket


def test_costs_and_retry_after():
    rl, clock = limiter()
    assert (rl.cost("cached"), rl.cost("uncached"), rl.cost("mcp_call")) == (1, 5, 3)
    for _ in range(11):
        assert rl.admit("c", 5).allowed  # 55 tokens
    d = rl.admit("c", 5)  # 5 left: one more uncached query fits
    assert d.allowed and d.remaining == 0
    d = rl.admit("c", 5)
    assert not d.allowed and d.retry_after == 5
    clock.advance(2.5)
    d = rl.admit("c", 5)
    assert not d.allowed and d.retry_after == 3  # ceil(2.5)
    clock.advance(2.5)
    assert rl.admit("c", 5).allowed
    rl2, _ = limiter()
    d = rl2.admit("big", 10_000)  # a cost above the bucket is capped, so it can be paid once
    assert d.allowed and d.remaining == 0


def test_refused_requests_cost_nothing():
    rl, clock = limiter()
    for _ in range(60):
        rl.admit("c", 1)
    for _ in range(100):
        assert not rl.admit("c", 1).allowed
    clock.advance(1)
    assert rl.admit("c", 1).allowed  # the refusals did not dig the bucket deeper


def test_headers():
    rl, _ = limiter()
    d = rl.admit("c", 5)
    assert dict(d.headers()) == {
        "RateLimit-Limit": "60",
        "RateLimit-Remaining": "55",
        "RateLimit-Reset": "5",
        "RateLimit-Policy": "60;w=60",
    }
    for _ in range(12):
        d = rl.admit("c", 5)
    assert not d.allowed and dict(d.headers())["Retry-After"] == "5"


def test_lru_of_buckets():
    rl, _ = limiter(ip_buckets_max=3)
    for ip in ("a", "b", "c"):
        for _ in range(60):
            rl.admit(ip, 1)
    assert not rl.admit("a", 1).allowed  # "a" is now the most recent
    rl.admit("d", 1)  # evicts "b", the least recently used
    assert rl.clients() == 3
    assert rl.admit("b", 1).remaining == 59  # back with a full bucket
    assert not rl.admit("a", 1).allowed and not rl.admit("d", 60).allowed


# ---- client addresses ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip,key",
    [
        ("192.0.2.7", "192.0.2.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::ffff:192.0.2.9", "192.0.2.9"),
        ("not an address", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_client_key(ip, key):
    assert client_key(ip) == key


def scope(xff: list[str] | None = None, peer: str = "203.0.113.5") -> dict:
    headers = [(b"x-forwarded-for", v.encode()) for v in xff or []]
    return {"type": "http", "headers": headers, "client": (peer, 1234)}


def test_client_is_the_right_most_forwarded_entry():
    assert client_of(scope(["198.51.100.1, 192.0.2.10"]), True) == "192.0.2.10"
    spoofed = "198.51.100.4, 203.0.113.8, 192.0.2.10"
    assert client_of(scope([spoofed]), True) == "192.0.2.10"  # spoofed left entries ignored
    assert client_of(scope(["198.51.100.4", "192.0.2.10"]), True) == "192.0.2.10"  # several headers are one list
    assert client_of(scope(["[2001:db8::1]:443"]), True) == "2001:db8::1"
    assert client_of(scope(["192.0.2.10:443"]), True) == "192.0.2.10"
    assert client_of(scope(["garbage"]), True) == "203.0.113.5"  # not an address: the peer
    assert client_of(scope(), True) == "203.0.113.5"
    assert client_of(scope(["192.0.2.10"]), False) == "203.0.113.5"  # untrusted proxy headers: the peer
    assert client_of({"type": "http", "headers": []}, True) == ""


# ---- service-wide limits ---------------------------------------------------------------------------------------


def test_global_rate_and_burst():
    rl, clock = limiter(global_burst=100, global_rps=20)
    keys = [f"client-{i}" for i in range(101)]
    assert all(rl.admit(k, 1).allowed for k in keys[:100])
    d = rl.admit(keys[100], 1)
    assert (d.allowed, d.status, d.reason, d.retry_after) == (False, 503, "global", GLOBAL_RETRY_AFTER)
    clock.advance(1)  # 20 more
    assert sum(rl.admit(f"x{i}", 1).allowed for i in range(25)) == 20


def test_uncached_computations_per_second():
    rl, clock = limiter(uncached_per_s=10)
    assert all(rl.admit(f"c{i}", 5, uncached=True).allowed for i in range(10))
    d = rl.admit("c10", 5, uncached=True)
    assert (d.allowed, d.status, d.reason, d.retry_after) == (False, 503, "uncached", 60)
    assert rl.admit("c10", 1).allowed  # cached answers still flow
    clock.advance(0.5)
    assert sum(rl.admit(f"d{i}", 5, uncached=True).allowed for i in range(10)) == 5


def test_egress_budget():
    rl, clock = limiter(egress_per_hour=3600 * 100)  # 100 bytes a second
    assert rl.admit("c", 1).allowed
    rl.record_egress(3600 * 100)
    d = rl.admit("c", 1)
    # Retry-After: until 64 KiB are free again (656 s at 100 bytes a second), not a fixed minute
    assert (d.allowed, d.status, d.reason, d.retry_after) == (False, 503, "egress", 656)
    clock.advance(1)
    assert rl.admit("c", 1).allowed and rl.egress_left() == 100
    rl.record_egress(1000)
    assert rl.egress_left() < 0 and not rl.admit("c", 1).allowed
    clock.advance(10)
    assert rl.admit("c", 1).allowed
    rl.record_egress(10**9)
    assert rl.admit("c", 1).retry_after == 3600  # never more than an hour


def test_bytes_are_charged_before_they_are_sent():
    rl, clock = limiter(egress_per_hour=8000 * 3600)  # 8000 bytes a second; a client's share is 1000 a second
    assert rl.egress_left() == 8000 * 3600 and rl.egress_left("c") == 1000 * 3600
    assert rl.spend("c", 1000 * 3600) is None  # the whole share at once
    assert rl.egress_left("c") == 0 and rl.egress_left() == 7000 * 3600
    d = rl.spend("c", 1)
    assert (d.allowed, d.status, d.reason, d.retry_after) == (False, 429, "client_egress", 1)
    assert rl.egress_left("c") == 0  # a refusal charges nothing
    assert rl.spend("other", 500_000) is None  # other clients have their own shares
    clock.advance(10)
    assert rl.spend("c", 10_000) is None and rl.spend("c", 1) is not None


def test_one_client_cannot_spend_the_service_budget():
    """Many concurrent large responses to one client: each is charged as it starts, so the client stops at its share
    and the service's budget is never overdrawn."""
    rl, _ = limiter(egress_per_hour=10 * 1024 * 1024)
    body = 624_000  # a large month, sent without gzip
    sent = sum(rl.spend("greedy", body) is None for _ in range(58))
    assert sent == 2  # 1.25 MiB: two bodies
    assert rl.egress_left("greedy") >= 0 and rl.egress_left() > 8 * 1024 * 1024
    assert rl.admit("someone-else", 1).allowed and rl.spend("someone-else", body) is None
    d = rl.admit("greedy", 1)
    assert (d.allowed, d.status, d.reason) == (True, 200, None)  # its share still holds bytes for a small answer


def test_bodies_larger_than_the_budget_need_it_full():
    rl, clock = limiter(egress_per_hour=3600 * 8)  # 8 bytes a second; a client's share 1 a second
    d = rl.spend("c", 10**6)
    assert d is None and rl.egress_left() < 0  # a full budget pays for one, then it is in debt
    d = rl.spend("d", 100)
    assert (d.allowed, d.status, d.reason) == (False, 503, "egress") and d.retry_after > 60
    assert rl.egress_left("d") == 3600  # nothing charged to a refused client


def test_service_refusals_take_no_client_tokens():
    rl, clock = limiter(global_burst=1, global_rps=1)
    assert rl.admit("a", 5).allowed  # takes the one global token
    for _ in range(20):
        d = rl.admit("b", 5)
        assert (d.allowed, d.status, d.reason) == (False, 503, "global")
    assert d.remaining == 60  # b's bucket is untouched
    rl2, _ = limiter(uncached_per_s=1)
    assert rl2.admit("a", 5, uncached=True).allowed
    assert not rl2.admit("b", 5, uncached=True).allowed
    assert rl2.admit("b", 1).remaining == 59  # the refusal took none of b's tokens


def test_limits_come_from_the_environment():
    s = load_settings({"TIFA_IP_BUCKET": "5", "TIFA_IP_REFILL": "0.5", "TIFA_GLOBAL_BURST": "7"})
    rl = RateLimiter(s.limits, clock=Clock(0))
    assert all(rl.admit("c", 1).allowed for _ in range(5)) and rl.admit("c", 1).retry_after == 2
    assert rl.window == 10
    assert sum(rl.admit(f"k{i}", 1).allowed for i in range(10)) == 2  # 7 in the burst, 5 already used


# ---- through the app -------------------------------------------------------------------------------------------


def test_429_through_the_app_with_spoofing_ignored():
    app, _, mono = make_app(env={**ROOMY, "TIFA_IP_BUCKET": "3"})
    with TestClient(app) as c:
        for i in range(3):
            # each request claims another address on the left; the proxy's own entry (right-most) is the same
            r = c.get("/v1/meta", headers={"X-Forwarded-For": f"198.51.100.{i}, 192.0.2.44"})
            assert r.status_code == 200 and r.headers["ratelimit-remaining"] == str(2 - i)
        r = c.get("/v1/meta", headers={"X-Forwarded-For": "198.51.100.99, 192.0.2.44"})
        assert r.status_code == 429 and r.headers["retry-after"] == "1"
        assert assert_envelope(r, error=True)["error"]["code"] == "rate_limited"
        assert r.headers["cache-control"] == "no-store"
        assert c.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.45"}).status_code == 200
        mono.advance(1)
        assert c.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.44"}).status_code == 200


def test_ipv6_clients_share_a_bucket_per_64():
    app, _, _ = make_app(env={**ROOMY, "TIFA_IP_BUCKET": "2"})
    with TestClient(app) as c:
        assert c.get("/v1/meta", headers={"X-Forwarded-For": "2001:db8:5:6::1"}).status_code == 200
        assert c.get("/v1/meta", headers={"X-Forwarded-For": "2001:db8:5:6:ffff::2"}).status_code == 200
        assert c.get("/v1/meta", headers={"X-Forwarded-For": "2001:db8:5:6::3"}).status_code == 429
        assert c.get("/v1/meta", headers={"X-Forwarded-For": "2001:db8:5:7::1"}).status_code == 200


def test_untrusted_forwarded_header_is_ignored():
    app, _, _ = make_app(env={**ROOMY, "TIFA_IP_BUCKET": "2"}, trust_forwarded=False)
    with TestClient(app, client=("192.0.2.80", 5000)) as c:
        for i in range(2):
            assert c.get("/v1/meta", headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code == 200
        assert c.get("/v1/meta", headers={"X-Forwarded-For": "198.51.100.9"}).status_code == 429


def test_503_when_the_service_is_at_its_request_limit():
    app, _, _ = make_app(env={"TIFA_GLOBAL_BURST": "4", "TIFA_GLOBAL_RPS": "1"})
    with TestClient(app) as c:
        codes = [c.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}"}).status_code for i in range(5)]
        assert codes == [200, 200, 200, 200, 503]
        r = c.get("/v1/status", headers={"X-Forwarded-For": "192.0.2.99"})
        assert r.status_code == 503 and r.headers["retry-after"] == "60"
        assert assert_envelope(r, error=True)["error"]["code"] == "over_capacity"


def test_503_when_the_egress_budget_is_spent():
    app, _, mono = make_app(env={**ROOMY, "TIFA_EGRESS_PER_HOUR": "3600"})  # a byte a second
    limiter = app.state.tifa.limiter
    with TestClient(app) as c:
        r = c.get("/v1/months/2025-10", headers={"Accept-Encoding": "identity", "X-Forwarded-For": "192.0.2.1"})
        assert r.status_code == 200 and len(r.content) > 3600  # larger than the whole budget: sent from a full one
        deficit = -limiter.egress_left()
        assert deficit > len(r.content) - 3600
        r = c.get("/v1/status", headers={"X-Forwarded-For": "192.0.2.2"})
        assert r.status_code == 503 and "data allowance" in r.json()["error"]["message"]
        # Retry-After: until the budget holds bytes again, which here takes far longer than a minute
        assert int(r.headers["retry-after"]) > 60
        mono.advance(deficit + 3600 + 600)
        assert c.get("/v1/status", headers={"X-Forwarded-For": "192.0.2.2"}).status_code == 200


def test_a_large_response_is_refused_when_the_client_share_cannot_cover_it():
    """The response is computed, but as it starts its length is charged: past the client's share it is replaced by a
    429 and no body goes out, however many are asked for."""
    share = 30_000
    app, _, mono = make_app(env={**ROOMY, "TIFA_EGRESS_PER_HOUR": str(8 * share)})
    limiter = app.state.tifa.limiter
    with TestClient(app) as c:
        greedy = {"Accept-Encoding": "identity", "X-Forwarded-For": "198.51.100.7"}
        answers = [c.get("/v1/months/2025-10", headers=greedy) for _ in range(20)]
        sent = [r for r in answers if r.status_code == 200]
        assert 2 <= len(sent) < 20 and {r.status_code for r in answers} == {200, 429}
        assert sum(len(r.content) for r in sent) <= share  # never more than its share
        assert [r.status_code for r in answers] == [200] * len(sent) + [429] * (20 - len(sent))
        refused = answers[-1]
        assert assert_envelope(refused, error=True)["error"]["code"] == "rate_limited"
        assert "gzip" in refused.json()["error"]["message"] and int(refused.headers["retry-after"]) > 60
        assert limiter.egress_left() > 8 * share - share - 20 * 1000  # the service's budget is mostly intact
        other = c.get("/v1/months/2025-10", headers={"X-Forwarded-For": "198.51.100.8", "Accept-Encoding": "gzip"})
        assert other.status_code == 200 and other.headers["content-encoding"] == "gzip"
        mono.advance(3600)
        assert c.get("/v1/months/2025-10", headers=greedy).status_code == 200  # the share refills within the hour


def test_head_requests_are_charged_without_the_body():
    share = 30_000
    app, _, _ = make_app(env={**ROOMY, "TIFA_EGRESS_PER_HOUR": str(8 * share)})
    limiter = app.state.tifa.limiter
    with TestClient(app) as c:
        for _ in range(20):
            r = c.head("/v1/months/2025-10", headers={"Accept-Encoding": "identity", "X-Forwarded-For": "192.0.2.5"})
            assert r.status_code == 200 and r.content == b""
        assert share - limiter.egress_left("192.0.2.5") < 20 * 1000  # headers only
