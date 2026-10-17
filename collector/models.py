"""Canonical record and normalization of bridge payloads.

Bridge contract (shared by bridge_mock/ and the real Node bridge)
-----------------------------------------------------------------
Health   GET /health            -> {"ok": bool, "state": "CONNECTED" | "UNPAIRED" | ...}
Chats    GET /chats             -> {"chats": [{"chat_id": str, "name": str}, ...]}
History  GET /chats/{id}/messages?since=<unix>&limit=N   (N = 1..500)
                                -> {"messages": [<Message>, ...]  (timestamp >= since, oldest first:
                                    the OLDEST N after since, never the newest N),
                                    "oldest_available": int | null,  (oldest timestamp the bridge
                                    still holds; history is a window, older messages are gone)
                                    "truncated": bool}  optional: true when the source's own
                                    window cap stopped before `since` although older messages
                                    exist; absent = unknown
Webhook  POST /webhook          {"event": "message", "message": <Message>}
                                {"event": "state", "state": "DISCONNECTED" | "UNPAIRED" | ...}
         The bridge sends header X-Bridge-Token when BRIDGE_TOKEN is configured.

<Message> = {
    "id":        str,          stable, unique within the chat: whatsapp-web.js id._serialized,
                               the SAME form in webhook events and history (it is the dedup key)
    "chat_id":   str,          JID: ...@g.us group | ...@newsletter channel | ...@c.us / ...@lid direct
    "author":    str | null,   sender JID in groups (may be an opaque @lid id); null for channel posts
    "timestamp": int,          Unix SECONDS (not milliseconds)
    "body":      str | null,   text, or the caption of a media message
    "type":      str,          "chat" for plain text, otherwise media kind (image, video, ptt, ...)
    "channel_invite_code": str | null,  optional, channels only
    "server_id": int | str | null,      optional, channels only
}

The bridge only renames fields; all interpretation (chat type, URL, author fallback,
validation) happens here, in Python, so WhatsApp library changes stay inside the bridge.
"""
import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .privacy import safe_meta
from .timeutil import iso_from_unix, now_iso

log = logging.getLogger(__name__)

SOURCE = "whatsapp"

# Unix seconds in this range cover 2001..2286. A value above it is almost certainly
# milliseconds (JS Date.now()), which would otherwise become a date ~50 000 years ahead.
_MIN_TS = 1_000_000_000
_MAX_TS = 9_999_999_999

# Default for how far in the future a message date may be (Config.future_tolerance).
DEFAULT_FUTURE_TOLERANCE = 300.0


@dataclass(frozen=True)
class CanonicalMessage:
    source: str
    chat_id: str
    chat_type: str
    message_id: str
    author: str | None
    url: str
    date: str
    text: str | None
    media_type: str | None
    collected_at: str
    via: str

    def as_row(self) -> dict[str, Any]:
        """Mapping with exactly the columns db.insert_* expects."""
        return asdict(self)


class InvalidPayload(ValueError):
    """The payload doesn't match the bridge contract.

    The message is logged and stored in account_events, so it must say WHAT is wrong without
    quoting the value: a value can be message text or a person's id.
    """


class FutureTimestamp(InvalidPayload):
    """The message is dated later than now + tolerance (bad clock or bridge bug)."""


def chat_type_of(chat_id: str) -> str:
    """Map a WhatsApp JID suffix to our chat_type. Raises InvalidPayload if unknown."""
    if chat_id.endswith("@g.us"):
        return "group"
    if chat_id.endswith("@newsletter"):
        return "channel"
    if chat_id.endswith(("@c.us", "@lid", "@s.whatsapp.net")):
        return "direct"
    raise InvalidPayload("unknown JID type")


def build_url(chat_id: str, message_id: str, invite_code: Any = None, server_id: Any = None) -> str:
    """Public permalink for channel posts when known; otherwise a stable synthetic id.

    WhatsApp groups have no public permalinks, so wa://<chat_id>/<message_id> is an
    identifier, not a link. It is unique because (chat_id, message_id) is our dedup key.
    """
    if invite_code and server_id not in (None, ""):
        return f"https://whatsapp.com/channel/{invite_code}/{server_id}"
    return f"wa://{chat_id}/{message_id}"


def _require_str(msg: Mapping[str, Any], key: str) -> str:
    value = msg.get(key)
    if not isinstance(value, str) or not value.strip():
        got = "empty string" if isinstance(value, str) else type(value).__name__
        raise InvalidPayload(f"{key!r} must be a non-empty string, got {got}")
    return value.strip()


