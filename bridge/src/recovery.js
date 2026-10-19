// Session guard: notices when the WhatsApp Web page under whatsapp-web.js is gone, makes the
// bridge say so honestly, and re-initializes the client a few times with growing pauses.
//
// Why: on 2026-10-19 the page detached (likely after the Mac slept). whatsapp-web.js fired no
// event, its state stayed CONNECTED, /health said ok, and every read answered 503 for ~9 hours,
// which the collector treats as per-chat errors instead of pausing the whole loop.
//
// No WhatsApp code here, so both adapters (wa.js, fake-wa.js) share it and tests drive it.

export const PAGE_DETACHED = 'PAGE_DETACHED';

// Puppeteer / CDP errors that mean the page or the browser is gone (not a per-chat problem).
const SESSION_GONE = new RegExp([
  'detached Frame',
  'Target closed',
  'Session closed',
  'Execution context was destroyed',
  'Protocol error.*(Target|Session)',
  'Connection closed',
  'browser has disconnected',
].join('|'), 'i');

export function isSessionGone(err) {
  return SESSION_GONE.test(String(err?.message ?? err));
}

const firstLine = (err) => String(err?.message ?? err).split('\n')[0].slice(0, 200);

/**
 * @param {object} p
 * @param {() => Promise<void>} p.restart     destroy + initialize the client (session files kept)
 * @param {() => void} p.onDetached           mark the adapter state PAGE_DETACHED, ready=false
 * @param {Function} [p.log]
 * @param {number[]} [p.delays]               pause before each attempt; its length = max attempts
 * @param {Function} [p.setTimer] [p.clearTimer]  injectable for tests
 */
export function createSessionGuard({ restart, onDetached, log = () => {},
                                     delays = [30_000, 120_000, 600_000],
                                     setTimer = setTimeout, clearTimer = clearTimeout }) {
  let detached = false;
  let attempts = 0;
  let timer = null;
  let gaveUp = false;
  let stopped = false;

  function schedule() {
    if (stopped || !detached) return;
    if (attempts >= delays.length) {
      gaveUp = true;
      log('error', 'session lost; automatic recovery gave up, restart the bridge manually',
          { attempts });
      return;
    }
    const delayMs = delays[attempts];
    attempts += 1;
    log('warn', 'session recovery scheduled', { attempt: attempts, of: delays.length,
                                                in_ms: delayMs });
    timer = setTimer(async () => {
      timer = null;
      if (stopped || !detached) return;
      log('info', 'restarting WhatsApp client', { attempt: attempts });
      try {
        await restart();
      } catch (err) {
        log('warn', 'client restart failed', { attempt: attempts, error: firstLine(err) });
      }
      // Success is confirmed only by ready(); until then, try again later.
      if (detached) schedule();
    }, delayMs);
  }

  function detach(reason) {
    if (stopped || detached) return;
    detached = true;
    log('warn', 'WhatsApp Web page lost', { reason });
    onDetached();
    schedule();
  }

  return {
    /** An operation failed: if the page/browser is gone, switch to recovery. Returns true then. */
    report(err) {
      if (!isSessionGone(err)) return false;
      detach(firstLine(err));
      return true;
    },
    detach,
    /** The client fired 'ready': recovery (if any) succeeded. */
    ready() {
      if (timer) clearTimer(timer);
      timer = null;
      if (detached) log('info', 'session recovered', { attempts });
      detached = false;
      attempts = 0;
      gaveUp = false;
    },
    stop() {
      stopped = true;
      if (timer) clearTimer(timer);
      timer = null;
    },
    state: () => ({ detached, attempts, gaveUp }),
  };
}
