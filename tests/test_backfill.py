import random
import time

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
from collector.source import AccountError, BridgeClient
from collector.timeutil import iso_from_unix, now_iso, parse_iso

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


# --- review fix 1: truncated history is reported, also on the first collection ---------------

def test_first_collection_of_a_truncated_window_is_reported(conn):
    h = Harness(conn, window=100, history=150)       # 50 messages per chat already lost
    h.drain()
    truncated = {e["chat_id"] for e in h.events("history_truncated")}
    assert GROUP in truncated and CHANNEL in truncated
    assert all("oldest available" in e["detail"] for e in h.events("history_truncated"))
    # Still collected everything the window holds, and the cursor moved on.
    assert h.count(GROUP) == 100 and h.target()["cursor_ts"] is not None
    assert not h.events("possible_gap")              # no cursor yet: only the explicit signal


def test_complete_first_collection_records_where_history_starts(conn):
    h = Harness(conn, window=20, history=20)          # nothing lost
    h.drain()
    assert not h.events("history_truncated")
    starts = [e for e in h.events("history_start") if e["chat_id"] == GROUP]
    assert len(starts) == 1 and "history available from" in starts[0]["detail"]


def test_truncation_with_an_existing_cursor_is_reported_too(conn):
    h = Harness(conn, window=20, history=20, batch_size=50)
    h.drain()
    chat = h.world.chats[GROUP]
    for i in range(30):                               # 30 new > window of 20
        h.world.add(chat, h.world.new_message(chat, NOW + 100 + i))
    db.make_all_due(conn)
    h.drain()
    assert [e["chat_id"] for e in h.events("history_truncated")] == [GROUP]
    assert h.events("possible_gap")                   # both facts recorded


def test_truncated_flag_sets_a_note_on_the_target(conn):
    fb = FakeBridge(conn)
    fb.add(NOW)
    fb.extra = {"truncated": True, "oldest_available": NOW}
    fb.worker.start()
    assert fb.worker.tick().status == "ok"
    assert fb.target()["last_error"] == "history incomplete (window truncated)"
    assert fb.events("history_truncated")


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), ("yes", None),
                                             (1, None)])
def test_truncated_field_is_parsed_strictly(conn, value, expected):
    fb = FakeBridge(conn)
    fb.add(NOW)
    fb.extra = {"truncated": value}
    assert fb.source.fetch_history(GROUP, None, 10).truncated is expected


def test_source_without_truncated_field_is_unknown_not_complete(conn):
    fb = FakeBridge(conn)
    fb.add(NOW)
    assert fb.source.fetch_history(GROUP, None, 10).truncated is None


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


# --- coverage gaps: account errors outside the health check -------------------------------

def test_429_during_history_fetch_pauses_loop_and_leaves_target_alone(h):
    h.worker.start()
    h.worker.ticks = 1                     # next tick does not re-sync: the 429 hits the fetch
    h.faults.fail_429, h.faults.retry_after = 1.0, 17
    result = h.worker.tick()
    assert result.status == "paused" and result.pause == 17
    assert all(t["fail_count"] == 0 and t["status"] == "active" and t["cursor_ts"] is None
               for t in db.active_targets(h.conn))
    assert h.conn.execute("SELECT COUNT(*) FROM account_events "
                          "WHERE kind='rate_limited' AND chat_id IS NOT NULL").fetchone()[0] == 1


def test_startup_with_bridge_down_does_not_crash_and_then_pauses(h):
    db.sync_targets(h.conn, [{"chat_id": GROUP, "chat_type": "group", "name": "g"}])
    h.faults.auth_fail = True
    h.worker.start()                       # must not raise
    assert h.worker.tick().status == "paused"
    h.faults.auth_fail = False             # bridge comes back: collection resumes
    assert h.worker.tick().status == "ok"


def test_failed_membership_sync_keeps_existing_targets(h):
    h.worker.start()
    before = [t["chat_id"] for t in db.active_targets(h.conn)]
    h.faults.fail_5xx = 1.0                # /chats answers 503
    h.worker.sync()                        # must not raise
    assert [t["chat_id"] for t in db.active_targets(h.conn)] == before


# --- HTTP 400: our request is malformed -> loop-level, never a skip -------------------------

def test_400_pauses_loop_logs_event_and_leaves_targets_untouched(conn):
    def bridge(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True, "state": "CONNECTED"})
        if request.url.path == "/chats":
            return httpx.Response(200, json={"chats": [{"chat_id": GROUP, "name": "g"}]})
        return httpx.Response(400, json={"error": "invalid 'since' parameter"})

    source = BridgeClient("http://bridge", recorder=lambda *a, **k: db.log_event(conn, *a, **k),
                          client=httpx.Client(transport=httpx.MockTransport(bridge)))
    worker = Worker(Config(jitter=0, backoff_base=1), conn, source, rng=random.Random(0),
                    sleep=lambda s: None)
    worker.start()
    first, second = worker.tick(), worker.tick()

    assert first.status == second.status == "paused" and first.detail == "bad_request"
    assert second.pause > first.pause                          # backoff keeps growing
    target = db.get_target(conn, GROUP)
    assert (target["status"], target["enabled"], target["fail_count"]) == ("active", 1, 0)
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM account_events")]
    assert "bad_request" in kinds and "cooldown" in kinds
    assert "target_skipped" not in kinds


