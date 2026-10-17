// Wiring: adapter events -> filter -> webhook, plus the HTTP server. No I/O setup here
// (that is main.js), so tests build the whole bridge around a fake adapter.
import { chatIdOf, createAllowlist, toContract } from './filter.js';
import { createServer } from './server.js';

export function createBridge(cfg, wa, { log = () => {}, webhook, recorder = null, now } = {}) {
  const stats = { forwarded: 0, dropped: {} };
  // One allowlist for both paths, so webhook and history forward exactly the same messages.
  const allow = createAllowlist({ base: cfg.contentTypes, extraByChat: cfg.extraTypesByChat });

  // One incoming WhatsApp message -> webhook, if it is collectable content.
  async function forward(msg) {
    try {
      if (recorder) recorder.message(msg);
      const chatId = chatIdOf(msg);
      const res = toContract(msg, wa.chatInfoById(chatId), allow);
      if (!res.message) {
        stats.dropped[res.drop] = (stats.dropped[res.drop] || 0) + 1;
        return { dropped: res.drop };
      }
      stats.forwarded += 1;
      return await webhook.message(res.message);
    } catch (err) {
      // One bad event must never take the bridge down.
      log('error', 'forward failed', { error: String(err?.stack || err) });
      return { error: String(err) };
    }
  }

  wa.onMessage(forward);
  wa.onState((state) => {
    webhook.state(state).catch((err) => log('error', 'state webhook failed', { error: String(err) }));
  });

  const server = createServer(cfg, wa, {
    log,
    now,
    allow,
    onHistory: recorder ? (chatId, chat, raw) => recorder.history(chatId, chat, raw) : undefined,
    onChats: recorder ? (list) => recorder.chats(list) : undefined,
  });

  // Test controls in fake mode only: change the session state, push a fixture message
  // through the live (webhook) path. Same token check as every other endpoint.
  if (cfg.fakeFixtures && wa.findMessage) {
    const readJson = async (req) => {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    };
    server.extraRoutes.set('POST /_fake/state', async (req) => {
      wa.setSessionState(await readJson(req));
      return [200, wa.status()];
    });
    server.extraRoutes.set('POST /_fake/emit', async (req) => {
      const { id } = await readJson(req);
      const msg = wa.findMessage(id);
      if (!msg) return [404, { error: 'no such fixture message' }];
      return [200, (await forward(msg)) ?? {}];
    });
  }

  return { server, forward, stats };
}
