"""HTTP client for TfL's line status (contracts: "Poller").

One `httpx.Client` per process keeps the connection to TfL alive between polls. Each fetch has a connect timeout of
3 s, a read timeout of 12 s and a total deadline of 15 s, asks for gzip, and caps the body at 2 MB both as sent and
decoded. The client decompresses the raw bytes itself, never asking zlib for more than the room left under the
cap, so a small compressed body that would expand hugely is refused before it is expanded.

The app key travels as the `app_key` query parameter, so the request URL is a secret:

- httpx and httpcore log every URL at INFO and DEBUG, so their loggers are held at WARNING;
- nothing here logs a URL, and `TflError` messages are built from fixed text and redacted exception text, raised
  outside the `except` block so no httpx exception (which carries the request and its URL) is chained to them;
- `repr()` of the client never shows the key.

If TfL answers 401 or 403 to a request with the key, the client makes one anonymous request instead and reports
`auth = "degraded"`; for the next `key_backoff` seconds it asks anonymously straight away, then tries the key again.
"""

from __future__ import annotations

import json
import logging
import re
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from tifa.config import TFL_BASE_URL
from tifa.core.tfl import URL_PATH

log = logging.getLogger("tifa.poller.tfl")

# httpx logs each request line (with the query string) at INFO; httpcore logs at DEBUG. Keep both quiet whatever
# the root level is, here as well as in tifa.main, so a test run or a REPL cannot print the key either.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 12.0
TOTAL_TIMEOUT = 15.0
MAX_BYTES = 2_000_000  # decoded; the real answer is about 16 KB
KEY_BACKOFF = 900  # seconds to ask anonymously after TfL refused the key
USER_AGENT = "tifa/0.1 (TfL line status recorder)"
AUTH_OK, AUTH_DEGRADED, AUTH_ANONYMOUS = "ok", "degraded", "anonymous"

_APP_KEY_PARAM = re.compile(r"(app_key=)[^&\s\"'<>\\]+", re.IGNORECASE)
_RETRYABLE = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)  # a kept-alive connection gone stale


