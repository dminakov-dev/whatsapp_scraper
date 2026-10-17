// Bridge settings from the environment (.env is loaded by `node --env-file-if-exists`).
import { CONTENT_TYPES, isCollectable } from './filter.js';

function num(env, name, def, { min = -Infinity, max = Infinity } = {}) {
  const raw = env[name];
  if (raw === undefined || raw === '') return def;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < min || value > max) {
    throw new Error(`${name} must be a number in [${min}, ${max}], got ${JSON.stringify(raw)}`);
  }
  return value;
}

// CONTENT_TYPES=chat,image,... replaces the default allowlist (rarely needed).
function typeList(env, name, def) {
  const raw = env[name];
  if (raw === undefined || raw.trim() === '') return [...def];
  const types = raw.split(',').map((t) => t.trim()).filter(Boolean);
  if (!types.length) throw new Error(`${name} must list at least one type`);
  return types;
}

// EXTRA_TYPES_BY_CHAT={"<group or channel id>": ["location"], ...}: extra types for single
// chats only. Direct chats can't be listed: nothing from them ever leaves the bridge.
function extraTypes(env, name) {
  const raw = env[name];
  if (raw === undefined || raw.trim() === '') return {};
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error(`${name} must be a JSON object, e.g. {"1203...@g.us": ["location"]}`);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${name} must be a JSON object of chat id -> array of types`);
  }
  for (const [chatId, types] of Object.entries(parsed)) {
    if (!isCollectable(chatId)) throw new Error(`${name}: ${chatId} is not a group or channel id`);
    if (!Array.isArray(types) || !types.every((t) => typeof t === 'string' && t)) {
      throw new Error(`${name}: types for ${chatId} must be an array of strings`);
    }
  }
  return parsed;
}

function bool(env, name, def) {
  const raw = env[name];
  if (raw === undefined || raw === '') return def;
  return ['1', 'true', 'yes', 'on'].includes(String(raw).toLowerCase());
}

export function loadConfig(env = process.env) {
  const host = env.BRIDGE_HOST || '127.0.0.1';
  if (!['127.0.0.1', '::1', 'localhost'].includes(host)) {
    // The bridge holds a logged-in WhatsApp session: never expose it beyond this machine.
    throw new Error(`BRIDGE_HOST must be a loopback address, got ${host}`);
  }
  return {
    host,
    port: num(env, 'BRIDGE_PORT', 8002, { min: 1, max: 65535 }),
    token: env.BRIDGE_TOKEN || '',
    webhookUrl: env.WEBHOOK_URL || 'http://127.0.0.1:8000/webhook',
    // Retry policy for a webhook answered with 500 (or not delivered at all).
    webhookAttempts: num(env, 'WEBHOOK_ATTEMPTS', 4, { min: 1, max: 10 }),
    webhookBackoffMs: num(env, 'WEBHOOK_BACKOFF_MS', 1000, { min: 0 }),
    webhookTimeoutMs: num(env, 'WEBHOOK_TIMEOUT_MS', 5000, { min: 100 }),
    // History window: start with max(2*limit, HISTORY_MIN_FETCH) messages and double while
    // the window doesn't reach `since`, up to HISTORY_MAX_FETCH.
    historyMinFetch: num(env, 'HISTORY_MIN_FETCH', 50, { min: 1, max: 5000 }),
    historyMaxFetch: num(env, 'HISTORY_MAX_FETCH', 500, { min: 1, max: 5000 }),
    // Bridge-side pacing guard: history calls closer than this get 429 + Retry-After.
    historyMinIntervalS: num(env, 'HISTORY_MIN_INTERVAL', 0, { min: 0 }),
    // A WhatsApp Web operation slower than this answers 504 (collector retries the target).
    // Keep it below the collector's REQUEST_TIMEOUT so the bridge answers first.
    opTimeoutMs: num(env, 'OP_TIMEOUT_MS', 8000, { min: 100 }),
    presenceUnavailable: bool(env, 'PRESENCE_UNAVAILABLE', true),
    // Content-type allowlist: base types for every chat, plus per-chat extras (empty).
    contentTypes: typeList(env, 'CONTENT_TYPES', CONTENT_TYPES),
    extraTypesByChat: extraTypes(env, 'EXTRA_TYPES_BY_CHAT'),
    authDir: env.WWEBJS_AUTH_DIR || '.wwebjs_auth',
    fakeFixtures: env.BRIDGE_FAKE_FIXTURES || '',
    recordDir: env.RECORD_DIR || '',
  };
}
