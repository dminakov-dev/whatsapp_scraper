"""Mock bridge settings (MOCK_* variables in .env).

Fault rates are probabilities per request (0..1). They live in a mutable `Faults`
object so tests and the /_admin/faults endpoint can change them at runtime.
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass
class Faults:
    fail_5xx: float = 0.0      # 503 Service Unavailable
    fail_409: float = 0.0      # 409 Conflict (WhatsApp Web busy / page reloading)
    fail_429: float = 0.0      # 429 Too Many Requests, with Retry-After
    slow: float = 0.0          # respond after `slow_seconds` (beyond the client timeout)
    duplicate: float = 0.0     # webhook: send the same event twice
    malformed: float = 0.0     # webhook: also send a broken payload
    auth_fail: bool = False    # account unpaired: every endpoint answers 401
    retry_after: int = 10
    slow_seconds: float = 15.0


@dataclass
class MockSettings:
    host: str = "127.0.0.1"
    port: int = 8001
    webhook_url: str = "http://127.0.0.1:8000/webhook"
    bridge_token: str = ""
    seed: int = 42
    window: int = 100          # messages the bridge keeps per chat (like WhatsApp Web)
    history: int = 150         # messages generated per chat at startup (> window on purpose)
    emit_interval: float = 3.0 # seconds between live webhook events; 0 disables
    log_dir: str = "logs"
    log_level: str = "INFO"
    faults: Faults = field(default_factory=Faults)


def load_settings(env_file: str | None = ".env") -> MockSettings:
    if env_file:
        load_dotenv(env_file, override=False)

    def get(name, default, cast):
        raw = os.getenv(name)
        return default if raw is None or raw.strip() == "" else cast(raw.strip())

    f = Faults()
    faults = Faults(
        fail_5xx=get("MOCK_FAIL_5XX", f.fail_5xx, float),
        fail_409=get("MOCK_FAIL_409", f.fail_409, float),
        fail_429=get("MOCK_FAIL_429", f.fail_429, float),
        slow=get("MOCK_SLOW", f.slow, float),
        duplicate=get("MOCK_DUPLICATE", f.duplicate, float),
        malformed=get("MOCK_MALFORMED", f.malformed, float),
        auth_fail=get("MOCK_AUTH_FAIL", "0", str) in ("1", "true", "yes"),
        retry_after=get("MOCK_RETRY_AFTER", f.retry_after, int),
        slow_seconds=get("MOCK_SLOW_SECONDS", f.slow_seconds, float),
    )
    d = MockSettings()
    return MockSettings(
        host=get("MOCK_HOST", d.host, str),
        port=get("MOCK_PORT", d.port, int),
        webhook_url=get("MOCK_WEBHOOK_URL", d.webhook_url, str),
        bridge_token=get("BRIDGE_TOKEN", d.bridge_token, str),
        seed=get("MOCK_SEED", d.seed, int),
        window=get("MOCK_WINDOW", d.window, int),
        history=get("MOCK_HISTORY", d.history, int),
        emit_interval=get("MOCK_EMIT_INTERVAL", d.emit_interval, float),
        log_dir=get("LOG_DIR", d.log_dir, str),
        log_level=get("LOG_LEVEL", d.log_level, str),
        faults=faults,
    )
