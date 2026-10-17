import random

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge_mock.app import create_app
from bridge_mock.settings import Faults, MockSettings
from bridge_mock.world import Chat, World
from collector import db
from collector.backfill import Worker
from collector.config import Config
from collector.models import normalize
from collector.source import BridgeClient

NOW = 1_760_700_000
GROUP = "120363111111111111@g.us"
CHANNEL = "120363000000000001@newsletter"
DEAD = "120363999999999999@g.us"


class Harness:
    """Mock bridge app + BridgeClient + DB + Worker, wired together in-process."""

    def __init__(self, conn, *, window=100, history=150, batch_size=50, chats=None):
        self.conn = conn
        self.world = World(seed=1, window=window, history=history, now=NOW, chats=chats)
        self.app = create_app(MockSettings(emit_interval=0, faults=Faults()), self.world)
        self.faults = self.app.state.faults
        self.cfg = Config(batch_size=batch_size, tick_sleep=0, jitter=0, backoff_base=1,
                          backoff_max=300, target_interval=60)
        self.source = BridgeClient("http://testserver", client=TestClient(self.app),
                                   recorder=lambda *a, **k: db.log_event(conn, *a, **k))
        self.sleeps = []
        self.worker = Worker(self.cfg, conn, self.source, rng=random.Random(0),
                             sleep=self.sleeps.append)

    def drain(self):
        self.worker.run(until_idle=True, max_ticks=500)

    def target(self, chat_id=GROUP):
        return db.get_target(self.conn, chat_id)

    def count(self, chat_id=None):
        if chat_id:
            return self.conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?",
                                     (chat_id,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def events(self, kind):
        return self.conn.execute("SELECT * FROM account_events WHERE kind=?", (kind,)).fetchall()


@pytest.fixture
def h(conn):
    return Harness(conn)


# --- basic behaviour ----------------------------------------------------------------------

def test_drain_collects_every_window_message_and_skips_direct_and_dead(h):
    h.drain()
    for chat_id in (GROUP, CHANNEL):
        assert h.count(chat_id) == 100            # the whole window
        assert h.target(chat_id)["cursor_ts"] is not None
    assert h.count("380500000001@c.us") == 0      # direct chat never a target
    assert h.target("380500000001@c.us") is None
    assert h.target(DEAD)["status"] == "skipped"  # 404 -> permanent skip


def test_one_tick_is_one_small_batch(conn):
    h = Harness(conn, batch_size=10)
    h.worker.start()
    result = h.worker.tick()
    assert result.status == "ok" and result.inserted == 10
    assert h.count() == 10


def test_restart_continues_from_cursor_without_duplicates(conn):
    h = Harness(conn)
    h.drain()
    before = h.count()
    cursors = {r["chat_id"]: r["cursor_ts"] for r in db.active_targets(conn)}

    # "Restart": a fresh worker on the same DB and bridge
    again = Worker(h.cfg, conn, h.source, rng=random.Random(1), sleep=lambda s: None)
    again.run(until_idle=True, max_ticks=500)
    assert h.count() == before
    assert {r["chat_id"]: r["cursor_ts"] for r in db.active_targets(conn)} == cursors
    total, distinct = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT chat_id||'|'||message_id) FROM messages").fetchone()
    assert total == distinct


def test_new_messages_are_picked_up_incrementally(h):
    h.drain()
    before = h.count(GROUP)
    chat = h.world.chats[GROUP]
    for _ in range(3):
        h.world.add(chat, h.world.new_message(chat, NOW + 10))
    db.make_all_due(h.conn)
    h.drain()
    assert h.count(GROUP) == before + 3


# --- approved case 1: 429 -----------------------------------------------------------------

def test_429_pauses_whole_loop_for_retry_after_and_never_skips(h):
    h.worker.start()
    h.faults.fail_429, h.faults.retry_after = 1.0, 42
    result = h.worker.tick()
    assert result.status == "paused" and result.pause == 42
    for target in db.active_targets(h.conn):
        assert target["fail_count"] == 0 and target["status"] == "active"
    assert h.events("rate_limited") and h.events("cooldown")
    # once the limit lifts, collection resumes normally
    h.faults.fail_429 = 0
    assert h.worker.tick().status == "ok"


# --- approved case 2: 401 / bridge down -----------------------------------------------------

def test_401_pauses_loop_without_touching_targets(h):
    h.worker.start()
    h.faults.auth_fail = True
    first, second = h.worker.tick(), h.worker.tick()
    assert first.status == second.status == "paused"
    assert second.pause > first.pause                # backoff grows with consecutive failures
    assert h.events("auth_failure") and h.events("cooldown")
    assert all(t["fail_count"] == 0 for t in db.active_targets(h.conn))


