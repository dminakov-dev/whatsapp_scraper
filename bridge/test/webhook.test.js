import assert from 'node:assert/strict';
import http from 'node:http';
import { test } from 'node:test';

import { backoffMs, createWebhook } from '../src/webhook.js';

// A real HTTP receiver answering with a scripted sequence of status codes.
async function receiver(statuses) {
  const hits = [];
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    hits.push({ token: req.headers['x-bridge-token'], body: JSON.parse(Buffer.concat(chunks)) });
    res.writeHead(statuses[Math.min(hits.length - 1, statuses.length - 1)]);
    res.end('{}');
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { hits, url: `http://127.0.0.1:${server.address().port}/webhook`,
           close: () => new Promise((r) => server.close(r)) };
}

const noSleep = { sleep: async () => {}, random: () => 0.5 };

test('500 is retried with backoff until 200; token sent every time', async () => {
  const rx = await receiver([500, 500, 200]);
  const waits = [];
  const hook = createWebhook({ url: rx.url, token: 'tkn', attempts: 4, backoffMs: 100,
                               sleep: async (ms) => { waits.push(ms); }, random: () => 0.5 });
  const res = await hook.message({ id: 'x' });
  await rx.close();
  assert.deepEqual(res, { delivered: true, status: 200, attempts: 3 });
  assert.equal(rx.hits.length, 3);
  assert.ok(rx.hits.every((h) => h.token === 'tkn'));
  assert.deepEqual(rx.hits[0].body, { event: 'message', message: { id: 'x' } });
  assert.deepEqual(waits, [100, 200]);
});

test('200 (also "ignored") and 401 are final: no retry', async () => {
  for (const status of [200, 401, 400]) {
    const rx = await receiver([status]);
    const res = await createWebhook({ url: rx.url, ...noSleep }).state('CONNECTED');
    await rx.close();
    assert.equal(rx.hits.length, 1, String(status));
    assert.equal(res.status, status);
  }
});

test('gives up after the last attempt: the event is lost, not thrown', async () => {
  const rx = await receiver([500]);
  const res = await createWebhook({ url: rx.url, attempts: 3, ...noSleep }).message({ id: 'y' });
  await rx.close();
  assert.equal(rx.hits.length, 3);
  assert.equal(res.delivered, false);
});

test('unreachable collector is retried, then given up', async () => {
  const res = await createWebhook({ url: 'http://127.0.0.1:9/webhook', attempts: 2, timeoutMs: 500,
                                    ...noSleep }).message({ id: 'z' });
  assert.equal(res.delivered, false);
  assert.equal(res.attempts, 2);
});

test('backoff doubles with jitter in [0.5, 1.5)', () => {
  assert.equal(backoffMs(1000, 1, () => 0.5), 1000);
  assert.equal(backoffMs(1000, 3, () => 0.5), 4000);
  assert.equal(backoffMs(1000, 2, () => 0), 1000);
});
