"""Source adapter boundary: everything WhatsApp-bridge-specific on the pull side lives here.

The backfill worker only sees `Source` methods, CanonicalMessage objects and three error
types. A different source (public Telegram channel, RSS) would be another class with the
same methods; db / dedup / cursor / backfill logic would not change.

Error scope (mirrors the lecture's release() classification):
- TargetError     this chat is not accessible (403/404/410 only)         -> skip target
- RetryableError  this request failed transiently (408, 409, 5xx, read timeout, bad body)
                                                                         -> retry target later
- AccountError    loop-level: the account/bridge is unusable (401, 429, unreachable,
                  /health failing) or our own request is malformed (400 and any other
                  unexpected 4xx: a bug on our side)                      -> pause the whole loop
An unexpected 4xx is not the chat's fault, so it must never skip a target: pausing makes
the bug visible in account_events instead of silently disabling chats one by one.
"""
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote

import httpx

from .models import (DEFAULT_FUTURE_TOLERANCE, CanonicalMessage, InvalidPayload, chat_type_of,
                     is_future, normalize, parse_unix_timestamp)
from .timeutil import iso_from_unix, parse_iso

log = logging.getLogger(__name__)

# recorder(kind, detail, chat_id=..., status_code=..., latency_ms=...) -> db.log_event partial
Recorder = Callable[..., None]


