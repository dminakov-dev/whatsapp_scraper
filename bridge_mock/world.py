"""Fake WhatsApp state: chats with a bounded history window, deterministic from a seed."""
import random
import time
from dataclasses import dataclass, field

WORDS = (
    "update report city market road water power school meeting price weather traffic "
    "council bridge station delivery schedule volunteer event closed open new old today "
    "tomorrow morning evening confirmed rumour photo video statement local regional"
).split()
MEDIA_TYPES = ("image", "video", "ptt", "document")


@dataclass
class Chat:
    chat_id: str
    name: str
    kind: str                   # group | channel | direct (informational; collector uses the JID)
    dead: bool = False          # listed, but history requests fail with 404 (e.g. group we left)
    invite_code: str | None = None
    messages: list[dict] = field(default_factory=list)
    next_server_id: int = 1


def default_chats() -> list[Chat]:
    return [
        Chat("120363000000000001@newsletter", "Mock News Channel", "channel", invite_code="MOCKCH1"),
        Chat("120363000000000002@newsletter", "Mock Tech Channel", "channel", invite_code="MOCKCH2"),
        Chat("120363000000000003@newsletter", "Mock Sports Channel", "channel", invite_code="MOCKCH3"),
        Chat("120363111111111111@g.us", "Mock Community Group", "group"),
        Chat("120363222222222222@g.us", "Mock Neighbours Group", "group"),
        Chat("120363999999999999@g.us", "Group We Left", "group", dead=True),
        Chat("380500000001@c.us", "Private Person", "direct"),
    ]


class World:
    def __init__(self, seed: int = 42, window: int = 100, history: int = 150,
                 now: int | None = None, chats: list[Chat] | None = None):
        self.rng = random.Random(seed)
        self.window = window
        self.chats = {c.chat_id: c for c in (chats or default_chats())}
        self.authors = [f"{self.rng.randint(10**13, 10**14 - 1)}@lid" for _ in range(6)]
        self._seed_history(history, now or int(time.time()))

    # --- generation -----------------------------------------------------------------

    def _text(self) -> str:
        return " ".join(self.rng.choices(WORDS, k=self.rng.randint(3, 14))).capitalize() + "."

    def new_message(self, chat: Chat, ts: int) -> dict:
        is_media = self.rng.random() < 0.1
        msg_type = self.rng.choice(MEDIA_TYPES) if is_media else "chat"
        body = "" if msg_type == "ptt" else self._text()
        msg = {
            "id": f"false_{chat.chat_id}_{self.rng.getrandbits(64):016X}",
            "chat_id": chat.chat_id,
            "author": None,
            "timestamp": ts,
            "body": body,
            "type": msg_type,
        }
        if chat.kind == "channel":
            msg["channel_invite_code"] = chat.invite_code
            msg["server_id"] = chat.next_server_id
            chat.next_server_id += 1
        elif chat.kind == "group":
            msg["author"] = self.rng.choice(self.authors)
        return msg

    def add(self, chat: Chat, msg: dict) -> None:
        chat.messages.append(msg)
        if len(chat.messages) > self.window:
            del chat.messages[: len(chat.messages) - self.window]

    def _seed_history(self, count: int, now: int) -> None:
        for chat in self.chats.values():
            ts = now - count * 600
            for _ in range(count):
                # ~15% of messages share the previous message's second (same-second bursts)
                if chat.messages and self.rng.random() < 0.15:
                    ts = chat.messages[-1]["timestamp"]
                else:
                    ts += self.rng.randint(60, 900)
                self.add(chat, self.new_message(chat, min(ts, now)))

    def emit(self, now: int) -> list[dict]:
        """New live messages for one random chat: usually one, sometimes a same-second burst."""
        chat = self.rng.choice([c for c in self.chats.values() if not c.dead])
        n = self.rng.randint(2, 4) if self.rng.random() < 0.15 else 1
        new = [self.new_message(chat, now) for _ in range(n)]
        for msg in new:
            self.add(chat, msg)
        return new

    # --- queries --------------------------------------------------------------------

    def history(self, chat_id: str, since: int, limit: int) -> tuple[list[dict], int | None]:
        """Messages with timestamp >= since, oldest first, at most `limit`."""
        chat = self.chats[chat_id]
        matching = [dict(m) for m in chat.messages if m["timestamp"] >= since]
        oldest = chat.messages[0]["timestamp"] if chat.messages else None
        return matching[:limit], oldest
