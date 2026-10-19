// Lost WhatsApp Web page (live 2026-10-19: "Attempted to use detached Frame" for ~9 hours while
// /health said ok): the bridge must report it (503 on /health -> collector pauses the loop) and
// re-initialize the client with growing pauses, giving up after the last attempt.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createSessionGuard, isSessionGone, PAGE_DETACHED } from '../src/recovery.js';
import { enc, startBridge } from './helpers.js';

const CHANNEL = '120363000000000001@newsletter';
const LIVE_ERROR = "Attempted to use detached Frame '952E04749675F231DD5C6E35513CEFBB'.";

// Timers we fire by hand: [{ fn, ms }]
function manualTimers() {
  const pending = [];
  return {
    pending,
    setTimer: (fn, ms) => { const t = { fn, ms }; pending.push(t); return t; },
    clearTimer: (t) => { const i = pending.indexOf(t); if (i >= 0) pending.splice(i, 1); },
    async fireNext() {
      const t = pending.shift();
      assert.ok(t, 'no timer pending');
      await t.fn();
      return t.ms;
    },
  };
}

function capture() {
  const lines = [];
  return { lines, log: (level, msg, extra = {}) => lines.push({ level, msg, ...extra }) };
}

// --- recognising a lost page --------------------------------------------------------------

test('page/browser-gone errors are recognised; chat-level errors are not', () => {
  for (const msg of [LIVE_ERROR, 'Protocol error (Runtime.callFunctionOn): Target closed',
                     'Protocol error: Session closed. Most likely the page has been closed.',
                     'Execution context was destroyed, most likely because of a navigation']) {
    assert.ok(isSessionGone(new Error(msg)), msg);
  }
  for (const msg of ['r: r', 'chat not found', 'operation timed out']) {
    assert.equal(isSessionGone(new Error(msg)), false, msg);
  }
});

// --- the guard state machine --------------------------------------------------------------

test('detach -> PAGE_DETACHED once, restarts after 30 s / 2 min / 10 min, then gives up', async () => {
  const timers = manualTimers();
  const { lines, log } = capture();
  let detachedCalls = 0;
  let restarts = 0;
  const guard = createSessionGuard({
    log, ...timers,
    onDetached: () => { detachedCalls += 1; },
    restart: async () => { restarts += 1; throw new Error('initialize failed'); },
  });
  assert.equal(guard.report(new Error(LIVE_ERROR)), true);
  assert.equal(guard.report(new Error(LIVE_ERROR)), true);       // already recovering
  assert.equal(detachedCalls, 1);
  const waited = [await timers.fireNext(), await timers.fireNext(), await timers.fireNext()];
  assert.deepEqual(waited, [30_000, 120_000, 600_000]);
  assert.equal(restarts, 3);
  assert.equal(timers.pending.length, 0);                        // no 4th attempt
  assert.deepEqual(guard.state(), { detached: true, attempts: 3, gaveUp: true });
  assert.ok(lines.some((l) => l.level === 'error' && /restart the bridge manually/.test(l.msg)));
});

test('ready() after a restart ends recovery and resets the attempt counter', async () => {
  const timers = manualTimers();
  let guard;
  guard = createSessionGuard({
    ...timers, onDetached: () => {},
    restart: async () => guard.ready(),
  });
  guard.detach('test');
  await timers.fireNext();
  assert.deepEqual(guard.state(), { detached: false, attempts: 0, gaveUp: false });
  assert.equal(timers.pending.length, 0);
});

test('ordinary errors do not start recovery; stop() prevents it after shutdown', () => {
  const timers = manualTimers();
  const guard = createSessionGuard({ ...timers, onDetached: () => assert.fail('detached'),
                                     restart: async () => assert.fail('restarted') });
  assert.equal(guard.report(new Error('r: r')), false);
  guard.stop();
  guard.detach('browser disconnected during shutdown');
  assert.equal(timers.pending.length, 0);
});

// --- end to end over HTTP (fake adapter, same guard) ---------------------------------------

async function brokenBridge({ failRestarts = 0 } = {}) {
  const timers = manualTimers();
  const b = await startBridge({ waOptions: { recovery: timers } });
  b.wa.breakPage({ failRestarts });                    // silent: state still CONNECTED
  return { b, timers };
}

test('/health probes the page: a silently lost page answers 503 PAGE_DETACHED', async () => {
  const { b } = await brokenBridge();
  try {
    assert.equal(b.wa.status().state, 'CONNECTED');    // what fooled /health live
    const h = await b.get('/health');
    assert.equal(h.status, 503);
    assert.deepEqual(h.body, { ok: false, state: PAGE_DETACHED });
    assert.deepEqual(b.webhook.sent.at(-1), { event: 'state', state: PAGE_DETACHED });
    // While recovering, data endpoints say "not ready" instead of failing per chat.
    assert.equal((await b.get(`/chats/${enc(CHANNEL)}/messages`)).status, 409);
  } finally {
    await b.close();
  }
});

test('a request hitting the lost page switches the bridge to PAGE_DETACHED too', async () => {
  const { b } = await brokenBridge();
  try {
    assert.equal((await b.get(`/chats/${enc(CHANNEL)}/messages`)).status, 503);
    assert.equal((await b.get('/health')).status, 503);
    assert.equal(b.wa.guardState().detached, true);
  } finally {
    await b.close();
  }
});

test('recovery: after a failed restart the next one succeeds and /health is ok again', async () => {
  const { b, timers } = await brokenBridge({ failRestarts: 1 });
  try {
    await b.get('/health');
    assert.equal(await timers.fireNext(), 30_000);     // attempt 1 fails
    assert.equal((await b.get('/health')).status, 503);
    assert.equal(await timers.fireNext(), 120_000);    // attempt 2 succeeds
    assert.deepEqual((await b.get('/health')).body, { ok: true, state: 'CONNECTED' });
    assert.equal((await b.get(`/chats/${enc(CHANNEL)}/messages?limit=2`)).status, 200);
    assert.deepEqual(b.webhook.sent.map((e) => e.state).filter(Boolean),
                     [PAGE_DETACHED, 'CONNECTED']);
    assert.equal(b.wa.calls.restarts, 2);
  } finally {
    await b.close();
  }
});

test('gives up after 3 failed restarts: /health stays 503 until a manual restart', async () => {
  const { b, timers } = await brokenBridge({ failRestarts: 99 });
  try {
    await b.get('/health');
    for (let i = 0; i < 3; i += 1) await timers.fireNext();
    assert.equal(timers.pending.length, 0);
    assert.equal((await b.get('/health')).status, 503);
    assert.equal(b.wa.guardState().gaveUp, true);
  } finally {
    await b.close();
  }
});

test('an unlinked device still shows as 401, not PAGE_DETACHED', async () => {
  const { b } = await brokenBridge();
  try {
    await b.get('/health');
    b.wa.setSessionState({ state: 'UNPAIRED', paired: false, ready: false });
    assert.equal((await b.get('/health')).status, 401);
  } finally {
    await b.close();
  }
});
