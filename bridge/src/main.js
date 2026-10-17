// Entry point: `npm start` (real WhatsApp session) or `npm run fake` (fixtures, no phone).
import { createBridge } from './app.js';
import { loadConfig } from './config.js';
import { createWebhook } from './webhook.js';

// JSON lines on stdout. Message text is never logged: ids, counts and states only.
function log(level, msg, extra = {}) {
  console.log(JSON.stringify({ ts: new Date().toISOString(), level, msg, ...extra }));
}

async function main() {
  const cfg = loadConfig();

  let wa;
  if (cfg.fakeFixtures) {
    const { createFakeWa, loadFixture } = await import('./fake-wa.js');
    wa = createFakeWa(loadFixture(cfg.fakeFixtures));
    log('warn', 'FAKE MODE: serving fixtures, no WhatsApp session', { fixtures: cfg.fakeFixtures });
  } else {
    // Imported lazily so fake mode and tests never load puppeteer.
    const { createWa } = await import('./wa.js');
    wa = createWa(cfg, { log });
  }

  let recorder = null;
  if (cfg.recordDir) {
    const { createRecorder } = await import('./record.js');
    recorder = createRecorder(cfg.recordDir, { log });
    log('warn', 'RECORDING raw responses (private data: review before committing)',
        { dir: cfg.recordDir });
  }

  const webhook = createWebhook({
    url: cfg.webhookUrl, token: cfg.token, attempts: cfg.webhookAttempts,
    backoffMs: cfg.webhookBackoffMs, timeoutMs: cfg.webhookTimeoutMs, log,
  });
  const { server } = createBridge(cfg, wa, { log, webhook, recorder });

  if (!cfg.token) log('warn', 'BRIDGE_TOKEN is empty: endpoints are not authenticated');
  server.listen(cfg.port, cfg.host, () => {
    log('info', 'bridge listening', { url: `http://${cfg.host}:${cfg.port}`, webhook: cfg.webhookUrl });
  });

  const shutdown = async (signal) => {
    log('info', 'shutting down', { signal });
    server.close();
    try {
      await wa.stop();
    } catch (err) {
      log('warn', 'client stop failed', { error: String(err) });
    }
    process.exit(0);
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  // A WhatsApp Web hiccup must not kill the process; requests answer 503 instead.
  process.on('unhandledRejection', (err) => log('error', 'unhandled rejection', { error: String(err) }));

  await wa.start();
}

main().catch((err) => {
  log('error', 'fatal', { error: String(err?.stack || err) });
  process.exit(1);
});