def test_unreachable_bridge_pauses_loop(conn):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    source = BridgeClient("http://bridge", client=httpx.Client(transport=httpx.MockTransport(down)),
                          recorder=lambda *a, **k: db.log_event(conn, *a, **k))
    db.sync_targets(conn, [{"chat_id": GROUP, "chat_type": "group", "name": "g"}])
    worker = Worker(Config(jitter=0), conn, source, rng=random.Random(0), sleep=lambda s: None)
    result = worker.tick()
    assert result.status == "paused"
    assert conn.execute("SELECT COUNT(*) FROM account_events WHERE kind='disconnected'").fetchone()[0]
    assert db.get_target(conn, GROUP)["fail_count"] == 0


# --- target-level transient errors ---------------------------------------------------------

@pytest.mark.parametrize("fault", ["fail_5xx", "fail_409"])
def test_transient_errors_retry_target_with_growing_backoff(h, fault):
    h.worker.start()
    setattr(h.faults, fault, 1.0)
    result = h.worker.backfill_target(h.target(GROUP))
    assert result.status == "retry"
    t1 = h.target(GROUP)
    h.worker.backfill_target(t1)
    t2 = h.target(GROUP)
    assert (t1["fail_count"], t2["fail_count"]) == (1, 2)
    assert t2["next_run_at"] > t1["next_run_at"] and t2["status"] == "active"
    assert t2["cursor_ts"] is None                 # nothing advanced on failure


def test_backoff_is_exponential_and_capped(h):
    assert [h.worker.backoff(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 8]
    assert h.worker.backoff(50) == 300


# --- approved case 3: webhook inserts don't move the cursor; backfill fills the gap --------

def test_webhook_rows_leave_cursor_and_backfill_fills_the_rest(h):
    h.worker.start()
    received_live = h.world.chats[GROUP].messages[-5:]       # receiver saw only the last 5
    db.insert_messages(h.conn, [normalize(m, via="webhook").as_row() for m in received_live])
    assert h.target(GROUP)["cursor_ts"] is None

    h.drain()
    assert h.count(GROUP) == 100                              # gap filled, no duplicates
    vias = dict(h.conn.execute(
        "SELECT via, COUNT(*) FROM messages WHERE chat_id=? GROUP BY via", (GROUP,)).fetchall())
    assert vias == {"webhook": 5, "backfill": 95}


# --- approved case 4: same-second messages across a batch boundary --------------------------

def test_same_second_messages_split_across_batches_are_all_stored(conn):
    chat = Chat(GROUP, "Burst group", "group")
    h = Harness(conn, history=0, batch_size=2, chats=[chat])
    for ts in (NOW, NOW + 1, NOW + 1, NOW + 1, NOW + 2):     # 3 messages share NOW+1
        h.world.add(chat, h.world.new_message(chat, ts))
    h.drain()
    assert h.count(GROUP) == 5
    assert h.target(GROUP)["cursor_ts"] == "2025-10-17T11:20:02Z"


def test_stuck_cursor_is_detected_and_logged(conn):
    chat = Chat(GROUP, "Huge burst", "group")
    h = Harness(conn, history=0, batch_size=2, chats=[chat])
    h.world.add(chat, h.world.new_message(chat, NOW))
    for _ in range(10):                                       # 10 messages in one second
        h.world.add(chat, h.world.new_message(chat, NOW + 1))
    h.world.add(chat, h.world.new_message(chat, NOW + 5))
    h.drain()                                                 # must terminate
    assert h.events("stuck_cursor")
    assert h.target(GROUP)["cursor_ts"] == "2025-10-17T11:20:05Z"


# --- approved case 5: possible gap -----------------------------------------------------------

def test_cursor_older_than_window_logs_possible_gap(conn):
    h = Harness(conn, window=20, history=20, batch_size=50)
    h.drain()
    assert not h.events("possible_gap")
    chat = h.world.chats[GROUP]
    for i in range(30):                                       # 30 new > window of 20
        h.world.add(chat, h.world.new_message(chat, NOW + 100 + i))
    db.make_all_due(conn)
    h.drain()
    gaps = h.events("possible_gap")
    assert len(gaps) == 1 and gaps[0]["chat_id"] == GROUP


# --- resilience ------------------------------------------------------------------------------

def test_unexpected_exception_does_not_stop_the_loop(h, monkeypatch):
    calls = {"n": 0}
    real_tick = h.worker.tick

    def flaky_tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_tick()

    monkeypatch.setattr(h.worker, "tick", flaky_tick)
    h.worker.run(max_ticks=3)
    assert calls["n"] == 3 and h.events("worker_error")
    assert h.count() > 0