class SourceError(Exception):
    def __init__(self, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class TargetError(SourceError):
    """Permanent for this chat."""


class RetryableError(SourceError):
    """Transient for this chat."""


class AccountError(SourceError):
    """Loop-level: the account / bridge is unusable, or our request itself is wrong."""

    def __init__(self, detail: str, status_code: int | None = None, *,
                 kind: str, retry_after: float | None = None):
        super().__init__(detail, status_code)
        self.kind = kind    # auth_failure | rate_limited | disconnected | bad_request | bridge_unhealthy
        self.retry_after = retry_after  # seconds, from Retry-After when the bridge sent it


@dataclass
class HistoryPage:
    messages: list[CanonicalMessage]
    raw_count: int                      # messages the bridge returned, before dropping bad ones
    dropped: int                        # messages not accepted (malformed, other chat, future)
    oldest_available: str | None        # oldest timestamp the bridge still holds (ISO)
    # How far this page reached: the newest readable, non-future timestamp among ALL raw
    # messages of this chat, including ones normalization dropped. The cursor follows this,
    # not only accepted messages, so a run of malformed messages can't freeze it.
    newest: str | None = field(default=None)
    future: int = 0                     # of `dropped`: dated later than now + tolerance


class Source(Protocol):
    def health(self) -> dict: ...
    def list_chats(self) -> list[dict]: ...
    def fetch_history(self, chat_id: str, since: str | None, limit: int) -> HistoryPage: ...


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date (RFC 9110)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def classify(status: int, headers: httpx.Headers | dict, detail: str) -> SourceError | None:
    """HTTP status -> error type (None for success). Pure function, easy to test."""
    if status < 400:
        return None
    if status in (403, 404, 410):
        return TargetError(detail, status)
    if status in (408, 409) or status >= 500:
        return RetryableError(detail, status)
    if status == 401:
        return AccountError(detail, status, kind="auth_failure")
    if status == 429:
        retry_after = parse_retry_after(headers.get("Retry-After"))
        return AccountError(detail, status, kind="rate_limited", retry_after=retry_after)
    # 400 and any 4xx the contract doesn't define (405, 413, 422, ...): our request is wrong
    return AccountError(detail, status, kind="bad_request")


class BridgeClient:
    """HTTP client for the bridge contract documented in collector/models.py."""

    def __init__(self, base_url: str, *, token: str = "", timeout: float = 10.0,
                 recorder: Recorder | None = None, client: httpx.Client | None = None,
                 future_tolerance: float = DEFAULT_FUTURE_TOLERANCE):
        self.base_url = base_url.rstrip("/")
        headers = {"X-Bridge-Token": token} if token else {}
        self.http = client or httpx.Client(timeout=timeout)
        self.http.headers.update(headers)
        self.timeout = timeout
        self.record = recorder or (lambda *a, **k: None)
        self.future_tolerance = future_tolerance

    def close(self) -> None:
        self.http.close()

    # --- transport ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None, chat_id: str | None = None) -> dict:
        url = f"{self.base_url}{path}"
        started = time.monotonic()
        try:
            resp = self.http.get(url, params=params, timeout=self.timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            self.record("disconnected", f"GET {path}: {type(exc).__name__}", chat_id=chat_id)
            raise AccountError(f"bridge unreachable: {type(exc).__name__}",
                               kind="disconnected") from exc
        except httpx.TimeoutException as exc:
            ms = int((time.monotonic() - started) * 1000)
            self.record("timeout", f"GET {path}: {type(exc).__name__}", chat_id=chat_id,
                        latency_ms=ms)
            raise RetryableError(f"timeout after {ms} ms") from exc
        except httpx.HTTPError as exc:
            self.record("bridge_error", f"GET {path}: {exc!r}", chat_id=chat_id)
            raise RetryableError(f"transport error: {type(exc).__name__}") from exc

        ms = int((time.monotonic() - started) * 1000)
        detail = self._describe(path, resp)
        error = classify(resp.status_code, resp.headers, detail)
        kind = "request" if error is None else getattr(error, "kind", "bridge_error")
        # Every response is recorded: the code/latency log is the raw material for the report.
        self.record(kind, detail, chat_id=chat_id, status_code=resp.status_code, latency_ms=ms)
        if error is not None:
            raise error
        try:
            body = resp.json()
        except ValueError as exc:
            raise RetryableError(f"invalid JSON from bridge: {detail}", resp.status_code) from exc
        if not isinstance(body, dict):
            raise RetryableError(f"unexpected body type from bridge: {detail}", resp.status_code)
        return body

    @staticmethod
    def _describe(path: str, resp: httpx.Response) -> str:
        """Short detail line, including any rate-limit headers the bridge exposes."""
        parts = [f"GET {path} -> {resp.status_code}"]
        for name, value in resp.headers.items():
            if name.lower() == "retry-after" or name.lower().startswith("x-ratelimit"):
                parts.append(f"{name}={value}")
        return " ".join(parts)

    # --- Source interface -----------------------------------------------------------

    def health(self) -> dict:
        """Raises AccountError unless the bridge is reachable and the session is connected.

        A failing health check is loop-level whatever its status (5xx, 409, timeout, bad
        JSON): it describes the whole bridge, not one chat.
        """
        try:
            body = self._get("/health")
        except (RetryableError, TargetError) as exc:
            self.record("bridge_unhealthy", f"GET /health: {exc.detail}",
                        status_code=exc.status_code)
            raise AccountError(f"health check failed: {exc.detail}", exc.status_code,
                               kind="bridge_unhealthy") from exc
        if not body.get("ok"):
            raise AccountError(f"bridge not ready: state={body.get('state')}",
                               kind="auth_failure")
        return body

    def list_chats(self) -> list[dict]:
        """Chats the account is a member of, with chat_type derived from the JID."""
        chats = []
        for chat in self._get("/chats").get("chats") or []:
            try:
                chat_id = chat["chat_id"]
                chats.append({"chat_id": chat_id, "chat_type": chat_type_of(chat_id),
                              "name": chat.get("name")})
            except (KeyError, TypeError, InvalidPayload):
                log.warning("ignoring malformed chat entry: %.200r", chat)
        return chats

    def fetch_history(self, chat_id: str, since: str | None, limit: int) -> HistoryPage:
        """Messages with date >= since (inclusive), oldest first, normalized."""
        since_unix = int(parse_iso(since).timestamp()) if since else 0
        body = self._get(f"/chats/{quote(chat_id, safe='@.')}/messages",
                         params={"since": since_unix, "limit": limit}, chat_id=chat_id)
        raw = body.get("messages")
        if not isinstance(raw, list):
            raise RetryableError("history response without a messages list")

        messages: list[CanonicalMessage] = []
        reached: list[int | float] = []   # readable timestamps of this chat's raw messages
        future = 0
        for r in raw:
            # Guard against a bridge bug mixing chats: a message must belong to the chat asked for.
            if not isinstance(r, dict) or str(r.get("chat_id", "")).strip() != chat_id:
                continue
            try:
                ts = parse_unix_timestamp(r.get("timestamp"))
            except InvalidPayload:
                ts = None                 # unreadable: can't tell where it sits in time
            if ts is not None and is_future(ts, self.future_tolerance):
                future += 1               # never used as a position: it would freeze the cursor
                ts = None
            if ts is not None:
                reached.append(ts)
            m = normalize(r, via="backfill", future_tolerance=self.future_tolerance)
            if m is not None:
                messages.append(m)
        oldest = body.get("oldest_available")
        return HistoryPage(
            messages=messages,
            raw_count=len(raw),
            dropped=len(raw) - len(messages),
            oldest_available=iso_from_unix(oldest) if isinstance(oldest, (int, float)) else None,
            newest=iso_from_unix(max(reached)) if reached else None,
            future=future,
        )
