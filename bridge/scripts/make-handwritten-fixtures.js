// Generates fixtures/handwritten/session.json: a fake session in the shape whatsapp-web.js
// returns (Chat / Channel / Message fields). All ids are synthetic; no real people.
// Run: node scripts/make-handwritten-fixtures.js
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(here, '..', 'fixtures', 'handwritten', 'session.json');

export const CHANNEL = '120363000000000001@newsletter';
export const GROUP = '120363111111111111@g.us';
export const ANNOUNCE = '120363222222222222@g.us';
export const DIRECT = '15550000001@c.us';
export const STATUS = 'status@broadcast';
const T0 = 1792000000; // 2026-10-14T00:26:40Z
const MEMBERS = ['100000000000001@lid', '100000000000002@lid', '100000000000003@lid'];

function wid(serialized) {
  const [user, server] = serialized.split('@');
  return { server, user, _serialized: serialized };
}

let seq = 0;
function msg(chatId, t, { type = 'chat', body = '', author = null, extra = {} } = {}) {
  seq += 1;
  const key = `3EB0${String(seq).padStart(16, '0')}`;
  const group = chatId.endsWith('@g.us');
  const serialized = `false_${chatId}_${key}${group && author ? `_${author}` : ''}`;
  return {
    _data: { id: { fromMe: false, remote: chatId, id: key, _serialized: serialized }, type, t,
             ...(chatId.endsWith('@newsletter') ? { serverId: 100 + seq } : {}) },
    id: { fromMe: false, remote: chatId, id: key, _serialized: serialized,
          ...(group && author ? { participant: author } : {}) },
    ack: 0,
    hasMedia: ['image', 'video', 'audio', 'ptt', 'document', 'sticker'].includes(type),
    body,
    type,
    timestamp: t,
    from: chatId,
    to: '15559999999@c.us',
    author: group ? author : undefined,
    deviceType: 'android',
    isForwarded: false,
    forwardingScore: 0,
    isStatus: chatId === STATUS,
    isStarred: false,
    broadcast: chatId === STATUS,
    fromMe: false,
    hasQuotedMsg: false,
    hasReaction: false,
    duration: undefined,
    location: undefined,
    vCards: [],
    mentionedIds: [],
    links: [],
    ...extra,
  };
}

// Channel: 40 posts, one every 10 minutes, a same-second burst, a revoked post, a
// caption-less image. Stored newest-first, like a partially loaded chat in WhatsApp Web.
const channelMsgs = [];
for (let i = 0; i < 40; i += 1) {
  const t = T0 + i * 600;
  if (i === 7) channelMsgs.push(msg(CHANNEL, t, { type: 'image', body: '' }));
  else if (i === 12) channelMsgs.push(msg(CHANNEL, t, { type: 'video', body: 'Video caption' }));
  else channelMsgs.push(msg(CHANNEL, t, { body: `Channel post ${i}` }));
}
channelMsgs.push(msg(CHANNEL, T0 + 20 * 600, { body: 'Same-second burst post' }));
channelMsgs.push(msg(CHANNEL, T0 + 25 * 600 + 1, { type: 'revoked', body: '' }));
channelMsgs.push(msg(CHANNEL, T0 + 26 * 600 + 1, { type: 'newsletter_notification', body: '',
                                                    extra: { isNotification: true } }));
channelMsgs.reverse();

