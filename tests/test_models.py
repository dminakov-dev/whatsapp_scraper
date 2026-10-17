import pytest

from collector import db
from collector.models import build_url, chat_type_of, normalize

TS = 1_760_700_000  # 2025-10-17T11:20:00Z


def group_msg(**overrides):
    msg = {"id": "false_123@g.us_ABC", "chat_id": "123@g.us", "author": "111@lid",
           "timestamp": TS, "body": "hello", "type": "chat"}
    msg.update(overrides)
    return msg


@pytest.mark.parametrize("jid, expected", [
    ("123-456@g.us", "group"),
    ("120363@newsletter", "channel"),
    ("380501112233@c.us", "direct"),
    ("987654@lid", "direct"),
])
def test_chat_type_from_jid(jid, expected):
    assert chat_type_of(jid) == expected


def test_group_message():
    m = normalize(group_msg(), via="webhook")
    assert m.chat_type == "group"
    assert m.author == "111@lid"
    assert m.date == "2025-10-17T11:20:00Z"
    assert m.text == "hello" and m.media_type is None
    assert m.url == "wa://123@g.us/false_123@g.us_ABC"
    assert m.via == "webhook" and m.source == "whatsapp"


def test_channel_post_uses_permalink_and_channel_as_author():
    m = normalize({"id": "p1", "chat_id": "120363@newsletter", "author": None, "timestamp": TS,
                   "body": "news", "type": "chat", "channel_invite_code": "0029Va",
                   "server_id": 154}, via="backfill")
    assert m.url == "https://whatsapp.com/channel/0029Va/154"
    assert m.author == "120363@newsletter"


def test_channel_post_without_invite_code_falls_back_to_synthetic_url():
    assert build_url("120363@newsletter", "p1", None, 154) == "wa://120363@newsletter/p1"


def test_media_with_caption_and_without():
    photo = normalize(group_msg(type="image", body="caption"), via="webhook")
    assert (photo.media_type, photo.text) == ("image", "caption")
    voice = normalize(group_msg(type="ptt", body=""), via="webhook")
    assert (voice.media_type, voice.text) == ("ptt", None)


def test_timestamp_as_digit_string_is_accepted():
    assert normalize(group_msg(timestamp=str(TS)), via="webhook").date == "2025-10-17T11:20:00Z"


@pytest.mark.parametrize("bad", [
    None,
    "not a dict",
    [1, 2, 3],
    {},
    group_msg(id=""),
    group_msg(id=None),
    group_msg(chat_id="weird-jid"),
    group_msg(timestamp=None),
    group_msg(timestamp="yesterday"),
    group_msg(timestamp=TS * 1000),        # milliseconds
    group_msg(timestamp=-5),
    group_msg(timestamp=True),
    group_msg(timestamp=float("nan")),
    group_msg(body=12345),
    group_msg(body="", type="chat"),       # empty text message
    group_msg(author=None),                # group message must have an author
])
def test_malformed_payloads_return_none_and_never_raise(bad):
    assert normalize(bad, via="webhook") is None


def test_canonical_row_fits_the_database(conn):
    row = normalize(group_msg(), via="webhook").as_row()
    assert set(row) == set(db.MESSAGE_COLUMNS)
    assert db.insert_messages(conn, [row]) == 1
