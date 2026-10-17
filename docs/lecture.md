# Lecture reference

The lecture (slides, lesson notes) is in `course-materials/` (gitignored). The PDF can't be
rendered here (`pdftoppm` is not installed); extract its text with:

```bash
uv run --no-project --with pypdf python -I -c "import sys, pypdf; [print(p.extract_text()) for p in pypdf.PdfReader(sys.argv[1]).pages]" "course-materials/Лекція 5 — Scraping and Crawling.pptx.pdf"
```

## Ideas from the lecture we reused
- **Classify every error by scope** (slide 32, `release()`): account-level signals cool down the
  account, target-level ones only the target ("403 not a member" is not the account's fault).
  → `collector/source.py: classify()`, loop-level vs target-level.
- **Check the session before every run** (slide 11) → `GET /health` before each tick.
- **Continuity check / alert on silent data loss** (slide 12) → `possible_gap` event when the
  bridge's history window no longer reaches our cursor.
- **Frontier = targets table with cursor and schedule** (slides 5, 8) → `targets` with
  `cursor_ts`, `next_run_at`; backoff 1 → 2 → 4 → 8 s + jitter, honor `Retry-After`.
- **Measure the cause, don't guess** (FloodWait lesson, slide 24) → every bridge response goes
  to `account_events` with status, latency and rate-limit headers.
- **Targets are synced from membership; nothing is joined from code** (slides 27, 34).

## Code landmarks in the lecturer's repo (for comparison)
- `crawl/functions/whatsapp_backfill.py` → `backfill_next_whatsapp_target()` — one chat per run, channel vs group branch, 4xx skip / 409+5xx retry (we refined this: only 403/404/410 skip).
- `auth/whatsapp_bridge_client.py` — Python client for the Node bridge (our `collector/source.py` plays this role).
- `database/result_rows.py` — canonical rows; `database/functions.py` — `ensure_*` schema functions.
- The lecturer's bridge is OpenWA, so its event is named `message.received`; whatsapp-web.js itself emits `message`. Our bridge forwards with its own stable event name (`message`, `state`).