def test_skipped_target_can_be_reenabled_and_resumes_from_cursor(h):
    h.drain()
    assert h.target(DEAD)["status"] == "skipped"
    db.reenable_targets(h.conn, DEAD)
    target = h.target(DEAD)
    assert (target["status"], target["fail_count"], target["next_run_at"]) == ("active", 0, None)
    # the chat is still gone on the bridge, so the next attempt skips it again — visibly
    h.drain()
    assert h.target(DEAD)["status"] == "skipped"
    assert len(h.events("target_skipped")) == 2


# --- review regressions: a contract-following fake bridge -----------------------------------
# Each stored message has a true position in time used for ordering and `since` filtering,
# while its payload `timestamp` may be garbage. This is how a real bridge would see it.

class FakeBridge:
    def __init__(self, conn, *, batch_size=50, overlap=600.0, chat_id=GROUP):
        self.conn, self.chat_id, self.store, self.sinces = conn, chat_id, [], []
        self.health_status = 200
        self.extra = {}                               # extra history-response fields
        self.cfg = Config(batch_size=batch_size, tick_sleep=0, jitter=0, backoff_base=1,
                          backoff_max=300, target_interval=60, backfill_overlap=overlap)
        self.source = BridgeClient("http://bridge", recorder=lambda *a, **k: db.log_event(conn, *a, **k),
                                   client=httpx.Client(transport=httpx.MockTransport(self.handle)))
        self.worker = Worker(self.cfg, conn, self.source, rng=random.Random(0), sleep=lambda s: None)

    def add(self, true_ts, **payload):
        msg = {"id": f"m{len(self.store)}", "chat_id": self.chat_id, "author": "111@lid",
               "timestamp": true_ts, "body": "hi", "type": "chat"}
        msg.update(payload)
        self.store.append((true_ts, msg))
        return msg["id"]

    def handle(self, request):
        if request.url.path == "/health":
            return httpx.Response(self.health_status, json={"ok": True, "state": "CONNECTED"})
        if request.url.path == "/chats":
            return httpx.Response(200, json={"chats": [{"chat_id": self.chat_id, "name": "g"}]})
        since, limit = int(request.url.params["since"]), int(request.url.params["limit"])
        self.sinces.append(since)
        page = [m for ts, m in sorted(self.store, key=lambda p: p[0]) if ts >= since][:limit]
        return httpx.Response(200, json={"messages": page, "oldest_available": None, **self.extra})

    def ids(self):
        return {r[0] for r in self.conn.execute("SELECT message_id FROM messages")}

    def target(self):
        return db.get_target(self.conn, self.chat_id)

    def events(self, kind):
        return self.conn.execute("SELECT * FROM account_events WHERE kind=?", (kind,)).fetchall()


# finding 1: pagination follows the raw page, not only accepted messages

def test_full_page_of_malformed_messages_is_stepped_over_and_reported(conn):
    fb = FakeBridge(conn, batch_size=50)
    for i in range(50):
        fb.add(NOW + i, author=None)                  # malformed but readable timestamps
    good = {fb.add(NOW + 100 + i) for i in range(20)}
    fb.worker.start()
    first = fb.worker.tick()
    assert first.status == "ok" and first.inserted == 0
    t = fb.target()
    assert t["cursor_ts"] == "2025-10-17T11:20:49Z"    # moved past the malformed block
    assert "50 malformed" in t["last_error"]
    assert fb.events("dropped_messages")
    fb.worker.run(until_idle=True, max_ticks=20)
    assert fb.ids() == good
    assert len(fb.sinces) < 10                         # no re-fetching the same page forever


def test_full_page_without_readable_timestamps_escalates_and_gets_past_it(conn):
    fb = FakeBridge(conn, batch_size=50)
    for i in range(50):
        fb.add(NOW + i, timestamp="oops")             # no position can be read from these
    good = {fb.add(NOW + 100 + i) for i in range(70)}
    fb.worker.start()
    assert fb.worker.tick().inserted == 70            # bigger batch reached readable messages
    assert fb.ids() == good


