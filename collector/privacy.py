"""Person identifiers in logs.

A person is any JID on a phone-number or LID server (@c.us, @lid, @s.whatsapp.net). They also
hide inside group message ids: whatsapp-web.js serializes them as
false_<group>@g.us_<key>_<participant>@c.us. Groups (@g.us) and channels (@newsletter) are not
persons and are never rewritten.

mask_ids() / safe_meta() are for logs and account_events of REJECTED payloads
(LOG_IDENTIFIERS=masked): ********4567@c.us is readable enough to correlate, useless to contact.
Stored messages and exports always keep the full identifiers.
"""
import re
from collections.abc import Mapping
from typing import Any

PERSON_JID = re.compile(r"(\d+)@(c\.us|lid|s\.whatsapp\.net)")

LOG_MODES = ("masked", "full")
_SAFE_FIELDS = ("chat_id", "id", "type")
_MAX_FIELD = 200


def _mask_digits(digits: str) -> str:
    keep = digits[-4:] if len(digits) > 4 else ""
    return "*" * (len(digits) - len(keep)) + keep


def mask_ids(text: str) -> str:
    """Mask every person JID in text, keeping the last 4 digits and the server."""
    return PERSON_JID.sub(lambda m: f"{_mask_digits(m.group(1))}@{m.group(2)}", text)


def safe_meta(raw: Any, mode: str = "masked") -> str:
    """Loggable description of a rejected payload: chat_id, id and type only, never text.

    Values that aren't short strings are described by their type, so a body smuggled into
    an unexpected field can't reach the log either.
    """
    if not isinstance(raw, Mapping):
        return f"payload={type(raw).__name__}"
    parts = []
    for key in _SAFE_FIELDS:
        value = raw.get(key)
        if isinstance(value, str) and len(value) <= _MAX_FIELD:
            shown = value if mode == "full" else mask_ids(value)
        elif value is None:
            shown = "-"
        else:
            shown = f"<{type(value).__name__}>"
        parts.append(f"{key}={shown}")
    return " ".join(parts)
