"""One timestamp format for the whole project: ISO-8601 UTC, second precision, 'Z' suffix.

Cursors are stored as these strings and compared in SQL. Because every value has the
same fixed-width format, string order == time order, so `date >= cursor_ts` is correct.
WhatsApp timestamps are whole Unix seconds, so second precision loses nothing.
"""
from datetime import datetime, timedelta, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime: timezone is required")
    return dt.astimezone(timezone.utc).strftime(ISO_FORMAT)


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


def iso_from_unix(seconds: int | float) -> str:
    return to_iso(datetime.fromtimestamp(int(seconds), tz=timezone.utc))


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)


def iso_after(seconds: float, start: str | None = None) -> str:
    """Timestamp `seconds` after `start` (default: now). Used for next_run_at."""
    base = parse_iso(start) if start else datetime.now(timezone.utc)
    return to_iso(base + timedelta(seconds=seconds))
