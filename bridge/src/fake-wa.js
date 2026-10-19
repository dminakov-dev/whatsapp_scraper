// Fake adapter: serves a session fixture instead of WhatsApp Web. Used by the tests and by
// `npm run fake` (an end-to-end run of the collector without a linked phone).
//
// Fixture format (also what the recorder writes from a live session):
//   { "state": "CONNECTED",
//     "chats": [ <whatsapp-web.js Chat / Channel fields> ],
//     "messages": { "<chat id>": [ <whatsapp-web.js Message fields>, ... ] } }
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { inviteCodeOf } from './invite.js';
import { createSessionGuard, PAGE_DETACHED } from './recovery.js';

// What puppeteer throws once the page under whatsapp-web.js is gone (seen live 2026-10-19).
export const detachedFrameError = () =>
  new Error("Attempted to use detached Frame 'FAKE0000'.");

export function loadFixture(dirOrFile) {
  const file = dirOrFile.endsWith('.json') ? dirOrFile : path.join(dirOrFile, 'session.json');
  return JSON.parse(readFileSync(file, 'utf8'));
}

// Same semantics as whatsapp-web.js Chat.fetchMessages: the latest `limit` messages;
// sorted ascending only when it had to cut, otherwise in the order it holds them.
export function fakeFetch(stored, limit) {
  const msgs = stored.filter((m) => !m.isNotification);
  if (limit > 0 && msgs.length > limit) {
    const sorted = [...msgs].sort((a, b) => (a.timestamp > b.timestamp ? 1 : -1));
    return sorted.slice(sorted.length - limit);
  }
  return [...msgs];
}

/**
 * @param {object} fixture
 * @param {object} [opts]
 * @param {object} [opts.recovery]  createSessionGuard overrides (delays, setTimer, log, ...)
 */
export function createFakeWa(fixture, { recovery = {} } = {}) {
  let status = {
    state: fixture.state || 'CONNECTED',
    ready: (fixture.state || 'CONNECTED') === 'CONNECTED',
    paired: !['UNPAIRED', 'UNPAIRED_IDLE'].includes(fixture.state),
  };
  const messageHandlers = [];
  const stateHandlers = [];
  const calls = { fetch: [], listChats: 0 };
  let delayMs = 0;
  let failNext = null;
  let pageGone = false;          // simulated detached page: every page call throws
  let restartFailures = 0;       // how many restarts fail before one succeeds
  calls.restarts = 0;

  function setState(patch) {
    const before = status.state;
    status = { ...status, ...patch };
    if (status.state !== before) for (const h of stateHandlers) h(status.state);
  }

  // Same guard as the real adapter; "restart" here repairs the simulated page.
  const guard = createSessionGuard({
    ...recovery,
    onDetached: () => setState({ state: PAGE_DETACHED, ready: false }),
    restart: async () => {
      calls.restarts += 1;
      if (restartFailures > 0) {
        restartFailures -= 1;
        throw new Error('fake initialize failed');
      }
      pageGone = false;
      guard.ready();                                   // like the client's 'ready' event
      setState({ state: 'CONNECTED', ready: true });
    },
  });

  const byId = new Map(fixture.chats.map((c) => [c.id._serialized, c]));

  function makeChat(raw) {
    const id = raw.id._serialized;
    return {
      ...raw,
      async fetchMessages({ limit } = {}) {
        calls.fetch.push({ chatId: id, limit });
        if (pageGone) throw detachedFrameError();
        if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
        if (failNext) {
          const err = failNext;
          failNext = null;
          throw err;
        }
        return fakeFetch(fixture.messages[id] || [], limit);
      },
    };
  }

  return {
    status: () => (guard.state().detached && status.paired !== false
      ? { ...status, state: PAGE_DETACHED, ready: false }
      : status),
    async probe() {
      if (!pageGone) return 'ok';
      return guard.report(detachedFrameError()) ? 'detached' : 'unresponsive';
    },
    reportError: (err) => guard.report(err),
    async listChats() {
      calls.listChats += 1;
      if (pageGone) throw detachedFrameError();
      return fixture.chats.map(makeChat);
    },
    async getChat(chatId) {
      const raw = byId.get(chatId);
      return raw ? makeChat(raw) : null;
    },
    chatInfo: (chat) => ({ inviteCode: inviteCodeOf(chat) }),
    chatInfoById: (chatId) => ({ inviteCode: inviteCodeOf(byId.get(chatId)) }),
    onMessage: (h) => messageHandlers.push(h),
    onState: (h) => stateHandlers.push(h),
    start: async () => {},
    stop: async () => { guard.stop(); },

    // --- test controls ---------------------------------------------------------------
    calls,
    setSessionState(patch) { setState(patch); },
    /** Simulate the page detaching silently (state stays CONNECTED, like live). */
    breakPage({ failRestarts = 0 } = {}) {
      pageGone = true;
      restartFailures = failRestarts;
    },
    guardState: () => guard.state(),
    setDelay(ms) { delayMs = ms; },
    failNextFetch(err) { failNext = err; },
    findMessage(id) {
      for (const msgs of Object.values(fixture.messages)) {
        const m = msgs.find((x) => x.id._serialized === id);
        if (m) return m;
      }
      return null;
    },
    // Deliver a message the way whatsapp-web.js fires 'message'; resolves when handlers did.
    async emit(msg) {
      await Promise.all(messageHandlers.map((h) => h(msg)));
    },
  };
}
