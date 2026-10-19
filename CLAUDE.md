# WhatsApp Collector — OSINT course homework (Lesson 5)

## Who I am and how to work with me
- I'm Dima, a student in the "AI for OSINT and intelligence" course. This project is my homework, and I have to understand every file I submit.
- Explain *why* behind each design choice, briefly. When you write something non-obvious, say what it does in a sentence or two.
- Talk to me in Russian (Cyrillic only, no Latin transliteration; keep English only for established technical terms). Code, comments, README and commit messages stay in English.
- Don't do big jumps silently: propose a plan for each phase, then implement step by step.

## Environment
- macOS, Docker installed, Android phone. Run natively; Docker only as a bonus at the end.
- System Python is 3.9 — always use `.venv/bin/python` (3.12, created with `uv venv --python 3.12`). Node.js only for the thin bridge.
- Tests: `.venv/bin/python -m pytest -q`. All services bind to 127.0.0.1.
- Course materials (assignment, lecture PDF, lesson notes) are in `course-materials/`, gitignored — not ours to redistribute. `docs/` is ours.

## Docs (read when needed)
- `docs/assignment.md` — homework requirements, deliverables, grading, bonuses, hints.
- `docs/report-notes.md` — WhatsApp limitations, URL scheme, volume notes, event queries for REPORT.md.
- `docs/lecture.md` — lecture ideas we reused, the lecturer's code landmarks, how to read the PDF.
- `docs/decisions.md` — design decisions and trade-offs not obvious from the code.
- `README.md` — architecture, data model, cursor and error policies, CLI usage.

