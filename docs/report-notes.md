# Notes for REPORT.md

Material for the 0.5–1 page limitations report (Phase 3).

> `account_events` currently holds **mock data only** (from `bridge_mock` fault injection).
> Observed limits for the report must come from the real bridge (Phase 2+).

## Known WhatsApp limitations
- **E2E boundary:** groups — only messages received *after* the number joined; channels (not E2E) — history before subscribing is available (~last 100 posts).
- `fetchMessages` for groups is unreliable in whatsapp-web.js (often returns only what's loaded in the web client).
- Group authors may be `@lid` ids instead of phone numbers.
- The library sits on WhatsApp Web internals → can break after WhatsApp updates.
- Chromium can't do authenticated SOCKS5 → a proxy needs a local relay (e.g. gost), as in the lecture.
- If the primary phone stays offline for ~2 weeks, linked devices get logged out.
- WhatsApp forbids unofficial clients → the number can be banned; that's why we use a spare number.

## URL scheme (explain in the report)
WhatsApp group messages have no public permalink. Channel posts may have one
(`https://whatsapp.com/channel/<invite_code>/<server_id>`) when the bridge provides the data.
Otherwise we store a synthetic stable id `wa://<chat_id>/<message_id>` — an identifier, not a link.

## Volume reality check
WhatsApp is low-volume: the lecturer's production WhatsApp collector had only 328 rows
(2 accounts, 5 targets), vs 5.1M for Telegram. To reach ≥ 200 records quickly, subscribe to
3+ active public channels — channel backfill gives roughly the last ~100 posts each. Group
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
