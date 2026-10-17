"""Export stored messages to JSONL (one canonical record per line).

Usage:
    python -m collector.export                              # all chats -> exports/messages.jsonl
    python -m collector.export --chat-type channel --out sample/messages.jsonl
    python -m collector.export --since 2026-10-01T00:00:00Z --limit 500

Exports go to exports/ by default, which is gitignored: group content can contain
private persons' data. Review a file before committing it as sample data.
"""
import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path

from . import db
from .config import load_config
from .log import setup_logging

log = logging.getLogger(__name__)

# Canonical core first (author · url · date · text), then context and provenance.
EXPORT_FIELDS = ("author", "url", "date", "text", "source", "chat_id", "chat_type",
                 "message_id", "media_type", "collected_at", "via")


def export_jsonl(conn: sqlite3.Connection, out_path: str, *, chat_type: str | None = None,
                 since: str | None = None, limit: int | None = None) -> int:
    """Write matching messages, oldest first, atomically. Returns the number of records."""
    where, params = [], []
    if chat_type:
        where.append("chat_type = ?")
        params.append(chat_type)
    if since:
        where.append("date >= ?")
        params.append(since)
    sql = f"SELECT {', '.join(EXPORT_FIELDS)} FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date, id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as f:
        for row in conn.execute(sql, params):
            f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            count += 1
    os.replace(tmp, out)  # atomic: readers never see a half-written file
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Export messages from SQLite to JSONL")
    parser.add_argument("--out", default="exports/messages.jsonl")
    parser.add_argument("--chat-type", choices=("group", "channel"))
    parser.add_argument("--since", help="ISO-8601 UTC, e.g. 2026-10-01T00:00:00Z")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    cfg = load_config()
    setup_logging("export", cfg.log_dir, cfg.log_level)
    conn = db.connect(cfg.db_path)
    try:
        n = export_jsonl(conn, args.out, chat_type=args.chat_type, since=args.since,
                         limit=args.limit)
    finally:
        conn.close()
    log.info("exported %d record(s) to %s", n, args.out)


if __name__ == "__main__":
    main()
