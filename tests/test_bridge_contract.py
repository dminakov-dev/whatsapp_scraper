"""Contract test: the real Node bridge (bridge/, fake mode over a session fixture) driven by
the unchanged collector code — BridgeClient, Worker (backfill) and the webhook receiver.

This is the "swap by BRIDGE_URL only" check: nothing here talks to bridge internals except
the fake-mode controls (/_fake/state, /_fake/emit). Run on recorded data with
CONTRACT_FIXTURES=bridge/fixtures/recorded. Skipped when `node` is not installed.
"""
import json
import os
import random
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from collector import db
from collector.backfill import Worker
from collector.config import Config
from collector.receiver import create_app
from collector.source import AccountError, BridgeClient

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "bridge"
FIXTURES = Path(os.environ.get("CONTRACT_FIXTURES") or BRIDGE / "fixtures" / "handwritten")
TOKEN = "contract-token"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.05)
    raise TimeoutError("service did not come up")


@pytest.fixture(scope="module")
def fixture_data():
    return json.loads((FIXTURES / "session.json").read_text())


@pytest.fixture(scope="module")
def env(tmp_path_factory, fixture_data):
    tmp = tmp_path_factory.mktemp("contract")
    db_path = str(tmp / "collector.db")
    rx_port, bridge_port = free_port(), free_port()

    # Collector side: the real webhook receiver.
    cfg = Config(bridge_url=f"http://127.0.0.1:{bridge_port}", bridge_token=TOKEN,
                 db_path=db_path, receiver_port=rx_port, batch_size=3, tick_sleep=0, jitter=0,
                 target_interval=3600)
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=rx_port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_until(lambda: httpx.get(f"http://127.0.0.1:{rx_port}/health").status_code == 200)

    # Bridge side: the Node process, configured only through its environment.
    proc = subprocess.Popen(
        ["node", "src/main.js"], cwd=BRIDGE,
        env={**os.environ, "BRIDGE_PORT": str(bridge_port), "BRIDGE_TOKEN": TOKEN,
             "BRIDGE_FAKE_FIXTURES": str(FIXTURES), "WEBHOOK_URL": f"http://127.0.0.1:{rx_port}/webhook",
             "WEBHOOK_BACKOFF_MS": "50", "BRIDGE_FAKE_RECOVERY_MS": "300,300,300"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    headers = {"X-Bridge-Token": TOKEN}
    wait_until(lambda: httpx.get(f"{cfg.bridge_url}/health", headers=headers).status_code == 200)

    conn = db.connect(db_path)
    client = BridgeClient(cfg.bridge_url, token=TOKEN,
                          recorder=lambda *a, **k: db.log_event(conn, *a, **k))
    control = httpx.Client(base_url=cfg.bridge_url, headers=headers)
    yield {"cfg": cfg, "conn": conn, "client": client, "control": control, "proc": proc}

    control.close()
    client.close()
    conn.close()
    proc.terminate()
    proc.wait(timeout=5)
    server.should_exit = True
    thread.join(timeout=5)


def first_id(fixture_data, chat_suffix, predicate):
    for chat_id, msgs in fixture_data["messages"].items():
        if chat_id.endswith(chat_suffix):
            for m in msgs:
                if predicate(m):
                    return m["id"]["_serialized"]
    pytest.skip(f"fixture has no such message in a {chat_suffix} chat")


def stored(conn, **where):
    sql = "SELECT * FROM messages" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "")
    return conn.execute(sql, tuple(where.values())).fetchall()


def test_health_and_chats(env):
    assert env["client"].health() == {"ok": True, "state": "CONNECTED"}
    chats = env["client"].list_chats()
    assert chats and {c["chat_type"] for c in chats} <= {"group", "channel"}


def test_webhook_path_stores_content_and_drops_the_rest(env, fixture_data):
    text_id = first_id(fixture_data, "@g.us", lambda m: m["type"] == "chat" and m.get("body"))
    system_id = first_id(fixture_data, "@g.us", lambda m: m["type"] in ("gp2", "e2e_notification"))
    direct_id = first_id(fixture_data, "@c.us", lambda m: True)

    r = env["control"].post("/_fake/emit", json={"id": text_id})
    assert r.json()["delivered"] is True
    for dropped in (system_id, direct_id):
        assert "dropped" in env["control"].post("/_fake/emit", json={"id": dropped}).json()

    rows = stored(env["conn"])
    assert [(r["message_id"], r["via"]) for r in rows] == [(text_id, "webhook")]
    kinds = {r["kind"] for r in env["conn"].execute("SELECT kind FROM account_events")}
    assert "malformed_payload" not in kinds


def test_backfill_collects_every_chat_through_the_bridge(env):
    client, conn = env["client"], env["conn"]
    worker = Worker(env["cfg"], conn, client, rng=random.Random(0), sleep=lambda s: None)
    worker.run(until_idle=True, max_ticks=500)

    for chat in client.list_chats():
        served = client.fetch_history(chat["chat_id"], None, 500)
        assert served.dropped == 0, chat["chat_id"]           # nothing the collector rejects
        ids = {m.message_id for m in served.messages}
        assert {r["message_id"] for r in stored(conn, chat_id=chat["chat_id"])} == ids
    # The message first stored via webhook was deduped, not stored twice.
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == \
        conn.execute("SELECT COUNT(DISTINCT chat_id || message_id) FROM messages").fetchone()[0]
    assert not stored(conn, chat_type="direct")
    by_chat = dict(conn.execute("SELECT chat_id, COUNT(*) FROM messages GROUP BY chat_id").fetchall())
    assert sum(by_chat.values()) > 0
    if FIXTURES.name == "handwritten":
        # 40 posts + 1 same-second burst; 8 content messages among the group's system noise
        assert by_chat == {"120363000000000001@newsletter": 41, "120363111111111111@g.us": 8,
                           "120363222222222222@g.us": 5}
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM account_events")}
    assert not kinds & {"malformed_payload", "bad_request", "target_skipped", "future_timestamp"}


def test_direct_chat_history_is_a_loop_level_bad_request_never_a_skip(env, fixture_data):
    direct = next(c for c in fixture_data["messages"] if c.endswith(("@c.us", "@lid")))
    with pytest.raises(AccountError) as exc:
        env["client"].fetch_history(direct, None, 10)
    assert exc.value.status_code == 400 and exc.value.kind == "bad_request"


def test_unpaired_session_pauses_the_loop_and_is_reported_by_webhook(env):
    control, client, conn = env["control"], env["client"], env["conn"]
    control.post("/_fake/state", json={"state": "UNPAIRED", "ready": False, "paired": False})
    try:
        with pytest.raises(AccountError) as exc:
            client.health()
        assert exc.value.kind in ("auth_failure", "bridge_unhealthy") and exc.value.status_code == 401
        with pytest.raises(AccountError) as exc:
            client.list_chats()
        assert exc.value.kind == "auth_failure"
        wait_until(lambda: conn.execute(
            "SELECT 1 FROM account_events WHERE kind = 'state' AND detail = 'UNPAIRED'").fetchone())
    finally:
        control.post("/_fake/state", json={"state": "CONNECTED", "ready": True, "paired": True})
    assert client.health()["ok"] is True


def test_wrong_token_is_auth_failure(env):
    bad = BridgeClient(env["cfg"].bridge_url, token="wrong")
    try:
        with pytest.raises(AccountError) as exc:
            bad.list_chats()
        assert exc.value.kind == "auth_failure"
    finally:
        bad.close()


def test_lost_page_pauses_the_whole_loop_and_recovers(env):
    """Live 2026-10-19: the page detached while /health said ok, so every read was a per-chat
    503. Now /health probes the page: 503 -> bridge_unhealthy -> loop pause, targets untouched;
    the bridge re-initializes the client by itself and the loop resumes."""
    control, client, conn = env["control"], env["client"], env["conn"]
    fails_before = dict(conn.execute("SELECT chat_id, fail_count FROM targets").fetchall())
    control.post("/_fake/break", json={})

    with pytest.raises(AccountError) as exc:
        client.health()
    assert exc.value.status_code == 503 and exc.value.kind == "bridge_unhealthy"

    worker = Worker(env["cfg"], conn, client, rng=random.Random(0), sleep=lambda s: None)
    assert worker.tick().status == "paused"
    assert dict(conn.execute("SELECT chat_id, fail_count FROM targets").fetchall()) == fails_before

    def healthy():
        try:
            return client.health()["ok"] is True
        except AccountError:                # still recovering
            return False

    wait_until(healthy, timeout=10)
    states = [r[0] for r in conn.execute(
        "SELECT detail FROM account_events WHERE kind = 'state' ORDER BY id")]
    assert "PAGE_DETACHED" in states and states[-1] == "CONNECTED"
