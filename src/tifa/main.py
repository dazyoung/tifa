"""Entry point: ``python -m tifa.main``.

TIFA_ROLE picks the ASGI app (``api`` or ``poller``), imported only once settings are valid, and uvicorn
serves it on $PORT. On Cloud Run, Google's front end is the only peer, so the client address and scheme
come from the right-most X-Forwarded-For entry and X-Forwarded-Proto (the left-most entries are whatever
the client sent and cannot be trusted).
"""

from __future__ import annotations

import argparse
import importlib
import ipaddress
import json
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

from tifa.config import ROLES, ConfigError, Settings, get_settings

APPS = {"api": "tifa.api.app:app", "poller": "tifa.poller.app:app"}
GRACEFUL_SHUTDOWN_S = 8  # Cloud Run allows 10 s after SIGTERM

Scope = MutableMapping[str, Any]
ASGIApp = Callable[[Scope, Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]]

log = logging.getLogger("tifa")


class ProxyHeaders:
    """ASGI middleware: take the client address and scheme from the proxy's headers.

    Uses the right-most X-Forwarded-For entry, which Google's front end appends, so a client cannot spoof it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or ())
            proto = headers.get(b"x-forwarded-proto", b"").decode("latin-1").split(",")[-1].strip().lower()
            client = client_ip(headers.get(b"x-forwarded-for", b"").decode("latin-1"))
            if proto in ("http", "https") or client:
                scope = dict(scope)
                if proto in ("http", "https"):
                    scope["scheme"] = proto if scope["type"] == "http" else ("wss" if proto == "https" else "ws")
                if client:
                    scope["client"] = (client, 0)
        await self.app(scope, receive, send)


def client_ip(x_forwarded_for: str) -> str | None:
    """The right-most X-Forwarded-For entry if it is an IP address, else None."""
    last = x_forwarded_for.rsplit(",", 1)[-1].strip()
    if last.startswith("[") and "]" in last:  # "[2001:db8::1]:443"
        last = last[1 : last.index("]")]
    elif last.count(":") == 1:  # "192.0.2.1:443"
        last = last.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        return None


class RedactingFormatter(logging.Formatter):
    """Masks app_key values and known secrets anywhere in a formatted record, tracebacks included."""

    # The value ends at a separator, a quote or a backslash (so an escaped quote stays intact).
    _APP_KEY = re.compile(r"(app_key=)[^&\s\"'<>\\]+", re.IGNORECASE)

    def __init__(self, *args: Any, secrets: Iterable[str] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._secrets = tuple(s for s in secrets if s and len(s) >= 8)

    def redact(self, text: str) -> str:
        text = self._APP_KEY.sub(r"\1REDACTED", text)
        for secret in self._secrets:
            text = text.replace(secret, "REDACTED")
        return text

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))


class JsonFormatter(RedactingFormatter):
    """One JSON object per line with the fields Cloud Logging maps (severity, message, time).

    Text is redacted before it is encoded: redacting the encoded line could cut through a JSON escape and leave a
    line Cloud Logging cannot parse (it would then lose the severity).
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        entry = {
            "severity": record.levelname,
            "message": self.redact(message),
            "logger": record.name,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
        }
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    secrets = [settings.tfl_app_key] if settings.tfl_app_key else []
    if settings.log_json:
        handler.setFormatter(JsonFormatter(secrets=secrets))
    else:
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secrets=secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    # httpx logs every request URL at INFO, and TfL URLs can carry the app key.
    for name in ("httpx", "httpcore", "hpack", "urllib3", "google.auth", "grpc"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))


def load_app(role: str) -> ASGIApp:
    """Import the role's ASGI app lazily, so one role never imports the other's dependencies."""
    module_name, _, attr = APPS[role].partition(":")
    return getattr(importlib.import_module(module_name), attr)


def build_app(settings: Settings) -> ASGIApp:
    if settings.role is None:
        raise ConfigError(f"TIFA_ROLE must be set to one of {', '.join(ROLES)}")
    app = load_app(settings.role)
    return ProxyHeaders(app) if settings.proxy_headers else app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tifa", description="Run a Tifa service (the role comes from TIFA_ROLE).")
    parser.add_argument("role", nargs="?", choices=ROLES, help="override TIFA_ROLE")
    parser.add_argument("--check", action="store_true", help="validate settings and import the app, then exit")
    args = parser.parse_args(argv)
    if args.role:
        os.environ["TIFA_ROLE"] = args.role  # so every get_settings() caller sees the same role
        get_settings.cache_clear()

    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"tifa: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings)
    if settings.role is None:
        print(f"tifa: TIFA_ROLE must be set to one of {', '.join(ROLES)}", file=sys.stderr)
        return 2
    try:
        app = build_app(settings)
    except Exception:
        log.exception("could not load the %s app", settings.role)
        return 1
    if args.check:
        print(f"tifa: {settings.role} app ok (store={settings.store}, tfl_auth={settings.tfl_auth})")
        return 0

    import uvicorn  # imported here so --check and tests stay light

    log.info(
        "starting %s on port %d (store=%s, revision=%s)",
        settings.role,
        settings.port,
        settings.store,
        settings.revision or "local",
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=False,  # ProxyHeaders does this; uvicorn would trust the left-most, spoofable entry
        server_header=False,
        date_header=True,
        access_log=False,  # Cloud Run logs every request already
        log_config=None,  # keep the handlers configured above
        lifespan="on",  # a failing startup stops the server, so a broken revision never takes traffic
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
