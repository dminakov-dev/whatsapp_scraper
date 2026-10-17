import os

import pytest

from collector.config import Config, ConfigError, load_config


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """load_dotenv writes into os.environ; give each test its own copy without our keys."""
    names = {f.upper() for f in Config.__dataclass_fields__} | {"RECEIVER_PORT"}
    clean = {k: v for k, v in os.environ.items() if k not in names}
    monkeypatch.setattr(os, "environ", clean)


def test_defaults_without_env_file():
    assert load_config(None) == Config()


def test_values_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BRIDGE_URL=http://bridge:3000/\nBATCH_SIZE=20\nTICK_SLEEP=0.5\nLOG_LEVEL=debug\n")
    cfg = load_config(str(env))
    assert cfg.bridge_url == "http://bridge:3000"      # trailing slash stripped
    assert (cfg.batch_size, cfg.tick_sleep, cfg.log_level) == (20, 0.5, "DEBUG")


def test_real_environment_wins_over_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BATCH_SIZE=20\n")
    os.environ["BATCH_SIZE"] = "7"
    assert load_config(str(env)).batch_size == 7


def test_blank_value_falls_back_to_default():
    os.environ["BATCH_SIZE"] = "   "
    assert load_config(None).batch_size == Config().batch_size


@pytest.mark.parametrize("name, value, message", [
    ("BATCH_SIZE", "abc", "not a valid int"),
    ("TICK_SLEEP", "fast", "not a valid float"),
    ("JITTER", "-1", "must not be negative"),
    ("BATCH_SIZE", "0", "BATCH_SIZE must be between 1 and 125"),
    ("BATCH_SIZE", "126", "BATCH_SIZE must be between 1 and 125"),
    ("SYNC_EVERY", "0", "SYNC_EVERY must be at least 1"),
])
def test_invalid_values_fail_fast(name, value, message):
    os.environ[name] = value
    with pytest.raises(ConfigError, match=message):
        load_config(None)


def test_backoff_max_below_base_is_rejected():
    os.environ.update(BACKOFF_BASE="10", BACKOFF_MAX="5")
    with pytest.raises(ConfigError, match="BACKOFF_MAX"):
        load_config(None)


def test_config_is_immutable():
    with pytest.raises(AttributeError):
        Config().batch_size = 1


def test_largest_batch_size_still_fits_the_bridge_limit_when_stuck():
    from collector.config import BRIDGE_MAX_LIMIT, STUCK_LIMIT_FACTOR
    os.environ["BATCH_SIZE"] = "125"
    assert load_config(None).batch_size * STUCK_LIMIT_FACTOR <= BRIDGE_MAX_LIMIT


def test_retry_after_max_below_backoff_max_is_rejected():
    os.environ.update(BACKOFF_MAX="600", RETRY_AFTER_MAX="300")
    with pytest.raises(ConfigError, match="RETRY_AFTER_MAX"):
        load_config(None)


# --- review fixes 2 and 5: receiver boundary, finite numbers, ranges ---------------------

@pytest.mark.parametrize("name, value, message", [
    ("TICK_SLEEP", "nan", "finite"),
    ("JITTER", "inf", "finite"),
    ("BACKOFF_MAX", "-inf", "finite"),
    ("RECEIVER_PORT", "0", "RECEIVER_PORT must be between 1 and 65535"),
    ("RECEIVER_PORT", "70000", "RECEIVER_PORT must be between 1 and 65535"),
    ("REQUEST_TIMEOUT", "0", "REQUEST_TIMEOUT must be greater than 0"),
    ("LOG_IDENTIFIERS", "partial", "LOG_IDENTIFIERS must be one of"),
    ("ALLOW_NON_LOOPBACK", "maybe", "not a valid boolean"),
])
def test_non_finite_and_out_of_range_values_fail_fast(name, value, message):
    os.environ[name] = value
    with pytest.raises(ConfigError, match=message):
        load_config(None)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host):
    os.environ["HOST"] = host
    assert load_config(None).host == host


def test_non_loopback_host_fails_at_startup():
    os.environ["HOST"] = "0.0.0.0"
    with pytest.raises(ConfigError, match="not a loopback address"):
        load_config(None)


def test_non_loopback_needs_explicit_opt_in_and_a_token():
    os.environ.update(HOST="0.0.0.0", ALLOW_NON_LOOPBACK="1")
    with pytest.raises(ConfigError, match="requires a non-empty BRIDGE_TOKEN"):
        load_config(None)
    os.environ["BRIDGE_TOKEN"] = "long-random-token"
    assert load_config(None).host == "0.0.0.0"


def test_log_identifiers_default_is_masked():
    assert load_config(None).log_identifiers == "masked"
    os.environ["LOG_IDENTIFIERS"] = "FULL"
    assert load_config(None).log_identifiers == "full"
