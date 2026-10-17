"""Inspect and manage targets (the frontier) from the command line.

    python -m collector.targets list               # all targets
    python -m collector.targets list --skipped     # only skipped ones, with the reason
    python -m collector.targets reenable CHAT_ID   # undo a skip / disable for one chat
    python -m collector.targets reenable --all     # ... for every skipped/disabled chat
    python -m collector.targets disable CHAT_ID    # switch a synced chat off

Skips are never final: the reason (status code + detail) stays in last_error, and
`reenable` puts the chat back in rotation from its existing cursor.
"""
import argparse
import sqlite3
import sys
from typing import TextIO

from . import db
from .config import load_config


def _print_targets(rows: list[sqlite3.Row], out: TextIO) -> None:
    if not rows:
        print("no targets", file=out)
        return
    print(f"{'CHAT_ID':<36} {'TYPE':<8} {'STATUS':<8} {'ON':<3} {'FAILS':>5}  "
          f"{'CURSOR':<20}  {'SINCE':<20}  LAST_ERROR", file=out)
    for r in rows:
        print(f"{r['chat_id']:<36} {r['chat_type']:<8} {r['status']:<8} "
              f"{'yes' if r['enabled'] else 'no':<3} {r['fail_count']:>5}  "
              f"{r['cursor_ts'] or '-':<20}  {r['updated_at']:<20}  {r['last_error'] or ''}",
              file=out)


def run(argv: list[str], conn: sqlite3.Connection, out: TextIO = sys.stdout) -> int:
    """Execute one command; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m collector.targets",
                                     description="Inspect and manage collection targets")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show targets")
    p_list.add_argument("--skipped", action="store_true", help="only skipped targets")

    p_re = sub.add_parser("reenable", help="undo a skip or disable")
    which = p_re.add_mutually_exclusive_group(required=True)
    which.add_argument("chat_id", nargs="?")
    which.add_argument("--all", action="store_true")

    p_dis = sub.add_parser("disable", help="switch a chat off")
    p_dis.add_argument("chat_id")

    args = parser.parse_args(argv)

    if args.command == "list":
        _print_targets(db.list_targets(conn, status="skipped" if args.skipped else None), out)
        return 0

    if args.command == "reenable":
        if args.all:
            n = db.reenable_targets(conn)
            print(f"re-enabled {n} target(s)", file=out)
            if n:
                db.log_event(conn, "target_reenabled", f"manual --all: {n} target(s)")
            return 0
        if db.get_target(conn, args.chat_id) is None:
            print(f"unknown target: {args.chat_id}", file=out)
            return 1
        n = db.reenable_targets(conn, args.chat_id)
        print(f"re-enabled {args.chat_id}" if n else f"{args.chat_id} is already active",
              file=out)
        if n:
            db.log_event(conn, "target_reenabled", "manual", chat_id=args.chat_id)
        return 0

    if args.command == "disable":
        if not db.disable_target(conn, args.chat_id):
            print(f"unknown target: {args.chat_id}", file=out)
            return 1
        print(f"disabled {args.chat_id}", file=out)
        return 0

    return 2  # unreachable: argparse enforces a known command


def main() -> None:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    try:
        code = run(sys.argv[1:], conn)
    finally:
        conn.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