## Status (replace, don't append)
- Talkman SIM active (stays in the main phone, only for SMS). WhatsApp registered on a separate old Android phone as primary device: clean install, no restored backup, contacts permission denied, backup off, neutral profile, 2FA PIN on. Subscribing manually to 3–5 public channels.
- Phase 1 done: collector, `bridge_mock`, README. Phase 2 bridge built and tested on handwritten fixtures, **never linked yet**. External review fixed 2026-10-17 (collector hardening, privacy masking in logs, allowlist hook, `truncated` signal). Export pseudonymization was removed again: exports keep full ids, and the sample excludes regular groups. Tests: 241 pytest, 85 Node. Everything pushed to the private repo `dminakov-dev/whatsapp_scrpr` (main @ `6be9ded` + this file).
- **2026-10-18 live:** linked, `CONNECTED`. whatsapp-web.js read paths patched for WhatsApp Web 2.3000.x (`bridge/patches/`). `/chats`: 6 channels + 2 announcement-only groups (groups **disabled** until Dima says). First stage-B tick OK: 10 CNN posts, real channel permalinks, `history_start` (channel history ≈ 1 month, WhatsApp's limit), history call ~6 s → `OP_TIMEOUT_MS=20000`, `REQUEST_TIMEOUT=25`. Quiet continuous channel backfill running (command in runbook step 4). 2026-10-19: page detached overnight (Mac sleep), /health still said ok → fixed: /health probes the page (503 `PAGE_DETACHED`), bridge auto-recovers (3 attempts). First webhook message stored. Keep the Mac awake while collecting (`caffeinate -i npm start` in `bridge/`). Results: `docs/report-notes.md` "Live observations".
- Not done: live link, sample data, REPORT.md. `account_events` holds **mock data only** — not usable as observed limits.

## Runbook: first real link (2026-10-18)
**0. One-time `.env`** (root; does not exist yet; the bridge reads it via `../.env`). Start from `.env.example`, then set:
`BRIDGE_URL=http://127.0.0.1:8002`, `BRIDGE_TOKEN=<openssl rand -hex 32>`, `RECORD_DIR=fixtures/recorded`, and quiet-mode bridge settings `HISTORY_MIN_FETCH=20`, `HISTORY_MAX_FETCH=100`, `HISTORY_MIN_INTERVAL=90`, `PRESENCE_UNAVAILABLE=1`. Never print the token in chat.

**1. Start order** (each in its own terminal, from the repo root):
1. `.venv/bin/python -m collector.receiver` — receiver first, so no webhook is lost.
2. `cd bridge && caffeinate -i npm start` — bridge on 127.0.0.1:8002; first run prints a QR. `caffeinate -i` keeps the Mac from sleeping while it runs (sleep detached the page on 2026-10-19).
3. **No backfill process yet** (stage A is webhook only).

**2. Link:** the old phone → WhatsApp → Linked devices → Link a device → scan the terminal QR. Wait for log `state CONNECTED` and `presence set to unavailable`. Check:
`set -a; . ./.env; set +a; curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" http://127.0.0.1:8002/health` → `{"ok":true,"state":"CONNECTED"}`. Then `curl … /chats` once: only `@g.us` / `@newsletter`, groups carry `announce`.

**3. Stage A — webhook only (a few hours).** Leave receiver + bridge running; watch `logs/receiver.log` and the bridge log. Expect channel posts / group messages stored with `via='webhook'`.

**4. Stage B — slow channel backfill.**
- Sync targets without fetching history, then switch groups off (sync keeps `enabled`):
  `.venv/bin/python -c "from collector.config import load_config; from collector.source import BridgeClient; from collector import db; c=load_config(); s=BridgeClient(c.bridge_url, token=c.bridge_token); print(db.sync_targets(db.connect(c.db_path), s.list_chats()))"`
  then `.venv/bin/python -m collector.targets list` and `… targets disable <group id>` for every `@g.us`.
- Run the worker slowly (stop with Ctrl+C, restart with the same command; cursors make it resume): `BATCH_SIZE=10 TICK_SLEEP=120 JITTER=60 TARGET_INTERVAL=1800 SYNC_EVERY=30 .venv/bin/python -m collector.backfill`
  (`HISTORY_MIN_INTERVAL=90` < `TICK_SLEEP`, so the bridge's 429 is only a safety net; a 429 pauses the whole loop). Groups only later, after Dima's OK.

**5. Live-validation checklist** (record each result in `docs/report-notes.md`):
- [ ] group delivery: a group message arrives via webhook with `author` (often `@lid`);
- [x] channel delivery: `message` fires for channel posts (2026-10-18; first `forwarded` after the patch still to confirm);
- [ ] id consistency: the same message via webhook and history has the same `id`. Check no near-duplicates: `SELECT chat_id, date, text, COUNT(*) FROM messages GROUP BY 1,2,3 HAVING COUNT(*) > 1` must be empty;
- [x] channel permalink: invite code and `serverId` present → `https://whatsapp.com/channel/…` (2026-10-18);
- [x] `truncated`: first CNN collection logged `history_start` (not truncated): channel history ≈ 1 month is WhatsApp's limit (2026-10-18);
- [ ] receiver outage: stop the receiver (Ctrl+C), let posts arrive (bridge logs `event lost`), restart it, let backfill run → the missed posts appear `via='backfill'`, no duplicates, no unexpected `possible_gap`.

**6. Recorded fixtures.** The bridge writes raw responses to `bridge/fixtures/recorded/session.json` (gitignored: real text, member ids, `_data`). Then:
- run the same contract tests on them: `cd bridge && CONTRACT_FIXTURES=fixtures/recorded npm test` and `CONTRACT_FIXTURES=bridge/fixtures/recorded .venv/bin/python -m pytest -q tests/test_bridge_contract.py`; fix only the bridge if they fail;
- to commit any of it: write a small redaction script first (replace person JIDs everywhere including inside message ids, blank group texts, keep channel posts), save the result OUTSIDE `recorded/` (e.g. `bridge/fixtures/live/`), review it with Dima, and only then commit.

**7. Stop and ask Dima before continuing if:**
- any ban/restriction sign: the phone shows a warning, "account restricted / banned", logged out unexpectedly, captcha or verification request;
- unexpected `401` / `UNPAIRED` / `CONFLICT` after linking, repeated `disconnected`, or a state you don't recognize;
- anything that could send, react, mark as read, change presence to online or join (including a library option or upgrade that might do it implicitly), or any need to touch the read-only guard;
- direct-chat data showing up anywhere (logs, DB, recorded fixtures);
- the whatsapp-web.js / WhatsApp Web version needs changing, or Puppeteer fails on Node 25 (try Node 22/24 LTS only after asking);
- the bridge logs `restart the bridge manually` (automatic recovery gave up) or `PAGE_DETACHED` keeps recurring.

## Phases
1. ~~Phase 1 — Python logic against `bridge_mock/`.~~ Done.
2. **Phase 2 — real bridge** (`bridge/`). Built; remaining: the runbook above. Never adapt the collector to the bridge; hardening the collector on its own is fine (separate commit, a test per fix). Group `fetchMessages` is unreliable → expect small windows and `possible_gap` events.
3. **Phase 3 — deliverables:**
   - submission sample ≥ 200 records from **channels and announcement-only groups only**. Regular groups are never in the submission export. Exports keep full ids (no pseudonymization; see `docs/decisions.md`);
   - channels: `python -m collector.export --chat-type channel --out sample/messages.jsonl`. Announcement-only groups: the collector does **not** store `announce` yet (`source.list_chats` ignores it), so selecting them needs a small collector change first (e.g. keep `announce` in `targets` or add an export `--chat-id` filter). Propose it before Phase 3;
   - review the sample by hand for private data (mentions or phone numbers in text) before committing;
   - REPORT.md from real `account_events`.
4. **Phase 4 — bonuses** (optional): Docker Compose, stats dashboard.

**Decision point 2026-10-20:** if the real bridge isn't linked and collecting by then, add a fallback public-source adapter (public Telegram channel or RSS) for submission: a new class implementing `Source` in `collector/source.py` (`health`, `list_chats`, `fetch_history` → `CanonicalMessage`). db / dedup / cursor / backfill code must not change.

## Rules (non-negotiable)
- **Read-only.** Never send, react, mark as read or join anything from code. Joining is manual on the phone.
- **Groups and channels only;** direct (1:1) chats are never stored. Collect the minimum needed.
- **A cursor advances only together with the DB commit** (same transaction), never backwards.
- **Pacing:** one chat per tick, jittered sleep between requests.
- **Errors by scope:** 403/404/410 → skip the target (reversible); 408/409/5xx/timeout → retry that target with backoff; 400/other 4xx/401/429/bridge down/failing `/health` → pause the whole loop, never touch targets. One bad chat or malformed event must never crash the process.
- **Secrets:** `.env` only (`.env.example` committed). Never commit `.env`, `.wwebjs_auth/`, `.wwebjs_cache/`, `data/`, `logs/`, `exports/`, `course-materials/`.
- Sample data: public channels, reviewed for private persons' data before committing.

## Bridge contract (the Phase 2 Node bridge MUST follow it)
Payload shapes: top of `collector/models.py`. The collector classifies errors only by HTTP status (`collector/source.py: classify()`).

| Code | Meaning on the bridge side | Collector action |
|---|---|---|
| 400 | our request is malformed — a bug on **our** side | loop-level pause with backoff, `bad_request`, targets untouched |
| 401 | session logged out / unpaired, or wrong token | loop-level pause, `auth_failure` |
| 403 / 404 / 410 | chat not accessible / no longer a member / gone | skip target (reversible: `python -m collector.targets reenable`). **Only these three skip.** |
| 408 / 409 | timed out / client busy (WhatsApp Web loading) | retry target later with backoff |
| 429 | rate limited | loop-level pause for the full `Retry-After` (≥ own backoff, capped at `RETRY_AFTER_MAX`, `retry_after_capped`); never a skip |
| 5xx / timeout | bridge or WhatsApp Web failure | retry target with exponential backoff + jitter |
| other 4xx | not in the contract → our bug | like 400 |
| unreachable | bridge process down | loop-level pause, `disconnected` |

- **Direct chats / status:** never listed; a history request for one is our bug → **400**, never 403 (403 would skip/re-enable forever).
- **`GET /health`:** 200 `{"ok": true, "state": "CONNECTED"}` only when the session is usable; otherwise 401 or 200 with `ok: false` and the whatsapp-web.js state. Any other `/health` failure → loop-level pause, `bridge_unhealthy`.
- **History `GET /chats/{id}/messages?since=<unix s>&limit=<1..500>`:** messages with `timestamp >= since`, **oldest first**, the **oldest** `limit` of them — never the newest `limit` (`fetchMessages` returns the latest N; sort ascending, filter, then cut). Always include `oldest_available` (oldest timestamp in the window, or null). Optional `truncated: bool`: true when the source's own window cap (`HISTORY_MAX_FETCH`) stopped before `since` although older messages exist → collector logs `history_truncated` (also on the first collection); absent = unknown. `BATCH_SIZE ≤ 125` because the stuck-cursor retry asks for 4×.
- **`id` is `id._serialized` on both paths** (webhook and history): it is the dedup key with `chat_id`; different id forms would store every message twice.
- **`timestamp`** is Unix seconds; dates beyond now + `FUTURE_TOLERANCE` are rejected (`future_timestamp`) and never move the cursor.
- **Webhook `POST /webhook`:** `{"event": "message", "message": …}` or `{"event": "state", "state": …}`. Response 200 = done, never retry (also for rejected payloads); 500 = our storage failed, retry a few times with backoff (safe: dedup). Send `X-Bridge-Token` when configured; the bridge should also require it on its own GET endpoints (the collector sends it).

## Project layout
```
bridge/            Phase 2: Node.js bridge (whatsapp-web.js)
bridge_mock/       fake bridge: same contract, history window, fault injection
collector/         config, log, timeutil, db, models, source (adapter), receiver,
                   backfill, export, targets (CLI)
tests/             pytest — run before every commit
docs/              our docs (see "Docs")
course-materials/  lecture and assignment originals, gitignored
```
