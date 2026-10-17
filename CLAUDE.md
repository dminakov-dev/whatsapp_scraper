# WhatsApp Collector — OSINT course homework (Lesson 5)

## Who I am and how to work with me
- I'm Dima, a student in the "AI for OSINT and intelligence" course. This project is my homework, and I have to understand every file I submit.
- Explain *why* behind each design choice, briefly. When you write something non-obvious, say what it does in a sentence or two.
- Talk to me in Russian (Cyrillic only, no Latin transliteration; keep English only for established technical terms). Code, comments, README and commit messages stay in English.
- Don't do big jumps silently: propose a plan for each phase, then implement step by step.

## Environment
- macOS (MacBook), Docker installed, Android phone.
- Run everything natively on macOS during development. Docker only at the end (bonus).
- Python 3.11+ for all collector logic; Node.js only for the thin WhatsApp bridge.

## The assignment (from the lecture slides)
Build a Python project that collects data from one social network (we chose **WhatsApp**).

Mandatory requirements:
1. **Incremental collection** — by cursor / last id, never everything every time.
2. **Canonical model** — `author · url · date · text`, stored in SQLite (plus a JSONL export).
3. **Respect limits** — pacing + backoff on errors/429, no bans.
4. **Resilience and dedup** — a source failure must not crash the run; restart continues from the cursor; no duplicate rows.
5. **Config and run** — `.env` for secrets (outside code), `README.md`, `requirements.txt`.

Also explicitly required: **logging**, and a **re-run continues where it stopped** (idempotent).

Deliverables: Git repo with code + README; sample data (≥ 200 records) in canonical format; a 0.5–1 page report: which source, which limits we hit, how we handled them, **and what exactly we stored**.

Deadline: about one week from 2026-10-16 (≈ 2026-10-23); exact date and submission format "as in previous lessons".

**Decision point:** if by ~2026-10-20 the real WhatsApp bridge is not linked and collecting, switch to the fallback public-source adapter for submission and finish WhatsApp afterwards.

Hints from the assignment (follow them):
- Start with the canonical table and the dedup key — everything else rests on it.
- Study limits **empirically**: log response codes and `Retry-After` / `X-RateLimit-*` headers instead of guessing (the FloodWait lesson). For WhatsApp there is no HTTP 429 from the platform itself, so log every bridge response code, latency, disconnect / auth-failure reasons and state changes to `account_events` — that log is the raw material for the report.
- One run = a small batch; let the cursor move gradually.
- The assignment recommends a source with official/public access. WhatsApp is the hard option on purpose (learning). **Keep the source pluggable**: all source-specific code lives behind an adapter (mock / whatsapp), so if the spare number is delayed past the deadline we can add a public-source adapter (e.g. public Telegram channel or RSS) without touching db / dedup / cursor / backfill logic.

Grading: incrementality & dedup 25% · limits & errors 25% · canonical model 20% · code cleanliness / README / config 20% · limitations report 10%.

Bonuses (only after the core is done): multiple-account rotation, proxy egress, stats dashboard, history backfill, Docker / docker-compose.

## Architecture
Mirrors the lecturer's setup: a thin bridge emits events, Python owns all logic.

```
Android phone (spare Talkman number, WhatsApp primary device)
   │ linked device (QR / pairing code)
   ▼
bridge/ (Node.js, whatsapp-web.js, headless Chrome, LocalAuth)
   │  POST each incoming message → http://localhost:8000/webhook
   │  exposes GET endpoints for chats / history (used by backfill)
   ▼
collector/ (Python, FastAPI receiver + backfill worker) ──► data/collector.db (SQLite)
```

- **Live path:** bridge pushes `message` events to the Python webhook (passive, no polling).
- **Backfill path:** Python worker asks the bridge for history, one chat per tick, with pacing.
- **Gap-fill on startup:** webhooks sent while the receiver was down are lost, so on startup the backfill worker runs each chat forward from its cursor.

