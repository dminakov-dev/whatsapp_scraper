// HTTP side of the bridge contract (see the "Bridge contract" table in CLAUDE.md).
// It talks to WhatsApp only through the adapter interface below, so tests run it against
// fixtures (fake-wa.js) and production against whatsapp-web.js (wa.js).
//
// Adapter interface:
//   status()            -> { state: string, ready: boolean, paired: boolean | null }
//   listChats()         -> Promise<chat[]>   (whatsapp-web.js Chat/Channel shaped objects)
//   getChat(chatId)     -> Promise<chat | null>, chat.fetchMessages({ limit }) -> Promise<msg[]>
//   chatInfo(chat)      -> { inviteCode } extras for toContract
import { createHash, timingSafeEqual } from 'node:crypto';
import http from 'node:http';

import { chatKind, isCollectable, toChatEntry } from './filter.js';
import { historyWindow } from './window.js';

// whatsapp-web.js / WAState values meaning "no usable session: someone must scan a QR".
export const UNPAIRED_STATES = new Set(['UNPAIRED', 'UNPAIRED_IDLE', 'LOGGED_OUT', 'QR']);

export class HttpError extends Error {
  constructor(status, message, headers = {}) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

class TimeoutError extends Error {}

function sha(value) {
  return createHash('sha256').update(String(value)).digest();
}

// Constant-time comparison; hashing first makes both sides the same length.
export function tokenMatches(sent, expected) {
  return timingSafeEqual(sha(sent ?? ''), sha(expected));
}

function intParam(params, name, def, min, max) {
  const raw = params.get(name);
  if (raw === null || raw === '') return def;
  if (!/^\d+$/.test(raw)) throw new HttpError(400, `${name} must be an integer`);
  const value = Number(raw);
  if (value < min || value > max) throw new HttpError(400, `${name} must be in [${min}, ${max}]`);
  return value;
}

/**
 * @param {object} cfg   loadConfig() result
 * @param {object} wa    adapter
 * @param {object} [opts]
 * @param {(level: string, msg: string, extra?: object) => void} [opts.log]
 * @param {() => number} [opts.now]  ms clock, injectable for tests
 * @param {(chatId: string, chat: object, raw: object[]) => void} [opts.onHistory]  recorder hook
 * @param {(chats: object[]) => void} [opts.onChats]  recorder hook
 */
export function createServer(cfg, wa, { log = () => {}, now = Date.now, onHistory, onChats } = {}) {
  let busy = false;          // one WhatsApp Web operation at a time
  let lastHistoryAt = null;  // ms, for HISTORY_MIN_INTERVAL

  // Run one WhatsApp Web operation. While one is in flight the client is "busy" (409):
  // puppeteer can't cancel an evaluate, so after a timeout (504) we stay busy until the
  // operation really ends instead of piling more work onto the page.
  async function withClient(fn) {
    if (busy) throw new HttpError(409, 'client busy');
    busy = true;
    const op = Promise.resolve().then(fn).finally(() => { busy = false; });
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new TimeoutError('operation timed out')), cfg.opTimeoutMs);
    });
    try {
      return await Promise.race([op, timeout]);
    } catch (err) {
      if (err instanceof TimeoutError) {
        op.catch(() => {}); // it may still fail later; nobody is waiting for it
        throw new HttpError(504, 'WhatsApp Web operation timed out');
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function requireUsable() {
    const s = wa.status();
    if (s.paired === false || UNPAIRED_STATES.has(s.state)) {
      throw new HttpError(401, `session not paired (state=${s.state})`);
    }
    if (!s.ready || s.state !== 'CONNECTED') {
      throw new HttpError(409, `client not ready (state=${s.state})`);
    }
  }

  function health() {
    const s = wa.status();
    if (s.paired === false || UNPAIRED_STATES.has(s.state)) {
      return [401, { ok: false, state: 'UNPAIRED' }];
    }
    const ok = Boolean(s.ready) && s.state === 'CONNECTED';
    return [200, { ok, state: s.state }];
  }

  async function chats() {
    requireUsable();
    const list = await withClient(() => wa.listChats());
    if (onChats) onChats(list);
    const seen = new Set();
    const out = [];
    for (const chat of list) {
      const entry = toChatEntry(chat);
      if (entry && !seen.has(entry.chat_id)) {
        seen.add(entry.chat_id);
        out.push(entry);
      }
    }
    return [200, { chats: out }];
  }

  function paceHistory() {
    if (cfg.historyMinIntervalS <= 0) return;
    const t = now();
    if (lastHistoryAt !== null) {
      const waitMs = lastHistoryAt + cfg.historyMinIntervalS * 1000 - t;
      if (waitMs > 0) {
        const retryAfter = String(Math.ceil(waitMs / 1000));
        throw new HttpError(429, 'history requested too often', { 'Retry-After': retryAfter });
      }
    }
    lastHistoryAt = t;
  }

  async function messages(chatId, params) {
    const since = intParam(params, 'since', 0, 0, Number.MAX_SAFE_INTEGER);
    const limit = intParam(params, 'limit', 50, 1, 500);
    if (!isCollectable(chatId)) {
      // Direct chats and status are never listed in /chats, so asking for one is a bug on
      // the collector side: 400 (loop-level pause), never 403 (that would skip a target).
      throw new HttpError(400, `not a group or channel: ${chatKind(chatId)}`);
    }
    requireUsable();
    paceHistory();
    const page = await withClient(async () => {
      const chat = await wa.getChat(chatId);
      if (!chat) throw new HttpError(404, 'chat not found');
      return historyWindow({
        fetchLatest: async (m) => {
          const raw = await chat.fetchMessages({ limit: m });
          if (onHistory) onHistory(chatId, chat, raw);
          return raw;
        },
        since,
        limit,
        minFetch: cfg.historyMinFetch,
        maxFetch: cfg.historyMaxFetch,
        chatInfo: wa.chatInfo(chat),
      });
    });
    log('info', 'history served', { chat_id: chatId, since, limit, returned: page.messages.length,
                                    ...page.stats });
    return [200, { messages: page.messages, oldest_available: page.oldest_available }];
  }

  async function route(req, url) {
    const m = /^\/chats\/([^/]+)\/messages$/.exec(url.pathname);
    const known = url.pathname === '/health' || url.pathname === '/chats' || m;
    // 405 only for a path that exists; any other path is 404 whatever the method, so an
    // unmounted route (e.g. /_fake/* with the real adapter) looks exactly like a missing one.
    if (!known) throw new HttpError(404, 'no such endpoint');
    if (req.method !== 'GET') throw new HttpError(405, 'method not allowed');
    if (url.pathname === '/health') return health();
    if (url.pathname === '/chats') return chats();
    let chatId;
    try {
      chatId = decodeURIComponent(m[1]);
    } catch {
      throw new HttpError(400, 'bad chat id encoding');
    }
    return messages(chatId, url.searchParams);
  }

  // Extra routes (fake mode test controls) can be mounted here; same token check applies.
  const extraRoutes = new Map();

  async function handle(req, res) {
    const started = now();
    let status = 500;
    let body;
    let headers = {};
    const url = new URL(req.url, 'http://127.0.0.1');
    try {
      if (cfg.token && !tokenMatches(req.headers['x-bridge-token'], cfg.token)) {
        throw new HttpError(401, 'bad or missing X-Bridge-Token');
      }
      const extra = extraRoutes.get(`${req.method} ${url.pathname}`);
      if (extra) {
        [status, body] = await extra(req, url);
      } else {
        [status, body] = await route(req, url);
      }
    } catch (err) {
      if (err instanceof HttpError) {
        status = err.status;
        headers = err.headers;
        body = { error: err.message };
      } else {
        // WhatsApp Web / puppeteer failure: transient from the collector's point of view.
        status = 503;
        body = { error: 'bridge error' };
        log('error', 'request failed', { path: url.pathname, error: String(err?.stack || err) });
      }
    }
    res.writeHead(status, { 'Content-Type': 'application/json', ...headers });
    res.end(JSON.stringify(body));
    if (status >= 400) {
      log('warn', 'request answered with error', { method: req.method, path: url.pathname, status,
                                                   error: body?.error, ms: now() - started });
    }
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      log('error', 'unhandled', { error: String(err) });
      if (!res.headersSent) res.writeHead(500);
      res.end();
    });
  });
  server.extraRoutes = extraRoutes;
  return server;
}