def test_unreadable_page_backs_off_with_visible_error_instead_of_hot_looping(conn):
    fb = FakeBridge(conn, batch_size=50)
    for i in range(250):                              # more than limit * STUCK_LIMIT_FACTOR
        fb.add(NOW + i, timestamp="oops")
    fb.worker.start()
    result = fb.worker.tick()
    t = fb.target()
    assert result.status == "retry"
    assert t["fail_count"] == 1 and "no readable timestamp" in t["last_error"]
    assert t["next_run_at"] > now_iso() and t["cursor_ts"] is None
    assert fb.events("unreadable_page")
    assert db.pick_due_target(conn) is None           # not picked again right away


# finding 2: future-dated messages never move the cursor

def test_future_dated_message_never_moves_the_cursor(conn):
    fb = FakeBridge(conn)
    base = int(time.time()) - 7200
    fb.add(base)
    future_id = fb.add(int(time.time()) + 30 * 86400)
    fb.worker.start()
    fb.worker.tick()
    assert fb.target()["cursor_ts"] == iso_from_unix(base)
    assert future_id not in fb.ids()
    assert fb.events("future_timestamp")

    later = fb.add(base + 100)                        # a normal message arrives afterwards
    db.make_all_due(conn)
    fb.worker.run(until_idle=True, max_ticks=10)
    assert later in fb.ids()
    assert fb.target()["cursor_ts"] == iso_from_unix(base + 100)


# finding 3: overlap picks up late-delivered messages

def test_late_delivered_message_inside_overlap_is_collected(conn):
    fb = FakeBridge(conn, overlap=600)
    for i in range(5):
        fb.add(NOW + i * 60)
    fb.worker.start()
    fb.worker.run(until_idle=True, max_ticks=10)
    cursor = int(parse_iso(fb.target()["cursor_ts"]).timestamp())
    late = fb.add(cursor - 120)                       # sent offline, stamped 2 min before cursor
    db.make_all_due(conn)
    fb.worker.run(until_idle=True, max_ticks=10)
    assert late in fb.ids()
    assert cursor - 600 in fb.sinces                  # the request started at cursor - overlap
    assert fb.target()["cursor_ts"] == iso_from_unix(cursor)   # cursor did not move back


def test_without_overlap_a_late_message_is_missed(conn):
    """Documents why the overlap exists: with 0 the same late message is never fetched."""
    fb = FakeBridge(conn, overlap=0)
    for i in range(5):
        fb.add(NOW + i * 60)
    fb.worker.start()
    fb.worker.run(until_idle=True, max_ticks=10)
    late = fb.add(NOW + 4 * 60 - 120)
    db.make_all_due(conn)
    fb.worker.run(until_idle=True, max_ticks=10)
    assert late not in fb.ids()


def test_overlap_window_denser_than_a_batch_still_makes_progress(conn):
    fb = FakeBridge(conn, batch_size=5, overlap=600)
    ids = {fb.add(NOW + i) for i in range(40)}        # 40 messages inside one overlap window
    fb.worker.start()
    fb.worker.run(until_idle=True, max_ticks=50)
    assert fb.ids() == ids
    assert fb.target()["cursor_ts"] == iso_from_unix(NOW + 39)
    assert len(fb.sinces) < 40


# finding 5: Retry-After honored in full, with a floor and a sanity cap

def pause_worker(conn, **cfg):
    config = Config(jitter=0, backoff_base=1, backoff_max=300, retry_after_max=3600, **cfg)
    return Worker(config, conn, source=None, rng=random.Random(0), sleep=lambda s: None)


def rate_limited(seconds):
    return AccountError("429", 429, kind="rate_limited", retry_after=seconds)


def test_retry_after_longer_than_backoff_max_is_honored(conn):
    assert pause_worker(conn)._account_pause(rate_limited(900)).pause == 900


def test_retry_after_zero_cannot_busy_loop(conn):
    worker = pause_worker(conn)
    pauses = [worker._account_pause(rate_limited(0)).pause for _ in range(3)]
    assert pauses == [1, 2, 4]                        # our own growing backoff is the floor


def test_absurd_retry_after_is_capped_and_logged(conn):
    assert pause_worker(conn)._account_pause(rate_limited(7200)).pause == 3600
    assert conn.execute("SELECT COUNT(*) FROM account_events "
                        "WHERE kind='retry_after_capped'").fetchone()[0] == 1


# finding 7: a failing /health pauses the loop with growing backoff

def test_failing_health_check_pauses_loop_with_growing_backoff(conn):
    fb = FakeBridge(conn)
    fb.add(NOW)
    fb.worker.start()
    fb.health_status = 503
    first, second = fb.worker.tick(), fb.worker.tick()
    assert first.status == second.status == "paused" and first.detail == "bridge_unhealthy"
    assert second.pause > first.pause and fb.worker.account_failures == 2
    assert fb.events("bridge_unhealthy") and not fb.events("worker_error")
    assert fb.target()["fail_count"] == 0 and fb.target()["cursor_ts"] is None
    fb.health_status = 200
    assert fb.worker.tick().status == "ok"
