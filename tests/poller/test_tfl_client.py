"""tifa.poller.tfl_client: timeouts, gzip, the app key as a query parameter (never logged or raised), the anonymous
fallback on 401/403 and the error codes a failed slot records."""

import gzip
import http.server
import json
import logging
import os
import threading
import traceback
import tracemalloc
import zlib

import httpx
import pytest
import respx

from tests.poller.helpers import KEY, payload
from tifa.core.tfl import URL_PATH
from tifa.poller import tfl_client as tc
from tifa.poller.tfl_client import TflClient, TflError

BASE = "https://api.tfl.example"
BODY = json.dumps(payload()).encode()


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def handler(*responses):
    """A MockTransport handler answering with each response (or raising each exception) in turn; it records the
    requests it saw."""
    seen = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        return item

    handle.seen = seen
    return handle


def ok(body: bytes = BODY, **headers) -> httpx.Response:
    """A 200 whose body is decoded only when the client reads it (as from the network)."""
    return httpx.Response(200, stream=httpx.ByteStream(body), headers=headers)


def client(handle, key=KEY, **kw) -> TflClient:
    return TflClient(key, BASE, transport=httpx.MockTransport(handle), **kw)


def assert_no_key(*texts):
    for text in texts:
        assert KEY not in text


def everything_about(exc: BaseException) -> str:
    return "\n".join([str(exc), repr(exc), "".join(traceback.format_exception(exc))])


# ---- requests ------------------------------------------------------------------------------------------------


def test_sends_the_key_as_a_query_parameter_and_asks_for_gzip():
    h = handler(ok())
    result = client(h).fetch_status()
    (req,) = h.seen
    assert req.url.path == URL_PATH
    assert req.url.params["app_key"] == KEY
    assert "gzip" in req.headers["accept-encoding"]
    assert req.headers["user-agent"].startswith("tifa/")
    assert result.raw == payload() and result.auth == "ok" and result.status == 200
    assert result.size == len(BODY)


def test_without_a_key_asks_anonymously():
    h = handler(ok())
    result = client(h, key=None).fetch_status()
    assert "app_key" not in h.seen[0].url.params
    assert result.auth == "anonymous"


def test_gzip_bodies_are_decoded():
    h = handler(ok(gzip.compress(BODY), **{"content-encoding": "gzip"}))
    result = client(h).fetch_status()
    assert result.raw == payload() and result.size == len(BODY)


class Chunks(httpx.SyncByteStream):
    """A body that arrives in pieces of `size` bytes, as from the network."""

    def __init__(self, body: bytes, size: int) -> None:
        self.body, self.size = body, size

    def __iter__(self):
        for i in range(0, len(self.body), self.size):
            yield self.body[i : i + self.size]


