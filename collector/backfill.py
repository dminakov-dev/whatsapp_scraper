"""Paced backfill / gap-fill worker: the pull path, and the only code that moves cursors.

One tick = health check -> (periodic) membership sync -> ONE due target -> one small
batch from (cursor - overlap) -> insert + advance cursor in one transaction -> sleep with jitter.

The cursor follows how far the bridge's page reached (HistoryPage.newest), not only the
messages we accepted, so malformed messages are stepped over instead of re-fetched forever.

Failures by scope (see collector/source.py):
- AccountError  (400/other 4xx, 401, 429, bridge down, /health failing): pause the whole loop;
                targets are not touched
- TargetError   (403/404/410):           skip that target (reversible: python -m collector.targets)
- RetryableError(408, 409, 5xx, timeout): retry that target later with exponential backoff
Any other exception is logged and the loop continues: one bad chat never kills the run.
"""
import argparse
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import db
from .config import STUCK_LIMIT_FACTOR, Config, load_config
from .log import setup_logging
from .source import AccountError, BridgeClient, RetryableError, Source, TargetError
from .timeutil import iso_after, now_iso

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    status: str                 # idle | ok | retry | skipped | paused
    chat_id: str | None = None
    inserted: int = 0
    pause: float = 0.0          # seconds the loop should wait (paused only)
    detail: str = ""


