"""Canonical record and normalization of bridge payloads.

Bridge contract (shared by bridge_mock/ and the real Node bridge)
-----------------------------------------------------------------
Health   GET /health            -> {"ok": bool, "state": "CONNECTED" | "UNPAIRED" | ...}
Chats    GET /chats             -> {"chats": [{"chat_id": str, "name": str}, ...]}
History  GET /chats/{id}/messages?since=<unix>&limit=N
                                -> {"messages": [<Message>, ...]  (timestamp >= since, oldest first),
                                    "oldest_available": int | null}  (oldest timestamp the bridge
                                    still holds; history is a window, older messages are gone)
Webhook  POST /webhook          {"event": "message", "message": <Message>}
                                {"event": "state", "state": "DISCONNECTED" | "UNPAIRED" | ...}
         The bridge sends header X-Bridge-Token when BRIDGE_TOKEN is configured.

<Message> = {
    "id":        str,          stable, unique within the chat (whatsapp-web.js id._serialized)
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
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .timeutil import iso_from_unix, now_iso

log = logging.getLogger(__name__)

SOURCE = "whatsapp"

# Unix seconds in this range cover 2001..2286. A value above it is almost certainly
# milliseconds (JS Date.now()), which would otherwise become a date ~50 000 years ahead.
_MIN_TS = 1_000_000_000
_MAX_TS = 9_999_999_999


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
    """The payload doesn't match the bridge contract."""


def chat_type_of(chat_id: str) -> str:
    """Map a WhatsApp JID suffix to our chat_type. Raises InvalidPayload if unknown."""
    if chat_id.endswith("@g.us"):
        return "group"
    if chat_id.endswith("@newsletter"):
        return "channel"
    if chat_id.endswith(("@c.us", "@lid", "@s.whatsapp.net")):
        return "direct"
    raise InvalidPayload(f"unknown JID type: {chat_id!r}")


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
        raise InvalidPayload(f"{key!r} must be a non-empty string, got {value!r}")
    return value.strip()


def _parse_timestamp(value: Any) -> str:
    if isinstance(value, bool):  # bool is a subclass of int in Python
        raise InvalidPayload(f"timestamp must be a number, got {value!r}")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if not isinstance(value, (int, float)):
        raise InvalidPayload(f"timestamp must be a number, got {value!r}")
    if not _MIN_TS <= value <= _MAX_TS:
        raise InvalidPayload(f"timestamp {value!r} out of range (milliseconds instead of seconds?)")
    return iso_from_unix(value)


def _to_canonical(msg: Any, via: str) -> CanonicalMessage:
    if not isinstance(msg, Mapping):
        raise InvalidPayload(f"message must be an object, got {type(msg).__name__}")

    chat_id = _require_str(msg, "chat_id")
    message_id = _require_str(msg, "id")
    chat_type = chat_type_of(chat_id)
    date = _parse_timestamp(msg.get("timestamp"))

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


def normalize(msg: Any, via: str) -> CanonicalMessage | None:
    """Bridge message -> CanonicalMessage, or None if malformed. Never raises.

    A bad payload is logged with the reason and dropped; it must not crash the
    receiver or abort a backfill batch.
    """
    try:
        return _to_canonical(msg, via)
    except InvalidPayload as exc:
        log.warning("dropping malformed message (%s): %.200r", exc, msg)
        return None
    except Exception:  # a bug in our own parsing: still drop, but with a traceback
        log.exception("unexpected error normalizing message: %.200r", msg)
        return None
