// Contract test: invariants of the bridge contract (CLAUDE.md) that must hold for ANY session
// fixture, handwritten today, recorded from the live session tomorrow:
//   CONTRACT_FIXTURES=fixtures/recorded npm test
import assert from 'node:assert/strict';
import path from 'node:path';
import { after, before, test } from 'node:test';

import { loadFixture } from '../src/fake-wa.js';
import { CONTENT_TYPES, isCollectable } from '../src/filter.js';
import { enc, HANDWRITTEN, startBridge } from './helpers.js';

const DIR = process.env.CONTRACT_FIXTURES ? path.resolve(process.env.CONTRACT_FIXTURES) : HANDWRITTEN;
const fixture = loadFixture(DIR);
const KEYS = ['id', 'chat_id', 'author', 'timestamp', 'body', 'type'];
const CHANNEL_KEYS = ['channel_invite_code', 'server_id'];

let b;
let chats;
before(async () => {
  b = await startBridge({ fixture });
  chats = (await b.get('/chats')).body.chats;
});
after(async () => { await b.close(); });

const rawIds = new Map(Object.entries(fixture.messages).map(
  ([chat, msgs]) => [chat, new Set(msgs.map((m) => m.id._serialized))]));

function checkMessage(m, chatId) {
  const kind = chatId.endsWith('@g.us') ? 'group' : 'channel';
  assert.deepEqual(Object.keys(m).sort(),
                   [...KEYS, ...(kind === 'channel' ? CHANNEL_KEYS : [])].sort(), m.id);
  assert.equal(m.chat_id, chatId);
  assert.ok(rawIds.get(chatId)?.has(m.id), `id must be the raw id._serialized: ${m.id}`);
  assert.ok(Number.isInteger(m.timestamp) && m.timestamp > 1e9 && m.timestamp < 1e10,
            `unix seconds: ${m.timestamp}`);
  assert.ok(CONTENT_TYPES.has(m.type), m.type);
  assert.ok(m.body === null || (typeof m.body === 'string' && m.body !== ''));
  assert.ok(m.body !== null || m.type !== 'chat', 'text message without text');
  if (kind === 'group') assert.ok(typeof m.author === 'string' && m.author, 'group author');
  else assert.equal(m.author, null);
}

test(`[${path.basename(DIR)}] /health is 200 ok:true when connected`, async () => {
  assert.deepEqual((await b.get('/health')).body, { ok: true, state: 'CONNECTED' });
});

test(`[${path.basename(DIR)}] /chats lists only groups and channels, once each`, () => {
  assert.ok(chats.length > 0);
  const ids = chats.map((c) => c.chat_id);
  assert.equal(new Set(ids).size, ids.length);
  for (const c of chats) {
    assert.ok(isCollectable(c.chat_id), c.chat_id);
    if (c.chat_id.endsWith('@g.us')) assert.equal(typeof c.announce, 'boolean');
  }
});

test(`[${path.basename(DIR)}] full history: valid messages, oldest first, oldest_available`, async () => {
  for (const { chat_id: chatId } of chats) {
    const r = await b.get(`/chats/${enc(chatId)}/messages?since=0&limit=500`);
    assert.equal(r.status, 200, chatId);
    const stamps = r.body.messages.map((m) => m.timestamp);
    assert.deepEqual(stamps, [...stamps].sort((x, y) => x - y), `${chatId} oldest first`);
    r.body.messages.forEach((m) => checkMessage(m, chatId));
    const raw = (fixture.messages[chatId] || []).map((m) => Number(m.timestamp));
    if (r.body.messages.length) {
      assert.ok(r.body.oldest_available <= stamps[0]);
      assert.ok(raw.includes(r.body.oldest_available));
    }
  }
});

test(`[${path.basename(DIR)}] pages: the OLDEST limit after since, and paging covers everything`, async () => {
  for (const { chat_id: chatId } of chats) {
    const all = (await b.get(`/chats/${enc(chatId)}/messages?since=0&limit=500`)).body.messages;
    if (all.length < 3) continue;
    // Walk the chat like the collector does: since = last timestamp seen, dedup by id.
    const seen = new Map();
    let since = 0;
    for (let i = 0; i < all.length + 5; i += 1) {
      const page = (await b.get(`/chats/${enc(chatId)}/messages?since=${since}&limit=2`)).body.messages;
      const expected = all.filter((m) => m.timestamp >= since).slice(0, 2);
      assert.deepEqual(page.map((m) => m.id), expected.map((m) => m.id), `${chatId} since=${since}`);
      page.forEach((m) => seen.set(m.id, m));
      const next = page.at(-1)?.timestamp ?? since;
      if (next === since) {
        // Same-second run longer than the page: ask for more, like the stuck-cursor retry.
        const wide = (await b.get(`/chats/${enc(chatId)}/messages?since=${since}&limit=8`)).body.messages;
        wide.forEach((m) => seen.set(m.id, m));
        const after = wide.find((m) => m.timestamp > since);
        if (!after) break;
        since = after.timestamp;
      } else {
        since = next;
      }
    }
    assert.deepEqual([...seen.keys()].sort(), all.map((m) => m.id).sort(), chatId);
  }
});

test(`[${path.basename(DIR)}] webhook path forwards exactly what history serves`, async () => {
  b.webhook.sent.length = 0;
  for (const msgs of Object.values(fixture.messages)) for (const m of msgs) await b.forward(m);
  const listed = new Set(chats.map((c) => c.chat_id));
  for (const e of b.webhook.sent) {
    assert.equal(e.event, 'message');
    assert.ok(listed.has(e.message.chat_id), `forwarded from an unlisted chat: ${e.message.chat_id}`);
    checkMessage(e.message, e.message.chat_id);
  }
  for (const chatId of listed) {
    const hist = (await b.get(`/chats/${enc(chatId)}/messages?since=0&limit=500`)).body.messages;
    const live = b.webhook.sent.filter((e) => e.message.chat_id === chatId).map((e) => e.message);
    const byId = (xs) => [...xs].sort((x, y) => (x.id < y.id ? -1 : 1));
    // Same id, same shape on both paths: that is what makes dedup work.
    assert.deepEqual(byId(live), byId(hist), chatId);
  }
});

test(`[${path.basename(DIR)}] nothing from direct chats or status leaves the bridge`, async () => {
  for (const chatId of Object.keys(fixture.messages).filter((c) => !isCollectable(c))) {
    assert.equal((await b.get(`/chats/${enc(chatId)}/messages`)).status, 400, chatId);
    assert.ok(!b.webhook.sent.some((e) => e.message.chat_id === chatId), chatId);
  }
});
