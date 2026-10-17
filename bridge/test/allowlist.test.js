// Content-type allowlist hook: default unchanged; per-chat extras open a type for one chat only.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadConfig } from '../src/config.js';
import { CONTENT_TYPES, createAllowlist, toContract } from '../src/filter.js';
import { enc, startBridge } from './helpers.js';

const GROUP = '120363111111111111@g.us';
const ANNOUNCE = '120363222222222222@g.us';

function loc(chatId) {
  return { id: { remote: chatId, _serialized: `false_${chatId}_L1_100000000000001@lid` },
           type: 'location', body: '', timestamp: 1792000000, author: '100000000000001@lid' };
}

test('default allowlist is the fixed CONTENT_TYPES set: location dropped everywhere', () => {
  const allow = createAllowlist();
  for (const t of CONTENT_TYPES) assert.ok(allow(GROUP, t), t);
  assert.equal(allow(GROUP, 'location'), false);
  assert.equal(toContract(loc(GROUP)).drop, 'type_location');
  assert.deepEqual(loadConfig({}).extraTypesByChat, {});
  assert.deepEqual(new Set(loadConfig({}).contentTypes), CONTENT_TYPES);
});

test('a per-chat extra type passes only for that chat', () => {
  const allow = createAllowlist({ extraByChat: { [GROUP]: ['location'] } });
  const { message } = toContract(loc(GROUP), {}, allow);
  assert.equal(message.type, 'location');
  assert.equal(message.body, null);            // only the type: no coordinates are forwarded
  assert.equal(toContract(loc(ANNOUNCE), {}, allow).drop, 'type_location');
  assert.equal(toContract({ ...loc(GROUP), type: 'vcard' }, {}, allow).drop, 'type_vcard');
});

test('EXTRA_TYPES_BY_CHAT and CONTENT_TYPES are parsed and validated', () => {
  const cfg = loadConfig({ EXTRA_TYPES_BY_CHAT: JSON.stringify({ [GROUP]: ['location'] }),
                           CONTENT_TYPES: 'chat, image' });
  assert.deepEqual(cfg.extraTypesByChat, { [GROUP]: ['location'] });
  assert.deepEqual(cfg.contentTypes, ['chat', 'image']);
  for (const bad of ['not json', '[]', '{"15550000001@c.us": ["location"]}',
                     `{"${GROUP}": "location"}`, `{"${GROUP}": [1]}`]) {
    assert.throws(() => loadConfig({ EXTRA_TYPES_BY_CHAT: bad }), /EXTRA_TYPES_BY_CHAT/, bad);
  }
  assert.throws(() => loadConfig({ CONTENT_TYPES: ' , ' }), /CONTENT_TYPES/);
});

test('end to end: history and webhook both honor the per-chat extra', async () => {
  const b = await startBridge({ env: { EXTRA_TYPES_BY_CHAT: JSON.stringify({ [GROUP]: ['location'] }) } });
  try {
    const hist = (await b.get(`/chats/${enc(GROUP)}/messages?limit=500`)).body.messages;
    assert.ok(hist.some((m) => m.type === 'location'));
    assert.ok(!hist.some((m) => m.type === 'vcard'));
    const res = await b.forward(loc(GROUP));
    assert.equal(b.webhook.sent.at(-1).message.type, 'location');
    assert.ok(res.delivered);
    assert.equal((await b.forward(loc(ANNOUNCE))).dropped, 'type_location');
  } finally {
    await b.close();
  }
});

test('default bridge still drops location from history', async () => {
  const b = await startBridge();
  try {
    const hist = (await b.get(`/chats/${enc(GROUP)}/messages?limit=500`)).body.messages;
    assert.ok(!hist.some((m) => m.type === 'location'));
  } finally {
    await b.close();
  }
});