def gzip_of_spaces(n: int) -> bytes:
    """gzip of n spaces, built without holding n bytes at once (a "zip bomb" when n is large)."""
    z = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    piece = b" " * 1_000_000
    parts = [z.compress(piece) for _ in range(n // len(piece))]
    return b"".join([*parts, z.compress(b" " * (n % len(piece))), z.flush()])


def gzipped(body: bytes, **kw) -> httpx.Response:
    return ok(gzip.compress(body), **{"content-encoding": "gzip"}, **kw)


def test_gzip_arriving_in_small_pieces_is_decoded():
    stream = Chunks(gzip.compress(BODY), 7)
    h = handler(lambda request: httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"}))
    assert client(h).fetch_status().raw == payload()


@pytest.mark.parametrize("encoding", ["gzip", "x-gzip", "deflate", "identity, gzip", "GZIP"])
def test_the_gzip_family_is_decoded(encoding):
    body = zlib.compress(BODY) if encoding == "deflate" else gzip.compress(BODY)
    assert client(handler(ok(body, **{"content-encoding": encoding}))).fetch_status().raw == payload()


def test_the_cap_counts_decoded_bytes():
    body = b"[" + b" " * 1998 + b"]"  # 2,000 bytes decoded, a few dozen sent
    assert client(handler(gzipped(body)), max_bytes=2000).fetch_status().size == 2000
    with pytest.raises(TflError) as info:
        client(handler(gzipped(b" " + body)), max_bytes=2000).fetch_status()
    assert info.value.code == "too_large"


def test_a_gzip_bomb_is_refused_before_it_is_expanded():
    bomb = gzip_of_spaces(200_000_000)  # 200 MB of spaces in about 200 KB, sent as one network chunk
    assert len(bomb) < 300_000
    c = client(handler(ok(bomb, **{"content-encoding": "gzip"})))
    tracemalloc.start()
    try:
        with pytest.raises(TflError) as info:
            c.fetch_status()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert info.value.code == "too_large"
    assert peak < 3 * tc.MAX_BYTES  # a few MB at most, never the 200 MB it decodes to


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, br", "compress"])
def test_an_encoding_it_did_not_ask_for_is_refused(encoding):
    with pytest.raises(TflError) as info:
        client(handler(ok(BODY, **{"content-encoding": encoding}))).fetch_status()
    assert info.value.code == "bad_encoding"
    assert info.value.__context__ is None and info.value.__cause__ is None


def test_a_body_too_large_as_sent_is_refused():
    noise = os.urandom(5_000)  # does not compress: more than 4,000 bytes are sent
    with pytest.raises(TflError) as info:
        client(handler(gzipped(noise)), max_bytes=4000).fetch_status()
    assert info.value.code == "too_large"


def test_through_a_real_socket():
    """The real transport against a server on the loopback interface: chunked gzip, then a bomb."""
    bodies = {"/ok": gzip.compress(BODY), "/bomb": gzip_of_spaces(50_000_000)}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = bodies["/bomb" if self.path.startswith("/bomb") else "/ok"]
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for i in range(0, len(body), 1000):
                    part = body[i : i + 1000]
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass  # the client hung up on the bomb

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with TflClient(None, base) as c:
            result = c.fetch_status()
        assert result.raw == payload() and result.size == len(BODY)
        with TflClient(None, base + "/bomb") as c, pytest.raises(TflError) as info:
            c.fetch_status()
        assert info.value.code == "too_large"
    finally:
        server.shutdown()
        server.server_close()


def test_one_client_keeps_its_connection_settings():
    c = client(handler(ok()))
    timeout = c._http.timeout
    assert (timeout.connect, timeout.read) == (tc.CONNECT_TIMEOUT, tc.READ_TIMEOUT) == (3.0, 12.0)
    assert tc.TOTAL_TIMEOUT == 15.0


def test_each_request_gets_a_timeout_within_the_total():
    clock = Clock()
    seen = []

    def respond(request):
        seen.append(request.extensions["timeout"])
        return ok()

    c = client(handler(respond), monotonic=clock)
    c.fetch_status()
    assert seen[0]["connect"] == 3.0 and seen[0]["read"] == 12.0


# ---- the anonymous fallback ----------------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_key_falls_back_to_one_anonymous_request(code, caplog):
    h = handler(httpx.Response(code, text="denied"), ok())
    result = client(h).fetch_status()
    assert [("app_key" in r.url.params) for r in h.seen] == [True, False]
    assert result.auth == "degraded" and result.raw == payload()
    assert "refused the app key" in caplog.text
    assert_no_key(caplog.text)


def test_after_a_refusal_the_key_rests_then_is_tried_again():
    clock = Clock()
    h = handler(httpx.Response(401), ok(), ok(), ok())
    c = client(h, monotonic=clock, key_backoff=900)
    assert c.fetch_status().auth == "degraded"
    clock.t += 60
    assert c.fetch_status().auth == "degraded"  # anonymous straight away
    assert [("app_key" in r.url.params) for r in h.seen] == [True, False, False]
    clock.t += 900
    assert c.fetch_status().auth == "ok"  # the key again, and TfL took it
    assert "app_key" in h.seen[-1].url.params


def test_anonymous_failure_after_a_refused_key_is_an_error_marked_degraded():
    h = handler(httpx.Response(403), httpx.Response(429))
    with pytest.raises(TflError) as info:
        client(h).fetch_status()
    assert info.value.code == "http_429" and info.value.auth == "degraded"


def test_without_a_key_a_401_is_just_an_error():
    h = handler(httpx.Response(401))
    with pytest.raises(TflError) as info:
        client(h, key=None).fetch_status()
    assert info.value.code == "http_401" and len(h.seen) == 1


# ---- failures ------------------------------------------------------------------------------------------------


def _request():
    return httpx.Request("GET", f"{BASE}{URL_PATH}?app_key={KEY}")


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (httpx.Response(500, text="oops"), "http_500"),
        (httpx.Response(503), "http_503"),
        (httpx.Response(429), "http_429"),
        (httpx.Response(301, headers={"location": "https://elsewhere.example/"}), "http_301"),
        (ok(b"<html>maintenance</html>"), "bad_json"),
        (ok(b"\xff\xfe not utf-8"), "bad_json"),
        (ok(b"not gzip at all", **{"content-encoding": "gzip"}), "bad_encoding"),
    ],
)
def test_bad_answers_have_codes(answer, code):
    with pytest.raises(TflError) as info:
        client(handler(answer)).fetch_status()
    assert info.value.code == code
    assert_no_key(everything_about(info.value))


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ConnectTimeout("timed out connecting to " + KEY, request=_request()), "timeout"),
        (httpx.ReadTimeout("read timed out", request=_request()), "timeout"),
        (httpx.PoolTimeout("pool", request=_request()), "timeout"),
        (httpx.ConnectError(f"cannot reach {BASE}{URL_PATH}?app_key={KEY}", request=_request()), "network"),
        (httpx.UnsupportedProtocol("weird", request=_request()), "network"),
    ],
)
def test_transport_failures_have_codes_and_never_carry_the_key(exc, code, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TflError) as info:
        client(handler(exc)).fetch_status()
    err = info.value
    assert err.code == code
    assert err.__context__ is None and err.__cause__ is None  # no httpx exception (with the URL) chained
    assert_no_key(everything_about(err), caplog.text)