// Group: content mixed with every kind of system message the bridge must drop.
const groupMsgs = [
  msg(GROUP, T0 + 0, { type: 'e2e_notification', body: '' }),
  msg(GROUP, T0 + 5, { type: 'gp2', body: '', extra: { recipients: [MEMBERS[2]] } }),
  msg(GROUP, T0 + 60, { body: 'Hello group', author: MEMBERS[0] }),
  msg(GROUP, T0 + 120, { type: 'image', body: 'Photo caption', author: MEMBERS[1] }),
  msg(GROUP, T0 + 180, { type: 'notification_template', body: '' }),
  msg(GROUP, T0 + 240, { type: 'call_log', body: '' }),
  msg(GROUP, T0 + 300, { type: 'vcard', body: 'BEGIN:VCARD\nTEL:+15550000000\nEND:VCARD',
                         author: MEMBERS[0] }),
  msg(GROUP, T0 + 360, { type: 'location', body: '', author: MEMBERS[1],
                         extra: { location: { latitude: '0.0', longitude: '0.0' } } }),
  msg(GROUP, T0 + 420, { type: 'revoked', body: '', author: MEMBERS[2] }),
  msg(GROUP, T0 + 480, { type: 'ciphertext', body: '', author: MEMBERS[2] }),
  msg(GROUP, T0 + 540, { body: '', author: MEMBERS[0] }), // empty text
  msg(GROUP, T0 + 600, { type: 'ptt', body: '', author: MEMBERS[2] }),
  msg(GROUP, T0 + 660, { type: 'poll_creation', body: 'Which day?', author: MEMBERS[0] }),
  msg(GROUP, T0 + 720, { type: 'document', body: 'report.pdf', author: MEMBERS[1] }),
  msg(GROUP, T0 + 720, { body: 'Same second as the document', author: MEMBERS[2] }),
  msg(GROUP, T0 + 780, { type: 'protocol', body: '' }),
  msg(GROUP, T0 + 840, { type: 'sticker', body: '', author: MEMBERS[0] }),
  msg(GROUP, T0 + 900, { body: 'Last group message', author: MEMBERS[1] }),
];

const announceMsgs = [0, 1, 2, 3, 4].map((i) =>
  msg(ANNOUNCE, T0 + 3600 + i * 60, { body: `Announcement ${i}`, author: MEMBERS[0] }));

const directMsgs = [
  msg(DIRECT, T0 + 100, { body: 'Private hello' }),
  msg(DIRECT, T0 + 200, { body: 'Private reply' }),
];
const statusMsgs = [msg(STATUS, T0 + 300, { type: 'image', body: 'Status caption' })];

const chats = [
  { id: wid(CHANNEL), name: 'Test Channel', isChannel: true, isGroup: false, isReadOnly: true,
    isMuted: false, unreadCount: 0, timestamp: T0 + 39 * 600, description: 'Synthetic channel',
    channelMetadata: { inviteCode: '0029VaTESTCHANNEL0001', membershipType: 'subscriber' } },
  { id: wid(GROUP), name: 'Test Group', isGroup: true, isReadOnly: false, isMuted: false,
    unreadCount: 0, archived: false, pinned: false, timestamp: T0 + 900,
    groupMetadata: { id: wid(GROUP), subject: 'Test Group', announce: false, restrict: false,
                     size: 3, participants: MEMBERS.map((m) => ({ id: wid(m), isAdmin: false })) } },
  { id: wid(ANNOUNCE), name: 'Test Announcements', isGroup: true, isReadOnly: true,
    isMuted: false, unreadCount: 0, timestamp: T0 + 3840,
    groupMetadata: { id: wid(ANNOUNCE), subject: 'Test Announcements', announce: true,
                     restrict: true, size: 3, participants: [] } },
  { id: wid(DIRECT), name: 'Some Person', isGroup: false, isReadOnly: false, isMuted: false,
    unreadCount: 1, timestamp: T0 + 200 },
  { id: wid(STATUS), name: 'Status', isGroup: false, isReadOnly: false, unreadCount: 0,
    timestamp: T0 + 300 },
];

const session = {
  state: 'CONNECTED',
  note: 'Synthetic fixture in whatsapp-web.js 1.34.7 shapes; generated by scripts/make-handwritten-fixtures.js',
  chats,
  messages: {
    [CHANNEL]: channelMsgs,
    [GROUP]: groupMsgs,
    [ANNOUNCE]: announceMsgs,
    [DIRECT]: directMsgs,
    [STATUS]: statusMsgs,
  },
};

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  mkdirSync(path.dirname(out), { recursive: true });
  writeFileSync(out, `${JSON.stringify(session, null, 2)}\n`);
  console.log(`wrote ${out}`);
}
