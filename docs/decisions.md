# Design decisions and trade-offs

Decisions that aren't obvious from the code. Anything enforced by a test is not repeated here.

## Startup gap-fill = "make every target due", not a catch-up pass
On startup `db.make_all_due()` sets `next_run_at = NULL` for every active target, and the normal
paced loop then walks them one per tick. A separate "catch up everything" pass would fire a
burst of requests at every restart, which is exactly what pacing is meant to avoid.
**Trade-off:** a restart also clears a pending retry delay of a failing chat, so it is retried
right away. `fail_count` is kept, so if it fails again the backoff continues from where it was.

## `AccountError` really means "loop-level"
The name comes from the lecture's account-health logic, but the class also covers our own bad
requests (400 and any 4xx outside the contract) and a failing `/health`. What they share is the
action: pause the whole loop and never touch targets. Renaming it was considered and postponed
to avoid churn; read `AccountError` as `LoopLevelError`.

## Status codes are recorded once per response
`source.BridgeClient` records every bridge response as one `account_events` row with its status
code and latency (`request`, `rate_limited`, `bad_request`, …). Events derived from it
(`cooldown`, `target_skipped`) deliberately carry no `status_code`, so
`GROUP BY status_code` in report queries counts each response exactly once.

## Webhook retries are the bridge's job
The receiver answers 200 for anything it will never accept (malformed, direct chat, disabled
target) and 500 only when its own storage failed. `bridge_mock` never retries, and anything
missed is recovered by backfill gap-fill. The real bridge should retry a 500 a few times with
backoff (safe thanks to dedup); see the bridge contract in `CLAUDE.md`.

## Bridge: one WhatsApp Web operation at a time, busy = 409
Puppeteer can't cancel a running `page.evaluate`, so a timed-out history request (504) keeps
running in the browser. The bridge stays "busy" until it really ends and answers 409 to anything
else meanwhile, instead of queueing more work onto a page that is already slow.
**Trade-off:** a slow chat delays every other chat by one retry; the collector backs off per
target, so this costs time, not data.

## Bridge: history window grows by doubling
`fetchMessages` loads older messages from WhatsApp's servers step by step, so asking for 500
every time would be the loudest possible pattern. The window starts at `max(2·limit, 50)` and
doubles only while it doesn't reach `since`. **Trade-off:** a chat far behind costs several
round trips in one request (bounded by `HISTORY_MAX_FETCH` and `OP_TIMEOUT_MS`).

## Bridge-side pacing guard answers 429, not 409
`HISTORY_MIN_INTERVAL` protects the account even if the collector is misconfigured. It answers
429 + `Retry-After` because pacing is account-wide: 429 pauses the whole loop, while 409 would
mark individual targets as failing and grow their `fail_count` for no fault of theirs.

## No durable webhook queue on the bridge
A webhook that fails all retries is dropped, not persisted for replay. A durable queue would be
a second on-disk store of group content (one more place to secure, review and delete), and it
isn't needed: webhooks never move the cursor, and backfill re-reads every chat from its cursor
on startup and every `TARGET_INTERVAL`. For our low-volume sources (a few channels and groups),
the bridge's 500-message history window covers any realistic receiver downtime. When it does
not, the loss is visible as a `possible_gap` event instead of being silent.
**Trade-off:** a chat busier than the window during an outage loses the overflow for good.

## Rejected payloads are logged by metadata, person ids masked
A rejected payload is described only by its reason plus `chat_id`, `id` and `type`
(`collector/privacy.py: safe_meta`). Text never reaches a log or `account_events.detail`, and
person ids are masked to their last 4 digits, including the participant JID that whatsapp-web.js
appends to group message ids. `LOG_IDENTIFIERS=full` shows ids when debugging the bridge.
Stored messages are not masked: the canonical record needs the real author.

## Exports keep full ids; the sample excludes regular groups
Export pseudonymization (salted HMAC of person ids) was built and then removed. Exports always
keep full identifiers, because the canonical record (`author · url · date · text`) is what
the assignment asks for. Privacy is handled by **what** goes into the submission sample, not by
rewriting it: the sample contains only channels and announcement-only groups
(`groupMetadata.announce`: only admins post). In those, the author is the channel itself or a
group admin, whose id every member already sees. Regular groups, where ordinary members write,
are never included in the submission export. They are still collected for our own use and stay
in the gitignored database and `exports/`. Every sample is still reviewed by hand before
committing (mentions or phone numbers inside text).

## Post-homework TODO (not now)
- **Location and contact cards.** The bridge whitelist drops `location`, `vcard` and `multi_vcard`
  (coordinates, phone numbers). Collecting them would need explicit fields and a privacy review.
- **Per-chat content types.** The hook exists: `CONTENT_TYPES` / `EXTRA_TYPES_BY_CHAT` in the
  bridge (`createAllowlist`, empty by default) let e.g. `location` through for one chat. Today it
  forwards only the type and caption. Using it for real means mapping the type's fields
  (coordinates, vCard) into the model extension below, with the same log-masking rules.
- **Group admins.** Record `groupMetadata.participants` with `isAdmin` per group (who controls a
  group is useful OSINT context).
- **Author display name.** Record `pushname` / `notifyName` per message next to the author id.
- These need a model extension, e.g. an `extra` JSON column in `messages` / `targets`, plus the
  same log-masking rules.
