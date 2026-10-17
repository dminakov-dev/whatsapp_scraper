"""Collector settings, loaded once from the environment / .env file.

Every setting has a safe local default, so the collector runs against bridge_mock with
an empty .env. Switching to the real bridge is a matter of changing BRIDGE_URL.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(ValueError):
    """A setting is present but has an invalid value."""


@dataclass(frozen=True)
class Config:
    # Bridge
    bridge_url: str = "http://127.0.0.1:8001"
    bridge_token: str = ""

    # Collector process
    host: str = "127.0.0.1"
    receiver_port: int = 8000
    db_path: str = "data/collector.db"
    log_dir: str = "logs"
    log_level: str = "INFO"

    # Pacing and backoff, in seconds
    tick_sleep: float = 5.0
    jitter: float = 2.0
    backoff_base: float = 1.0
    backoff_max: float = 300.0
    request_timeout: float = 10.0

    # Messages requested per backfill call ("one run = a small batch")
    batch_size: int = 50


def _get(name: str, default, cast):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw.strip())
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid {cast.__name__}") from None
    if cast in (int, float) and value < 0:
        raise ConfigError(f"{name}={raw!r} must not be negative")
    return value


def load_config(env_file: str | None = ".env") -> Config:
    """Read settings from the environment, after loading env_file if it exists.

    Real environment variables win over the file (override=False), so Docker or a
    one-off `TICK_SLEEP=0 python ...` can override .env without editing it.
    """
    if env_file:
        load_dotenv(env_file, override=False)

    d = Config()
    cfg = Config(
        bridge_url=_get("BRIDGE_URL", d.bridge_url, str).rstrip("/"),
        bridge_token=_get("BRIDGE_TOKEN", d.bridge_token, str),
        host=_get("HOST", d.host, str),
        receiver_port=_get("RECEIVER_PORT", d.receiver_port, int),
        db_path=_get("DB_PATH", d.db_path, str),
        log_dir=_get("LOG_DIR", d.log_dir, str),
        log_level=_get("LOG_LEVEL", d.log_level, str).upper(),
        tick_sleep=_get("TICK_SLEEP", d.tick_sleep, float),
        jitter=_get("JITTER", d.jitter, float),
        backoff_base=_get("BACKOFF_BASE", d.backoff_base, float),
        backoff_max=_get("BACKOFF_MAX", d.backoff_max, float),
        request_timeout=_get("REQUEST_TIMEOUT", d.request_timeout, float),
        batch_size=_get("BATCH_SIZE", d.batch_size, int),
    )
    if cfg.batch_size == 0:
        raise ConfigError("BATCH_SIZE must be at least 1")
    if cfg.backoff_max < cfg.backoff_base:
        raise ConfigError("BACKOFF_MAX must be >= BACKOFF_BASE")
    return cfg
