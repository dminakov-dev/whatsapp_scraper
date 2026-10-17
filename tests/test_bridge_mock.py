import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge_mock.app import create_app
from bridge_mock.settings import Faults, MockSettings
from bridge_mock.world import World
from collector.models import normalize

NOW = 1_760_700_000
CHANNEL = "120363000000000001@newsletter"
GROUP = "120363111111111111@g.us"
DEAD = "120363999999999999@g.us"


def make_client(window=100, history=150, **faults):
    settings = MockSettings(window=window, history=history, emit_interval=0,
                            faults=Faults(**faults))
    world = World(seed=1, window=window, history=history, now=NOW)
    return TestClient(create_app(settings, world))


def test_health_and_chat_list():
    client = make_client()
    assert client.get("/health").json() == {"ok": True, "state": "CONNECTED"}
    chats = client.get("/chats").json()["chats"]
    assert {c["chat_id"] for c in chats} >= {CHANNEL, GROUP, DEAD, "380500000001@c.us"}


def test_history_is_a_window_of_the_last_n_messages():
    client = make_client(window=100, history=150)
    body = client.get(f"/chats/{GROUP}/messages", params={"since": 0, "limit": 500}).json()
    assert len(body["messages"]) == 100
    assert body["oldest_available"] == body["messages"][0]["timestamp"]


def test_since_is_inclusive_and_sorted_and_limited():
    client = make_client()
    all_msgs = client.get(f"/chats/{GROUP}/messages", params={"limit": 500}).json()["messages"]
    pivot = all_msgs[40]["timestamp"]
    page = client.get(f"/chats/{GROUP}/messages", params={"since": pivot, "limit": 10}).json()
    stamps = [m["timestamp"] for m in page["messages"]]
    assert len(stamps) == 10 and stamps == sorted(stamps) and stamps[0] == pivot


def test_history_contains_same_second_bursts():
    world = World(seed=1, now=NOW)
    stamps = [m["timestamp"] for m in world.chats[GROUP].messages]
    assert len(stamps) != len(set(stamps))


def test_every_mock_message_satisfies_the_bridge_contract():
    """If the mock drifts from what normalize() expects, this fails first."""
    world = World(seed=1, now=NOW)
    for chat in world.chats.values():
        for msg in chat.messages:
            assert normalize(msg, via="backfill") is not None, msg


def test_dead_and_unknown_chats_return_404():
    client = make_client()
    assert client.get(f"/chats/{DEAD}/messages").status_code == 404
    assert client.get("/chats/nope@g.us/messages").status_code == 404


@pytest.mark.parametrize("faults, status", [
    ({"fail_5xx": 1.0}, 503),
    ({"fail_409": 1.0}, 409),
    ({"fail_429": 1.0, "retry_after": 7}, 429),
    ({"auth_fail": True}, 401),
])
def test_fault_injection(faults, status):
    client = make_client(**faults)
    resp = client.get(f"/chats/{GROUP}/messages")
    assert resp.status_code == status
    if status == 429:
        assert resp.headers["Retry-After"] == "7"


def test_health_reflects_auth_failure_only():
    assert make_client(fail_5xx=1.0).get("/health").status_code == 200
    assert make_client(auth_fail=True).get("/health").status_code == 401


def test_admin_can_change_faults_at_runtime():
    client = make_client()
    client.post("/_admin/faults", json={"fail_409": 1})
    assert client.get(f"/chats/{GROUP}/messages").status_code == 409
    client.post("/_admin/faults", json={"fail_409": 0})
    assert client.get(f"/chats/{GROUP}/messages").status_code == 200


def run_emitter(app, rounds=30):
    """Drive the webhook emitter against a fake receiver; return the bodies it got."""
    received = []

    def handler(request: httpx.Request):
        received.append(request.content.decode())
        return httpx.Response(200)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(rounds):
                await app.state.emit_once(client)

    asyncio.run(go())
    return received


def test_emitter_sends_new_messages_that_also_appear_in_history():
    client = make_client()
    bodies = run_emitter(client.app, rounds=5)
    events = [json.loads(b) for b in bodies]
    assert events and all(e["event"] == "message" for e in events)
    last = events[-1]["message"]
    history = client.app.state.world.chats[last["chat_id"]].messages
    assert any(m["id"] == last["id"] for m in history)


def test_emitter_duplicates_and_malformed_payloads():
    client = make_client(duplicate=1.0, malformed=1.0)
    bodies = run_emitter(client.app, rounds=10)
    stats = client.app.state.stats
    assert stats["duplicates"] > 0 and stats["malformed"] > 0
    assert len(bodies) == stats["webhook_sent"]


def test_unpaired_account_emits_nothing():
    client = make_client(auth_fail=True)
    assert run_emitter(client.app, rounds=5) == []