class TflError(Exception):
    """A TfL request that gave no usable answer. `code` is what a failed slot records:

    "timeout", "network", "http_<status>", "too_large", "bad_encoding" or "bad_json"."""

    def __init__(self, code: str, message: str, *, status: int | None = None, auth: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.auth = auth

    def __repr__(self) -> str:
        return f"TflError({self.code!r}, {str(self)!r})"


@dataclass(frozen=True)
class FetchResult:
    raw: Any  # the decoded JSON body (validate it before use)
    auth: str  # "ok" (with the key), "degraded" (the key was refused; anonymous) or "anonymous" (no key)
    status: int  # HTTP status, 200
    size: int  # decoded body bytes
    elapsed_ms: int


class TflClient:
    def __init__(
        self,
        app_key: str | None = None,
        base_url: str = TFL_BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
        total_timeout: float = TOTAL_TIMEOUT,
        max_bytes: int = MAX_BYTES,
        key_backoff: int = KEY_BACKOFF,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = app_key or None
        self._url = base_url.rstrip("/") + URL_PATH
        self._connect, self._read, self._total = connect_timeout, read_timeout, total_timeout
        self._max_bytes = max_bytes
        self._key_backoff = key_backoff
        self._monotonic = monotonic
        self._key_refused_at: float | None = None
        self._http = httpx.Client(
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"},
            timeout=httpx.Timeout(connect_timeout, read=read_timeout),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=120),
            follow_redirects=False,
            trust_env=False,  # no proxy settings or .netrc from the environment
        )

    def __repr__(self) -> str:
        return f"TflClient(key={'set' if self._key else 'none'})"

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TflClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def redact(self, text: str) -> str:
        """Text with the key and any app_key=... value masked."""
        text = _APP_KEY_PARAM.sub(r"\1REDACTED", text)
        if self._key:
            text = text.replace(self._key, "REDACTED")
        return text

    def fetch_status(self) -> FetchResult:
        """GET the line status of every mode Tifa records. Raises TflError; never logs or raises the URL."""
        start = self._monotonic()
        deadline = start + self._total
        backing_off = (
            self._key is not None
            and self._key_refused_at is not None
            and start - self._key_refused_at < self._key_backoff
        )
        if self._key is None:
            auth = AUTH_ANONYMOUS
        elif backing_off:
            auth = AUTH_DEGRADED
        else:
            status, body = self._get(deadline, with_key=True)
            if status not in (401, 403):
                self._key_refused_at = None
                return self._result(status, body, AUTH_OK, start)
            log.warning("TfL refused the app key (HTTP %d); asking anonymously", status)
            self._key_refused_at = self._monotonic()
            auth = AUTH_DEGRADED
        status, body = self._get(deadline, with_key=False)
        return self._result(status, body, auth, start)

    def _result(self, status: int, body: bytes, auth: str, start: float) -> FetchResult:
        if status != 200:
            raise TflError(f"http_{status}", f"TfL answered HTTP {status}", status=status, auth=auth)
        try:
            raw = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raw = _BAD
        if raw is _BAD:
            raise TflError("bad_json", "TfL's answer is not JSON", status=status, auth=auth)
        elapsed = int((self._monotonic() - start) * 1000)
        return FetchResult(raw=raw, auth=auth, status=status, size=len(body), elapsed_ms=elapsed)

    def _get(self, deadline: float, *, with_key: bool) -> tuple[int, bytes]:
        """(status, decoded body) of one GET, with one retry when a kept-alive connection turns out to be stale."""
        for attempt in (1, 2):
            try:
                return self._get_once(deadline, with_key=with_key)
            except _Stale as stale:
                if attempt == 2 or deadline - self._monotonic() < self._connect + 1:
                    raise TflError("network", f"TfL connection failed ({stale.kind})") from None
        raise AssertionError("unreachable")  # pragma: no cover

    def _get_once(self, deadline: float, *, with_key: bool) -> tuple[int, bytes]:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TflError("timeout", "TfL request timed out (total)")
        timeout = httpx.Timeout(
            connect=min(self._connect, remaining),
            read=min(self._read, remaining),
            write=min(self._connect, remaining),
            pool=min(self._connect, remaining),
        )
        params = {"app_key": self._key} if with_key and self._key else None
        error: TflError | None = None
        stale: str | None = None
        status = 0
        body = _Body(self._max_bytes)
        try:
            with self._http.stream("GET", self._url, params=params, timeout=timeout) as resp:
                status = resp.status_code
                if resp.is_stream_consumed:  # built in memory (a mock's answer), which httpx has read and decoded
                    chunks: Iterable[bytes] = [resp.content]
                else:  # from the network: the bytes as sent, which _Body decodes within the cap
                    body.start(resp.headers.get("content-encoding"))
                    chunks = resp.iter_raw()
                for chunk in chunks:
                    if not body.feed(chunk):
                        error = TflError("too_large", "TfL's answer is unexpectedly large", status=status)
                        break
                    if self._monotonic() > deadline:
                        error = TflError("timeout", "TfL request timed out (total)", status=status)
                        break
                else:
                    if not body.finish():
                        error = TflError("too_large", "TfL's answer is unexpectedly large", status=status)
        except httpx.TimeoutException as exc:
            error = TflError("timeout", f"TfL request timed out ({_phase(exc)})")
        except (_BadEncoding, httpx.DecodingError):
            error = TflError("bad_encoding", "TfL's answer could not be decompressed", status=status or None)
        except _RETRYABLE as exc:
            stale = type(exc).__name__
        except httpx.HTTPError as exc:
            # The text of a transport error names the problem, not the URL; redact it anyway.
            error = TflError("network", f"TfL request failed ({type(exc).__name__}: {self.redact(str(exc))[:200]})")
        # Raised here, outside the except blocks: an httpx exception holds the request (and the key in its URL), so
        # it must not become the __context__ of what the caller logs.
        if stale is not None:
            raise _Stale(stale)
        if error is not None:
            raise error
        return status, body.value()


class _Stale(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _BadEncoding(Exception):
    """A body that cannot be decoded: an unknown Content-Encoding or a broken gzip stream."""


class _Body:
    """A response body as it arrives, decoded (gzip, or none) and capped at `limit` bytes both as sent and decoded.
    zlib is never asked for more than the room left under the cap plus one byte, so a body that would decode past
    the cap is refused after at most that much output, whatever one network chunk would expand to."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._raw = 0
        self._size = 0
        self._parts: list[bytes] = []
        self._zlib: Any = None

    def start(self, content_encoding: str | None) -> None:
        codings = [c.strip().lower() for c in (content_encoding or "").split(",")]
        codings = [c for c in codings if c and c != "identity"]
        if not codings:
            return
        if codings in (["gzip"], ["x-gzip"], ["deflate"]):
            self._zlib = zlib.decompressobj(32 + zlib.MAX_WBITS)  # a gzip or zlib header, detected
            return
        raise _BadEncoding(content_encoding)

    def feed(self, data: bytes) -> bool:
        """Take one chunk as sent; False once the body is over the cap."""
        self._raw += len(data)
        if self._raw > self._limit:
            return False
        if self._zlib is None:
            return self._add(data)
        while data and not self._zlib.eof:  # bytes after the end of the stream are ignored, as httpx does
            try:
                out = self._zlib.decompress(data, self._limit - self._size + 1)
            except zlib.error:
                raise _BadEncoding("gzip") from None
            if not self._add(out):
                return False
            data = self._zlib.unconsumed_tail
        return True

    def finish(self) -> bool:
        """The end of the body; False if what zlib still held takes it over the cap."""
        if self._zlib is None:
            return True
        try:
            out = self._zlib.flush(self._limit - self._size + 1)
        except zlib.error:
            raise _BadEncoding("gzip") from None
        return self._add(out)

    def _add(self, out: bytes) -> bool:
        self._size += len(out)
        if self._size > self._limit:
            return False
        if out:
            self._parts.append(out)
        return True

    def value(self) -> bytes:
        return b"".join(self._parts)


_BAD = object()


def _phase(exc: httpx.TimeoutException) -> str:
    return {
        httpx.ConnectTimeout: "connect",
        httpx.ReadTimeout: "read",
        httpx.WriteTimeout: "write",
        httpx.PoolTimeout: "pool",
    }.get(type(exc), "timeout")
