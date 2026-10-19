import json
import logging
import sys
import types

import pytest

from tifa import main as entry
from tifa.config import Settings, get_settings

KEY = "abcd" * 8  # the length of a TfL key, not a real one
QUIETED = ("httpx", "httpcore", "hpack", "urllib3", "google.auth", "grpc")


@pytest.fixture(autouse=True)
def _restore_logging():
    """configure_logging() and main() replace the root handler and levels; put them back for later tests."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in QUIETED}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, value in levels.items():
        logging.getLogger(name).setLevel(value)


async def _echo_app(scope, receive, send):
    _echo_app.seen = scope


async def _call(app, scope):
    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    await app(scope, receive, send)
    return _echo_app.seen


def _http(headers):
    return {
        "type": "http",
        "scheme": "http",
        "client": ("192.0.2.1", 4321),
        "headers": [(k.encode(), v.encode()) for k, v in headers],
    }


@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("198.51.100.7, 203.0.113.9", "203.0.113.9"),  # the client's own entry is ignored
        ("evil, 203.0.113.9", "203.0.113.9"),
        ("203.0.113.9:443", "203.0.113.9"),
        ("2001:db8::1", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("203.0.113.9, not-an-ip", None),
        ("", None),
    ],
)
def test_client_ip_is_the_right_most_entry(xff, expected):
    assert entry.client_ip(xff) == expected


async def test_proxy_headers_set_client_and_scheme():
    app = entry.ProxyHeaders(_echo_app)
    seen = await _call(app, _http([("x-forwarded-for", "198.51.100.7, 203.0.113.9"), ("x-forwarded-proto", "https")]))
    assert seen["client"] == ("203.0.113.9", 0)
    assert seen["scheme"] == "https"


async def test_proxy_headers_leave_requests_without_them_alone():
    app = entry.ProxyHeaders(_echo_app)
    seen = await _call(app, _http([]))
    assert seen["client"] == ("192.0.2.1", 4321)
    assert seen["scheme"] == "http"


async def test_proxy_headers_ignore_bad_values():
    app = entry.ProxyHeaders(_echo_app)
    seen = await _call(app, _http([("x-forwarded-for", "garbage"), ("x-forwarded-proto", "gopher")]))
    assert seen["client"] == ("192.0.2.1", 4321)
    assert seen["scheme"] == "http"


async def test_proxy_headers_pass_lifespan_through():
    app = entry.ProxyHeaders(_echo_app)
    scope = {"type": "lifespan"}
    assert await _call(app, scope) is scope


def _record(msg, *args, exc=None):
    return logging.LogRecord("tifa.test", logging.INFO, __file__, 1, msg, args, exc)


def test_formatter_redacts_app_keys_and_known_secrets():
    fmt = entry.RedactingFormatter("%(message)s", secrets=[KEY])
    text = fmt.format(_record("GET https://api.tfl.gov.uk/Line?app_key=%s&x=1 and %s", "abc123secret", KEY))
    assert "abc123secret" not in text and KEY not in text
    assert "app_key=REDACTED&x=1" in text


def test_json_formatter_is_one_redacted_object_per_line():
    fmt = entry.JsonFormatter(secrets=[KEY])
    try:
        raise RuntimeError(f"failed with {KEY}")
    except RuntimeError:
        record = _record("boom", exc=sys.exc_info())
    line = fmt.format(record)
    assert "\n" not in line
    entry_ = json.loads(line)
    assert entry_["severity"] == "INFO"
    assert entry_["logger"] == "tifa.test"
    assert entry_["message"].startswith("boom\nTraceback")
    assert KEY not in line
    assert entry_["time"].endswith("Z")


@pytest.mark.parametrize(
    "msg",
    [
        'TfL request failed: url="%s"',
        "TfL request failed: url='%s'",
        "TfL request failed: %s",
        'nested: {"url": "%s"}',
    ],
)
def test_json_formatter_stays_valid_json_around_app_keys(msg):
    fmt = entry.JsonFormatter(secrets=[KEY])
    url = f"https://api.tfl.gov.uk/Line/Mode/tube/Status?app_key={KEY}&detail=true"
    line = fmt.format(logging.LogRecord("tifa.poller", logging.ERROR, __file__, 1, msg, (url,), None))
    entry_ = json.loads(line)  # raises if redaction broke the encoding
    assert entry_["severity"] == "ERROR"
    assert KEY not in line
    assert "app_key=REDACTED&detail=true" in entry_["message"]


def test_formatter_stops_an_app_key_at_a_backslash():
    fmt = entry.RedactingFormatter("%(message)s")
    assert fmt.format(_record(r"'app_key=%s\n'", "abc123secret")) == r"'app_key=REDACTED\n'"


def test_configure_logging_quiets_httpx():
    entry.configure_logging(Settings(log_json=True, tfl_app_key=KEY))
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, entry.JsonFormatter)


@pytest.fixture
def fake_apps(monkeypatch):
    """Point both roles at a stand-in module, as the real apps are tested elsewhere."""
    module = types.ModuleType("tifa_fake_app")
    module.app = _echo_app
    monkeypatch.setitem(sys.modules, "tifa_fake_app", module)
    monkeypatch.setattr(entry, "APPS", {"api": "tifa_fake_app:app", "poller": "tifa_fake_app:app"})
    for name in ("TIFA_ROLE", "K_SERVICE", "TIFA_STORE", "TIFA_PROXY_HEADERS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_build_app_wraps_only_behind_a_proxy(fake_apps):
    assert entry.build_app(Settings(role="api")) is _echo_app
    wrapped = entry.build_app(Settings(role="poller", proxy_headers=True))
    assert isinstance(wrapped, entry.ProxyHeaders) and wrapped.app is _echo_app


def test_main_check(fake_apps, monkeypatch, capsys):
    monkeypatch.setenv("TIFA_ROLE", "api")
    assert entry.main(["--check"]) == 0
    assert "api app ok" in capsys.readouterr().out


def test_main_role_argument_overrides_the_environment(fake_apps, monkeypatch, capsys):
    monkeypatch.setenv("TIFA_ROLE", "api")
    assert entry.main(["poller", "--check"]) == 0
    assert "poller app ok" in capsys.readouterr().out
    assert get_settings().role == "poller"


def test_main_needs_a_role(fake_apps, capsys):
    assert entry.main(["--check"]) == 2
    assert "TIFA_ROLE" in capsys.readouterr().err


def test_main_reports_bad_settings(fake_apps, monkeypatch, capsys):
    monkeypatch.setenv("TIFA_ROLE", "api")
    monkeypatch.setenv("PORT", "nope")
    assert entry.main(["--check"]) == 2
    assert "PORT" in capsys.readouterr().err


def test_main_serves_with_uvicorn(fake_apps, monkeypatch):
    calls = {}
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kw: calls.update(app=app, **kw)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("TIFA_ROLE", "api")
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setenv("TIFA_PROXY_HEADERS", "1")
    assert entry.main([]) == 0
    assert isinstance(calls["app"], entry.ProxyHeaders)
    assert calls["port"] == 8123 and calls["host"] == "0.0.0.0"
    assert calls["proxy_headers"] is False and calls["lifespan"] == "on" and calls["log_config"] is None
