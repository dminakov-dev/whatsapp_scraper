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
- Talkman SIM active (stays in the main phone, only for SMS). WhatsApp registered on a separate old Android phone as primary device: clean install, no restored backup, contacts permission denied, backup off, neutral profile, 2FA PIN on. Subscribing manually to 3–5 public channels; account warming until Phase 2 (~2026-10-19).
- Phase 1 done (2026-10-17): collector (`db`, `models`, `source`, `receiver`, `backfill`, `export`, `targets` CLI), `bridge_mock` with fault injection, 193 tests, README draft.
- Not done: Phase 2 bridge, sample data, REPORT.md. `account_events` so far holds **mock data only** — not usable as observed limits.

## Phases
1. ~~Phase 1 — Python logic against `bridge_mock/`.~~ Done.
2. **Phase 2 — real bridge** (`bridge/`, Node.js, whatsapp-web.js). Check current whatsapp-web.js docs first. Swap by `BRIDGE_URL` only; Python must not change. Checklist:
   - implement the contract below exactly; record real responses as fixtures and add a contract test (`/health` in each state, `/chats`, history page, webhook event);
   - forward only content messages: drop `status@broadcast` and system types (`e2e_notification`, `gp2`, `notification_template`, `call_log`, `revoked`, …) — otherwise they arrive as empty "media" rows or `malformed_payload` noise;
   - find out whether channel `invite_code` / `server_id` are available (else URLs stay `wa://…`);
   - group `fetchMessages` is unreliable → expect small windows and `possible_gap` events.
3. **Phase 3 — deliverables:** ≥ 200 records from public channels (`python -m collector.export --chat-type channel`), review for private data, REPORT.md from real `account_events`.
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

- **`GET /health`:** 200 `{"ok": true, "state": "CONNECTED"}` only when the session is usable; otherwise 401 or 200 with `ok: false` and the whatsapp-web.js state. Any other `/health` failure → loop-level pause, `bridge_unhealthy`.
- **History `GET /chats/{id}/messages?since=<unix s>&limit=<1..500>`:** messages with `timestamp >= since`, **oldest first**, the **oldest** `limit` of them — never the newest `limit` (`fetchMessages` returns the latest N; sort ascending, filter, then cut). Always include `oldest_available` (oldest timestamp in the window, or null). `BATCH_SIZE ≤ 125` because the stuck-cursor retry asks for 4×.
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
