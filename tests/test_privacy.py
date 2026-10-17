"""Review fix 3: rejected payloads never leak text or full person ids into logs/account_events;
stored messages keep full ids; exports pseudonymize person ids by default."""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from collector import db, receiver
from collector.config import Config
from collector.export import export_jsonl
from collector.models import normalize
from collector.privacy import mask_ids, pseudonym, pseudonymize_ids, safe_meta

NOW = 1_760_700_000
GROUP = "120363111111111111@g.us"
PHONE = "972501234567@c.us"
LID = "123456789012345@lid"
SECRET = "SECRET-TEXT do not log"
# whatsapp-web.js group message id: the participant JID is the last segment
GROUP_MSG_ID = f"false_{GROUP}_3EB0AAAA_{PHONE}"
SALT = "test-salt-0123456789abcdef"


# --- masking primitives -----------------------------------------------------------------

def test_mask_keeps_last_four_digits_of_person_ids_only():
    assert mask_ids(PHONE) == "********4567@c.us"
    assert mask_ids(LID) == "***********2345@lid"
    assert mask_ids(GROUP_MSG_ID) == f"false_{GROUP}_3EB0AAAA_********4567@c.us"
    assert mask_ids("120363000000000001@newsletter") == "120363000000000001@newsletter"
    assert mask_ids(f"false_{PHONE}_KEY") == "false_********4567@c.us_KEY"


def test_safe_meta_never_includes_body_or_unexpected_values():
    raw = {"chat_id": PHONE, "id": GROUP_MSG_ID, "type": "chat", "body": SECRET,
           "author": PHONE}
    meta = safe_meta(raw)
    assert SECRET not in meta and "972501234567" not in meta
    assert "chat_id=********4567@c.us" in meta and "type=chat" in meta
    assert safe_meta({"chat_id": {"nested": SECRET}}).startswith("chat_id=<dict>")
    assert safe_meta("raw string " + SECRET) == "payload=str"
    assert "972501234567" in safe_meta(raw, "full")


# --- receiver: logs and account_events --------------------------------------------------

def rejected(chat_id, msg_id, **extra):
    """A message the collector rejects (no timestamp) carrying secret text and a phone id."""
    return {"event": "message", "message": {"id": msg_id, "chat_id": chat_id, "author": PHONE,
                                             "body": SECRET, "type": "chat", **extra}}


def details(cfg):
    conn = db.connect(cfg.db_path)
    try:
        return [r[0] for r in conn.execute("SELECT detail FROM account_events")]
    finally:
        conn.close()


@pytest.mark.parametrize("payload, masked", [
    # a person id as the chat id
    (rejected(PHONE, "false_x_KEY"), "chat_id=********4567@c.us"),
    # a person id inside a group message id
    (rejected(GROUP, GROUP_MSG_ID), "id=false_120363111111111111@g.us_3EB0AAAA_********4567@c.us"),
])
def test_rejected_payload_is_masked_in_log_and_account_events(tmp_path, caplog, payload, masked):
    cfg = Config(db_path=str(tmp_path / "rx.db"))
    client = TestClient(receiver.create_app(cfg))
    with caplog.at_level(logging.DEBUG):
        assert client.post("/webhook", json=payload).json()["ignored"] == "malformed message"
    stored = details(cfg)
    assert len(stored) == 1 and masked in stored[0] and "timestamp" in stored[0]
    for text in (caplog.text, *stored):
        assert SECRET not in text
        assert "972501234567" not in text
    assert masked in caplog.text


def test_future_timestamp_event_is_masked_too(tmp_path):
    import time
    cfg = Config(db_path=str(tmp_path / "rx.db"))
    client = TestClient(receiver.create_app(cfg))
    client.post("/webhook", json=rejected(GROUP, GROUP_MSG_ID, timestamp=int(time.time()) + 86400))
    [detail] = details(cfg)
    assert SECRET not in detail and "972501234567" not in detail and "4567@c.us" in detail


def test_log_identifiers_full_shows_ids_but_still_never_text(tmp_path, caplog):
    cfg = Config(db_path=str(tmp_path / "rx.db"), log_identifiers="full")
    client = TestClient(receiver.create_app(cfg))
    with caplog.at_level(logging.WARNING):
        client.post("/webhook", json=rejected(GROUP, GROUP_MSG_ID))
    [detail] = details(cfg)
    assert GROUP_MSG_ID in detail and GROUP_MSG_ID in caplog.text
    assert SECRET not in detail and SECRET not in caplog.text


