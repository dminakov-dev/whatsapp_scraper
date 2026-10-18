// Our whatsapp-web.js patch (patches/whatsapp-web.js+1.34.7.patch, applied by postinstall):
// WhatsApp Web 2.3000.x dropped `_serialized` from live id objects. The patched page-side
// code resolves ids by kind and never guesses. These tests load the PATCHED Utils.js from
// node_modules with a fake `window`, so they test exactly what runs in the browser.
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readdirSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { afterEach, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { ID_FALLBACK_PREFIX, watchPageConsole } from '../src/wa.js';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');
const UTILS = path.join(ROOT, 'node_modules/whatsapp-web.js/src/util/Injected/Utils.js');
const require = createRequire(import.meta.url);

const GROUP = '120363111111111111@g.us';
const CHANNEL = '120363000000000001@newsletter';
const PHONE = '972501234567@c.us';
const MSG_KEY = `false_${GROUP}_3EB0AAAA_${PHONE}`;

let warnings;
let realWarn;
let modules;

beforeEach(() => {
  warnings = [];
  realWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(' '));
  modules = {};
  globalThis.window = { require: (name) => modules[name] };
  delete require.cache[UTILS];
  require(UTILS).LoadUtils();
});

afterEach(() => {
  console.warn = realWarn;
  delete globalThis.window;
});

const W = () => globalThis.window.WWebJS;
const fallbacks = () => warnings.filter((w) => w.startsWith(ID_FALLBACK_PREFIX));

// --- the patch is installed and stays on read paths ---------------------------------------

test('the patch is applied in node_modules (postinstall ran)', () => {
  assert.match(readFileSync(UTILS, 'utf8'), /\[wa-bridge patch\]/);
  assert.equal(typeof W().__serializedOf, 'function');
});

test('the patch file touches only Utils.js and adds no write-path code', () => {
  const files = readdirSync(path.join(ROOT, 'patches'));
  assert.deepEqual(files, ['whatsapp-web.js+1.34.7.patch']);
  const patch = readFileSync(path.join(ROOT, 'patches', files[0]), 'utf8');
  const touched = [...patch.matchAll(/^\+\+\+ b\/(.+)$/gm)].map((m) => m[1]);
  assert.deepEqual(touched, ['node_modules/whatsapp-web.js/src/util/Injected/Utils.js']);
  const added = patch.split('\n').filter((l) => l.startsWith('+') && !l.startsWith('+++'));
  const forbidden = /\b(sendMessage|sendSeen|sendPresenceAvailable|markUnread|sendReaction|react|join\w*|acceptInvite|acceptGroupV4Invite|editMessage|rejectCall|addParticipants|removeParticipants|subscribe\w*|mute|archive)\b/;
  for (const line of added) assert.doesNotMatch(line, forbidden, line);
});

// --- __serializedOf: kind-specific, never guesses -----------------------------------------

test('_serialized wins, with no warning', () => {
  assert.equal(W().__serializedOf({ _serialized: MSG_KEY, $1: 'x' }, 'msgkey'), MSG_KEY);
  assert.equal(W().__serializedOf(GROUP, 'wid'), GROUP);
  assert.deepEqual(fallbacks(), []);
});

test('message key without _serialized: the true_/false_ key, never a WID it holds', () => {
  // A message key carries the chat (remote) and participant WIDs as plain strings too.
  const key = { fromMe: false, remote: GROUP, participant: PHONE, id: '3EB0AAAA', $9: MSG_KEY };
  assert.equal(W().__serializedOf(key, 'msgkey', 'message.id'), MSG_KEY);
  assert.deepEqual(fallbacks(), [`${ID_FALLBACK_PREFIX} message.id msgkey value`]);
});

test('message key with only WIDs: undefined (skipped with a warning), not the chat id', () => {
  const key = { fromMe: false, remote: GROUP, participant: PHONE, id: '3EB0AAAA',
                $1: GROUP };                       // even a WID under the minified name
  assert.equal(W().__serializedOf(key, 'msgkey', 'message.id'), undefined);
  assert.deepEqual(fallbacks(), [`${ID_FALLBACK_PREFIX} message.id msgkey unresolved`]);
});