def test_a_stale_kept_alive_connection_is_retried_once():
    h = handler(httpx.RemoteProtocolError("Server disconnected", request=_request()), ok())
    assert client(h).fetch_status().raw == payload()
    assert len(h.seen) == 2


def test_a_connection_that_keeps_dropping_is_a_network_error():
    h = handler(httpx.ReadError("connection reset", request=_request()))
    with pytest.raises(TflError) as info:
        client(h).fetch_status()
    assert info.value.code == "network" and len(h.seen) == 2
    assert_no_key(everything_about(info.value))


def test_the_total_deadline_stops_a_slow_body():
    clock = Clock()

    class Slow(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(5):
                clock.t += 6  # each chunk arrives 6 s after the last
                yield b" "

    h = handler(lambda request: httpx.Response(200, stream=Slow()))
    with pytest.raises(TflError) as info:
        client(h, monotonic=clock).fetch_status()
    assert info.value.code == "timeout"


def test_no_time_left_for_the_anonymous_request_is_a_timeout():
    clock = Clock()

    def slow_refusal(request):
        clock.t += 16
        return httpx.Response(401)

    with pytest.raises(TflError) as info:
        client(handler(slow_refusal), monotonic=clock).fetch_status()
    assert info.value.code == "timeout"


def test_an_oversized_body_is_refused():
    with pytest.raises(TflError) as info:
        client(handler(ok(b"[" + b" " * 3000 + b"]")), max_bytes=2000).fetch_status()
    assert info.value.code == "too_large"


# ---- the key never shows -------------------------------------------------------------------------------------


def test_repr_and_redact_hide_the_key():
    c = client(handler(ok()))
    assert_no_key(repr(c))
    assert "set" in repr(c)
    text = c.redact(f"GET {BASE}{URL_PATH}?app_key={KEY}&x=1 failed; key {KEY}")
    assert_no_key(text)
    assert "app_key=REDACTED&x=1" in text


def test_http_libraries_never_log_request_lines(caplog):
    caplog.set_level(logging.DEBUG)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    client(handler(httpx.Response(401), ok())).fetch_status()
    assert "HTTP Request" not in caplog.text
    assert_no_key(caplog.text)


@respx.mock(assert_all_called=True)
def test_through_the_real_transport(respx_mock, caplog):
    caplog.set_level(logging.DEBUG)
    route = respx_mock.get(f"{BASE}{URL_PATH}").mock(
        side_effect=[
            httpx.Response(403),
            httpx.Response(200, content=gzip.compress(BODY), headers={"content-encoding": "gzip"}),
        ]
    )
    c = TflClient(KEY, BASE)
    try:
        result = c.fetch_status()
    finally:
        c.close()
    assert result.auth == "degraded" and result.raw == payload()
    assert route.calls[0].request.url.params["app_key"] == KEY
    assert "app_key" not in route.calls[1].request.url.params
    assert_no_key(caplog.text)


@respx.mock
def test_a_real_timeout_never_carries_the_key(respx_mock, caplog):
    caplog.set_level(logging.DEBUG)
    respx_mock.get(f"{BASE}{URL_PATH}").mock(side_effect=httpx.ReadTimeout)
    c = TflClient(KEY, BASE)
    try:
        with pytest.raises(TflError) as info:
            c.fetch_status()
    finally:
        c.close()
    assert info.value.code == "timeout"
    logging.getLogger("tifa.poller").exception("poll failed", exc_info=info.value)
    assert_no_key(everything_about(info.value), caplog.text)
