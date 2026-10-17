"""Export stored messages to JSONL (one canonical record per line).

Usage:
    python -m collector.export                              # all chats -> exports/messages.jsonl
    python -m collector.export --chat-type channel --out sample/messages.jsonl
    python -m collector.export --since 2026-10-01T00:00:00Z --limit 500
    python -m collector.export --no-pseudonymize            # own research: real author ids

Exports go to exports/ by default, which is gitignored: group content can contain
private persons' data. Review a file before committing it as sample data.

By default every person id (group authors, and the participant id inside group message ids
and wa:// urls) is replaced by a stable salted pseudonym (PSEUDONYM_SALT in .env), so a
sample shows who wrote what without revealing phone numbers. Message text is not touched:
mentions inside text still need a manual review.
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
from .privacy import MIN_SALT_LENGTH, pseudonymize_ids

log = logging.getLogger(__name__)

# Canonical core first (author · url · date · text), then context and provenance.
EXPORT_FIELDS = ("author", "url", "date", "text", "source", "chat_id", "chat_type",
                 "message_id", "media_type", "collected_at", "via")
# Fields that can carry a person id (chat_id never does: direct chats are never stored).
PERSON_FIELDS = ("author", "message_id", "url")


def positive_int(value: str) -> int:
    """argparse type: an integer >= 1 (0 or negative would otherwise mean "no limit")."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def export_jsonl(conn: sqlite3.Connection, out_path: str, *, chat_type: str | None = None,
                 since: str | None = None, limit: int | None = None,
                 pseudonym_salt: str | None = None) -> int:
    """Write matching messages, oldest first, atomically. Returns the number of records.

    With pseudonym_salt, person ids in PERSON_FIELDS are replaced by salted pseudonyms;
    without it, records are exported as stored.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    if pseudonym_salt is not None and len(pseudonym_salt) < MIN_SALT_LENGTH:
        raise ValueError(f"pseudonym salt must be at least {MIN_SALT_LENGTH} characters")
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
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as f:
        for row in conn.execute(sql, params):
            record = dict(row)
            if pseudonym_salt is not None:
                for field in PERSON_FIELDS:
                    record[field] = pseudonymize_ids(record[field], pseudonym_salt)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    os.replace(tmp, out)  # atomic: readers never see a half-written file
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Export messages from SQLite to JSONL")
    parser.add_argument("--out", default="exports/messages.jsonl")
    parser.add_argument("--chat-type", choices=("group", "channel"))
    parser.add_argument("--since", help="ISO-8601 UTC, e.g. 2026-10-01T00:00:00Z")
    parser.add_argument("--limit", type=positive_int)
    parser.add_argument("--no-pseudonymize", action="store_true",
                        help="export real person ids (own research only, never for samples)")
    args = parser.parse_args()

    cfg = load_config()
    salt = None
    if not args.no_pseudonymize:
        if len(cfg.pseudonym_salt) < MIN_SALT_LENGTH:
            parser.error(f"pseudonymization needs PSEUDONYM_SALT in .env (at least "
                         f"{MIN_SALT_LENGTH} characters, e.g. `openssl rand -hex 32`), "
                         f"or pass --no-pseudonymize")
        salt = cfg.pseudonym_salt
    setup_logging("export", cfg.log_dir, cfg.log_level)
    conn = db.connect(cfg.db_path)
    try:
        n = export_jsonl(conn, args.out, chat_type=args.chat_type, since=args.since,
                         limit=args.limit, pseudonym_salt=salt)
    finally:
        conn.close()
    log.info("exported %d record(s) to %s (%s)", n, args.out,
             "pseudonymized" if salt else "real person ids")


if __name__ == "__main__":
    main()
