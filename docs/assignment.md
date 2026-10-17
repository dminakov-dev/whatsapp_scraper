# The assignment (Lesson 5)

Our summary of the homework. The original text is in `course-materials/homework assignment.md`
(gitignored: course materials are not ours to redistribute).

Build a Python project that collects data from one social network (we chose **WhatsApp**).

## Mandatory requirements
1. **Incremental collection** — by cursor / last id, never everything every time.
2. **Canonical model** — `author · url · date · text`, stored in SQLite (plus a JSONL export).
3. **Respect limits** — pacing + backoff on errors/429, no bans.
4. **Resilience and dedup** — a source failure must not crash the run; restart continues from the cursor; no duplicate rows.
5. **Config and run** — `.env` for secrets (outside code), `README.md`, `requirements.txt`.

Also explicitly required: **logging**, and a **re-run continues where it stopped** (idempotent).

## Deliverables
- Git repo with code + README.
- Sample data (≥ 200 records) in canonical format.
- A 0.5–1 page report: which source, which limits we hit, how we handled them, **and what exactly we stored**.

Deadline: about one week from 2026-10-16 (≈ 2026-10-23); exact date and submission format "as in previous lessons".

## Hints from the assignment
- Start with the canonical table and the dedup key — everything else rests on it.
- Study limits **empirically**: log response codes and `Retry-After` / `X-RateLimit-*` headers instead of guessing (the FloodWait lesson). For WhatsApp there is no HTTP 429 from the platform itself, so log every bridge response code, latency, disconnect / auth-failure reasons and state changes to `account_events` — that log is the raw material for the report.
- One run = a small batch; let the cursor move gradually.
- The assignment recommends a source with official/public access. WhatsApp is the hard option on purpose (learning). **Keep the source pluggable** so a public-source adapter (public Telegram channel or RSS) can be added without touching db / dedup / cursor / backfill logic.

## Grading
Incrementality & dedup 25% · limits & errors 25% · canonical model 20% · code cleanliness / README / config 20% · limitations report 10%.

## Bonuses (only after the core is done)
Multiple-account rotation, proxy egress, stats dashboard, history backfill, Docker / docker-compose.
Own idea: an FTS5 full-text index over `messages.text` (the lecturer's `results.*` tables are full-text indexed).
