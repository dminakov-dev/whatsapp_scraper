// Read-only guard: fails if bridge code ever calls a whatsapp-web.js method that writes to
// WhatsApp (sends, marks as seen, reacts, joins, shows us online, changes chats).
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src');

// Matched as a method call: `.name(`. sendPresenceUnavailable is allowed on purpose:
// it is the "appear offline" signal (PRESENCE_UNAVAILABLE=1).
export const FORBIDDEN = [
  'sendMessage', 'sendSeen', 'markUnread', 'sendPresenceAvailable', 'sendStateTyping',
  'sendStateRecording', 'react', 'reply', 'forward', 'delete', 'edit', 'pin', 'unpin',
  'acceptInvite', 'acceptGroupV4Invite', 'acceptChannelAdminInvite', 'joinGroup',
  'subscribeToChannel', 'unsubscribeFromChannel', 'createGroup', 'createChannel', 'leave',
  'archive', 'unarchive', 'mute', 'unmute', 'clearMessages', 'syncHistory', 'vote',
  'setStatus', 'setDisplayName', 'setProfilePicture', 'addParticipants', 'removeParticipants',
  'logout',
];

function stripComments(code) {
  return code.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

export function forbiddenCalls(code) {
  const body = stripComments(code);
  return FORBIDDEN.filter((name) => new RegExp(`\\.${name}\\s*\\(`).test(body));
}

test('the guard itself: flags sendPresenceAvailable, allows sendPresenceUnavailable', () => {
  assert.deepEqual(forbiddenCalls('await client.sendPresenceAvailable();'), ['sendPresenceAvailable']);
  assert.deepEqual(forbiddenCalls('await client.sendPresenceUnavailable();'), []);
  assert.deepEqual(forbiddenCalls('chat.sendSeen()'), ['sendSeen']);
  assert.deepEqual(forbiddenCalls('// never call chat.sendSeen() here'), []);
});

test('no file in bridge/src calls a WhatsApp write method', () => {
  const files = readdirSync(SRC).filter((f) => f.endsWith('.js'));
  assert.ok(files.includes('wa.js'));
  for (const f of files) {
    assert.deepEqual(forbiddenCalls(readFileSync(path.join(SRC, f), 'utf8')), [], f);
  }
});

test('wa.js listens to "message", not "message_create", and sets presence unavailable', () => {
  const code = stripComments(readFileSync(path.join(SRC, 'wa.js'), 'utf8'));
  assert.match(code, /client\.on\('message',/);
  assert.doesNotMatch(code, /message_create/);
  assert.match(code, /\.sendPresenceUnavailable\(\)/);
});
