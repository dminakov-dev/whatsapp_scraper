// Push events to the collector's POST /webhook.
// 500 (any 5xx) = the collector's storage failed: retry with backoff, safe because inserts
// are deduped. Any other answer (200, 401, ...) is final. If the collector stays unreachable
// the event is lost here and backfill gap-fill recovers it from history later.

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function backoffMs(base, attempt, random = Math.random) {
  // attempt 1 -> base, 2 -> 2*base, 3 -> 4*base ... with +-50% jitter
  return Math.round(base * 2 ** (attempt - 1) * (0.5 + random()));
}

export function createWebhook({ url, token = '', attempts = 4, backoffMs: base = 1000,
                                timeoutMs = 5000, log = () => {}, fetchImpl = fetch,
                                sleep = defaultSleep, random = Math.random }) {
  async function post(body) {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['X-Bridge-Token'] = token;
    const payload = JSON.stringify(body);
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      let outcome;
      try {
        const resp = await fetchImpl(url, {
          method: 'POST', headers, body: payload, signal: AbortSignal.timeout(timeoutMs),
        });
        // Drain the body so the connection can be reused.
        await resp.arrayBuffer().catch(() => {});
        if (resp.status < 500) {
          if (resp.status >= 400) log('warn', 'webhook rejected', { status: resp.status });
          return { delivered: resp.status < 300, status: resp.status, attempts: attempt };
        }
        outcome = `status ${resp.status}`;
      } catch (err) {
        outcome = err?.name || 'network error';
      }
      if (attempt < attempts) {
        const wait = backoffMs(base, attempt, random);
        log('warn', 'webhook failed, retrying', { outcome, attempt, wait_ms: wait });
        await sleep(wait);
      } else {
        log('error', 'webhook failed, event lost (backfill will recover it)', { outcome, attempt });
        return { delivered: false, status: null, attempts: attempt, outcome };
      }
    }
    return { delivered: false, status: null, attempts };
  }

  return {
    post,
    message: (message) => post({ event: 'message', message }),
    state: (state) => post({ event: 'state', state }),
  };
}
