"""SQLite storage: schema, dedup inserts, targets (the frontier) and account events.

Rules this module enforces:
- Dedup key is UNIQUE(chat_id, message_id). Re-inserting a message is a no-op.
- Only `insert_and_advance` moves a cursor, and it does so in the same transaction as the
  insert: either both are committed or neither is.
- Cursors never move backwards (MAX of old and new).
"""
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .timeutil import now_iso

log = logging.getLogger(__name__)

# Columns a canonical message row must provide (id is autoincrement).
MESSAGE_COLUMNS = (
    "source", "chat_id", "chat_type", "message_id", "author", "url",
    "date", "text", "media_type", "collected_at", "via",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL DEFAULT 'whatsapp',
    chat_id      TEXT NOT NULL,
    chat_type    TEXT NOT NULL CHECK (chat_type IN ('group', 'channel', 'direct')),
    message_id   TEXT NOT NULL,
    author       TEXT,
    url          TEXT NOT NULL,
    date         TEXT NOT NULL,
    text         TEXT,
    media_type   TEXT,
    collected_at TEXT NOT NULL,
    via          TEXT NOT NULL CHECK (via IN ('webhook', 'backfill')),
    UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages (chat_id, date);

CREATE TABLE IF NOT EXISTS targets (
    chat_id        TEXT PRIMARY KEY,
    chat_type      TEXT NOT NULL CHECK (chat_type IN ('group', 'channel')),
    name           TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'skipped')),
    cursor_ts      TEXT,
    backfilled     INTEGER NOT NULL DEFAULT 0,
    backfill_count INTEGER NOT NULL DEFAULT 0,
    next_run_at    TEXT,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_events (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    chat_id     TEXT,
    status_code INTEGER,
    latency_ms  INTEGER,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON account_events (ts);
"""


def connect(db_path: str, *, ensure: bool = True) -> sqlite3.Connection:
    """Open the database (creating the folder if needed) and ensure the schema.

    WAL lets the webhook receiver and the backfill worker (two processes) read and
    write the same file; busy_timeout makes a writer wait instead of failing at once.
    ensure=False skips the schema check for short-lived per-request connections.
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    if ensure:
        ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to run on every startup (no separate migration step)."""
    conn.executescript(SCHEMA)


# --- messages -------------------------------------------------------------------------

_INSERT_SQL = (
    f"INSERT INTO messages ({', '.join(MESSAGE_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in MESSAGE_COLUMNS)}) "
    # Only the dedup key conflict is ignored. Unlike INSERT OR IGNORE, a NOT NULL or
    # CHECK violation still raises, so a normalization bug can't silently drop rows.
    "ON CONFLICT (chat_id, message_id) DO NOTHING"
)


def _insert_rows(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    before = conn.total_changes
    conn.executemany(_INSERT_SQL, ([row[c] for c in MESSAGE_COLUMNS] for row in rows))
    return conn.total_changes - before


def insert_messages(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    """Insert rows without touching any cursor (webhook path). Returns rows actually added."""
    with conn:
        return _insert_rows(conn, rows)


def insert_and_advance(
    conn: sqlite3.Connection,
    chat_id: str,
    rows: list[Mapping[str, Any]],
    new_cursor: str | None,
    next_run_at: str | None,
) -> int:
    """Backfill path: insert rows and advance the target's cursor in ONE transaction.

    If anything fails, the whole transaction rolls back and the cursor stays where it
    was, so the next run re-fetches the same range (dedup absorbs any overlap).
    """
    with conn:
        inserted = _insert_rows(conn, rows)
        conn.execute(
            """
            UPDATE targets SET
                cursor_ts      = CASE WHEN ? IS NULL THEN cursor_ts
                                      ELSE MAX(COALESCE(cursor_ts, ''), ?) END,
                backfilled     = 1,
                backfill_count = backfill_count + ?,
                next_run_at    = ?,
                fail_count     = 0,
                last_error     = NULL,
                updated_at     = ?
            WHERE chat_id = ?
            """,
            (new_cursor, new_cursor, inserted, next_run_at, now_iso(), chat_id),
        )
    return inserted


# --- targets (the frontier) -------------------------------------------------------------

def sync_targets(conn: sqlite3.Connection, chats: Iterable[Mapping[str, Any]]) -> int:
    """Upsert groups/channels the account is a member of. Returns how many are new.

    Direct (1:1) chats are never targets. Existing targets keep their cursor, enabled
    flag and status; only name and type are refreshed.
    """
    now = now_iso()
    with conn:
        before = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
        for chat in chats:
            if chat.get("chat_type") not in ("group", "channel"):
                continue
            conn.execute(
                """
                INSERT INTO targets (chat_id, chat_type, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (chat_id) DO UPDATE SET
                    name = excluded.name, chat_type = excluded.chat_type,
                    updated_at = excluded.updated_at
                """,
                (chat["chat_id"], chat["chat_type"], chat.get("name"), now, now),
            )
        after = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
    return after - before


def pick_due_target(conn: sqlite3.Connection, now: str | None = None) -> sqlite3.Row | None:
    """The one target to work on this tick: never-run first, then the longest overdue."""
    return conn.execute(
        """
        SELECT * FROM targets
        WHERE enabled = 1 AND status = 'active'
          AND (next_run_at IS NULL OR next_run_at <= ?)
        ORDER BY next_run_at ASC NULLS FIRST, chat_id
        LIMIT 1
        """,
        (now or now_iso(),),
    ).fetchone()


def get_target(conn: sqlite3.Connection, chat_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM targets WHERE chat_id = ?", (chat_id,)).fetchone()


def active_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM targets WHERE enabled = 1 AND status = 'active' ORDER BY chat_id"
    ).fetchall()


def list_targets(conn: sqlite3.Connection, *, status: str | None = None) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM targets", ()
    if status:
        sql, params = sql + " WHERE status = ?", (status,)
    return conn.execute(sql + " ORDER BY status DESC, chat_id", params).fetchall()


def reenable_targets(conn: sqlite3.Connection, chat_id: str | None = None) -> int:
    """Undo a skip (or a manual disable): the target becomes active, enabled, due now,
    with a clean failure history. chat_id=None re-enables every skipped/disabled target.
    The cursor is kept, so collection resumes where it stopped. Returns rows changed."""
    sql = """
        UPDATE targets SET status = 'active', enabled = 1, fail_count = 0,
                           last_error = NULL, next_run_at = NULL, updated_at = ?
        WHERE (status = 'skipped' OR enabled = 0)
    """
    params: tuple = (now_iso(),)
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params += (chat_id,)
    with conn:
        return conn.execute(sql, params).rowcount


def disable_target(conn: sqlite3.Connection, chat_id: str) -> bool:
    """Manually switch a synced chat off (enabled = 0). Returns False if it doesn't exist."""
    with conn:
        cur = conn.execute("UPDATE targets SET enabled = 0, updated_at = ? WHERE chat_id = ?",
                           (now_iso(), chat_id))
    return cur.rowcount == 1


def make_all_due(conn: sqlite3.Connection) -> int:
    """Startup gap-fill: every active target becomes due now, then the normal paced loop
    walks them one per tick. Returns how many targets were scheduled."""
    with conn:
        cur = conn.execute(
            "UPDATE targets SET next_run_at = NULL WHERE enabled = 1 AND status = 'active'"
        )
    return cur.rowcount


def mark_retry(conn: sqlite3.Connection, chat_id: str, error: str, next_run_at: str) -> int:
    """Target-level transient failure (409 / 5xx / timeout). Returns the new fail_count."""
    with conn:
        conn.execute(
            """
            UPDATE targets SET fail_count = fail_count + 1, last_error = ?,
                               next_run_at = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (error, next_run_at, now_iso(), chat_id),
        )
    row = get_target(conn, chat_id)
    return row["fail_count"] if row else 0


def mark_skipped(conn: sqlite3.Connection, chat_id: str, error: str) -> None:
    """Target-level permanent failure (404 / 410 / 403 / 400): stop trying this chat."""
    with conn:
        conn.execute(
            "UPDATE targets SET status = 'skipped', last_error = ?, updated_at = ? "
            "WHERE chat_id = ?",
            (error, now_iso(), chat_id),
        )


# --- account events (raw material for the limits report) ------------------------------

def log_event(
    conn: sqlite3.Connection,
    kind: str,
    detail: str | None = None,
    *,
    chat_id: str | None = None,
    status_code: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """Record a bridge/account signal. Never raises: losing one event must not crash a run."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO account_events (ts, kind, chat_id, status_code, latency_ms, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), kind, chat_id, status_code, latency_ms, detail),
            )
    except sqlite3.Error:
        log.exception("failed to record account event %s", kind)
