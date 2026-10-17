import assert from 'node:assert/strict';
import { test } from 'node:test';

import { chatKind, CONTENT_TYPES, toChatEntry, toContract } from '../src/filter.js';

const G = '120363111111111111@g.us';
const C = '120363000000000001@newsletter';

function m(chatId, fields = {}) {
  return {
    id: { fromMe: false, remote: chatId, id: 'K1', _serialized: `false_${chatId}_K1` },
    type: 'chat', body: 'hi', timestamp: 1792000000, from: chatId,
    author: chatId.endsWith('@g.us') ? '100000000000001@lid' : undefined,
    isStatus: false, broadcast: false, ...fields,
  };
}

test('chatKind classifies JIDs', () => {
  assert.equal(chatKind(G), 'group');
  assert.equal(chatKind(C), 'channel');
  assert.equal(chatKind('15550000001@c.us'), 'direct');
  assert.equal(chatKind('100000000000001@lid'), 'direct');
  assert.equal(chatKind('status@broadcast'), 'status');
  assert.equal(chatKind(''), 'unknown');
});

test('group text becomes a contract message with id._serialized and author', () => {
  const { message } = toContract(m(G));
  assert.deepEqual(message, {
    id: `false_${G}_K1`, chat_id: G, author: '100000000000001@lid', timestamp: 1792000000,
    body: 'hi', type: 'chat',
  });
});

test('channel post has null author and optional invite code / server id', () => {
  const { message } = toContract(m(C, { _data: { serverId: 77 } }), { inviteCode: 'ABC' });
  assert.equal(message.author, null);
  assert.equal(message.channel_invite_code, 'ABC');
  assert.equal(message.server_id, 77);
});

test('media without caption: body null, type kept', () => {
  const { message } = toContract(m(G, { type: 'image', body: '' }));
  assert.equal(message.body, null);
  assert.equal(message.type, 'image');
});

test('system and private-data types are dropped (whitelist)', () => {
  for (const type of ['gp2', 'e2e_notification', 'notification_template', 'call_log', 'revoked',
                      'protocol', 'ciphertext', 'reaction', 'vcard', 'multi_vcard', 'location',
                      'broadcast_notification', 'something_new_from_whatsapp']) {
    assert.equal(CONTENT_TYPES.has(type), false, type);
    assert.equal(toContract(m(G, { type })).drop, `type_${type}`);
  }
});

test('direct chats, status and broadcasts are dropped', () => {
  assert.equal(toContract(m('15550000001@c.us')).drop, 'direct');
  assert.equal(toContract(m('100000000000009@lid')).drop, 'direct');
  assert.equal(toContract(m('status@broadcast', { isStatus: true })).drop, 'status');
  assert.equal(toContract(m(G, { broadcast: true })).drop, 'status');
});

test('empty text, missing author in group, bad timestamp are dropped', () => {
  assert.equal(toContract(m(G, { body: '' })).drop, 'empty_text');
  assert.equal(toContract(m(G, { author: undefined })).drop, 'group_without_author');
  assert.equal(toContract(m(G, { timestamp: 'x' })).drop, 'bad_timestamp');
  assert.equal(toContract(null).drop, 'not_an_object');
});

test('chat entries: only groups/channels, announce reported for groups', () => {
  const wid = (s) => ({ _serialized: s });
  assert.deepEqual(toChatEntry({ id: wid(G), name: 'G', groupMetadata: { announce: true } }),
                   { chat_id: G, name: 'G', announce: true });
  assert.deepEqual(toChatEntry({ id: wid(C), name: 'C' }), { chat_id: C, name: 'C' });
  assert.equal(toChatEntry({ id: wid('15550000001@c.us'), name: 'P' }), null);
  assert.equal(toChatEntry({ id: wid('status@broadcast') }), null);
});
