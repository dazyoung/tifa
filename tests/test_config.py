import pytest

from tifa.config import EMULATOR_PROJECT, ConfigError, Limits, Settings, get_settings, load_settings

KEY = "abcd" * 8  # the length of a TfL key, not a real one


def test_defaults_are_safe_for_local_runs():
    s = load_settings({})
    assert s == Settings()
    assert s.role is None
    assert s.store == "memory"
    assert s.guard_mode == "off"
    assert not s.log_json and not s.proxy_headers and not s.on_cloud_run
    assert s.public_sources == ("a",)
    assert s.tfl_app_key is None and s.tfl_auth == "anonymous"
    assert s.tfl_base_url == "https://api.tfl.gov.uk"
    assert s.port == 8080
    assert s.limits == Limits()


def test_cloud_run_defaults():
    env = {
        "K_SERVICE": "tifa-poller",
        "K_REVISION": "tifa-poller-00001-abc",
        "PORT": "9000",
        "TIFA_ROLE": "poller",
        "GOOGLE_CLOUD_PROJECT": "example-project-1",
        "TIFA_REGION": "europe-west1",
    }
    s = load_settings(env)
    assert s.on_cloud_run and s.service == "tifa-poller" and s.revision == "tifa-poller-00001-abc"
    assert s.store == "firestore" and s.guard_mode == "enforce"
    assert s.log_json and s.proxy_headers
    assert s.port == 9000


def test_cloud_run_defaults_can_be_overridden():
    env = {
        "K_SERVICE": "x",
        "TIFA_STORE": "memory",
        "TIFA_LOG_JSON": "0",
        "TIFA_PROXY_HEADERS": "off",
        "TIFA_GUARD_MODE": "dry-run",
    }
    s = load_settings(env)
    assert (s.store, s.log_json, s.proxy_headers, s.guard_mode) == ("memory", False, False, "dry-run")


def test_values_are_parsed_and_normalised():
    env = {
        "TIFA_ROLE": " API ",
        "TIFA_PUBLIC_SOURCES": "a, f ,a,",
        "TIFA_ALLOWED_HOSTS": "Tifa.Example.org, localhost:*,127.0.0.1:8080",
        "TFL_APP_KEY": f"  {KEY}\n",  # secrets added with a trailing newline still work
        "TIFA_TFL_BASE_URL": "http://localhost:8099/",
        "TIFA_LOG_LEVEL": "DEBUG",
    }
    s = load_settings(env)
    assert s.role == "api"
    assert s.public_sources == ("a", "f")
    assert s.allowed_hosts == ("tifa.example.org", "localhost:*", "127.0.0.1:8080")
    assert s.tfl_app_key == KEY and s.tfl_auth == "ok"
    assert s.tfl_base_url == "http://localhost:8099"
    assert s.log_level == "debug"


def test_the_key_never_appears_in_repr():
    s = load_settings({"TFL_APP_KEY": KEY})
    assert KEY not in repr(s)
    assert KEY not in str(s)


def test_the_key_never_appears_in_errors():
    bad = "has spaces " + KEY
    with pytest.raises(ConfigError) as err:
        load_settings({"TFL_APP_KEY": bad})
    assert "TFL_APP_KEY" in str(err.value)
    assert KEY not in str(err.value)


def test_empty_key_means_anonymous():
    assert load_settings({"TFL_APP_KEY": "  "}).tfl_app_key is None


def test_firestore_needs_a_project():
    with pytest.raises(ConfigError, match="GOOGLE_CLOUD_PROJECT is required"):
        load_settings({"TIFA_STORE": "firestore"})


def test_emulator_gets_a_demo_project():
    s = load_settings({"TIFA_STORE": "firestore", "FIRESTORE_EMULATOR_HOST": "localhost:8080"})
    assert s.project == EMULATOR_PROJECT
    assert s.emulator_host == "localhost:8080"