class Worker:
    def __init__(self, cfg: Config, conn: sqlite3.Connection, source: Source, *,
                 rng: random.Random | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], str] = now_iso):
        self.cfg = cfg
        self.conn = conn
        self.source = source
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.clock = clock
        self.account_failures = 0   # consecutive loop-level failures (drives the pause)
        self.ticks = 0

    # --- helpers --------------------------------------------------------------------

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^(attempt-1), capped, + random jitter."""
        delay = min(self.cfg.backoff_max, self.cfg.backoff_base * 2 ** max(0, attempt - 1))
        return delay + self.rng.uniform(0, self.cfg.jitter)

    def _account_pause(self, exc: AccountError) -> TickResult:
        self.account_failures += 1
        own = self.backoff(self.account_failures)
        if exc.retry_after is None:
            pause = min(own, self.cfg.backoff_max)
        else:
            # Honor Retry-After in full, but never wait less than our own backoff: a
            # "Retry-After: 0" answered again and again must not become a busy loop.
            pause = max(exc.retry_after, own)
            if pause > self.cfg.retry_after_max:
                db.log_event(self.conn, "retry_after_capped",
                             f"Retry-After {exc.retry_after:.0f}s capped to "
                             f"{self.cfg.retry_after_max:.0f}s")
                pause = self.cfg.retry_after_max
        # No status_code here: the request event already carries it (avoids double counting).
        db.log_event(self.conn, "cooldown", f"{exc.kind}: {exc.detail}; pausing {pause:.1f}s")
        log.warning("loop-level failure (%s): %s — pausing whole loop for %.1fs",
                    exc.kind, exc.detail, pause)
        return TickResult("paused", pause=pause, detail=exc.kind)

    # --- membership -----------------------------------------------------------------

    def sync(self) -> None:
        """Targets = groups/channels the account is already in. AccountError propagates."""
        try:
            chats = self.source.list_chats()
        except (RetryableError, TargetError) as exc:
            log.warning("membership sync failed, keeping existing targets: %s", exc.detail)
            return
        added = db.sync_targets(self.conn, chats)
        if added:
            log.info("membership sync: %d new target(s)", added)

    # --- one tick -------------------------------------------------------------------

    def tick(self) -> TickResult:
        try:
            self.source.health()
            if self.ticks % self.cfg.sync_every == 0:
                self.sync()
        except AccountError as exc:
            return self._account_pause(exc)
        finally:
            self.ticks += 1

        target = db.pick_due_target(self.conn, self.clock())
        if target is None:
            self.account_failures = 0
            return TickResult("idle")
        return self.backfill_target(target)

    def backfill_target(self, target: sqlite3.Row) -> TickResult:
        chat_id, cursor = target["chat_id"], target["cursor_ts"]
        limit = self.cfg.batch_size
        since = iso_after(-self.cfg.backfill_overlap, cursor) if cursor else None
        try:
            page = self.source.fetch_history(chat_id, since, limit)
            if self._no_progress(page, cursor, limit) and since != cursor:
                # The overlap window alone holds a full batch of already-seen messages.
                since = cursor
                page = self.source.fetch_history(chat_id, since, limit)
            if self._no_progress(page, cursor, limit):
                # One second, or a run of unreadable messages, is bigger than a batch.
                limit *= STUCK_LIMIT_FACTOR
                log.info("%s: full batch made no progress past %s, retrying with limit=%d",
                         chat_id, cursor, limit)
                page = self.source.fetch_history(chat_id, since, limit)
        except AccountError as exc:
            return self._account_pause(exc)
        except TargetError as exc:
            db.mark_skipped(self.conn, chat_id, f"{exc.status_code}: {exc.detail}")
            db.log_event(self.conn, "target_skipped", exc.detail, chat_id=chat_id)
            log.warning("%s: permanent error %s — target skipped", chat_id, exc.status_code)
            return TickResult("skipped", chat_id, detail=exc.detail)
        except RetryableError as exc:
            attempt = target["fail_count"] + 1
            delay = self.backoff(attempt)
            db.mark_retry(self.conn, chat_id, f"{exc.status_code}: {exc.detail}",
                          iso_after(delay, self.clock()))
            log.warning("%s: transient error (%s), attempt %d, retry in %.1fs",
                        chat_id, exc.detail, attempt, delay)
            return TickResult("retry", chat_id, detail=exc.detail)

        self.account_failures = 0
        self._check_gap(chat_id, cursor, page.oldest_available)
        note = self._report_dropped(chat_id, page)
        full = page.raw_count >= limit

        if self._progressed(page, cursor):
            new_cursor = page.newest
        elif not full:
            new_cursor = None                      # caught up: the cursor stays where it is
        elif page.newest is None:
            # Even the bigger batch has no readable timestamp to advance to. Re-fetching at
            # once would loop on the same page, so back off and leave the reason visible.
            return self._unreadable_page(target, page, limit)
        else:
            # Even the bigger batch didn't leave this second: step over it and say so.
            new_cursor = iso_after(1, cursor)
            db.log_event(self.conn, "stuck_cursor",
                         f"more than {limit} messages at {cursor}; advanced cursor by 1s",
                         chat_id=chat_id)

        next_run = self.clock() if full else iso_after(self.cfg.target_interval, self.clock())
        rows = [m.as_row() for m in page.messages]
        inserted = db.insert_and_advance(self.conn, chat_id, rows, new_cursor, next_run,
                                         note=note)
        log.info("%s: fetched %d, dropped %d, inserted %d, cursor %s -> %s%s",
                 chat_id, page.raw_count, page.dropped, inserted, cursor, new_cursor or cursor,
                 " (more pending)" if full else "")
        return TickResult("ok", chat_id, inserted=inserted)

    @staticmethod
    def _progressed(page, cursor: str | None) -> bool:
        return page.newest is not None and (cursor is None or page.newest > cursor)

    def _no_progress(self, page, cursor: str | None, limit: int) -> bool:
        return page.raw_count >= limit and not self._progressed(page, cursor)

    def _report_dropped(self, chat_id: str, page) -> str | None:
        """Log dropped messages as events; return a short note for targets.last_error."""
        notes = []
        malformed = page.dropped - page.future
        if malformed:
            notes.append(f"{malformed} malformed message(s) dropped")
            db.log_event(self.conn, "dropped_messages", notes[-1], chat_id=chat_id)
        if page.future:
            notes.append(f"{page.future} message(s) dated after now+"
                         f"{self.cfg.future_tolerance:g}s rejected")
            db.log_event(self.conn, "future_timestamp", notes[-1], chat_id=chat_id)
        return "; ".join(notes) or None

    def _unreadable_page(self, target: sqlite3.Row, page, limit: int) -> TickResult:
        chat_id = target["chat_id"]
        detail = (f"{page.raw_count} message(s) from {target['cursor_ts'] or 'start'} "
                  f"have no readable timestamp (limit={limit})")
        db.log_event(self.conn, "unreadable_page", detail, chat_id=chat_id)
        attempt = target["fail_count"] + 1
        delay = self.backoff(attempt)
        db.mark_retry(self.conn, chat_id, detail, iso_after(delay, self.clock()))
        log.warning("%s: %s; retry in %.1fs", chat_id, detail, delay)
        return TickResult("retry", chat_id, detail=detail)

    def _check_gap(self, chat_id: str, cursor: str | None, oldest: str | None) -> None:
        """Continuity check: if the bridge no longer holds anything as old as our cursor,
        messages between the cursor and the window edge are gone for good."""
        if cursor is not None and oldest is not None and oldest > cursor:
            detail = f"cursor {cursor} is older than oldest available {oldest}"
            db.log_event(self.conn, "possible_gap", detail, chat_id=chat_id)
            log.warning("%s: possible gap — %s", chat_id, detail)

    # --- main loop ------------------------------------------------------------------

    def start(self) -> None:
        """Startup: sync membership, then make every target due (gap-fill)."""
        try:
            self.source.health()
            self.sync()
        except AccountError as exc:
            log.warning("startup: bridge not ready (%s); will keep retrying", exc.detail)
        scheduled = db.make_all_due(self.conn)
        log.info("startup gap-fill: %d target(s) scheduled", scheduled)

    def run(self, *, max_ticks: int | None = None, until_idle: bool = False) -> None:
        self.start()
        done = 0
        while max_ticks is None or done < max_ticks:
            try:
                result = self.tick()
            except Exception as exc:  # last line of defence: log and keep going
                log.exception("tick failed unexpectedly")
                db.log_event(self.conn, "worker_error", repr(exc)[:500])
                result = TickResult("paused", pause=self.backoff(1))
            done += 1
            if until_idle and result.status == "idle":
                break
            if max_ticks is not None and done >= max_ticks:
                break
            wait = result.pause if result.status == "paused" else (
                self.cfg.tick_sleep + self.rng.uniform(0, self.cfg.jitter))
            self.sleep(wait)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paced backfill / gap-fill worker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="run a single tick and exit")
    group.add_argument("--drain", action="store_true",
                       help="run until no target is due, then exit")
    args = parser.parse_args()

    cfg = load_config()
    setup_logging("backfill", cfg.log_dir, cfg.log_level)
    conn = db.connect(cfg.db_path)
    source = BridgeClient(cfg.bridge_url, token=cfg.bridge_token, timeout=cfg.request_timeout,
                          recorder=lambda *a, **k: db.log_event(conn, *a, **k),
                          future_tolerance=cfg.future_tolerance)
    worker = Worker(cfg, conn, source)
    log.info("backfill worker: bridge=%s db=%s", cfg.bridge_url, cfg.db_path)
    try:
        worker.run(max_ticks=1 if args.once else None, until_idle=args.drain)
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        source.close()
        conn.close()


if __name__ == "__main__":
    main()
