// HTTP behaviour of the bridge over the handwritten fixture: token, status codes, pacing.
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadFixture } from '../src/fake-wa.js';
import { enc, HANDWRITTEN, startBridge } from './helpers.js';

const CHANNEL = '120363000000000001@newsletter';
const GROUP = '120363111111111111@g.us';
const ANNOUNCE = '120363222222222222@g.us';
const DIRECT = '15550000001@c.us';

let b;
before(async () => { b = await startBridge(); });
after(async () => { await b.close(); });

test('every endpoint requires X-Bridge-Token', async () => {
  for (const p of ['/health', '/chats', `/chats/${enc(GROUP)}/messages`]) {
    assert.equal((await b.get(p, { token: null })).status, 401, p);
    assert.equal((await b.get(p, { token: 'wrong' })).status, 401, p);
  }
});

test('health: connected', async () => {
  const r = await b.get('/health');
  assert.equal(r.status, 200);
  assert.deepEqual(r.body, { ok: true, state: 'CONNECTED' });
});

test('chats: groups and channels only, announce for groups', async () => {
  const r = await b.get('/chats');
  assert.equal(r.status, 200);
  assert.deepEqual(r.body.chats, [
    { chat_id: CHANNEL, name: 'Test Channel' },
    { chat_id: GROUP, name: 'Test Group', announce: false },
    { chat_id: ANNOUNCE, name: 'Test Announcements', announce: true },
  ]);
});

test('group history: content only, oldest first, ids are id._serialized', async () => {
  const r = await b.get(`/chats/${enc(GROUP)}/messages?since=0&limit=500`);
  assert.equal(r.status, 200);
  assert.deepEqual(r.body.messages.map((m) => m.type),
                   ['chat', 'image', 'ptt', 'poll_creation', 'document', 'chat', 'sticker', 'chat']);
  assert.ok(r.body.messages.every((m) => m.id.startsWith(`false_${GROUP}_`)));
  assert.equal(r.body.oldest_available, 1792000000);  // the e2e notification still counts
  assert.equal(r.body.truncated, false);
  assert.ok(!JSON.stringify(r.body).includes('VCARD'));
});

test('channel history: invite code and server id attached', async () => {
  const r = await b.get(`/chats/${enc(CHANNEL)}/messages?since=0&limit=2`);
  assert.equal(r.body.messages.length, 2);
  assert.equal(r.body.messages[0].body, 'Channel post 0');
  assert.equal(r.body.messages[0].channel_invite_code, '0029VaTESTCHANNEL0001');
  assert.equal(typeof r.body.messages[0].server_id, 'number');
});

test('direct chat or status history is 400 (our bug), never 403', async () => {
  assert.equal((await b.get(`/chats/${enc(DIRECT)}/messages`)).status, 400);
  assert.equal((await b.get(`/chats/${enc('status@broadcast')}/messages`)).status, 400);
});

test('a capped window is reported as truncated', async () => {
  const capped = await startBridge({ env: { HISTORY_MIN_FETCH: '5', HISTORY_MAX_FETCH: '10' } });
  try {
    const r = await capped.get(`/chats/${enc(CHANNEL)}/messages?since=0&limit=2`);
    assert.equal(r.body.truncated, true);       // 42 posts, the cap is 10
    assert.equal(r.body.messages.length, 2);
  } finally {
    await capped.close();
  }
});

test('unknown group is 404; bad params are 400', async () => {
  assert.equal((await b.get(`/chats/${enc('120363999999999999@g.us')}/messages`)).status, 404);
  for (const q of ['limit=0', 'limit=501', 'limit=abc', 'since=-1', 'since=1.5']) {
    assert.equal((await b.get(`/chats/${enc(GROUP)}/messages?${q}`)).status, 400, q);
  }
});

test('unknown path is 404, non-GET is 405', async () => {
  assert.equal((await b.get('/nope')).status, 404);
  const r = await fetch(`${b.base}/chats`, { method: 'POST', headers: { 'X-Bridge-Token': 'test-token' } });
  assert.equal(r.status, 405);
});

test('session states map to the contract', async () => {
  b.wa.setSessionState({ state: 'OPENING', ready: false });
  const opening = await b.get('/health');
  assert.equal(opening.status, 200);
  assert.deepEqual(opening.body, { ok: false, state: 'OPENING' });
  assert.equal((await b.get('/chats')).status, 409);

  b.wa.setSessionState({ state: 'UNPAIRED', ready: false, paired: false });
  const h = await b.get('/health');
  assert.equal(h.status, 401);
  assert.deepEqual(h.body, { ok: false, state: 'UNPAIRED' });
  assert.equal((await b.get(`/chats/${enc(GROUP)}/messages`)).status, 401);

  b.wa.setSessionState({ state: 'CONNECTED', ready: true, paired: true });
  assert.equal((await b.get('/health')).status, 200);
});

test('a WhatsApp Web failure is 503, a slow one is 504 then 409 while still busy', async () => {
  b.wa.failNextFetch(new Error('Evaluation failed'));
  assert.equal((await b.get(`/chats/${enc(GROUP)}/messages`)).status, 503);

  const slow = await startBridge({ env: { OP_TIMEOUT_MS: '100' } });
  try {
    slow.wa.setDelay(400);
    assert.equal((await slow.get(`/chats/${enc(GROUP)}/messages`)).status, 504);
    assert.equal((await slow.get(`/chats/${enc(GROUP)}/messages`)).status, 409);
    await new Promise((r) => setTimeout(r, 400));
    slow.wa.setDelay(0);
    assert.equal((await slow.get(`/chats/${enc(GROUP)}/messages`)).status, 200);
  } finally {
    await slow.close();
  }
});

test('HISTORY_MIN_INTERVAL answers 429 with Retry-After', async () => {
  let t = 1_000_000;
  const paced = await startBridge({ env: { HISTORY_MIN_INTERVAL: '30' }, now: () => t });
  try {
    assert.equal((await paced.get(`/chats/${enc(GROUP)}/messages`)).status, 200);
    t += 10_000;
    const r = await paced.get(`/chats/${enc(GROUP)}/messages`);
    assert.equal(r.status, 429);
    assert.equal(r.headers.get('retry-after'), '20');
    t += 20_000;
    assert.equal((await paced.get(`/chats/${enc(GROUP)}/messages`)).status, 200);
  } finally {
    await paced.close();
  }
});

test('live path forwards only content from groups/channels', async () => {
  const sent = b.webhook.sent;
  sent.length = 0;
  // Push every fixture message (direct, status and system ones included) through the webhook path.
  const fixture = loadFixture(HANDWRITTEN);
  for (const msgs of Object.values(fixture.messages)) for (const m of msgs) await b.forward(m);
  const chatsSeen = new Set(sent.map((e) => e.message.chat_id));
  assert.deepEqual([...chatsSeen].sort(), [GROUP, ANNOUNCE, CHANNEL].sort());
  const groupHistory = (await b.get(`/chats/${enc(GROUP)}/messages?limit=500`)).body.messages;
  assert.deepEqual(sent.filter((e) => e.message.chat_id === GROUP).map((e) => e.message.id).sort(),
                   groupHistory.map((m) => m.id).sort());
});

test('state changes are pushed as state events', async () => {
  b.webhook.sent.length = 0;
  b.wa.setSessionState({ state: 'CONFLICT', ready: false });
  b.wa.setSessionState({ state: 'CONNECTED', ready: true });
  assert.deepEqual(b.webhook.sent, [{ event: 'state', state: 'CONFLICT' },
                                    { event: 'state', state: 'CONNECTED' }]);
});