def test_non_json_body_is_described_by_size_only(tmp_path, caplog):
    cfg = Config(db_path=str(tmp_path / "rx.db"))
    client = TestClient(receiver.create_app(cfg))
    with caplog.at_level(logging.WARNING):
        client.post("/webhook", content=f"not json {SECRET} {PHONE}")
    assert details(cfg) == [f"body is not JSON ({len(f'not json {SECRET} {PHONE}')} bytes)"]
    assert SECRET not in caplog.text and "972501234567" not in caplog.text


def test_backfill_normalizer_logs_are_masked(caplog):
    with caplog.at_level(logging.WARNING):
        assert normalize({"id": GROUP_MSG_ID, "chat_id": GROUP, "body": SECRET, "type": "chat",
                          "timestamp": NOW}, via="backfill") is None   # group message w/o author
    assert "group message without author" in caplog.text
    assert SECRET not in caplog.text and "972501234567" not in caplog.text


def test_stored_messages_keep_full_identifiers(tmp_path):
    cfg = Config(db_path=str(tmp_path / "rx.db"))
    client = TestClient(receiver.create_app(cfg))
    ok = {"event": "message", "message": {"id": GROUP_MSG_ID, "chat_id": GROUP, "author": PHONE,
                                          "timestamp": NOW, "body": "hi", "type": "chat"}}
    assert client.post("/webhook", json=ok).json()["inserted"] == 1
    conn = db.connect(cfg.db_path)
    row = conn.execute("SELECT author, message_id, url FROM messages").fetchone()
    conn.close()
    assert row["author"] == PHONE and row["message_id"] == GROUP_MSG_ID and PHONE in row["url"]


# --- export pseudonymization ------------------------------------------------------------

def seed(conn):
    db.insert_messages(conn, [
        {"source": "whatsapp", "chat_id": GROUP, "chat_type": "group", "message_id": GROUP_MSG_ID,
         "author": PHONE, "url": f"wa://{GROUP}/{GROUP_MSG_ID}", "date": "2026-10-17T10:00:00Z",
         "text": "hi", "media_type": None, "collected_at": "2026-10-17T12:00:00Z", "via": "backfill"},
        {"source": "whatsapp", "chat_id": "1@newsletter", "chat_type": "channel",
         "message_id": "false_1@newsletter_K", "author": "1@newsletter",
         "url": "https://whatsapp.com/channel/X/1", "date": "2026-10-17T11:00:00Z",
         "text": "post", "media_type": None, "collected_at": "2026-10-17T12:00:00Z",
         "via": "backfill"},
    ])


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_pseudonym_is_stable_salted_and_not_reversible_by_eye():
    assert pseudonym(PHONE, SALT) == pseudonym(PHONE, SALT)
    assert pseudonym(PHONE, SALT) != pseudonym(PHONE, SALT + "x")
    assert pseudonym(PHONE, SALT).startswith("anon_") and "4567" not in pseudonym(PHONE, SALT)
    assert pseudonymize_ids(None, SALT) is None


def test_export_pseudonymizes_group_authors_everywhere(conn, tmp_path):
    seed(conn)
    out = tmp_path / "m.jsonl"
    export_jsonl(conn, str(out), pseudonym_salt=SALT)
    group, channel = read(out)
    anon = pseudonym(PHONE, SALT)
    assert group["author"] == anon
    assert group["message_id"].endswith(f"_{anon}") and GROUP in group["message_id"]
    assert anon in group["url"]
    assert "972501234567" not in out.read_text(encoding="utf-8")
    assert channel["author"] == "1@newsletter"          # channels are not persons


def test_export_without_salt_keeps_real_ids(conn, tmp_path):
    seed(conn)
    out = tmp_path / "m.jsonl"
    export_jsonl(conn, str(out))
    assert read(out)[0]["author"] == PHONE


def test_export_rejects_a_short_salt(conn, tmp_path):
    with pytest.raises(ValueError, match="salt"):
        export_jsonl(conn, str(tmp_path / "m.jsonl"), pseudonym_salt="short")
