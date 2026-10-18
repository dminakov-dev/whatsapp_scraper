// Real adapter: whatsapp-web.js. The only file that knows the library.
//
// Read-only by construction: this file (like all of src/) never calls sendMessage, sendSeen,
// reactions, joins or anything else that writes to WhatsApp; test/readonly.test.js enforces
// it. The one outgoing signal is sendPresenceUnavailable() (PRESENCE_UNAVAILABLE=1), which
// tells WhatsApp the linked device is NOT online.
import qrcode from 'qrcode-terminal';
import wwebjs from 'whatsapp-web.js';

import { inviteCodeOf } from './invite.js';

const { Client, LocalAuth } = wwebjs;

const firstLine = (err) => String(err?.message ?? err).split('\n')[0].slice(0, 200);

/**
 * Groups (getChats) and channels (getChannels) are listed independently: on a live session
 * getChats() can fail inside WhatsApp Web (minified "r: r" error) while channels still work.
 * One failing half is logged and skipped; only when both fail does the request fail (503).
 */
export async function listAllChats(client, log = () => {}) {
  const [chats, channels] = await Promise.allSettled([client.getChats(), client.getChannels()]);
  const all = [];
  const failed = [];
  for (const [name, res] of [['getChats', chats], ['getChannels', channels]]) {
    if (res.status === 'fulfilled') all.push(...res.value);
    else failed.push({ name, error: firstLine(res.reason) });
  }
  if (failed.length === 2) {
    throw new Error(`chat listing failed: ${failed.map((f) => `${f.name}: ${f.error}`).join('; ')}`);
  }
  for (const f of failed) {
    log('warn', 'partial chat list', { failed: f.name, error: f.error });
  }
  return all;
}

export function createWa(cfg, { log = () => {} } = {}) {
  const client = new Client({
    // Session lives in bridge/.wwebjs_auth/ (gitignored); the QR is needed only once.
    authStrategy: new LocalAuth({ dataPath: cfg.authDir }),
    puppeteer: {
      headless: true,
      args: ['--no-first-run', '--no-default-browser-check'],
    },
  });

  let status = { state: 'STARTING', ready: false, paired: null };
  const messageHandlers = [];
  const stateHandlers = [];

  function setStatus(patch) {
    const before = status.state;
    status = { ...status, ...patch };
    if (status.state !== before) {
      log('info', 'state', { state: status.state, ready: status.ready });
      for (const h of stateHandlers) h(status.state);
    }
  }

  client.on('qr', (qr) => {
    setStatus({ state: 'UNPAIRED', ready: false, paired: false });
    // eslint-disable-next-line no-console
    console.log('\nScan with the phone: WhatsApp > Linked devices > Link a device\n');
    qrcode.generate(qr, { small: true });
  });
  client.on('authenticated', () => setStatus({ state: 'AUTHENTICATED', paired: true }));
  client.on('auth_failure', (msg) => {
    log('error', 'auth failure', { msg: String(msg) });
    setStatus({ state: 'UNPAIRED', ready: false, paired: false });
  });
  client.on('ready', async () => {
    setStatus({ state: 'CONNECTED', ready: true, paired: true });
    if (cfg.presenceUnavailable) {
      try {
        await client.sendPresenceUnavailable();
        log('info', 'presence set to unavailable');
      } catch (err) {
        log('warn', 'could not set presence unavailable', { error: String(err) });
      }
    }
  });
  client.on('change_state', (state) => setStatus({ state: String(state) }));
  client.on('disconnected', (reason) => {
    // LOGOUT = the device was unlinked from the phone: a new QR is needed.
    const loggedOut = String(reason).toUpperCase().includes('LOGOUT');
    setStatus({ state: loggedOut ? 'UNPAIRED' : 'DISCONNECTED', ready: false,
                paired: loggedOut ? false : status.paired });
  });
  // 'message' = incoming messages only ('message_create' would add our own, which we never send).
  client.on('message', (msg) => {
    for (const h of messageHandlers) h(msg);
  });

  const inviteCodes = new Map(); // channel id -> invite code, learned from chat listings

  function chatInfo(chat) {
    const id = chat?.id?._serialized;
    const code = inviteCodeOf(chat);
    if (id && code) inviteCodes.set(id, code);
    return { inviteCode: code ?? (id ? inviteCodes.get(id) ?? null : null) };
  }

  return {
    status: () => status,
    async listChats() {
      const all = await listAllChats(client, log);
      for (const c of all) chatInfo(c);
      return all;
    },
    async getChat(chatId) {
      return (await client.getChatById(chatId)) ?? null;
    },
    chatInfo,
    // For webhook messages: invite code known from earlier listings, if any.
    chatInfoById: (chatId) => ({ inviteCode: inviteCodes.get(chatId) ?? null }),
    onMessage: (h) => messageHandlers.push(h),
    onState: (h) => stateHandlers.push(h),
    start: () => client.initialize(),
    stop: () => client.destroy(),
  };
}
