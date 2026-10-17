"""Collector settings, loaded once from the environment / .env file.

Every setting has a safe local default, so the collector runs against bridge_mock with
an empty .env. Switching to the real bridge is a matter of changing BRIDGE_URL.
"""
import math
import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .privacy import LOG_MODES


# Bridge contract: GET /chats/{id}/messages accepts limit 1..BRIDGE_MAX_LIMIT.
BRIDGE_MAX_LIMIT = 500
# A stuck backfill retries once with limit * STUCK_LIMIT_FACTOR, which must stay within the max.
STUCK_LIMIT_FACTOR = 4
# The receiver accepts webhooks carrying group content: it binds to this machine only,
# like the bridge (bridge/src/config.js), unless explicitly allowed AND protected by a token.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


class ConfigError(ValueError):
    """A setting is present but has an invalid value."""


@dataclass(frozen=True)
class Config:
    # Bridge
    bridge_url: str = "http://127.0.0.1:8001"
    bridge_token: str = ""

    # Collector process
    host: str = "127.0.0.1"
    # Non-loopback HOST is refused unless this is set, and then only with a BRIDGE_TOKEN
    allow_non_loopback: bool = False
    receiver_port: int = 8000
    db_path: str = "data/collector.db"
    log_dir: str = "logs"
    log_level: str = "INFO"
    # Person ids in logs / account_events of rejected payloads: masked (****4567@c.us) or full
    log_identifiers: str = "masked"

    # Pacing and backoff, in seconds
    tick_sleep: float = 5.0
    jitter: float = 2.0
    backoff_base: float = 1.0
    backoff_max: float = 300.0
    # Sanity cap for a bridge Retry-After; longer values are capped and logged
    retry_after_max: float = 3600.0
    request_timeout: float = 10.0

    # Messages requested per backfill call ("one run = a small batch")
    batch_size: int = 50
    # How long a caught-up target rests before it is polled again
    target_interval: float = 60.0
    # Re-sync the target list from chat membership every N ticks
    sync_every: int = 10
    # History is re-read from cursor - overlap, so late-delivered messages (sent offline,
    # stamped with their original time) are still picked up; dedup absorbs the repeats
    backfill_overlap: float = 600.0
    # Message dates later than now + this are rejected: they would push the cursor into
    # the future, and every later message would then fall behind it
    future_tolerance: float = 300.0


def _get(name: str, default, cast):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw.strip())
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid {cast.__name__}") from None
    if cast is float and not math.isfinite(value):
        raise ConfigError(f"{name}={raw!r} must be a finite number")
    if cast in (int, float) and value < 0:
        raise ConfigError(f"{name}={raw!r} must not be negative")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}={raw!r} is not a valid boolean")


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
        allow_non_loopback=_get_bool("ALLOW_NON_LOOPBACK", d.allow_non_loopback),
        receiver_port=_get("RECEIVER_PORT", d.receiver_port, int),
        db_path=_get("DB_PATH", d.db_path, str),
        log_dir=_get("LOG_DIR", d.log_dir, str),
        log_level=_get("LOG_LEVEL", d.log_level, str).upper(),
        log_identifiers=_get("LOG_IDENTIFIERS", d.log_identifiers, str).lower(),
        tick_sleep=_get("TICK_SLEEP", d.tick_sleep, float),
        jitter=_get("JITTER", d.jitter, float),
        backoff_base=_get("BACKOFF_BASE", d.backoff_base, float),
        backoff_max=_get("BACKOFF_MAX", d.backoff_max, float),
        retry_after_max=_get("RETRY_AFTER_MAX", d.retry_after_max, float),
        request_timeout=_get("REQUEST_TIMEOUT", d.request_timeout, float),
        batch_size=_get("BATCH_SIZE", d.batch_size, int),
        target_interval=_get("TARGET_INTERVAL", d.target_interval, float),
        sync_every=_get("SYNC_EVERY", d.sync_every, int),
        backfill_overlap=_get("BACKFILL_OVERLAP", d.backfill_overlap, float),
        future_tolerance=_get("FUTURE_TOLERANCE", d.future_tolerance, float),
    )
    max_batch = BRIDGE_MAX_LIMIT // STUCK_LIMIT_FACTOR
    if not 1 <= cfg.batch_size <= max_batch:
        raise ConfigError(f"BATCH_SIZE must be between 1 and {max_batch} "
                          f"(x{STUCK_LIMIT_FACTOR} for a stuck cursor must fit the bridge "
                          f"limit of {BRIDGE_MAX_LIMIT})")
    if cfg.host not in LOOPBACK_HOSTS:
        if not cfg.allow_non_loopback:
            raise ConfigError(f"HOST={cfg.host!r} is not a loopback address "
                              f"({', '.join(LOOPBACK_HOSTS)}); set ALLOW_NON_LOOPBACK=1 to override")
        if not cfg.bridge_token:
            raise ConfigError("a non-loopback HOST requires a non-empty BRIDGE_TOKEN")
    if not 1 <= cfg.receiver_port <= 65535:
        raise ConfigError(f"RECEIVER_PORT must be between 1 and 65535, got {cfg.receiver_port}")
    if cfg.request_timeout <= 0:
        raise ConfigError("REQUEST_TIMEOUT must be greater than 0")
    if cfg.log_identifiers not in LOG_MODES:
        raise ConfigError(f"LOG_IDENTIFIERS must be one of {', '.join(LOG_MODES)}")
    if cfg.sync_every == 0:
        raise ConfigError("SYNC_EVERY must be at least 1")
    if cfg.backoff_max < cfg.backoff_base:
        raise ConfigError("BACKOFF_MAX must be >= BACKOFF_BASE")
    if cfg.retry_after_max < cfg.backoff_max:
        raise ConfigError("RETRY_AFTER_MAX must be >= BACKOFF_MAX")
    return cfg
