"""tools/loadtest.py against small in-process ASGI apps that limit the way Tifa's API does (or does not)."""

import io
import ipaddress
import json
import time

import httpx
import pytest

from tools import loadtest as lt


class Limiter:
    """A token bucket per client and a global one, keyed on the right-most (or left-most) X-Forwarded-For entry."""

    def __init__(self, bucket=60, refill=1.0, global_burst=None, global_rps=20.0, key="right"):
        self.bucket, self.refill, self.key = bucket, refill, key
        self.global_burst, self.global_rps = global_burst, global_rps
        self.clients: dict[str, list[float]] = {}
        self.all = [float(global_burst or 0), time.monotonic()]
        self.requests = 0

    def client(self, scope) -> str:
        xff = dict(scope["headers"]).get(b"x-forwarded-for", b"").decode()
        if not xff:
            return scope["client"][0]
        parts = [p.strip() for p in xff.split(",")]
        return parts[-1] if self.key == "right" else parts[0]

    @staticmethod
    def take(b, size, rate) -> bool:
        now = time.monotonic()
        b[0] = min(size, b[0] + (now - b[1]) * rate)
        b[1] = now
        if b[0] >= 1:
            b[0] -= 1
            return True
        return False

    async def __call__(self, scope, receive, send):
        assert scope["type"] == "http"
        self.requests += 1
        if self.global_burst is not None and not self.take(self.all, self.global_burst, self.global_rps):
            return await self.reply(send, 503, {"retry-after": "60"})
        b = self.clients.setdefault(self.client(scope), [float(self.bucket), time.monotonic()])
        if not self.take(b, self.bucket, self.refill):
            return await self.reply(send, 429, {"retry-after": "1", "ratelimit-remaining": "0"})
        return await self.reply(send, 200, {"ratelimit-limit": str(self.bucket), "ratelimit-remaining": str(int(b[0]))})

    @staticmethod
    async def reply(send, status, headers):
        body = json.dumps({"data": {}, "meta": {}}).encode()
        raw = [(k.encode(), v.encode()) for k, v in headers.items()] + [(b"content-type", b"application/json")]
        await send({"type": "http.response.start", "status": status, "headers": raw})
        await send({"type": "http.response.body", "body": body})


def run(args, app=None, transport=None):
    out, err = io.StringIO(), io.StringIO()
    transport = transport or httpx.ASGITransport(app=app)
    code = lt.main([str(a) for a in args], transport=transport, out=out, err=err)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


LOCAL = ["--base-url", "http://127.0.0.1:8080", "--rate", "100000"]


def never(request):
    raise AssertionError("no request should be sent")


def test_a_target_that_is_not_local_needs_the_cost_flag():
    code, report, err = run(["--base-url", "https://tifa.example.org"], transport=httpx.MockTransport(never))
    assert code == lt.EXIT_REFUSED and report is None and lt.COST_FLAG in err


def test_requests_over_the_hard_cap_are_refused():
    code, _, err = run([*LOCAL, "--requests", "20001"], transport=httpx.MockTransport(never))
    assert code == lt.EXIT_REFUSED and "hard cap" in err
    assert run([*LOCAL, "--requests", "11", "--hard-cap", "10"], transport=httpx.MockTransport(never))[0] == 3


@pytest.mark.parametrize(
    "args", [["--requests", "0"], ["--rate", "0"], ["--path", "v1/status"], ["--base-url", "file:///x"]]
)
def test_usage_errors(args):
    assert run([*LOCAL, *args], transport=httpx.MockTransport(never))[0] == lt.EXIT_USAGE


def test_single_ip_is_limited_with_retry_after():
    app = Limiter()
    code, report, _ = run([*LOCAL, "--requests", "120"], app)
    assert code == 0 and report["ok"], report
    assert report["status"]["429"] >= 55 and report["successes"] <= report["successes_allowed"]
    assert report["first_429_at"] >= 60 and report["retry_after"] == ["1"]
    assert report["ratelimit_headers"]["ratelimit-remaining"] == "0" and app.requests == 120
    assert report["target"] == {"host": "127.0.0.1", "local": True, "paths": ["/v1/status"]}


def test_spoofed_forwarded_for_is_still_one_client():
    code, report, _ = run([*LOCAL, "--mode", "spoofed-xff", "--client-ip", "198.51.100.7", "--requests", "120"],
                          Limiter())  # fmt: skip
    assert code == 0 and report["checks"]["limited"] and report["status"]["429"] >= 55


def test_a_limiter_keyed_on_the_left_most_entry_fails_the_spoofing_check():
    code, report, _ = run([*LOCAL, "--mode", "spoofed-xff", "--client-ip", "198.51.100.7", "--requests", "120"],
                          Limiter(key="left"))  # fmt: skip
    assert code == lt.EXIT_FAILED and report["checks"]["limited"] is False and report["ok"] is False


def test_global_mode_reaches_the_global_ceiling():
    code, report, _ = run([*LOCAL, "--mode", "global", "--requests", "150"], Limiter(global_burst=100))
    assert code == 0 and report["checks"] == {"global_limited": True, "retry_after_on_503": True}
    assert report["status"].get("429") is None and report["status"]["503"] >= 45 and report["retry_after"] == ["60"]


