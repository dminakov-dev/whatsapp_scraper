import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { createBridge } from '../src/app.js';
import { loadConfig } from '../src/config.js';
import { createFakeWa, loadFixture } from '../src/fake-wa.js';

export const HANDWRITTEN = path.join(path.dirname(fileURLToPath(import.meta.url)), '..',
                                     'fixtures', 'handwritten');
export const TOKEN = 'test-token';

// A webhook stub that records what the bridge would POST.
export function recordingWebhook() {
  const sent = [];
  return {
    sent,
    message: async (message) => { sent.push({ event: 'message', message }); return { delivered: true }; },
    state: async (state) => { sent.push({ event: 'state', state }); return { delivered: true }; },
  };
}

export async function startBridge({ fixture = loadFixture(HANDWRITTEN), env = {}, webhook,
                                    now, waOptions } = {}) {
  const cfg = loadConfig({ BRIDGE_TOKEN: TOKEN, BRIDGE_PORT: '1', BRIDGE_FAKE_FIXTURES: 'x', ...env });
  const wa = createFakeWa(structuredClone(fixture), waOptions);
  const hook = webhook ?? recordingWebhook();
  const { server, forward } = createBridge(cfg, wa, { webhook: hook, now });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const get = async (p, { token = TOKEN } = {}) => {
    const resp = await fetch(base + p, { headers: token ? { 'X-Bridge-Token': token } : {} });
    return { status: resp.status, headers: resp.headers, body: await resp.json() };
  };
  return {
    base, wa, cfg, webhook: hook, forward, get,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}

export const enc = encodeURIComponent;
