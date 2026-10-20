"""HTTP plumbing for the public API: the response envelope, ETags and 304s, Cache-Control, gzip, CORS and errors.

Every JSON response is ``{"data": ..., "meta": {...}}`` (errors: ``{"error": {"code", "message"}, "meta": {...}}``)
and the meta carries the attribution and the notice. A body is built once per distinct content and kept in a
small cache, so its bytes, and therefore its strong ETag, stay the same while the content does:

- the ETag is a digest of the exact bytes sent; the gzip variant has its own tag (suffix "-gz"), as a different
  representation must;
- If-None-Match matches either variant (weak comparison, as RFC 9110 asks for If-None-Match) and gives a 304;
- ``generated_at`` is when that body was first built and ``last_poll`` the latest poll at that time, so a month
  that has not changed keeps its tag across polls.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode

from starlette.responses import Response

from tifa.api.views import API_VERSION, ATTRIBUTION, LICENCE, NOTICE
from tifa.core.timeutil import iso
from tifa.store import codec

JSON = "application/json"
GZIP_MIN = 1024  # bytes: smaller bodies are sent as they are
CACHE_TTL = 30.0  # seconds an entry is served before it is computed again (time moves on in the figures)
CACHE_MAX_BYTES = 48 * 1024 * 1024
CACHE_MAX_ENTRIES = 4096

NO_STORE = "no-store"
CC_STATUS = "public, max-age=30, stale-while-revalidate=30"
CC_CHANGES = "public, max-age=15"
CC_LINES = "public, max-age=3600"
CC_LINE = "public, max-age=60"
CC_MONTHS = "public, max-age=60"
CC_MONTH_PAST = "public, max-age=86400"
CC_MONTH_CURRENT = "public, max-age=60"
CC_LONG = "public, max-age=3600"
CC_SHORT = "public, max-age=60"
CC_STALE = "public, max-age=15"  # anything, while the store cannot be read

EXPOSE = "ETag, RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, RateLimit-Policy, Retry-After"
CORS_API = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Expose-Headers", EXPOSE),
]
CORS_PREFLIGHT_API = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS"),
    ("Access-Control-Allow-Headers", "Accept, Accept-Encoding, If-None-Match, Cache-Control"),
    ("Access-Control-Max-Age", "86400"),
]
MCP_HEADERS = "Accept, Content-Type, Mcp-Session-Id, Mcp-Protocol-Version, Last-Event-ID"
CORS_MCP = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Expose-Headers", f"{EXPOSE}, Mcp-Session-Id, Mcp-Protocol-Version"),
]
CORS_PREFLIGHT_MCP = [
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "POST, OPTIONS"),
    ("Access-Control-Allow-Headers", MCP_HEADERS),
    ("Access-Control-Max-Age", "86400"),
]
SECURITY = [("X-Content-Type-Options", "nosniff")]


def dumps(value: Any) -> bytes:
    """Compact JSON bytes; NaN and infinities are refused (strict JSON)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def meta(sources: Sequence[str], last_poll: int | None, generated_at: float, stale: bool) -> dict[str, Any]:
    return {
        "attribution": ATTRIBUTION,
        "notice": NOTICE,
        "licence": LICENCE,
        "sources": list(sources),
        "generated_at": iso(generated_at),
        "last_poll": iso(last_poll) if last_poll is not None else None,
        "stale": stale,
        "api_version": API_VERSION,
    }


def error_doc(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message}


@dataclass(frozen=True)
class Body:
    """One response body in both encodings, with their tags."""

    raw: bytes
    gz: bytes | None
    tag: str  # the opaque part of the ETag (no quotes)
    media_type: str = JSON

    @property
    def etag(self) -> str:
        return f'"{self.tag}"'

    @property
    def etag_gz(self) -> str:
        return f'"{self.tag}-gz"'


def make_body(raw: bytes, media_type: str = JSON) -> Body:
    gz = codec.compress(raw) if len(raw) >= GZIP_MIN else None
    if gz is not None and len(gz) >= len(raw):
        gz = None
    return Body(raw, gz, hashlib.sha256(raw).hexdigest()[:32], media_type)


def envelope_bytes(data_json: bytes, meta_doc: Mapping[str, Any], *, error: bool = False) -> bytes:
    """The envelope around already-encoded data (so the data is encoded once)."""
    key = b'{"error":' if error else b'{"data":'
    return key + data_json + b',"meta":' + dumps(meta_doc) + b"}"


# ---- conditional requests and encodings --------------------------------------------------------------------


def _tags(header: str) -> list[str]:
    out = []
    for part in header.split(","):
        part = part.strip()
        if part.startswith(("W/", "w/")):
            part = part[2:]
        if part:
            out.append(part)
    return out


def not_modified(if_none_match: str | None, body: Body) -> bool:
    """If-None-Match against a body: "*" or either variant's tag, compared weakly."""
    if not if_none_match:
        return False
    tags = _tags(if_none_match)
    return "*" in tags or body.etag in tags or body.etag_gz in tags


def accepts_gzip(accept_encoding: str | None) -> bool:
    if not accept_encoding:
        return False
    ok = None
    star = None
    for part in accept_encoding.split(","):
        name, _, params = part.strip().partition(";")
        name = name.strip().lower()
        q = 1.0
        for p in params.split(";"):
            k, _, v = p.strip().partition("=")
            if k.strip().lower() == "q":
                try:
                    q = float(v)
                except ValueError:
                    q = 0.0
        if name in ("gzip", "x-gzip"):
            ok = q > 0
        elif name == "*":
            star = q > 0
    return ok if ok is not None else bool(star)


