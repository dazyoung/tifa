"""Fair use: per-client token buckets and service-wide limits, all in memory.

- A client is the right-most X-Forwarded-For entry (Google's front end appends the address it saw; anything to its
  left is whatever the client sent), IPv6 addresses grouped by /64. Without trusted proxy headers it is the peer.
- Per client: a bucket of `ip_bucket` tokens refilled at `ip_refill` per second. A request costs `cost_cached`,
  an uncached stats, disruptions, changes or coverage query `cost_uncached`, an MCP tools/call `cost_mcp_call`.
  Over: 429 with Retry-After (seconds until the bucket holds the cost).
- Per client, too: a share of the egress budget, 1/CLIENT_EGRESS_SHARE of `egress_per_hour` bytes, which it may
  send at once and then at that rate per hour. Over: 429 with Retry-After (seconds until its share holds the body).
- Service-wide (the API runs on one instance): `global_rps` requests per second with bursts of `global_burst`; at
  most `uncached_per_s` uncached computations per second; at most `egress_per_hour` bytes sent per hour. Over: 503
  with Retry-After: 60, or for egress the seconds until the budget holds the body again (at least 60).
- Bytes are charged as a response starts, from its Content-Length, to the service and to the client: a body is sent
  only when both have room for it (one larger than a whole budget needs a full one), so concurrent requests cannot
  overdraw either. Headers, and bodies of unknown length, are charged as they go.
- Admission checks every limit before it takes anything: a refused request costs neither the client's tokens nor
  the service's capacity.
- The buckets live in an LRU of `ip_buckets_max` entries; a client that falls out comes back with full buckets.

Every limit comes from tifa.config.Limits, so TIFA_* variables can lower them for tests and load tests.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tifa.config import Limits
from tifa.main import client_ip as forwarded_ip

GLOBAL_RETRY_AFTER = 60  # seconds, for every service-wide limit (at least)
CLIENT_EGRESS_SHARE = 8  # a client may send at most this fraction (1/8) of the service's egress budget an hour
EGRESS_RETRY_BYTES = 64 * 1024  # an egress refusal before any work says when this much will be free again
MAX_RETRY_AFTER = 3600  # seconds: the longest Retry-After given


class TokenBucket:
    """`capacity` tokens, refilled continuously at `rate` per second. The level may go below zero when a cost is
    charged after the fact (egress)."""

    __slots__ = ("capacity", "rate", "tokens", "stamp")

    def __init__(self, capacity: float, rate: float, now: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(rate)
        self.tokens = float(capacity)
        self.stamp = now

    def level(self, now: float) -> float:
        if now > self.stamp:
            self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now
        return self.tokens

    def take(self, n: float, now: float) -> bool:
        if self.level(now) >= n:
            self.tokens -= n
            return True
        return False

    def charge(self, n: float, now: float) -> None:
        self.level(now)
        self.tokens -= n

    def wait(self, n: float, now: float) -> float:
        """Seconds until the bucket holds n tokens (inf if it never can)."""
        missing = n - self.level(now)
        if missing <= 0:
            return 0.0
        return missing / self.rate if self.rate > 0 else math.inf

    def until_full(self, now: float) -> float:
        return self.wait(self.capacity, now)


def client_egress(limits: Limits) -> int:
    """The bytes a client may send per hour (and at once): its share of the service's egress budget."""
    return max(1, limits.egress_per_hour // CLIENT_EGRESS_SHARE)


class _Client:
    """A client's buckets: request tokens and bytes."""

    __slots__ = ("tokens", "bytes")

    def __init__(self, limits: Limits, now: float) -> None:
        share = client_egress(limits)
        self.tokens = TokenBucket(limits.ip_bucket, limits.ip_refill, now)
        self.bytes = TokenBucket(share, share / 3600, now)


@dataclass(frozen=True)
class Decision:
    """The outcome of admit() or spend(): allowed, or refused with the status (429 or 503) and Retry-After seconds.
    limit, remaining and reset describe the client's bucket for the RateLimit-* headers."""

    allowed: bool
    status: int
    reason: str | None
    retry_after: int | None
    limit: int
    remaining: int
    reset: int
    window: int

    def headers(self) -> list[tuple[str, str]]:
        out = [
            ("RateLimit-Limit", str(self.limit)),
            ("RateLimit-Remaining", str(self.remaining)),
            ("RateLimit-Reset", str(self.reset)),
            ("RateLimit-Policy", f"{self.limit};w={self.window}"),
        ]
        if self.retry_after is not None:
            out.append(("Retry-After", str(self.retry_after)))
        return out


def client_key(ip: str | None) -> str:
    """The bucket key of an address: IPv4 as is (IPv4-mapped IPv6 too), IPv6 as its /64 network."""
    if not ip:
        return "unknown"
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return "unknown"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


def client_of(scope: Mapping, trust_forwarded: bool) -> str:
    """The client address of an ASGI request: the right-most X-Forwarded-For entry when the proxy is trusted (and
    the entry is an address), else the peer."""
    if trust_forwarded:
        values = [v for k, v in scope.get("headers") or () if k.lower() == b"x-forwarded-for"]
        if values:
            # several headers act as one comma-separated list; the proxy's entry is the last of all
            ip = forwarded_ip(b",".join(values).decode("latin-1"))
            if ip:
                return ip
    peer = scope.get("client")
    return str(peer[0]) if peer else ""


def _seconds(wait: float, least: int = 1) -> int:
    if math.isinf(wait):
        return MAX_RETRY_AFTER
    return min(MAX_RETRY_AFTER, max(least, math.ceil(wait)))


class RateLimiter:
    """Thread-safe. `clock` is a monotonic clock in seconds (tests pass a fake one)."""

    def __init__(self, limits: Limits | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = lim = limits or Limits()
        self._clock = clock
        self._lock = threading.Lock()
        now = clock()
        self._ips: OrderedDict[str, _Client] = OrderedDict()
        self._global = TokenBucket(lim.global_burst, lim.global_rps, now)
        self._uncached = TokenBucket(lim.uncached_per_s, lim.uncached_per_s, now)
        self._egress = TokenBucket(lim.egress_per_hour, lim.egress_per_hour / 3600, now)

    @property
    def window(self) -> int:
        """Seconds for an empty bucket to fill: the RateLimit-Policy window."""
        lim = self.limits
        return max(1, math.ceil(lim.ip_bucket / lim.ip_refill))

    def cost(self, kind: str) -> int:
        """The price of a request: "cached", "uncached" or "mcp_call"."""
        lim = self.limits
        return {"cached": lim.cost_cached, "uncached": lim.cost_uncached, "mcp_call": lim.cost_mcp_call}[kind]

    def _client(self, key: str, now: float) -> _Client:
        client = self._ips.get(key)
        if client is None:
            client = self._ips[key] = _Client(self.limits, now)
            while len(self._ips) > self.limits.ip_buckets_max:
                self._ips.popitem(last=False)
        else:
            self._ips.move_to_end(key)
        return client

    def admit(self, key: str, cost: int, *, uncached: bool = False) -> Decision:
        """Admit a request of `cost` tokens from client `key`, or refuse it. Uncached also takes one of the uncached
        computations per second. Every limit is checked before anything is taken: a client refused (429) uses none
        of the service's capacity, and a service-wide refusal (503) takes none of the client's tokens."""
        lim = self.limits
        cost = min(cost, lim.ip_bucket)  # a cost above the bucket could never be paid
        with self._lock:
            now = self._clock()
            client = self._client(key, now)
            bucket = client.tokens
            if bucket.level(now) < cost:
                return self._decision(bucket, now, False, 429, "client", _seconds(bucket.wait(cost, now)))
            if self._global.level(now) < 1:
                return self._decision(bucket, now, False, 503, "global", GLOBAL_RETRY_AFTER)
            if self._egress.level(now) <= 0:
                wait = self._egress.wait(min(EGRESS_RETRY_BYTES, self._egress.capacity), now)
                return self._decision(bucket, now, False, 503, "egress", _seconds(wait, GLOBAL_RETRY_AFTER))
            if client.bytes.level(now) <= 0:
                wait = client.bytes.wait(min(EGRESS_RETRY_BYTES, client.bytes.capacity), now)
                return self._decision(bucket, now, False, 429, "client_egress", _seconds(wait))
            if uncached and self._uncached.level(now) < 1:
                return self._decision(bucket, now, False, 503, "uncached", GLOBAL_RETRY_AFTER)
            bucket.take(cost, now)
            self._global.take(1, now)
            if uncached:
                self._uncached.take(1, now)
            return self._decision(bucket, now, True, 200, None, None)

    def spend(self, key: str, nbytes: int) -> Decision | None:
        """Charge `nbytes` about to be sent to client `key` to its share and to the service's egress budget, when
        both have room for them (a body larger than a whole budget needs a full one). None when charged; otherwise
        the refusal, and nothing is charged: 503 when the service's budget is short, 429 when the client's is."""
        with self._lock:
            now = self._clock()
            client = self._client(key, now)
            need = min(nbytes, self._egress.capacity)
            if self._egress.level(now) < need:
                wait = self._egress.wait(need, now)
                return self._decision(client.tokens, now, False, 503, "egress", _seconds(wait, GLOBAL_RETRY_AFTER))
            need = min(nbytes, client.bytes.capacity)
            if client.bytes.level(now) < need:
                wait = client.bytes.wait(need, now)
                return self._decision(client.tokens, now, False, 429, "client_egress", _seconds(wait))
            self._egress.charge(nbytes, now)
            client.bytes.charge(nbytes, now)
            return None

    def _decision(
        self, bucket: TokenBucket, now: float, ok: bool, status: int, reason: str | None, retry: int | None
    ) -> Decision:
        return Decision(
            allowed=ok,
            status=status,
            reason=reason,
            retry_after=retry,
            limit=self.limits.ip_bucket,
            remaining=max(0, math.floor(bucket.level(now))),
            reset=max(0, math.ceil(bucket.until_full(now))),
            window=self.window,
        )

    def record_egress(self, nbytes: int, key: str | None = None) -> None:
        """Count bytes sent after the fact (headers, bodies of unknown length, refusals): to the service and, given
        its key, to the client. The budgets may go negative and then refuse until they recover."""
        if nbytes > 0:
            with self._lock:
                now = self._clock()
                self._egress.charge(nbytes, now)
                if key is not None:
                    self._client(key, now).bytes.charge(nbytes, now)

    def egress_left(self, key: str | None = None) -> float:
        """Bytes left in the service's egress budget, or with a key in that client's share."""
        with self._lock:
            now = self._clock()
            if key is None:
                return self._egress.level(now)
            return self._client(key, now).bytes.level(now)

    def clients(self) -> int:
        with self._lock:
            return len(self._ips)