def test_poller_guard_needs_project_and_region():
    with pytest.raises(ConfigError, match="guard needs"):
        load_settings({"TIFA_ROLE": "poller", "TIFA_GUARD_MODE": "enforce"})
    s = load_settings(
        {
            "TIFA_ROLE": "poller",
            "TIFA_GUARD_MODE": "enforce",
            "GOOGLE_CLOUD_PROJECT": "example-project-1",
            "TIFA_REGION": "europe-west1",
        }
    )
    assert s.guard_mode == "enforce"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TIFA_ROLE", "worker"),
        ("TIFA_STORE", "sqlite"),
        ("TIFA_GUARD_MODE", "maybe"),
        ("TIFA_LOG_LEVEL", "loud"),
        ("TIFA_LOG_JSON", "perhaps"),
        ("GOOGLE_CLOUD_PROJECT", "Not_A_Project"),
        ("TIFA_REGION", "Europe West"),
        ("TIFA_API_SERVICE", "Tifa_API"),
        ("TIFA_PUBLIC_SOURCES", "a,x"),
        ("TIFA_ALLOWED_HOSTS", "https://tifa.example.org/"),
        ("TIFA_TFL_BASE_URL", "http://api.tfl.gov.uk"),
        ("TIFA_TFL_BASE_URL", "https://api.tfl.gov.uk?app_key=x"),
        ("TIFA_FIRESTORE_DATABASE", "Bad DB"),
        ("PORT", "70000"),
        ("PORT", "eighty"),
        ("TIFA_GUARD_5MIN", "0"),
        ("TIFA_GUARD_5MIN", "1.5"),
        ("TIFA_IP_REFILL", "nan"),
        ("TIFA_GUARD_MONTH_USD", "-1"),
    ],
)
def test_bad_values_are_rejected_by_name(name, value):
    with pytest.raises(ConfigError) as err:
        load_settings({name: value})
    assert name in str(err.value)


def test_every_problem_is_reported_at_once():
    with pytest.raises(ConfigError) as err:
        load_settings({"TIFA_ROLE": "x", "PORT": "x", "TIFA_STORE": "x"})
    message = str(err.value)
    assert "TIFA_ROLE" in message and "PORT" in message and "TIFA_STORE" in message


def test_limits_are_overridable():
    s = load_settings(
        {
            "TIFA_GUARD_5MIN": "10",
            "TIFA_IP_REFILL": "0.5",
            "TIFA_GUARD_MONTH_USD": "0.01",
            "TIFA_EGRESS_PER_HOUR": "1024",
        }
    )
    assert s.limits.guard_5min == 10
    assert s.limits.ip_refill == 0.5
    assert s.limits.guard_month_usd == 0.01
    assert s.limits.egress_per_hour == 1024
    assert s.limits.guard_1h == Limits().guard_1h


def test_limit_defaults_match_the_contract():
    lim = Limits()
    assert (lim.ip_bucket, lim.ip_refill, lim.ip_buckets_max) == (60, 1.0, 10_000)
    assert (lim.cost_cached, lim.cost_uncached, lim.cost_mcp_call) == (1, 5, 3)
    assert (lim.global_rps, lim.global_burst, lim.uncached_per_s) == (20.0, 100, 10)
    assert lim.egress_per_hour == 10 * 1024 * 1024
    assert (lim.guard_5min, lim.close_5min, lim.close_max) == (9_000, 1_800, 86_400)
    assert (lim.guard_1h, lim.close_1h, lim.guard_month_usd, lim.guard_every) == (36_000, 7_200, 2.0, 120)


def test_get_settings_reads_the_environment_once(monkeypatch):
    monkeypatch.setenv("TIFA_ROLE", "api")
    get_settings.cache_clear()
    try:
        assert get_settings().role == "api"
        monkeypatch.setenv("TIFA_ROLE", "poller")
        assert get_settings().role == "api"
        get_settings.cache_clear()
        assert get_settings().role == "poller"
    finally:
        get_settings.cache_clear()
