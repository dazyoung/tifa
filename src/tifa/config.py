"""Settings from environment variables.

Nothing is committed: production values come from the Cloud Run service definition and Secret Manager.
Every setting has a safe default for local runs and tests (in-memory store, guard off).
On Cloud Run (``K_SERVICE`` is set) the defaults switch to the Firestore store, JSON logs and proxy headers.

Fair-use and guard limits live in :class:`Limits`; each field ``x`` is read from ``TIFA_X``
(``guard_5min`` from ``TIFA_GUARD_5MIN``), so tests and dry runs can lower them.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from functools import cache
from urllib.parse import urlsplit

ROLES = ("api", "poller")
STORES = ("firestore", "memory")
SOURCES = ("a", "f")  # "a": live recording, "f": TfL's FOI release
GUARD_MODES = ("enforce", "dry-run", "off")
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")
EMULATOR_PROJECT = "demo-tifa"  # the emulator accepts any project; "demo-" ids never reach real Google Cloud
TFL_BASE_URL = "https://api.tfl.gov.uk"
DEFAULT_API_SERVICE = "tifa-api"
DEFAULT_DATABASE = "(default)"
DEFAULT_HOST = "0.0.0.0"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_PROJECT = re.compile(r"(?:[a-z0-9.-]+:)?[a-z][a-z0-9-]{4,28}[a-z0-9]")
_REGION = re.compile(r"[a-z]+-[a-z]+[0-9]+")
_SERVICE = re.compile(r"[a-z](?:[a-z0-9-]{0,47}[a-z0-9])?")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}(?::(?:[0-9]{{1,5}}|\*))?")  # host, host:port or host:*
_APP_KEY = re.compile(r"[A-Za-z0-9._~-]{8,256}")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    """A setting is missing or malformed. Messages name the variable, never a secret value."""


@dataclass(frozen=True, slots=True)
class Limits:
    """Fair-use limits and guard trips (contracts: "Fair use"). Field ``x`` is overridden by ``TIFA_X``."""

    ip_bucket: int = 60  # tokens per client IP
    ip_refill: float = 1.0  # tokens per second
    ip_buckets_max: int = 10_000  # LRU size of the per-IP buckets
    cost_cached: int = 1
    cost_uncached: int = 5  # uncached stats, disruptions, changes
    cost_mcp_call: int = 3  # MCP tools/call
    global_rps: float = 20.0
    global_burst: int = 100
    uncached_per_s: int = 10
    egress_per_hour: int = 10 * 1024 * 1024  # bytes
    guard_every: int = 120  # seconds between guard runs on the poll path
    guard_5min: int = 9_000  # requests in 5 minutes that close the API
    close_5min: int = 1_800  # first closure for that trip, doubling on repeats
    close_max: int = 86_400  # longest closure for the doubling trip
    guard_1h: int = 36_000  # requests in an hour that close the API
    close_1h: int = 7_200
    guard_month_usd: float = 2.00  # estimated month-to-date API cost that closes it until the 1st


@dataclass(frozen=True, slots=True)
class Settings:
    role: str | None = None  # TIFA_ROLE: "api" | "poller" (tifa.main requires one)
    project: str | None = None  # GOOGLE_CLOUD_PROJECT
    region: str | None = None  # TIFA_REGION
    api_service: str = DEFAULT_API_SERVICE  # TIFA_API_SERVICE: the public Cloud Run service the guard opens and closes
    tfl_app_key: str | None = field(default=None, repr=False)  # TFL_APP_KEY; None means anonymous requests
    tfl_base_url: str = TFL_BASE_URL  # TIFA_TFL_BASE_URL (tests point it at a fake)
    public_sources: tuple[str, ...] = ("a",)  # TIFA_PUBLIC_SOURCES
    allowed_hosts: tuple[str, ...] = ()  # TIFA_ALLOWED_HOSTS: Host values the MCP endpoint accepts
    store: str = "memory"  # TIFA_STORE
    firestore_database: str = DEFAULT_DATABASE  # TIFA_FIRESTORE_DATABASE
    emulator_host: str | None = None  # FIRESTORE_EMULATOR_HOST
    guard_mode: str = "off"  # TIFA_GUARD_MODE: enforce | dry-run (decide and log, never change IAM) | off
    host: str = DEFAULT_HOST  # TIFA_HOST
    port: int = 8080  # PORT (set by Cloud Run)
    log_level: str = "info"  # TIFA_LOG_LEVEL
    log_json: bool = False  # TIFA_LOG_JSON: one JSON object per line, as Cloud Logging parses it
    proxy_headers: bool = False  # TIFA_PROXY_HEADERS: trust X-Forwarded-For/-Proto from Google's front end
    service: str | None = None  # K_SERVICE (Cloud Run)
    revision: str | None = None  # K_REVISION (Cloud Run)
    limits: Limits = field(default_factory=Limits)

    @property
    def on_cloud_run(self) -> bool:
        return self.service is not None

    @property
    def tfl_auth(self) -> str:
        """How TfL requests start out: with the app key, or anonymously."""
        return "ok" if self.tfl_app_key else "anonymous"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate settings from ``env`` (default ``os.environ``). Raises ConfigError listing every problem."""
    env = os.environ if env is None else env
    errors: list[str] = []

    def get(name: str) -> str | None:
        value = env.get(name)
        if value is None:
            return None
        value = value.strip()
        return value or None

    def choice(name: str, options: tuple[str, ...], default: str) -> str:
        value = (get(name) or default).lower()
        if value not in options:
            errors.append(f"{name} must be one of {', '.join(options)}")
            return default
        return value

    def flag(name: str, default: bool) -> bool:
        value = (env.get(name) or "").strip().lower()
        if not value:
            return default
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        errors.append(f"{name} must be a boolean (1/0, true/false, yes/no, on/off)")
        return default

    service = get("K_SERVICE")
    cloud_run = service is not None
    emulator = get("FIRESTORE_EMULATOR_HOST")

    role = get("TIFA_ROLE")
    if role is not None:
        role = role.lower()
        if role not in ROLES:
            errors.append(f"TIFA_ROLE must be one of {', '.join(ROLES)}")
            role = None

    store = choice("TIFA_STORE", STORES, "firestore" if cloud_run else "memory")

    project = get("GOOGLE_CLOUD_PROJECT")
    if project is None and emulator:
        project = EMULATOR_PROJECT
    if project is not None and not _PROJECT.fullmatch(project):
        errors.append("GOOGLE_CLOUD_PROJECT is not a valid project ID")
    if store == "firestore" and project is None:
        errors.append("GOOGLE_CLOUD_PROJECT is required when TIFA_STORE=firestore")

    region = get("TIFA_REGION")
    if region is not None and not _REGION.fullmatch(region):
        errors.append("TIFA_REGION is not a valid region (for example europe-west1)")

    api_service = get("TIFA_API_SERVICE") or DEFAULT_API_SERVICE
    if not _SERVICE.fullmatch(api_service):
        errors.append("TIFA_API_SERVICE is not a valid Cloud Run service name")

    key = get("TFL_APP_KEY")
    if key is not None and not _APP_KEY.fullmatch(key):
        errors.append("TFL_APP_KEY is malformed (expected 8-256 URL-safe characters)")  # never echo the value
        key = None

    base_url = (get("TIFA_TFL_BASE_URL") or TFL_BASE_URL).rstrip("/")
    if not _valid_base_url(base_url):
        errors.append("TIFA_TFL_BASE_URL must be an https URL without a query (http only for localhost)")

    sources = _csv(get("TIFA_PUBLIC_SOURCES") or "a")
    if not sources or any(s not in SOURCES for s in sources):
        errors.append(f"TIFA_PUBLIC_SOURCES must be a comma-separated subset of {','.join(SOURCES)}")
        sources = ("a",)

    hosts = tuple(h.lower() for h in _csv(get("TIFA_ALLOWED_HOSTS") or ""))
    if any(not _HOST.fullmatch(h) for h in hosts):
        errors.append("TIFA_ALLOWED_HOSTS must be comma-separated host names, each optionally with :port or :*")

    database = get("TIFA_FIRESTORE_DATABASE") or DEFAULT_DATABASE
    if database != DEFAULT_DATABASE and not re.fullmatch(r"[a-z][a-z0-9-]{2,61}[a-z0-9]", database):
        errors.append("TIFA_FIRESTORE_DATABASE is not a valid database ID")

    guard_mode = choice("TIFA_GUARD_MODE", GUARD_MODES, "enforce" if cloud_run else "off")
    if role == "poller" and guard_mode != "off" and (project is None or region is None):
        errors.append("the guard needs GOOGLE_CLOUD_PROJECT and TIFA_REGION (or set TIFA_GUARD_MODE=off)")

    port = _int(get("PORT"), 8080)
    if port is None or not 1 <= port <= 65535:
        errors.append("PORT must be a TCP port number")
        port = 8080

    log_level = choice("TIFA_LOG_LEVEL", LOG_LEVELS, "info")
    log_json = flag("TIFA_LOG_JSON", cloud_run)
    proxy_headers = flag("TIFA_PROXY_HEADERS", cloud_run)
    limits = _limits(env, errors)

    if errors:
        raise ConfigError("invalid configuration: " + "; ".join(errors))
    return Settings(
        role=role,
        project=project,
        region=region,
        api_service=api_service,
        tfl_app_key=key,
        tfl_base_url=base_url,
        public_sources=sources,
        allowed_hosts=hosts,
        store=store,
        firestore_database=database,
        emulator_host=emulator,
        guard_mode=guard_mode,
        host=get("TIFA_HOST") or DEFAULT_HOST,
        port=port,
        log_level=log_level,
        log_json=log_json,
        proxy_headers=proxy_headers,
        service=service,
        revision=get("K_REVISION"),
        limits=limits,
    )


@cache
def get_settings() -> Settings:
    """The process-wide settings, read once. Tests that change the environment call ``get_settings.cache_clear()``."""
    return load_settings()


def _limits(env: Mapping[str, str], errors: list[str]) -> Limits:
    values: dict[str, int | float] = {}
    for f in fields(Limits):
        name = f"TIFA_{f.name.upper()}"
        raw = (env.get(name) or "").strip()
        if not raw:
            continue
        whole = type(f.default) is int
        number = _int(raw, None) if whole else _float(raw)
        if number is None or number <= 0:
            errors.append(f"{name} must be a positive {'integer' if whole else 'number'}")
            continue
        values[f.name] = number
    return Limits(**values)  # type: ignore[arg-type]


def _csv(value: str) -> tuple[str, ...]:
    """Comma-separated values, stripped, empty items dropped, first occurrence kept."""
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def _int(value: str | None, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return None


def _float(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _valid_base_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.query or parts.fragment or parts.username or parts.password or not parts.hostname:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS)