### Phases
1. **Phase 1 — no phone number yet (current).** Build all Python logic against a **fake bridge** (`bridge_mock/`) that serves fake chats/messages and POSTs fake events in the *same payload shape* we expect from the real bridge. Include fault injection (random 5xx, 409, timeouts, duplicate events, malformed payloads) so resilience and dedup can actually be tested.
2. **Phase 2 — real bridge.** Thin Node.js bridge on whatsapp-web.js. Before writing it, check the current whatsapp-web.js docs/API (it changes with WhatsApp Web updates). Swap the mock for the real bridge by config only; Python code must not change.
3. **Phase 3 — deliverables.** Collect ≥ 200 records (prefer public channels), export JSONL, write README and the report.
4. **Phase 4 — bonuses** (optional): Docker Compose, small stats dashboard, account-health events.

## Canonical data model (SQLite)
`messages`
- `id` INTEGER PK
- `source` TEXT = 'whatsapp'
- `chat_id` TEXT (JID: `...@g.us` group, `...@newsletter` channel, `...@c.us` person)
- `chat_type` TEXT: group | channel | direct
- `message_id` TEXT (WhatsApp message id)
- `author` TEXT (sender JID; may be an opaque `@lid` id in groups — store as is)
- `url` TEXT (see URL scheme below)
- `date` TEXT ISO-8601 UTC
- `text` TEXT
- `media_type` TEXT NULL
- `collected_at` TEXT ISO-8601 UTC
- `via` TEXT: webhook | backfill
- **UNIQUE(chat_id, message_id)** + `INSERT OR IGNORE` → idempotent.

Schema is created/updated idempotently at startup (`ensure_*` functions with `CREATE TABLE IF NOT EXISTS` etc.), as in the lecturer's project — no manual migration step.

