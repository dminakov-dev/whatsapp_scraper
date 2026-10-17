// History window: turn "the latest M messages" (all whatsapp-web.js can give) into the
// contract's "the OLDEST `limit` messages with timestamp >= since, oldest first".
import { toContract } from './filter.js';

function tsOf(msg) {
  const ts = Number(msg?.timestamp);
  return Number.isFinite(ts) ? ts : null;
}

/**
 * @param {object} p
 * @param {(m: number) => Promise<object[]>} p.fetchLatest  returns up to m latest messages, any order
 * @param {number} p.since     Unix seconds, inclusive
 * @param {number} p.limit     1..500
 * @param {number} p.minFetch  first window size floor
 * @param {number} p.maxFetch  window size ceiling
 * @param {object} [p.chatInfo] passed to toContract (channel invite code)
 * @param {Function} [p.allow]   type allowlist passed to toContract (see createAllowlist)
 */
export async function historyWindow({ fetchLatest, since, limit, minFetch, maxFetch, chatInfo = {},
                                      allow }) {
  // Start small (fewer loadEarlierMsgs round-trips to WhatsApp) and double only when the
  // window doesn't reach back to `since` yet.
  let m = Math.min(Math.max(2 * limit, minFetch), maxFetch);
  let raw = [];
  let fetches = 0;
  for (;;) {
    const got = await fetchLatest(m);
    fetches += 1;
    const grew = got.length > raw.length;
    raw = got;
    const stamps = raw.map(tsOf).filter((t) => t !== null);
    const oldest = stamps.length ? Math.min(...stamps) : null;
    const exhausted = raw.length < m;          // the chat has nothing older to load
    if (exhausted || oldest === null || oldest <= since || !grew || m >= maxFetch) break;
    m = Math.min(m * 2, maxFetch);
  }

  // whatsapp-web.js only sorts when it has to cut, so sort ourselves. Array.sort is stable:
  // same-second messages keep WhatsApp's order.
  const sorted = raw.filter((x) => tsOf(x) !== null).sort((a, b) => tsOf(a) - tsOf(b));
  // Oldest timestamp of everything we hold, system messages included: that is how far back
  // this window reaches. The collector compares it with `since` to detect gaps.
  const oldestAvailable = sorted.length ? tsOf(sorted[0]) : null;

  // Filter BEFORE cutting to `limit`: otherwise a page full of system messages would look
  // like "nothing new" to the collector.
  const dropped = {};
  const messages = [];
  for (const msg of sorted) {
    if (tsOf(msg) < since) continue;
    const res = toContract(msg, chatInfo, allow);
    if (res.message) messages.push(res.message);
    else dropped[res.drop] = (dropped[res.drop] || 0) + 1;
  }
  return {
    messages: messages.slice(0, limit),
    oldest_available: oldestAvailable,
    stats: { fetched: raw.length, window: m, fetches, dropped },
  };
}