def parse_unix_timestamp(value: Any) -> int | float:
    """Validated Unix seconds from a bridge `timestamp` field. Raises InvalidPayload."""
    if isinstance(value, bool):  # bool is a subclass of int in Python
        raise InvalidPayload("timestamp must be a number, got bool")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if not isinstance(value, (int, float)):
        raise InvalidPayload(f"timestamp must be a number, got {type(value).__name__}")
    if not _MIN_TS <= value <= _MAX_TS:
        raise InvalidPayload(f"timestamp {value!r} out of range (milliseconds instead of seconds?)")
    return value


def is_future(ts: int | float, tolerance: float) -> bool:
    return ts > time.time() + tolerance


def dated_in_future(msg: Any, tolerance: float) -> bool:
    """True if a raw bridge message has a valid timestamp later than now + tolerance.
    Lets callers tell a future-dated message apart from other malformed ones."""
    try:
        return isinstance(msg, Mapping) and is_future(parse_unix_timestamp(msg.get("timestamp")),
                                                       tolerance)
    except InvalidPayload:
        return False


def _to_canonical(msg: Any, via: str, future_tolerance: float) -> CanonicalMessage:
    if not isinstance(msg, Mapping):
        raise InvalidPayload(f"message must be an object, got {type(msg).__name__}")

    chat_id = _require_str(msg, "chat_id")
    message_id = _require_str(msg, "id")
    chat_type = chat_type_of(chat_id)
    ts = parse_unix_timestamp(msg.get("timestamp"))
    if is_future(ts, future_tolerance):
        raise FutureTimestamp(f"timestamp {iso_from_unix(ts)} is later than now + {future_tolerance:g}s")
    date = iso_from_unix(ts)

    body = msg.get("body")
    if body is not None and not isinstance(body, str):
        raise InvalidPayload(f"body must be a string or null, got {type(body).__name__}")
    text = body or None

    msg_type = msg.get("type") or "chat"
    media_type = None if msg_type == "chat" else str(msg_type)
    if text is None and media_type is None:
        raise InvalidPayload("message has neither text nor media")

    author = msg.get("author")
    if not isinstance(author, str) or not author:
        # Channel posts have no individual sender: the channel itself is the author.
        # In direct chats the other party is the chat itself.
        if chat_type in ("channel", "direct"):
            author = chat_id
        else:
            raise InvalidPayload("group message without author")

    return CanonicalMessage(
        source=SOURCE,
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=message_id,
        author=author,
        url=build_url(chat_id, message_id, msg.get("channel_invite_code"), msg.get("server_id")),
        date=date,
        text=text,
        media_type=media_type,
        collected_at=now_iso(),
        via=via,
    )


def normalize_with_reason(msg: Any, via: str, *,
                          future_tolerance: float = DEFAULT_FUTURE_TOLERANCE,
                          log_identifiers: str = "masked"
                          ) -> tuple[CanonicalMessage | None, str | None]:
    """Bridge message -> (CanonicalMessage, None), or (None, reason) if malformed. Never raises.

    A bad payload is logged with the reason and safe metadata only (chat_id, id, type;
    person ids masked unless log_identifiers="full"), never its text: it must not crash
    the receiver or abort a backfill batch, and it must not leak into logs either.
    """
    try:
        return _to_canonical(msg, via, future_tolerance), None
    except InvalidPayload as exc:
        log.warning("dropping malformed message (%s): %s", exc, safe_meta(msg, log_identifiers))
        return None, str(exc)
    except Exception as exc:  # a bug in our own parsing: still drop it
        # The traceback's exception text may quote the payload: only with LOG_IDENTIFIERS=full.
        log.error("unexpected error normalizing message (%s): %s", type(exc).__name__,
                  safe_meta(msg, log_identifiers), exc_info=log_identifiers == "full")
        return None, f"internal error: {type(exc).__name__}"


def normalize(msg: Any, via: str, *, future_tolerance: float = DEFAULT_FUTURE_TOLERANCE,
              log_identifiers: str = "masked") -> CanonicalMessage | None:
    """Bridge message -> CanonicalMessage, or None if malformed. Never raises."""
    return normalize_with_reason(msg, via, future_tolerance=future_tolerance,
                                 log_identifiers=log_identifiers)[0]
