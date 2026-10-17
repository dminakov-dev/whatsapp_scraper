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
