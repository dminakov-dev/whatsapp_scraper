// Real adapter: whatsapp-web.js. The only file that knows the library.
//
// Read-only by construction: this file (like all of src/) never calls sendMessage, sendSeen,
// reactions, joins or anything else that writes to WhatsApp; test/readonly.test.js enforces
// it. The one outgoing signal is sendPresenceUnavailable() (PRESENCE_UNAVAILABLE=1), which
// tells WhatsApp the linked device is NOT online.
import qrcode from 'qrcode-terminal';
import wwebjs from 'whatsapp-web.js';

import { inviteCodeOf } from './invite.js';
import { createSessionGuard, PAGE_DETACHED } from './recovery.js';

const { Client, LocalAuth } = wwebjs;

// Prefix of the console.warn lines our whatsapp-web.js patch (patches/) emits inside the
// page whenever an id had to be resolved by a fallback, or couldn't be resolved at all.
export const ID_FALLBACK_PREFIX = '[wa-bridge:id-fallback]';

/**
 * Forward the patch's page-side warnings to the bridge log at warn level, so we notice when
 * WhatsApp renames internals again. The same warning repeats per message, so within
 * `windowMs` repeats are only counted and reported with the next line for that key.
 */
export function watchPageConsole(page, log, { now = Date.now, windowMs = 60_000 } = {}) {
  const seen = new Map(); // key -> { last, suppressed }
  page.on('console', (message) => {
    const text = typeof message?.text === 'function' ? message.text() : String(message);
    if (!text.startsWith(ID_FALLBACK_PREFIX)) return;
    const detail = text.slice(ID_FALLBACK_PREFIX.length).trim();
    const t = now();
    const entry = seen.get(detail);
    if (entry && t - entry.last < windowMs) {
      entry.suppressed += 1;
      return;
    }
    log('warn', 'whatsapp-web.js id fallback', {
      detail, ...(entry?.suppressed ? { repeated_since_last: entry.suppressed } : {}),
    });
    seen.set(detail, { last: t, suppressed: 0 });
  });
}

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

  // Page lost -> PAGE_DETACHED (/health 503) and destroy + initialize with growing pauses.
  // destroy() closes only the browser: the session in .wwebjs_auth/ stays, so no new QR.
  const guard = createSessionGuard({
    log,
    onDetached: () => setStatus({ state: PAGE_DETACHED, ready: false }),
    restart: async () => {
      pageWatched = null;
      try {
        await client.destroy();
      } catch (err) {
        log('warn', 'client destroy failed (continuing)', { error: String(err?.message ?? err) });
      }
      await client.initialize();
    },
  });

  // Per page: forward the patch's console warnings, and notice the page or browser going away
  // even when no request is running. A restart creates a new page, so this runs again.
  let pageWatched = null;
  function watchPage() {
    const page = client.pupPage;
    if (!page || pageWatched === page) return;
    pageWatched = page;
    watchPageConsole(page, log);
    page.on('close', () => guard.detach('page closed'));
    client.pupBrowser?.on('disconnected', () => guard.detach('browser disconnected'));
  }

  client.on('qr', (qr) => {
    setStatus({ state: 'UNPAIRED', ready: false, paired: false });
    // eslint-disable-next-line no-console
    console.log('\nScan with the phone: WhatsApp > Linked devices > Link a device\n');
    qrcode.generate(qr, { small: true });
  });
  client.on('authenticated', () => {
    watchPage();
    setStatus({ state: 'AUTHENTICATED', paired: true });
  });
  client.on('auth_failure', (msg) => {
    log('error', 'auth failure', { msg: String(msg) });
    setStatus({ state: 'UNPAIRED', ready: false, paired: false });
  });
  client.on('ready', async () => {
    watchPage();
    guard.ready();
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
    // While recovering, intermediate states from destroy()/initialize() (DISCONNECTED,
    // AUTHENTICATED) must not look like an auth problem: report PAGE_DETACHED until 'ready'.
    // An unlinked device (paired=false) still wins, so it shows as 401 / UNPAIRED.
    status: () => (guard.state().detached && status.paired !== false
      ? { ...status, state: PAGE_DETACHED, ready: false }
      : status),
    /**
     * Cheap liveness check of the page for /health (no network, no WhatsApp request):
     * 'ok' | 'detached' (page gone, recovery started) | 'unresponsive' (no answer in time).
     */
    async probe(timeoutMs = 2000) {
      const page = client.pupPage;
      if (!page) {
        guard.detach('no page');
        return 'detached';
      }
      let timer;
      try {
        await Promise.race([
          page.evaluate(() => 1),
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error('probe timeout')), timeoutMs);
          }),
        ]);
        return 'ok';
      } catch (err) {
        return guard.report(err) ? 'detached' : 'unresponsive';
      } finally {
        clearTimeout(timer);
      }
    },
    /** A WhatsApp operation failed; a gone page switches the bridge to recovery. */
    reportError: (err) => guard.report(err),
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
    stop: () => {
      guard.stop();               // an intentional shutdown is not a lost session
      return client.destroy();
    },
  };
}