def respond(
    body: Body,
    *,
    status: int = 200,
    cache_control: str,
    headers: Mapping[str, str],
    extra: Iterable[tuple[str, str]] = (),
) -> Response:
    """The response for a body: 304 when If-None-Match matches (for a 200), gzip when accepted, with ETag and
    Cache-Control. `headers` are the request's."""
    use_gz = body.gz is not None and accepts_gzip(headers.get("accept-encoding"))
    out = [
        ("Cache-Control", cache_control),
        ("Vary", "Accept-Encoding"),
        ("ETag", body.etag_gz if use_gz else body.etag),
        *SECURITY,
        *extra,
    ]
    if status == 200 and not_modified(headers.get("if-none-match"), body):
        resp = Response(status_code=304)
        _set(resp, out)
        return resp
    content = body.gz if use_gz else body.raw
    resp = Response(content=content, status_code=status, media_type=body.media_type)
    if use_gz:
        out.append(("Content-Encoding", "gzip"))
    _set(resp, out)
    return resp


def _set(resp: Response, headers: Iterable[tuple[str, str]]) -> None:
    for k, v in headers:
        resp.headers[k] = v


# ---- the response cache ------------------------------------------------------------------------------------


@dataclass
class Entry:
    version: str  # the read model version it was computed for
    at: float  # monotonic time it was computed
    status: int
    digest: str  # of the data (or error) and the meta inputs that matter
    body: Body
    cache_control: str

    @property
    def size(self) -> int:
        return len(self.body.raw) + len(self.body.gz or b"") + 200


class ResponseCache:
    """Bodies by request key, LRU-bounded by entries and bytes. Thread-safe.

    An entry is fresh while the read model version is the one it was computed for and it is younger than `ttl`.
    A recomputation that gives the same content keeps the old body (and so the old ETag)."""

    def __init__(
        self,
        ttl: float = CACHE_TTL,
        max_bytes: int = CACHE_MAX_BYTES,
        max_entries: int = CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def fresh(self, key: str, version: str) -> Entry | None:
        with self._lock:
            e = self._entries.get(key)
            if e is None or e.version != version or self._clock() - e.at >= self.ttl:
                return None
            self._entries.move_to_end(key)
            return e

    def get(self, key: str, version: str) -> Entry | None:
        e = self.fresh(key, version)
        with self._lock:
            if e is None:
                self.misses += 1
            else:
                self.hits += 1
        return e

    def peek(self, key: str) -> Entry | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: str, entry: Entry) -> Entry:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old.size
            if entry.size > self.max_bytes // 4:  # too big to keep: serve it once
                return entry
            self._entries[key] = entry
            self._bytes += entry.size
            while self._entries and (len(self._entries) > self.max_entries or self._bytes > self.max_bytes):
                _, dropped = self._entries.popitem(last=False)
                self._bytes -= dropped.size
            return entry

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def __len__(self) -> int:
        return len(self._entries)


def build_entry(
    cache: ResponseCache,
    key: str,
    version: str,
    *,
    status: int,
    payload: Any,
    meta_inputs: tuple[Sequence[str], int | None, bool],
    cache_control: str,
    now: float,
) -> Entry:
    """Encode `payload` (the data, or for an error {"code", "message"}) into an entry, reusing the cached body when
    the content is unchanged. meta_inputs is (sources, last_poll, stale); last_poll does not count as a change."""
    sources, last_poll, stale = meta_inputs
    data_json = dumps(payload)
    digest = hashlib.sha256(
        data_json + b"|" + str(status).encode() + b"|" + ",".join(sources).encode() + (b"|s" if stale else b"|f")
    ).hexdigest()
    old = cache.peek(key)
    if old is not None and old.digest == digest and old.status == status:
        body = old.body
    else:
        raw = envelope_bytes(data_json, meta(sources, last_poll, now, stale), error=status >= 400)
        body = make_body(raw)
    return cache.put(key, Entry(version, cache._clock(), status, digest, body, cache_control))


# ---- request keys ------------------------------------------------------------------------------------------

ROUTE_PARAMS: dict[str, tuple[str, ...]] = {
    "/v1/changes": ("since",),
    "/v1/disruptions": ("line", "from", "to", "category", "cause", "planned", "source", "limit", "cursor"),
    "/v1/stats": ("span", "end", "line"),
    "/v1/coverage": ("from", "to"),
}


def request_key(path: str, query_string: bytes | str) -> str:
    """A canonical key for a request: the path and its known parameters in a fixed order (others are ignored, so
    they cannot be used to dodge the cache)."""
    names = ROUTE_PARAMS.get(path, ())
    if not names:
        return path
    qs = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
    pairs = parse_qsl(qs, keep_blank_values=True)
    kept = [(n, v) for name in names for n, v in pairs if n == name]
    return f"{path}?{urlencode(kept)}" if kept else path


def error_body(
    status: int, code: str, message: str, sources: Sequence[str], last_poll: int | None, stale: bool, now: float
) -> Body:
    raw = envelope_bytes(dumps(error_doc(code, message)), meta(sources, last_poll, now, stale), error=True)
    return make_body(raw)


def plain_error(
    status: int,
    code: str,
    message: str,
    *,
    sources: Sequence[str],
    last_poll: int | None,
    stale: bool,
    now: float,
    headers: Iterable[tuple[str, str]] = (),
) -> Response:
    """An error response outside the cache (no ETag needed: errors are never stored by clients)."""
    body = error_body(status, code, message, sources, last_poll, stale, now)
    resp = Response(content=body.raw, status_code=status, media_type=JSON)
    _set(resp, [("Cache-Control", NO_STORE), *SECURITY, *headers])
    return resp


def query_pairs(query_string: bytes) -> list[tuple[str, str]]:
    return parse_qsl(query_string.decode("latin-1"), keep_blank_values=True)
