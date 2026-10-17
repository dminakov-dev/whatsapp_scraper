import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from collector import db, receiver
from collector.config import Config

NOW = 1_760_700_000
GROUP = "120363111111111111@g.us"


def message(**overrides):
    msg = {"id": "false_g_ABC", "chat_id": GROUP, "author": "111@lid", "timestamp": NOW,
           "body": "hello", "type": "chat"}
    msg.update(overrides)
    return {"event": "message", "message": msg}


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "rx.db"), log_dir=str(tmp_path / "logs"))


@pytest.fixture
def client(cfg):
    return TestClient(receiver.create_app(cfg))


def count(cfg, sql="SELECT COUNT(*) FROM messages"):
    conn = db.connect(cfg.db_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_message_is_stored_once_even_if_delivered_twice(client, cfg):
    first = client.post("/webhook", json=message())
    second = client.post("/webhook", json=message())
    assert first.status_code == second.status_code == 200
    assert first.json()["inserted"] == 1 and second.json()["inserted"] == 0
    assert count(cfg) == 1
    assert count(cfg, "SELECT via FROM messages") == "webhook"


@pytest.mark.parametrize("body", [
    "this is not json",
    "[]",
    json.dumps({"event": "message"}),
    json.dumps({"event": "message", "message": {"id": "x"}}),
    json.dumps(message(timestamp=NOW * 1000)),
    json.dumps({"event": "who_knows"}),
])
def test_malformed_payloads_get_200_and_store_nothing(client, cfg, body):
    resp = client.post("/webhook", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 200 and resp.json()["ok"] is False
    assert count(cfg) == 0


def test_malformed_payload_is_logged_as_account_event(client, cfg):
    client.post("/webhook", content="garbage")
    assert count(cfg, "SELECT COUNT(*) FROM account_events WHERE kind='malformed_payload'") == 1


def test_database_failure_returns_500_so_the_bridge_retries(client, cfg, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "insert_messages", locked)
    assert client.post("/webhook", json=message()).status_code == 500
    monkeypatch.undo()
    # the retry succeeds and stores the message exactly once
    assert client.post("/webhook", json=message()).json()["inserted"] == 1
    assert count(cfg) == 1


def test_direct_chats_are_never_stored(client, cfg):
    resp = client.post("/webhook", json=message(chat_id="380501112233@c.us", author=None))
    assert resp.json()["ignored"] == "direct chat"
    assert count(cfg) == 0


def test_disabled_target_is_ignored(client, cfg):
    conn = db.connect(cfg.db_path)
    db.sync_targets(conn, [{"chat_id": GROUP, "chat_type": "group", "name": "g"}])
    with conn:
        conn.execute("UPDATE targets SET enabled = 0")
    conn.close()
    assert client.post("/webhook", json=message()).json()["ignored"] == "target disabled"
    assert count(cfg) == 0


def test_webhook_never_moves_the_cursor(client, cfg):
    conn = db.connect(cfg.db_path)
    db.sync_targets(conn, [{"chat_id": GROUP, "chat_type": "group", "name": "g"}])
    conn.close()
    client.post("/webhook", json=message())
    assert count(cfg, "SELECT cursor_ts FROM targets") is None


def test_state_event_is_recorded(client, cfg):
    assert client.post("/webhook", json={"event": "state", "state": "UNPAIRED"}).status_code == 200
    assert count(cfg, "SELECT detail FROM account_events WHERE kind='state'") == "UNPAIRED"


def test_token_is_enforced_when_configured(tmp_path):
    cfg = Config(db_path=str(tmp_path / "t.db"), bridge_token="s3cret")
    client = TestClient(receiver.create_app(cfg))
    assert client.post("/webhook", json=message()).status_code == 401
    assert client.post("/webhook", json=message(), headers={"X-Bridge-Token": "nope"}).status_code == 401
    ok = client.post("/webhook", json=message(), headers={"X-Bridge-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["inserted"] == 1
