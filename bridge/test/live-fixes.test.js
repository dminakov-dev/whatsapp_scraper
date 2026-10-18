// Fixes from the first live session (2026-10-18):
// - getChats() failing inside WhatsApp Web must not hide channels (/chats was 503);
// - every incoming message is logged (forwarded or dropped) without text, ids masked.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { createBridge } from '../src/app.js';
import { loadConfig } from '../src/config.js';
import { createFakeWa, loadFixture } from '../src/fake-wa.js';
import { maskIds } from '../src/mask.js';
import { listAllChats } from '../src/wa.js';
import { enc, HANDWRITTEN, recordingWebhook, TOKEN } from './helpers.js';

const GROUP = '120363111111111111@g.us';
const CHANNEL = '120363000000000001@newsletter';
const PHONE = '972501234567@c.us';
const SECRET = 'SECRET-TEXT do not log';

// What puppeteer throws when WhatsApp Web's own code fails inside page.evaluate.
const waError = () => Object.assign(new Error('r: r\n    at #evaluate (ExecutionContext.js:391:56)'),
                                    { name: 'r' });

function capture() {
  const lines = [];
  return { lines, log: (level, msg, extra = {}) => lines.push({ level, msg, ...extra }) };
}

// --- masking ---------------------------------------------------------------------------

test('maskIds keeps the last 4 digits of person ids only', () => {
  assert.equal(maskIds(PHONE), '********4567@c.us');
  assert.equal(maskIds(`false_${GROUP}_3EB0_${PHONE}`), `false_${GROUP}_3EB0_********4567@c.us`);
  assert.equal(maskIds('123456789012345@lid'), '***********2345@lid');
  assert.equal(maskIds(CHANNEL), CHANNEL);
  assert.equal(maskIds(null), null);
});

// --- listAllChats ----------------------------------------------------------------------

test('getChats failing: channels are still listed and the failure is logged', async () => {
  const { lines, log } = capture();
  const client = { getChats: async () => { throw waError(); },
                   getChannels: async () => [{ id: { _serialized: CHANNEL } }] };
  const all = await listAllChats(client, log);
  assert.deepEqual(all.map((c) => c.id._serialized), [CHANNEL]);
  assert.deepEqual(lines, [{ level: 'warn', msg: 'partial chat list', failed: 'getChats',
                             error: 'r: r' }]);
});

test('getChannels failing: groups are still listed', async () => {
  const client = { getChats: async () => [{ id: { _serialized: GROUP } }],
                   getChannels: async () => { throw waError(); } };
  assert.deepEqual((await listAllChats(client)).map((c) => c.id._serialized), [GROUP]);
});

test('both failing: the listing fails (the server answers 503)', async () => {
  const client = { getChats: async () => { throw waError(); },
                   getChannels: async () => { throw waError(); } };
  await assert.rejects(listAllChats(client), /getChats: r: r; getChannels: r: r/);
});

test('both working: groups and channels together, no warning', async () => {
  const { lines, log } = capture();
  const client = { getChats: async () => [{ id: { _serialized: GROUP } }],
                   getChannels: async () => [{ id: { _serialized: CHANNEL } }] };
  assert.equal((await listAllChats(client, log)).length, 2);
  assert.deepEqual(lines, []);
});

test('GET /chats answers 200 with channels when getChats fails', async () => {
  const fixture = loadFixture(HANDWRITTEN);
  const wa = createFakeWa(fixture);
  const channels = fixture.chats.filter((c) => c.id._serialized.endsWith('@newsletter'));
  wa.listChats = () => listAllChats({ getChats: async () => { throw waError(); },
                                      getChannels: async () => channels });
  const cfg = loadConfig({ BRIDGE_TOKEN: TOKEN });
  const { server } = createBridge(cfg, wa, { webhook: recordingWebhook() });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const resp = await fetch(`${base}/chats`, { headers: { 'X-Bridge-Token': TOKEN } });
    assert.equal(resp.status, 200);
    assert.deepEqual((await resp.json()).chats, [{ chat_id: CHANNEL, name: 'Test Channel' }]);
    const hist = await fetch(`${base}/chats/${enc(CHANNEL)}/messages?limit=2`,
                             { headers: { 'X-Bridge-Token': TOKEN } });
    assert.equal(hist.status, 200);
  } finally {
    await new Promise((r) => server.close(r));
  }
});

// --- message received log --------------------------------------------------------------

function bridgeWithLog() {
  const { lines, log } = capture();
  const cfg = loadConfig({ BRIDGE_TOKEN: TOKEN });
  const { forward } = createBridge(cfg, createFakeWa(loadFixture(HANDWRITTEN)),
                                   { webhook: recordingWebhook(), log });
  return { lines, forward };
}

function msg(chatId, fields = {}) {
  return { id: { remote: chatId, _serialized: `false_${chatId}_3EB0${chatId.endsWith('@g.us') ? `_${PHONE}` : ''}` },
           type: 'chat', body: SECRET, timestamp: 1792000000,
           author: chatId.endsWith('@g.us') ? PHONE : undefined, ...fields };
}

test('every incoming message is logged: forwarded or dropped with a reason', async () => {
  const { lines, forward } = bridgeWithLog();
  await forward(msg(GROUP));
  await forward(msg(CHANNEL, { _data: { serverId: 5 } }));
  await forward(msg(PHONE));                                 // direct chat
  await forward(msg(GROUP, { type: 'gp2', body: '' }));      // system message
  const received = lines.filter((l) => l.msg === 'message received');
  assert.deepEqual(received.map((l) => [l.chat_kind, l.type, l.result, l.reason]), [
    ['group', 'chat', 'forwarded', undefined],
    ['channel', 'chat', 'forwarded', undefined],
    ['direct', 'chat', 'dropped', 'direct'],
    ['group', 'gp2', 'dropped', 'type_gp2'],
  ]);
});

test('the message log never holds text or full person ids', async () => {
  const { lines, forward } = bridgeWithLog();
  await forward(msg(GROUP));
  await forward(msg(PHONE));
  const text = JSON.stringify(lines);
  assert.ok(!text.includes(SECRET));
  assert.ok(!text.includes('972501234567'));
  assert.ok(text.includes('********4567@c.us'));             // masked, still correlatable
  assert.ok(text.includes(GROUP));                            // groups are not persons
});
