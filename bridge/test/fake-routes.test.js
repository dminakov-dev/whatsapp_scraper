// The /_fake/* test controls must exist only in fake mode. With the real adapter config they
// are not mounted at all and answer 404, like any path that doesn't exist.
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

import { createBridge } from '../src/app.js';
import { loadConfig } from '../src/config.js';
import { createFakeWa, loadFixture } from '../src/fake-wa.js';
import { createWa } from '../src/wa.js';
import { HANDWRITTEN, recordingWebhook, TOKEN } from './helpers.js';

const FAKE_ROUTES = ['/_fake/state', '/_fake/emit'];

async function serve(cfg, wa) {
  const { server } = createBridge(cfg, wa, { webhook: recordingWebhook() });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = async (method, p, body) => {
    const resp = await fetch(base + p, {
      method, headers: { 'X-Bridge-Token': TOKEN, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: resp.status, body: await resp.json() };
  };
  return { call, close: () => new Promise((resolve) => server.close(resolve)) };
}

test('real adapter config (no BRIDGE_FAKE_FIXTURES): /_fake/* is 404 for any method', async () => {
  const cfg = loadConfig({ BRIDGE_TOKEN: TOKEN,
                           WWEBJS_AUTH_DIR: mkdtempSync(path.join(os.tmpdir(), 'wa-auth-')) });
  assert.equal(cfg.fakeFixtures, '');
  // The real whatsapp-web.js adapter; never started, so no browser is launched.
  const wa = createWa(cfg);
  const s = await serve(cfg, wa);
  try {
    for (const p of FAKE_ROUTES) {
      for (const method of ['POST', 'GET']) {
        const r = await s.call(method, p, method === 'POST' ? { state: 'CONNECTED', ready: true } : undefined);
        assert.equal(r.status, 404, `${method} ${p}`);
        assert.deepEqual(r.body, { error: 'no such endpoint' });
      }
    }
    // Nothing was changed through the missing control.
    assert.equal(wa.status().state, 'STARTING');
  } finally {
    await s.close();
  }
});

test('fake adapter without fake-mode config: still not mounted (the config is the gate)', async () => {
  const cfg = loadConfig({ BRIDGE_TOKEN: TOKEN });
  const s = await serve(cfg, createFakeWa(loadFixture(HANDWRITTEN)));
  try {
    for (const p of FAKE_ROUTES) assert.equal((await s.call('POST', p, {})).status, 404, p);
  } finally {
    await s.close();
  }
});

test('fake mode: /_fake/* is mounted (control case)', async () => {
  const cfg = loadConfig({ BRIDGE_TOKEN: TOKEN, BRIDGE_FAKE_FIXTURES: HANDWRITTEN });
  const s = await serve(cfg, createFakeWa(loadFixture(HANDWRITTEN)));
  try {
    const r = await s.call('POST', '/_fake/state', { state: 'OPENING', ready: false });
    assert.equal(r.status, 200);
    assert.equal(r.body.state, 'OPENING');
    assert.equal((await s.call('POST', '/_fake/emit', { id: 'nope' })).status, 404);
    // Known path, wrong method: 405 (not 404).
    assert.equal((await s.call('POST', '/chats', {})).status, 405);
  } finally {
    await s.close();
  }
});