def test_no_limits_fail_the_checks_unless_only_reporting():
    unlimited = Limiter(bucket=10**9, refill=0)
    code, report, _ = run([*LOCAL, "--requests", "80"], unlimited)
    assert code == lt.EXIT_FAILED and report["checks"]["limited"] is False
    assert run([*LOCAL, "--requests", "80", "--no-check"], unlimited)[0] == 0


def test_paths_are_requested_in_turn():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200)

    run([*LOCAL, "--requests", "4", "--path", "/v1/status", "--path", "/v1/stats?span=7d"],
        transport=httpx.MockTransport(handler))  # fmt: skip
    assert sorted(seen) == ["/v1/stats", "/v1/stats", "/v1/status", "/v1/status"]


def test_made_up_addresses_are_documentation_addresses_and_global_clients_differ():
    import random

    rng = random.Random(1)
    docs = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
    made_up = [ipaddress.ip_address(lt.spoofed(rng)) for _ in range(200)]
    assert all(any(a in n for n in docs) for a in made_up)
    nets = {ipaddress.ip_network(f"{lt.distinct_client(i)}/64", strict=False) for i in range(70_000)}
    assert len(nets) == 70_000 and all(n.subnet_of(ipaddress.ip_network("2001:db8::/32")) for n in list(nets)[:100])
    assert lt.headers_for("single-ip", 0, None, rng) == {}
    assert lt.headers_for("spoofed-xff", 0, "198.51.100.7", rng)["X-Forwarded-For"].endswith(", 198.51.100.7")


@pytest.mark.parametrize(
    "url,local",
    [("http://localhost:8080", True), ("http://[::1]:8080", True), ("http://api.localhost", True),
     ("https://tifa.example.org", False), ("http://127.0.0.2", False)],
)  # fmt: skip
def test_what_counts_as_local(url, local):
    assert lt.is_local(url) is local


def test_503s_are_told_apart_by_their_message():
    def body(message):
        return json.dumps({"error": {"code": "over_capacity", "message": message}, "meta": {}}).encode()

    assert lt.busy_reason(body("The service has sent its hourly data allowance. Retry in a minute.")) == "egress"
    assert lt.busy_reason(body("Too many uncached queries at once. Retry in a minute.")) == "uncached"
    assert lt.busy_reason(body("The service is at its request limit. Retry in a minute.")) == "global"
    assert lt.busy_reason(body("The MCP server is not running.")) == "other"
    assert lt.busy_reason(b"<html>busy</html>") == "other"


def test_n_in_a_path_numbers_each_request():
    seen = []

    def handler(request):
        seen.append(str(request.url.query, "ascii"))
        return httpx.Response(200)

    run([*LOCAL, "--requests", "3", "--path", "/v1/disruptions?limit={n}"], transport=httpx.MockTransport(handler))
    assert sorted(seen) == ["limit=1", "limit=2", "limit=3"]


def test_expect_503_goes_with_global_mode_only():
    code, _, err = run([*LOCAL, "--expect-503", "uncached"], transport=httpx.MockTransport(never))
    assert code == lt.EXIT_USAGE and "--mode global" in err


def test_egress_mode_fails_without_an_egress_limit():
    code, report, _ = run([*LOCAL, "--mode", "egress", "--requests", "50"], Limiter())
    assert code == lt.EXIT_FAILED and report["checks"]["egress_limited"] is False


def real_api(**limits):
    app = pytest.importorskip("tifa.api.app")
    from tifa.config import load_settings
    from tifa.store.memory import MemoryStore

    env = {"TIFA_ROLE": "api", "TIFA_STORE": "memory", "TIFA_PROXY_HEADERS": "1"}
    env.update({f"TIFA_{k.upper()}": str(v) for k, v in limits.items()})
    return app.create_app(load_settings(env), store=MemoryStore(), mcp_factory=None)


def test_the_real_api_enforces_its_egress_allowance():
    code, report, _ = run([*LOCAL, "--mode", "egress", "--egress-per-hour", "20000", "--requests", "100"],
                          real_api(egress_per_hour=20000))  # fmt: skip
    assert code == 0 and report["ok"], report
    assert report["busy_reasons"] == {"egress": report["status"]["503"]} and report["status"].get("429") is None
    assert report["bytes"]["ok"] <= report["bytes"]["ok_allowed"] and report["checks"]["retry_after_on_503"]


def test_the_real_api_enforces_its_uncached_ceiling_and_its_request_ceiling():
    code, report, _ = run([*LOCAL, "--mode", "global", "--expect-503", "uncached", "--requests", "60",
                           "--path", "/v1/disruptions?limit={n}"], real_api())  # fmt: skip
    assert code == 0 and report["checks"]["uncached_limited"] and set(report["busy_reasons"]) == {"uncached"}
    assert report["successes"] == 10  # TIFA_UNCACHED_PER_S, all at once
    code, report, _ = run([*LOCAL, "--mode", "global", "--expect-503", "global", "--requests", "150"], real_api())
    # The burst of 100, plus whatever the 20/s refill adds while a slow machine sends the rest.
    ok, busy = report["successes"], report["busy_reasons"].get("global", 0)
    assert code == 0 and set(report["busy_reasons"]) == {"global"} and 100 <= ok <= 110 and ok + busy == 150
