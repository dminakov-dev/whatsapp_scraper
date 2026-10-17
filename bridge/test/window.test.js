import assert from 'node:assert/strict';
import { test } from 'node:test';

import { fakeFetch } from '../src/fake-wa.js';
import { historyWindow } from '../src/window.js';

const C = '120363000000000001@newsletter';
const T0 = 1792000000;

function post(i, t = T0 + i * 60, type = 'chat') {
  return { id: { remote: C, _serialized: `false_${C}_${i}` }, type, body: `p${i}`, timestamp: t };
}

// A chat holding `n` posts, newest first (unsorted on purpose), with a call log.
function chat(n) {
  const msgs = Array.from({ length: n }, (_, i) => post(i)).reverse();
  const fetches = [];
  return {
    fetches,
    fetchLatest: async (m) => { fetches.push(m); return fakeFetch(msgs, m); },
  };
}

const run = (c, since, limit, extra = {}) =>
  historyWindow({ fetchLatest: c.fetchLatest, since, limit, minFetch: 10, maxFetch: 400, ...extra });

test('returns the OLDEST `limit` messages >= since, oldest first', async () => {
  const c = chat(100);
  const page = await run(c, T0 + 30 * 60, 5);
  assert.deepEqual(page.messages.map((x) => x.body), ['p30', 'p31', 'p32', 'p33', 'p34']);
});

test('since is inclusive', async () => {
  const page = await run(chat(100), T0 + 50 * 60, 1);
  assert.equal(page.messages[0].timestamp, T0 + 50 * 60);
});

test('window doubles until it reaches since, and no further', async () => {
  const c = chat(300);
  const page = await run(c, T0 + 100 * 60, 5);  // need the latest 200
  assert.deepEqual(c.fetches, [10, 20, 40, 80, 160, 320]);
  assert.equal(page.messages[0].body, 'p100');
});

test('small chat: one fetch, oldest_available is the first message', async () => {
  const c = chat(7);
  const page = await run(c, 0, 50);
  assert.deepEqual(c.fetches, [100]);
  assert.equal(page.messages.length, 7);
  assert.equal(page.oldest_available, T0);
});

test('since older than the window: oldest_available > since (collector sees a gap)', async () => {
  const c = chat(1000);
  const page = await run(c, T0, 5);
  assert.equal(c.fetches.at(-1), 400);              // capped at maxFetch
  assert.equal(page.oldest_available, T0 + 600 * 60);
  assert.equal(page.messages[0].timestamp, T0 + 600 * 60);
});

test('empty chat: no messages, oldest_available null', async () => {
  const page = await run(chat(0), 0, 50);
  assert.deepEqual(page.messages, []);
  assert.equal(page.oldest_available, null);
});

test('system messages are filtered before the cut and still count for oldest_available', async () => {
  const msgs = [post(0, T0, 'gp2'), ...Array.from({ length: 10 }, (_, i) => post(i + 1, T0 + 1, 'e2e_notification')),
                post(20, T0 + 100)];
  const page = await historyWindow({ fetchLatest: async (m) => fakeFetch(msgs, m),
                                     since: 0, limit: 3, minFetch: 50, maxFetch: 50 });
  assert.deepEqual(page.messages.map((x) => x.body), ['p20']);
  assert.equal(page.oldest_available, T0);
  assert.equal(page.stats.dropped.type_e2e_notification, 10);
});

test('same-second messages are all returned in a stable order', async () => {
  const msgs = [post(1, T0), post(2, T0), post(3, T0), post(4, T0 + 1)];
  const page = await historyWindow({ fetchLatest: async () => msgs, since: T0, limit: 3,
                                     minFetch: 10, maxFetch: 10 });
  assert.deepEqual(page.messages.map((x) => x.body), ['p1', 'p2', 'p3']);
});

test('stops growing when WhatsApp has nothing older to give', async () => {
  const msgs = Array.from({ length: 30 }, (_, i) => post(i, T0 + 1000 + i));
  const fetches = [];
  // Pretend WhatsApp keeps returning the same 30 even when asked for more.
  const page = await historyWindow({ fetchLatest: async (m) => { fetches.push(m); return msgs.slice(0, Math.min(30, m)); },
                                     since: 0, limit: 5, minFetch: 30, maxFetch: 400 });
  assert.deepEqual(fetches, [30, 60]);
  assert.equal(page.messages.length, 5);
});

// --- truncated: our cap stopped the window before `since` ----------------------------------

test('truncated when the window is capped before reaching since', async () => {
  const page = await run(chat(1000), T0, 5);              // needs 1000, cap is 400
  assert.equal(page.truncated, true);
});

test('not truncated when the chat simply has nothing older (exhausted)', async () => {
  const page = await run(chat(7), 0, 50);
  assert.equal(page.truncated, false);
});

test('not truncated when the window reaches since', async () => {
  const page = await run(chat(300), T0 + 100 * 60, 5);
  assert.equal(page.truncated, false);
});

test('not truncated when WhatsApp stops giving older messages below the cap', async () => {
  const msgs = Array.from({ length: 30 }, (_, i) => post(i, T0 + 1000 + i));
  const page = await historyWindow({ fetchLatest: async (m) => msgs.slice(0, Math.min(30, m)),
                                     since: 0, limit: 5, minFetch: 30, maxFetch: 400 });
  assert.equal(page.truncated, false);
});
