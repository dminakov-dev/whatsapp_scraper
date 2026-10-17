# WhatsApp bridge (Phase 2)

A thin, read-only Node.js bridge over [whatsapp-web.js](https://wwebjs.dev) 1.34.7 that
implements the bridge contract from [`../CLAUDE.md`](../CLAUDE.md) and
[`../collector/models.py`](../collector/models.py). The Python collector switches to it by
changing `BRIDGE_URL` only.

## Run

```bash
cd bridge && npm install          # also downloads Chromium for puppeteer (~150 MB)
npm start                         # real session; first run prints a QR in the terminal
npm run fake                      # fixtures instead of WhatsApp (no phone needed)
npm test                          # node:test, incl. the contract test
```

Linking: on the phone, WhatsApp → Linked devices → Link a device, scan the terminal QR.
The session is kept in `bridge/.wwebjs_auth/` (gitignored); delete it to unlink locally.

Then in the root `.env`: `BRIDGE_URL=http://127.0.0.1:8002` (the mock stays on 8001).

## Settings (root `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8002` | loopback only; other hosts are refused |
| `BRIDGE_TOKEN` | empty | required in `X-Bridge-Token` on every endpoint, sent on webhooks |
| `WEBHOOK_URL` | `http://127.0.0.1:8000/webhook` | collector receiver |
| `WEBHOOK_ATTEMPTS` / `WEBHOOK_BACKOFF_MS` | 4 / 1000 | retries on 5xx or no connection; backoff doubles, ±50% jitter |
| `HISTORY_MIN_FETCH` / `HISTORY_MAX_FETCH` | 50 / 500 | history window: first size floor / ceiling |
| `HISTORY_MIN_INTERVAL` | 0 s | history calls closer than this get `429` + `Retry-After` (quiet mode) |
| `OP_TIMEOUT_MS` | 8000 | slower WhatsApp Web operations answer `504`; keep below the collector's `REQUEST_TIMEOUT` |
| `PRESENCE_UNAVAILABLE` | 1 | after `ready`, tell WhatsApp the device is offline |
| `RECORD_DIR` | empty | save raw responses as a fixture (gitignored; private data, redact before committing) |
| `BRIDGE_FAKE_FIXTURES` | empty | serve a fixture directory instead of WhatsApp |

## How it maps whatsapp-web.js to the contract

- **`GET /health`.** `ready` + state `CONNECTED` → `200 {ok:true}`. Starting / connecting /
  `CONFLICT` → `200 {ok:false, state}`. QR pending, logged out, `UNPAIRED*` → `401`.
- **`GET /chats`.** `client.getChats()` + `client.getChannels()`, deduplicated; only `@g.us`
  and `@newsletter`. Groups also carry `announce` (`groupMetadata.announce`: admins-only
  posting, behaves like a channel).
- **`GET /chats/{id}/messages`.** `fetchMessages({limit: M})` returns the **latest** M and only
  sorts when it cuts, so the bridge sorts ascending itself, filters, keeps `timestamp >= since`
  and returns the **oldest** `limit`. M starts at `max(2·limit, HISTORY_MIN_FETCH)` and doubles
  while the window doesn't reach `since` (each step is a `loadEarlierMsgs` round-trip to
  WhatsApp). `oldest_available` is the oldest timestamp in the window, system messages included.
- **Webhook.** The `message` event (incoming only; `message_create` would add our own) goes
  through the same filter as history, so both paths produce identical messages.
- **Ids.** `id._serialized` everywhere; `chat_id` is `id.remote`.

### Status codes

| Situation | Code | Collector action |
|---|---|---|
| bad / missing token, session unpaired | 401 | loop pause, `auth_failure` |
| bad `since` / `limit`; direct chat or status requested | 400 | loop pause, `bad_request` (our bug) |
| group / channel not found | 404 | skip target (reversible) |
| client not ready, or another operation in flight | 409 | retry target |
| history requested faster than `HISTORY_MIN_INTERVAL` | 429 + `Retry-After` | loop pause |
| WhatsApp Web / puppeteer error | 503 | retry target |
| operation slower than `OP_TIMEOUT_MS` | 504 | retry target |

A direct chat is never listed, so a request for one is a collector bug: `400`, never `403`
(`403` means "skip target", and the target would be re-enabled and skipped forever).

## What is forwarded

Whitelist by `type`: `chat`, `image`, `video`, `audio`, `ptt`, `document`, `sticker`,
`poll_creation`. Everything else is dropped: system types (`gp2`, `e2e_notification`,
`notification_template`, `call_log`, `revoked`, `protocol`, `ciphertext`, …, plus any new type
WhatsApp adds), `vcard` / `multi_vcard` (phone numbers), `location` (coordinates), status
(`status@broadcast`), direct chats, and text messages without text.

## Read-only

The code never calls a write method; `test/readonly.test.js` fails on `sendMessage`,
`sendSeen`, `sendPresenceAvailable`, reactions, joins, `mute`, `syncHistory` and others.
In whatsapp-web.js 1.34.7 a chat is marked as read only by `sendSeen()` (directly or inside
`sendMessage`); `fetchMessages` / `loadEarlierMsgs` don't. The one outgoing signal is
`sendPresenceUnavailable()` ("appear offline").

## Not confirmed until the first live session

- whether channel posts fire the `message` event (if not, backfill still collects them);
- where the channel invite code (`channelMetadata.inviteCode` / `inviteLink`) and the post
  `serverId` live; until then channel URLs stay `wa://…`.

Record a session with `RECORD_DIR=fixtures/recorded npm start`, then run the same contract tests
on it: `CONTRACT_FIXTURES=fixtures/recorded npm test` and
`CONTRACT_FIXTURES=bridge/fixtures/recorded .venv/bin/python -m pytest -q tests/test_bridge_contract.py`.

## Layout

```
src/config.js     settings, loopback-only host
src/filter.js     content whitelist, WhatsApp message -> contract message, chat entries
src/window.js     oldest-first history window, oldest_available
src/server.js     HTTP contract: token, status codes, one operation at a time, pacing
src/webhook.js    POST to the collector with retry/backoff
src/app.js        wiring (events -> filter -> webhook), fake-mode controls
src/wa.js         whatsapp-web.js adapter (LocalAuth, QR, states, presence)
src/fake-wa.js    fixture adapter with fetchMessages semantics
src/record.js     raw-response recorder (RECORD_DIR)
src/main.js       entry point
fixtures/handwritten/  synthetic session in whatsapp-web.js shapes (scripts/make-handwritten-fixtures.js)
```

`npm audit` reports advisories in puppeteer's transitive install-time dependencies
(`basic-ftp`, `extract-zip`), which puppeteer uses to download a browser at install time,
not while the bridge runs.
