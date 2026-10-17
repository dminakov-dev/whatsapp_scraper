# WhatsApp Collector

An incremental, rate-limit-aware, deduplicating collector for WhatsApp groups and channels.
Homework for Lesson 5 of the "AI for OSINT and intelligence" course.

The collector stores every message in one canonical model (`author · url · date · text`)
in SQLite and exports it to JSONL. It is built to be **resilient**: a failing chat, a
malformed event, a rate limit or a bridge outage never crashes the run, and a restart
continues from where it stopped without creating duplicates.

> **Status:** Phase 1. All Python logic runs against `bridge_mock/`, a fake bridge with
> fault injection. The real WhatsApp bridge (Node.js, whatsapp-web.js) comes in Phase 2
> and is swapped in by changing `BRIDGE_URL` only.

## Architecture

```
WhatsApp account (spare number, linked device)
   │
   ▼
bridge (Phase 1: bridge_mock/, Phase 2: Node.js whatsapp-web.js)
   │  live:  POST /webhook  ──────────────►  collector/receiver.py ─┐
   │  pull:  GET /health, /chats,                                   ├─► data/collector.db
   │         /chats/{id}/messages  ◄───────  collector/backfill.py ─┘      (SQLite, WAL)
   ▼                                                                         │
                                                         collector/export.py ─► JSONL
```

Two paths write to the same database:

- **Live path (`receiver.py`).** The bridge pushes each new message. The receiver normalizes it and
  inserts it. It never touches cursors.
- **Pull path (`backfill.py`).** A paced worker asks the bridge for history, one chat per tick, from
  that chat's cursor. It is the **only** code that advances cursors. On startup, every chat is
  scheduled again (gap-fill), so messages pushed while the receiver was down are recovered.

The bridge contract (endpoints and payload shape) is documented at the top of
[`collector/models.py`](collector/models.py). All source-specific code sits behind the adapter in
[`collector/source.py`](collector/source.py). A different source would be another adapter, and the
storage, dedup, cursor and backfill code would stay the same.

## Quick start

Requirements: Python 3.11+ (developed on 3.12), macOS or Linux.

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements.txt
# or: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Run the three processes, each in its own terminal:

```bash
.venv/bin/python -m bridge_mock          # fake bridge on 127.0.0.1:8001
.venv/bin/python -m collector.receiver   # webhook receiver on 127.0.0.1:8000
.venv/bin/python -m collector.backfill   # paced backfill / gap-fill worker
```

Worker options:

| Command | What it does |
|---|---|
| `python -m collector.backfill` | runs forever: one chat per tick, with pauses |
| `python -m collector.backfill --once` | runs a single tick (one small batch) and exits |
| `python -m collector.backfill --drain` | runs until no chat is due, then exits |

Export:

```bash
.venv/bin/python -m collector.export                                   # -> exports/messages.jsonl
.venv/bin/python -m collector.export --chat-type channel --out sample/messages.jsonl
```

Tests:

```bash
.venv/bin/python -m pytest -q
```

## Configuration

All settings come from `.env`. See [`.env.example`](.env.example), which documents every
variable. Real environment variables override `.env`. Key settings:

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_URL` | `http://127.0.0.1:8001` | the only switch between the mock and the real bridge |
| `BRIDGE_TOKEN` | empty | shared secret in the `X-Bridge-Token` header (the receiver rejects other requests) |
| `BATCH_SIZE` | 50 | messages per backfill request ("one run = a small batch") |
| `TICK_SLEEP` / `JITTER` | 5 / 2 s | pause between ticks, plus random jitter |
| `BACKOFF_BASE` / `BACKOFF_MAX` | 1 / 300 s | exponential backoff `base·2^(n-1)`, capped, plus jitter |
| `TARGET_INTERVAL` | 60 s | how long a caught-up chat waits before it is polled again |
| `MOCK_FAIL_*`, `MOCK_SLOW`, `MOCK_DUPLICATE`, `MOCK_MALFORMED`, `MOCK_AUTH_FAIL` | see file | fault injection in the mock |

Every service binds to `127.0.0.1` only. Logs go to stdout and to rotating files in
`logs/` (`receiver.log`, `backfill.log`, `bridge_mock.log`; 5 MB × 5 each).

## Data model

`messages` holds one row per message (the canonical model):

