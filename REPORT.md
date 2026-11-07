# Report: WhatsApp channel collector (Lesson 5)

## Source and what was stored
Source: **WhatsApp** via an unofficial client (whatsapp-web.js 1.34.7 driving WhatsApp Web in
headless Chromium), linked as a second device to an account on a spare number. Six **public
channels** were collected on four active days (Oct 18, 19, 24, 26, 2026); groups and direct chats
were deliberately switched off.

`sample/messages.jsonl`: **297 records** (Sep 18 – Oct 19): 141 images, 80 videos, 74 text posts,
1 voice note, 1 poll. Each record is the canonical `author · url · date · text` plus `source,
chat_id, chat_type, message_id, media_type, collected_at, via`. `author` is the channel (posts have
no individual sender), `url` is the real permalink (`https://whatsapp.com/channel/<code>/<post>`),
`text` is the post text or media caption (empty for 7 media posts). Not stored: media files,
reactions, views, group members. Dedup key `(chat_id, message_id)`; the sample has no duplicates.

## Limits we hit (764 rows of `account_events`)
WhatsApp has no HTTP rate limit: **0 × 429, 0 auth failures, no ban**. We stayed quiet on purpose
(one chat per tick, 10 messages per request, 2–3 minute pauses, device shown offline). What
actually limited us:

- **History depth.** About one month of channel history is available. `fetchMessages` returns only
  the *latest* N posts, so the bridge loads a window and cuts it. On the busiest channel (~20 posts
  a day) our own cap of 500 messages was reached: 4 `history_truncated` and 1 `possible_gap`
  events; posts of ~Sep 24–30 were never collected. That loss comes from our cap plus downtime, not
  from WhatsApp's one-month limit.
- **Latency.** History calls: p50 2.2 s, p90 4.4 s, max 6.0 s (36 calls); timeouts raised.
- **A library broken by WhatsApp.** On the live WhatsApp Web version every read failed, because the
  page stopped exposing a field the library reads. No fixed release existed, so we patched the read
  paths locally (`patch-package`) and log every fallback.
- **The session silently dying.** The page detached 4 times (`PAGE_DETACHED`) while the library
  still said "connected". 221 failed requests (503) and **181 whole-loop pauses** (Oct 18–29) are
  this; it is why the live path stopped on Oct 19 and backfill advanced only in short windows.

## How we handled them
- **Pacing and backoff:** one chat per tick, jittered sleeps, exponential backoff; a `Retry-After`
  pause for 429 is implemented and tested, but was never needed.
- **Errors by scope:** a failing chat is retried or skipped (403/404/410 only, reversible); a
  bridge or account problem pauses the **whole** loop and never penalizes chats. After 181 pauses
  all 8 targets still had 0 failures.
- **Honest health:** `/health` now probes the page itself, answers 503 when it is gone, and the
  bridge restarts its client (3 attempts, no new QR).
- **Incrementality and dedup:** the per-chat cursor advances in the same SQLite transaction as the
  inserts; a restart re-reads from the cursor and the unique key absorbs repeats.
- **Visible data quality:** `history_start`, `history_truncated` and `possible_gap` events record
  where history is incomplete. The code is read-only (a test fails if it can send or mark as read).

## Limitations
Unofficial clients violate WhatsApp's terms and the number can be banned (not observed here); the
library depends on WhatsApp Web internals and broke once; only four days were active because of the
page-loss problem. Two promotional posts contain organisation phone numbers; they are kept because
they are public advertising in a public channel.