test('WID: only user@server values, $1 only if it matches, then user+server', () => {
  assert.equal(W().__serializedOf({ user: '120363', server: 'g.us', $1: GROUP }, 'wid'), GROUP);
  assert.equal(W().__serializedOf({ user: '120363', server: 'g.us', $1: MSG_KEY }, 'wid'),
               '120363@g.us');
  assert.equal(W().__serializedOf({ $1: MSG_KEY, x: 'false_abc' }, 'wid'), undefined);
  assert.deepEqual(fallbacks().map((w) => w.split(' ').at(-1)),
                   ['value', 'user@server', 'unresolved']);
});

// --- read paths -----------------------------------------------------------------------------

function wid(serialized) {
  const [user, server] = serialized.split('@');
  return { user, server, $1: serialized };           // what 2.3000.x hands out: no _serialized
}

test('getMessageModel: id._serialized and id.remote restored for message and webhook paths', () => {
  modules.WALinkify = { findLinks: () => [] };
  const message = {
    serialize: () => ({
      id: { fromMe: false, remote: wid(CHANNEL), id: 'K1', $1: `false_${CHANNEL}_K1` },
      from: wid(CHANNEL), body: 'post', type: 'chat', t: 1792000000,
    }),
    body: 'post',
  };
  const msg = W().getMessageModel(message);
  assert.equal(msg.id._serialized, `false_${CHANNEL}_K1`);
  assert.equal(msg.id.remote, CHANNEL);
  assert.equal(msg.from, CHANNEL);
});

test('getChats: a chat whose model throws is skipped with a warning, the rest is listed', async () => {
  const good = { id: wid(GROUP), serialize: () => ({ id: wid(GROUP), msgs: [] }), mute: {} };
  const broken = { id: wid(PHONE), serialize: () => { throw Object.assign(new Error('r'), { name: 'r' }); } };
  modules.WAWebCollections = { Chat: { getModelsArray: () => [good, broken] } };
  const chats = await W().getChats();
  assert.equal(chats.length, 1);
  assert.equal(chats[0].id._serialized, GROUP);
  assert.ok(fallbacks().some((w) => w.includes('getChatModel skipped chat r')));
});

test('getChannels: same isolation for channels', async () => {
  modules.WAWebChatGetters = { getIsNewsletter: () => true };
  const ok = { id: wid(CHANNEL), serialize: () => ({ id: wid(CHANNEL), msgs: [] }), mute: {} };
  const broken = { serialize: () => { throw new Error('r'); } };
  modules.WAWebCollections = { WAWebNewsletterCollection: { getModelsArray: () => [broken, ok] } };
  const channels = await W().getChannels();
  assert.deepEqual(channels.map((c) => c.id._serialized), [CHANNEL]);
});

test('getChatModel never passes an undefined key to IndexedDB', async () => {
  const asked = [];
  modules.WAWebCollections = {
    Msg: { get: (k) => { asked.push(k); return null; },
           getMessagesById: async (ks) => { asked.push(...ks); return { messages: [] }; } },
  };
  const chat = {
    id: wid(CHANNEL), mute: {}, formattedTitle: 'c',
    lastReceivedKey: { remote: CHANNEL, id: 'K' },   // unresolvable: only a WID inside
    serialize: () => ({ id: wid(CHANNEL), msgs: [1] }),
  };
  const model = await W().getChatModel(chat);
  assert.equal(model.lastMessage, null);
  assert.deepEqual(asked, []);                       // skipped, not looked up with undefined
  assert.ok(fallbacks().some((w) => w.includes('chat.lastReceivedKey msgkey unresolved')));
});

// --- Node side: page warnings reach the bridge log ---------------------------------------

test('page id-fallback warnings are logged at warn level, repeats folded per minute', () => {
  const page = new EventEmitter();
  const lines = [];
  let t = 0;
  watchPageConsole(page, (level, msg, extra) => lines.push({ level, msg, ...extra }),
                   { now: () => t });
  const emit = (text) => page.emit('console', { text: () => text });
  emit(`${ID_FALLBACK_PREFIX} message.id msgkey value`);
  emit(`${ID_FALLBACK_PREFIX} message.id msgkey value`);
  emit('some unrelated page log');
  t = 61_000;
  emit(`${ID_FALLBACK_PREFIX} message.id msgkey value`);
  assert.deepEqual(lines, [
    { level: 'warn', msg: 'whatsapp-web.js id fallback', detail: 'message.id msgkey value' },
    { level: 'warn', msg: 'whatsapp-web.js id fallback', detail: 'message.id msgkey value',
      repeated_since_last: 1 },
  ]);
});