| Field | Notes |
|---|---|
| `author` | sender JID. In groups it may be an opaque `@lid` id and is stored as is. For channel posts it is the channel itself |
| `url` | channel posts: `https://whatsapp.com/channel/<invite_code>/<server_id>` when known. Otherwise the synthetic stable id `wa://<chat_id>/<message_id>`, because groups have no public permalinks |
| `date` | ISO-8601 UTC, second precision (`2026-10-17T10:00:00Z`) |
| `text` | message text or media caption |
| `source`, `chat_id`, `chat_type`, `message_id`, `media_type`, `collected_at`, `via` | context and provenance (`via` = `webhook` or `backfill`) |

- **Dedup key:** `UNIQUE(chat_id, message_id)` with `INSERT ... ON CONFLICT DO NOTHING`. Inserting
  the same message again does nothing. Unlike `INSERT OR IGNORE`, this clause ignores only the dedup
  conflict: a `NOT NULL` or `CHECK` violation still raises, so a bug can't silently drop rows.
- **`targets`** is the frontier. It is synced from the chats the account is already a member of.
  Direct (1:1) chats are never targets, and nothing is ever joined from code. Each target keeps
  `cursor_ts`, `next_run_at`, `fail_count`, `last_error` and an `enabled` flag.
- **`account_events`** stores every bridge response (status code, latency, `Retry-After` /
  `X-RateLimit-*`) and every state change, pause, skip and possible gap. This table is the raw
  material for the limits report.

The schema is created idempotently at startup, so there is no migration step.

## Incrementality and cursors

- The cursor is the timestamp of the newest stored message in a chat. History requests are
  **inclusive** (`date >= cursor`), so messages that share the boundary second are never lost.
  The overlap is absorbed by the dedup key.
- The cursor advances **in the same transaction** as the insert. If the insert fails, both roll
  back and the next run re-fetches the same range. The cursor never moves backwards.
- **Continuity check:** the bridge reports `oldest_available` (history is a window of the latest
  messages). If that is newer than our cursor, messages in between are gone, and a `possible_gap`
  event is recorded.
- **Stuck cursor:** if more than a full batch shares one second, the worker retries once with a
  bigger batch. If that still fits in the same second, it steps over that second and records
  `stuck_cursor`.

## Limits and errors

Errors are classified by **scope**, as in the lecture's account-health logic:

| Signal | Scope | Action |
|---|---|---|
| 404 / 410 / 403 / 400 on a chat | chat | target marked `skipped` permanently |
| 409, 5xx, read timeout, invalid body | chat | retry that chat later, exponential backoff + jitter |
| 429 | account | pause the **whole** loop for `Retry-After` (or backoff); never a skip |
| 401, bridge unreachable, `/health` not OK | account | pause the whole loop; per-chat state untouched |
| webhook: malformed payload | — | `200` (retrying garbage is pointless), logged |
| webhook: database error | — | `500`, so the bridge may retry (safe thanks to dedup) |

`/health` is checked before every tick. Any unexpected exception is logged as
`worker_error`, and the loop keeps going.

Useful query for the report:

```sql
SELECT kind, status_code, COUNT(*) AS n, CAST(AVG(latency_ms) AS INT) AS avg_ms
FROM account_events GROUP BY kind, status_code ORDER BY n DESC;
```

## Project layout

```
collector/
  config.py     settings from .env (frozen dataclass, validated)
  log.py        rotating file + stdout logging
  timeutil.py   one ISO-8601 UTC format for every timestamp
  db.py         schema, dedup inserts, cursors, targets, account events
  models.py     bridge contract, canonical record, normalization
  source.py     source adapter: bridge client + scoped error types
  receiver.py   FastAPI webhook (live path)
  backfill.py   paced backfill / gap-fill worker (pull path)
  export.py     SQLite -> JSONL
bridge_mock/    fake bridge: same contract, history window, fault injection
tests/          pytest
```

## Rules this project follows

- **Read-only.** It never sends, reacts, marks as read or joins anything.
- It collects groups and channels only. **Direct chats are never stored.**
- Secrets live only in `.env`. `.env`, the WhatsApp session folders, `data/`, `logs/` and
  `exports/` are gitignored.
- Sample data for submission comes from public channels and is reviewed for private persons'
  data before committing.

## Known limitations

See `REPORT.md` (Phase 3) for the full list, with what we actually observed. In short:

- Groups are end-to-end encrypted, so only messages received after the number joined are
  available. Channel history before subscribing is limited to the most recent posts.
- `whatsapp-web.js` depends on WhatsApp Web internals and can break after WhatsApp updates.
- Unofficial clients are against WhatsApp's terms, so the number can be banned. That is why a
  spare number is used.
