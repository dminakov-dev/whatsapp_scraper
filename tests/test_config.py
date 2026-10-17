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
    ("BATCH_SIZE", "0", "BATCH_SIZE must be at least 1"),
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
