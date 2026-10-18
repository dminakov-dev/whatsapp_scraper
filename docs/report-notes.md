# Notes for REPORT.md

Material for the 0.5–1 page limitations report (Phase 3).

> Until 2026-10-18 `account_events` held **mock data only**. Since the first live link
> (2026-10-18) the database holds real events; only those count as observed limits.

## Known WhatsApp limitations
- **E2E boundary:** groups — only messages received *after* the number joined; channels (not E2E) — history before subscribing is available (~last 100 posts).
- `fetchMessages` for groups is unreliable in whatsapp-web.js (often returns only what's loaded in the web client).
- Group authors may be `@lid` ids instead of phone numbers.
- The library sits on WhatsApp Web internals → can break after WhatsApp updates.
- Chromium can't do authenticated SOCKS5 → a proxy needs a local relay (e.g. gost), as in the lecture.
- If the primary phone stays offline for ~2 weeks, linked devices get logged out.
- WhatsApp forbids unofficial clients → the number can be banned; that's why we use a spare number.

## Live observations (first link, 2026-10-18)
Setup: whatsapp-web.js 1.34.7, Puppeteer 24.38 / Chromium 146, Node 25, WhatsApp Web 2.3000.x,
1 account, 6 public channels + 2 announcement-only groups (groups disabled for now).

- **Library broke on the current WhatsApp Web.** With a healthy `CONNECTED` session every read
  failed inside the page with a minified `r: r` error (`getChats`, `getChannels`, message
  events): WhatsApp Web 2.3000.x no longer exposes `_serialized` on id objects, so the library
  passed `undefined` keys to IndexedDB. No fixed release exists (upstream PR #201910). We patch
  the read paths locally (`bridge/patches/`, see `docs/decisions.md`); the bridge logs every
  id fallback, so the next rename is visible. A concrete example of the "unofficial client on
  moving internals" limitation.
- **Channel permalinks confirmed.** Channel metadata carries the invite code and each post a
  `serverId`, so channel URLs are real links
  (`https://whatsapp.com/channel/<invite_code>/<server_id>`), not `wa://…`.
- **Channel history ≈ 1 month, WhatsApp's limit, not ours.** First collection of CNN reached
  back to 2026-09-18 (one month before the link). The bridge window was not capped
  (`truncated` false, event `history_start`, no `history_truncated`): WhatsApp simply returned
  nothing older.
- **The `message` event fires for channel posts** (seen in the bridge log for a channel post),
  so the live path covers channels, not only backfill.
- **History latency ~6 s** for one `fetchMessages` call (10-message page, window loaded from
  WhatsApp's servers), vs a few ms for `/health` and `/chats`. Timeouts raised accordingly:
  bridge `OP_TIMEOUT_MS=20000`, collector `REQUEST_TIMEOUT=25`.
- Old-format group ids embed the creator's phone number (`<phone>-<timestamp>@g.us`).

## URL scheme (explain in the report)
WhatsApp group messages have no public permalink. Channel posts may have one
(`https://whatsapp.com/channel/<invite_code>/<server_id>`) when the bridge provides the data.
Otherwise we store a synthetic stable id `wa://<chat_id>/<message_id>` — an identifier, not a link.

## Volume reality check
WhatsApp is low-volume: the lecturer's production WhatsApp collector had only 328 rows
(2 accounts, 5 targets), vs 5.1M for Telegram. To reach ≥ 200 records quickly, subscribe to
3+ active public channels — channel backfill gives what WhatsApp still holds (about one month
of posts for CNN on 2026-10-18). Group
messages only accumulate after joining.

## Querying events
Every bridge response is one event with its status code; derived events (`cooldown`,
`target_skipped`) carry no status code, so this counts each response once:

```sql
SELECT kind, status_code, COUNT(*) AS n, CAST(AVG(latency_ms) AS INT) AS avg_ms
FROM account_events GROUP BY kind, status_code ORDER BY n DESC;
```

Data completeness per chat (how far back history reached, and where it was cut or has gaps):

```sql
SELECT chat_id, kind, ts, detail FROM account_events
WHERE kind IN ('history_start', 'history_truncated', 'possible_gap')
ORDER BY chat_id, ts;
```
`history_start` = first collection reached back to this date (complete as far as WhatsApp
goes); `history_truncated` = the source's window cap stopped earlier, older messages exist but
were not collected; `possible_gap` = messages between our cursor and the window edge are gone.