Bonus idea: an FTS5 full-text index over `text` (the lecturer's `results.*` tables are full-text indexed).

`targets` (the frontier / state)
- **Synced from membership**, as in the lecture: the bridge lists the groups/channels the number is already in, and each one becomes a target automatically (direct chats excluded). Nothing is ever joined from code. Keep an `enabled` flag so a synced chat can be switched off.
- `chat_id` PK, `chat_type`, `name`, `enabled`
- `cursor_ts` (timestamp of last stored message), `backfilled` (bool), `backfill_count`
- `next_run_at`, `fail_count`, `last_error`

`account_events` (optional, for health / bonus): `ts`, `kind` (disconnected, auth_failure, rate_limited, bridge_error…), `detail`.

## Bridge error contract (the real Node bridge in Phase 2 MUST answer this way)
The collector classifies errors only by HTTP status (`collector/source.py: classify()`), so the bridge must map WhatsApp failures onto these codes:

| Code | Meaning on the bridge side | Collector scope → action |
|---|---|---|
| 400 | our request is malformed (bad `since`/`limit`, bad path) — a bug on **our** side | loop-level: pause whole loop with backoff, `bad_request` event, targets untouched |
| 401 | WhatsApp session logged out / unpaired, or wrong bridge API key | loop-level: pause, `auth_failure` event |
| 403 / 404 / 410 | chat not accessible / no longer a member / chat gone | target-level: skip (reversible via `python -m collector.targets reenable`). **Only these three codes skip a chat.** |
| 408 / 409 | request timed out / client busy (WhatsApp Web loading/reconnecting) | target-level: retry later with backoff |
| 429 | rate limited | loop-level: pause, honor `Retry-After` (seconds or HTTP date) in full — never shorter than our own backoff, capped only at `RETRY_AFTER_MAX` (1 h, cap logged as `retry_after_capped`); never a skip |
| 5xx / timeout | bridge or WhatsApp Web failure | target-level: retry with exponential backoff + jitter |
| any other 4xx (405, 413, 422, …) | not part of the contract → treated as our bug | loop-level: like 400, `bad_request` |
| unreachable | bridge process down | loop-level: pause, `disconnected` event |

`GET /health` must return 200 `{"ok": true, "state": "CONNECTED"}` only when the session is usable; otherwise 401 (or 200 with `ok: false` and the whatsapp-web.js state). Any other failure of `/health` (5xx, 409, timeout, non-JSON) is loop-level: pause with growing backoff, `bridge_unhealthy` event.

### History endpoint contract
`GET /chats/{chat_id}/messages?since=<unix seconds>&limit=<1..500>`

- **Order and window:** returns messages **oldest → newest, strictly from `since`** (`timestamp >= since`, inclusive). The bridge fetches a window of the last M messages (`fetchMessages({limit: M})`, M ≥ limit), sorts it ascending, filters `>= since`, and returns the **oldest** `limit` of the rest. Never the newest `limit`: `fetchMessages` returns the *latest* N, and returning those would make the collector's cursor jump over the middle silently.
- **`oldest_available`:** the response always includes `oldest_available` (Unix seconds of the oldest message in the window, or null), so the collector can detect when its cursor fell out of the window (`possible_gap` event).
- **`limit`** is at most 500 (`BRIDGE_MAX_LIMIT` in `collector/config.py`); `BATCH_SIZE × 4` (the stuck-cursor retry) must fit, so `BATCH_SIZE ≤ 125`.
- **`message_id` is `id._serialized`** in **both** the webhook event and the history response. It is the dedup key together with `chat_id`; if the two paths used different id forms (`id.id` vs `_serialized`), every message would be stored twice.
- **`timestamp`** is Unix seconds as WhatsApp reports it. The collector rejects dates later than now + `FUTURE_TOLERANCE` (event `future_timestamp`) and never lets them move the cursor.
- **Phase 2 must add a contract test with recorded real responses** (fixtures captured from the real bridge: webhook event, history page, `/chats`, `/health` in each state), checked against `collector/models.py` normalization and the order/`since` rules above.

**URL scheme:** WhatsApp group messages have no public permalink. Channel posts may (`https://whatsapp.com/channel/<invite_code>/<server_id>`) when the data is available. Otherwise use a synthetic stable id: `wa://<chat_id>/<message_id>`. Document this in the report.

## Rules (non-negotiable)
- **Read-only.** Never send messages, react, mark as read, or join groups/channels via code. Joining is done manually on the phone.
- **Skip direct (1:1) chats by default** — collect only groups/channels that are explicit targets. Collect the minimum needed.
- **Advance a cursor only after the DB commit succeeds.**
- **Pacing:** backfill one chat per tick, sleep with jitter between requests.
- **Error handling (refined from the lecture):** 403/404/410 → skip the target (reversible, reason kept in `last_error`); 408 / 409 / 5xx / timeout → exponential backoff with jitter, retry later; 400 and any other unexpected 4xx / 401 / 429 / bridge down / failing `/health` → pause the whole loop, never touch targets. See "Bridge error contract". One bad chat or a malformed event must never crash the process.
- **Secrets:** `.env` only, with `.env.example` committed. Never commit `.env`, `.wwebjs_auth/`, `.wwebjs_cache/`, `data/*.db`, or raw exports with private group content. Add these to `.gitignore` from the very first commit.
- Sample data for submission: prefer **public channels**; review it for private persons' data before committing.

## Known WhatsApp limitations (for the report)
- **E2E boundary:** groups — only messages received *after* the number joined; channels (not E2E) — history before subscribing is available (~last 100 posts).
- `fetchMessages` for groups is unreliable in whatsapp-web.js (often returns only what's loaded in the web client).
- Group authors may be `@lid` ids instead of phone numbers.
- The library sits on WhatsApp Web internals → can break after WhatsApp updates.
- Chromium can't do authenticated SOCKS5 → a proxy needs a local relay (e.g. gost), as in the lecture.
- If the primary phone stays offline for ~2 weeks, linked devices get logged out.
- WhatsApp forbids unofficial clients → the number can be banned; that's why we use a spare number.

## Suggested layout
```
bridge/            # Phase 2: Node.js, whatsapp-web.js
bridge_mock/       # Phase 1: fake bridge (Python) with fault injection
collector/
  config.py        # loads .env
  db.py            # schema, inserts, cursors
  models.py        # canonical record + normalization from bridge payloads
  receiver.py      # FastAPI webhook
  backfill.py      # paced backfill / gap-fill worker
  export.py        # SQLite → JSONL
tests/             # pytest: dedup, cursor, normalization, error handling
data/              # gitignored
.env.example  requirements.txt  README.md  REPORT.md  CLAUDE.md
```

## Volume reality check
WhatsApp is low-volume: the lecturer's production WhatsApp collector had only 328 rows (2 accounts, 5 targets), vs 5.1M for Telegram. To reach ≥ 200 records quickly, subscribe to 3+ active public channels — channel backfill gives roughly the last ~100 posts each. Group messages only accumulate after joining.

## Lecture reference (code landmarks in the lecturer's repo, for comparison)
- `crawl/functions/whatsapp_backfill.py` → `backfill_next_whatsapp_target()` — one chat per run, channel vs group branch, 4xx skip / 409+5xx retry.
- `auth/whatsapp_bridge_client.py` — Python client for the Node bridge (our `collector/` talks to `bridge/` the same way).
- `database/result_rows.py` — canonical rows; `database/functions.py` — `ensure_*` schema functions.
- The lecturer's bridge is OpenWA, so its event is named `message.received`; whatsapp-web.js itself emits `message`. Our bridge will forward with its own stable event name.

## Status
- Spare number: **Talkman SIM bought 2026-10-16, not activated yet.** Plan: second WhatsApp account on the Android phone, two-step verification PIN on, neutral profile.
- 2026-10-17: **Phase 1 done** — collector (db, models, source adapter, receiver, backfill, export), bridge_mock with fault injection, 113 tests, README draft. Bridge contract lives in `collector/models.py`.
- 2026-10-17: review fixes — cursor follows the raw page (malformed messages are stepped over), future dates rejected, history re-read with `BACKFILL_OVERLAP`, stricter 4xx classification, full `Retry-After`, loop-level `/health` failures, `BATCH_SIZE ≤ 125`; history endpoint contract added above.
- Next step: Phase 2 (real Node bridge) once the number is activated; decision point 2026-10-20.

## Lessons learned
(Add short entries here whenever we hit something non-obvious, so we don't step on the same rake twice.)
- 2026-10-17 — `INSERT OR IGNORE` ignores *every* constraint (NOT NULL, CHECK), not only UNIQUE: a row with a NULL field silently disappears. We use `INSERT ... ON CONFLICT(chat_id, message_id) DO NOTHING` instead — same dedup, but other violations raise and roll back the transaction.
- 2026-10-17 — The docs folder is named `docs ` (trailing space). `.gitignore` lists both `docs /` and `docs/`.
- 2026-10-17 — Derive the cursor from what the source *returned*, not only from what we *accepted*. Otherwise a full page of malformed messages freezes the cursor and the same page is re-fetched forever. Messages with no readable timestamp can't give a position at all: escalate the batch once, then back off with a visible `last_error`.
- 2026-10-17 — A monotonic cursor (`MAX`) makes any future-dated value permanent: one bad date and every later message falls behind the cursor. Validate dates (≤ now + tolerance) *before* they reach the cursor.
- 2026-10-17 — Message timestamps are send time, not arrival order: a message sent offline arrives later with an older timestamp. A timestamp cursor needs an overlap (`since = cursor − overlap`); dedup absorbs the repeats. If the overlap window alone holds a full batch, retry once from the exact cursor.
