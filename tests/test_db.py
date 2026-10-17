import sqlite3

import pytest

from collector import db
from tests.conftest import make_row

GROUP = {"chat_id": "123@g.us", "chat_type": "group", "name": "Test group"}


def cursor_of(conn, chat_id="123@g.us"):
    return db.get_target(conn, chat_id)["cursor_ts"]


def test_schema_is_idempotent(conn):
    db.ensure_schema(conn)
    db.ensure_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"messages", "targets", "account_events"} <= tables


def test_same_message_twice_is_stored_once(conn):
    assert db.insert_messages(conn, [make_row()]) == 1
    assert db.insert_messages(conn, [make_row(), make_row()]) == 0
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_same_message_id_in_another_chat_is_a_different_message(conn):
    db.insert_messages(conn, [make_row(chat_id="a@g.us"), make_row(chat_id="b@g.us")])
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_invalid_row_raises_instead_of_being_silently_ignored(conn):
    # INSERT OR IGNORE would swallow this NOT NULL violation; ON CONFLICT DO NOTHING must not.
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_messages(conn, [make_row(url=None)])


def test_webhook_insert_does_not_move_cursor(conn):
    db.sync_targets(conn, [GROUP])
    db.insert_messages(conn, [make_row(via="webhook")])
    assert cursor_of(conn) is None


def test_insert_and_advance_moves_cursor(conn):
    db.sync_targets(conn, [GROUP])
    rows = [make_row(message_id="m1"), make_row(message_id="m2", date="2026-10-17T10:05:00Z")]
    inserted = db.insert_and_advance(conn, "123@g.us", rows, "2026-10-17T10:05:00Z", None)
    target = db.get_target(conn, "123@g.us")
    assert inserted == 2
    assert target["cursor_ts"] == "2026-10-17T10:05:00Z"
    assert target["backfill_count"] == 2 and target["backfilled"] == 1


def test_failed_insert_rolls_back_and_cursor_stays(conn):
    db.sync_targets(conn, [GROUP])
    db.insert_and_advance(conn, "123@g.us", [make_row()], "2026-10-17T10:00:00Z", None)

    bad_batch = [make_row(message_id="m2", date="2026-10-17T11:00:00Z"),
                 make_row(message_id="m3", url=None)]  # second row violates NOT NULL
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_and_advance(conn, "123@g.us", bad_batch, "2026-10-17T11:00:00Z", None)

    assert cursor_of(conn) == "2026-10-17T10:00:00Z"  # not advanced
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1  # m2 rolled back


def test_cursor_never_moves_backwards(conn):
    db.sync_targets(conn, [GROUP])
    db.insert_and_advance(conn, "123@g.us", [], "2026-10-17T10:00:00Z", None)
    db.insert_and_advance(conn, "123@g.us", [], "2026-10-17T09:00:00Z", None)
    db.insert_and_advance(conn, "123@g.us", [], None, None)
    assert cursor_of(conn) == "2026-10-17T10:00:00Z"


def test_sync_targets_skips_direct_chats_and_keeps_state(conn):
    chats = [GROUP, {"chat_id": "ch@newsletter", "chat_type": "channel", "name": "News"},
             {"chat_id": "380@c.us", "chat_type": "direct", "name": "Person"}]
    assert db.sync_targets(conn, chats) == 2
    db.insert_and_advance(conn, "123@g.us", [], "2026-10-17T10:00:00Z", None)

    renamed = [{**GROUP, "name": "Renamed"}]
    assert db.sync_targets(conn, renamed) == 0
    target = db.get_target(conn, "123@g.us")
    assert target["name"] == "Renamed" and target["cursor_ts"] == "2026-10-17T10:00:00Z"
    assert db.get_target(conn, "380@c.us") is None


def test_pick_due_target_order_and_exclusions(conn):
    db.sync_targets(conn, [{"chat_id": c, "chat_type": "group", "name": c}
                           for c in ("a@g.us", "b@g.us", "c@g.us", "d@g.us")])
    now = "2026-10-17T12:00:00Z"
    db.mark_retry(conn, "a@g.us", "503", "2026-10-17T11:00:00Z")   # overdue
    db.mark_retry(conn, "b@g.us", "503", "2026-10-17T13:00:00Z")   # not yet due
    db.mark_skipped(conn, "c@g.us", "404")                          # permanently skipped
    # d@g.us never ran (next_run_at NULL) -> goes first
    assert db.pick_due_target(conn, now)["chat_id"] == "d@g.us"
    db.insert_and_advance(conn, "d@g.us", [], None, "2026-10-17T14:00:00Z")
    assert db.pick_due_target(conn, now)["chat_id"] == "a@g.us"


def test_mark_retry_counts_and_success_resets(conn):
    db.sync_targets(conn, [GROUP])
    assert db.mark_retry(conn, "123@g.us", "503", "2026-10-17T12:00:00Z") == 1
    assert db.mark_retry(conn, "123@g.us", "409", "2026-10-17T12:01:00Z") == 2
    db.insert_and_advance(conn, "123@g.us", [], None, None)
    target = db.get_target(conn, "123@g.us")
    assert target["fail_count"] == 0 and target["last_error"] is None


def test_log_event(conn):
    db.log_event(conn, "bridge_error", "HTTP 503", chat_id="123@g.us", status_code=503,
                 latency_ms=120)
    row = conn.execute("SELECT * FROM account_events").fetchone()
    assert (row["kind"], row["status_code"], row["latency_ms"]) == ("bridge_error", 503, 120)
