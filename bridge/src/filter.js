// Which WhatsApp messages leave the bridge, and in what shape.
// Pure functions over whatsapp-web.js Message objects (or plain objects of the same shape),
// so they are tested without a browser.

// Whitelist, not blacklist: WhatsApp keeps adding system types (membership changes,
// encryption notices, call logs) that carry members' phone numbers. Anything new is
// dropped until we decide otherwise. vcard / multi_vcard / location are left out on
// purpose: contact cards hold phone numbers, locations hold coordinates.
export const CONTENT_TYPES = new Set([
  'chat', 'image', 'video', 'audio', 'ptt', 'document', 'sticker', 'poll_creation',
]);

export function chatKind(jid) {
  if (typeof jid !== 'string' || jid === '') return 'unknown';
  if (jid === 'status@broadcast' || jid.endsWith('@broadcast')) return 'status';
  if (jid.endsWith('@g.us')) return 'group';
  if (jid.endsWith('@newsletter')) return 'channel';
  if (jid.endsWith('@c.us') || jid.endsWith('@lid') || jid.endsWith('@s.whatsapp.net')) {
    return 'direct';
  }
  return 'unknown';
}

// Only groups and channels are ever served or forwarded.
export function isCollectable(jid) {
  const kind = chatKind(jid);
  return kind === 'group' || kind === 'channel';
}

function serialized(id) {
  if (typeof id === 'string') return id;
  return id && typeof id._serialized === 'string' ? id._serialized : null;
}

// The chat a message belongs to. id.remote is the chat JID for incoming and outgoing
// messages alike; `from` / `to` swap depending on direction.
export function chatIdOf(msg) {
  return serialized(msg?.id?.remote) ?? (msg?.fromMe ? serialized(msg?.to) : serialized(msg?.from));
}

/**
 * whatsapp-web.js Message -> bridge contract message, or a drop reason.
 * @param {object} msg
 * @param {object} [chatInfo] optional per-chat extras, e.g. { inviteCode } for channels
 * @returns {{message: object} | {drop: string}}
 */
export function toContract(msg, chatInfo = {}) {
  if (!msg || typeof msg !== 'object') return { drop: 'not_an_object' };
  const id = serialized(msg.id);
  const chatId = chatIdOf(msg);
  if (!id || !chatId) return { drop: 'no_id' };
  if (msg.isStatus || msg.broadcast) return { drop: 'status' };

  const kind = chatKind(chatId);
  if (kind === 'direct') return { drop: 'direct' };
  if (kind !== 'group' && kind !== 'channel') return { drop: `chat_${kind}` };

  if (!CONTENT_TYPES.has(msg.type)) return { drop: `type_${msg.type || 'missing'}` };

  // whatsapp-web.js puts the caption of a media message in `body` ('' when there is none).
  const body = typeof msg.body === 'string' && msg.body !== '' ? msg.body : null;
  if (msg.type === 'chat' && body === null) return { drop: 'empty_text' };

  const ts = Number(msg.timestamp);
  if (!Number.isInteger(ts) || ts <= 0) return { drop: 'bad_timestamp' };

  let author = null;
  if (kind === 'group') {
    author = serialized(msg.author);
    if (!author) return { drop: 'group_without_author' };
  }

  const message = { id, chat_id: chatId, author, timestamp: ts, body, type: msg.type };
  if (kind === 'channel') {
    // Not confirmed on a live session yet: the post's server id may live in the raw model.
    const serverId = msg._data?.serverId ?? msg.serverId ?? null;
    message.channel_invite_code = chatInfo.inviteCode ?? null;
    message.server_id = serverId ?? null;
  }
  return { message };
}

// Group/channel entry for GET /chats, or null for anything we don't serve.
export function toChatEntry(chat) {
  const chatId = serialized(chat?.id);
  if (!chatId || !isCollectable(chatId)) return null;
  const entry = { chat_id: chatId, name: typeof chat.name === 'string' ? chat.name : null };
  if (chatKind(chatId) === 'group') {
    // Announcement-only group (only admins post): behaves like a channel.
    entry.announce = Boolean(chat.groupMetadata?.announce);
  }
  return entry;
}
