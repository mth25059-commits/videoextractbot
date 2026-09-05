/**
 * paysvc — the UPI top-up service, wrapped around `upi-fampay-gateway`.
 *
 *     cd paysvc && npm install && node server.js
 *
 * It does three things the bot cannot do itself: quote each order an amount no
 * other live order has, watch the bank's alert mail over IMAP, and say "that
 * credit was order X". The bot keeps the money record; this keeps the matching.
 *
 *   bot ──POST /order──►  unique amount + QR matrix ──► the user pays
 *                                                            │
 *                                    bank alert mail ◄────────┘
 *                                          │
 *   bot ◄──POST /paid────  gateway matched it  (or the bot asks: POST /check)
 *
 * **It binds to 127.0.0.1 and must stay there.** Anything that can reach this
 * port can quote amounts and read order state; nothing here authenticates a
 * *user*, only the bot, with a shared secret. Do not put it behind nginx.
 *
 * Two doors on purpose. The push (`/paid` → the bot) is what makes credits appear
 * without the user pressing anything. The pull (`/check`) is what makes it
 * correct anyway: if the bot was restarting when the money landed, the callback
 * is lost, and `/check` — from the "I have paid" button or the bot's boot-time
 * reconcile — finds the settled order in the journal and credits it then.
 */

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

import { FileStore } from './store.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

/**
 * Read .env files into process.env, and do it BEFORE the gateway is imported —
 * its config.js reads `process.env` at module load, so a static `import` at the
 * top of this file would run first and see nothing. Hence the dynamic import
 * further down.
 *
 * `paysvc/.env` is read first and wins, so the service can be moved to its own
 * box; normally everything lives in the bot's root `.env` and there is only one
 * file to fill in, and one place a mistyped PAYSVC_SECRET can hide.
 *
 * A ` #` after a value ends it. Without that, `IMAP_APP_PASSWORD=  # not yet`
 * reads as the string "# not yet", which is truthy — so `autoConfirmEnabled`
 * would come out true and the poller would start with no password to log in
 * with. Half-configured is the dangerous state; a placeholder must read as blank.
 * A `#` with no space before it is kept, because passwords contain them.
 */
function loadEnv(file) {
  if (!fs.existsSync(file)) return;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1);
    const bare = value.trim();
    if ((bare.startsWith('"') && bare.endsWith('"'))
        || (bare.startsWith("'") && bare.endsWith("'"))) {
      value = bare.slice(1, -1);
    } else {
      // Tested before trimming: once the leading spaces are gone, `KEY=   # note`
      // looks like a value that simply begins with '#'.
      const cut = value.search(/(^|\s)#/);
      value = (cut === -1 ? value : value.slice(0, cut)).trim();
    }
    if (process.env[key] === undefined) process.env[key] = value;
  }
}

loadEnv(path.join(HERE, '.env'));
loadEnv(path.join(ROOT, '.env'));

/* --------------------------------- config -------------------------------- */

const SECRET = (process.env.PAYSVC_SECRET || '').trim();
const PORT = Number(new URL(process.env.PAYSVC_URL || 'http://127.0.0.1:4400').port || 4400);
const HOST = '127.0.0.1';
const BOT_CALLBACK_PORT = Number(process.env.PAID_CALLBACK_PORT || 8081);
const JOURNAL = process.env.PAYSVC_JOURNAL
  || path.join(HERE, 'data', 'orders.json');

if (!SECRET) {
  console.error('\n  ✖  PAYSVC_SECRET is not set.\n'
    + '     Generate one and put it in the bot\'s .env (both sides read the same file):\n'
    + '       python -c "import secrets;print(secrets.token_urlsafe(32))"\n');
  process.exit(2);
}
if (SECRET.length < 16) {
  console.error('\n  ✖  PAYSVC_SECRET is too short to be worth having. Use 32+ characters.\n');
  process.exit(2);
}

// The gateway reads process.env at import time, which is why this is not a
// top-level static import. Everything above had to run first.
const { createGateway, config, autoConfirmEnabled, qrMatrix, displayAmount } =
  await import('upi-fampay-gateway');

const store = new FileStore(JOURNAL);

/* ------------------------- telling the bot about it ---------------------- */

/**
 * POST the settlement to the bot. The bot is the only thing that can move
 * credits, so this call is the whole point of the push door.
 *
 * Retried with a widening gap because the usual reason it fails is a bot that is
 * restarting, which is over in seconds. If every attempt fails the order stays
 * `paid` in the journal and the bot picks it up from `/check` — so this giving up
 * costs the user a wait, never their money.
 */
async function tellBot(intent, info) {
  const body = JSON.stringify({
    orderId: intent.id,
    amountPaise: info.amountPaise,
    listedPaise: intent.listedPaise,
    reference: intent.reference,
    bankRef: info.bankRef || '',
    matchedOn: info.matchedOn || '',
    verifiedAt: info.verifiedAt || Date.now(),
  });

  for (const waitMs of [0, 2000, 5000, 15000, 30000, 60000]) {
    if (waitMs) await new Promise((r) => setTimeout(r, waitMs));
    try {
      const res = await fetch(`http://127.0.0.1:${BOT_CALLBACK_PORT}/paid`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-paysvc-secret': SECRET },
        body,
        signal: AbortSignal.timeout(8000),
      });
      if (res.ok) return true;
      console.warn(`[paysvc] bot refused the callback for ${intent.id}: HTTP ${res.status}`);
      if (res.status === 401) return false;    // a wrong secret will not fix itself
    } catch (err) {
      console.warn(`[paysvc] bot unreachable for ${intent.id}: ${err.message}`);
    }
  }
  console.error(`[paysvc] gave up pushing ${intent.id} — the bot will find it on /check`);
  return false;
}

const gateway = createGateway({
  store,
  onPaid: tellBot,
  onSuspicious: (decision) => {
    // A mail that claimed to be the bank and failed the DKIM check. Loud, because
    // the only innocent explanation is the bank changing its mail setup.
    console.error(`[paysvc] REJECTED an unauthentic payment mail: ${decision.reason}`);
  },
});

/* ---------------------------------- http --------------------------------- */

const json = (res, code, obj) => {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
  res.end(body);
};

/** Constant-time, and length-safe: `timingSafeEqual` throws on a length mismatch. */
function authorised(req) {
  const given = Buffer.from(String(req.headers['x-paysvc-secret'] || ''));
  const want = Buffer.from(SECRET);
  return given.length === want.length && crypto.timingSafeEqual(given, want);
}

function readJson(req, limit = 8 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'));
      } catch {
        reject(new Error('body is not JSON'));
      }
    });
    req.on('error', reject);
  });
}

/** The QR as a flat grid, for whoever has to draw it. Python has Pillow, not a
 *  QR encoder — and one encoder in the system is one that cannot disagree with
 *  itself, so the bot renders these cells rather than re-deriving them. */
function qrCells(text) {
  const { size, cells } = qrMatrix(text);
  return { size, cells: cells.join('') };
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${HOST}:${PORT}`);

  if (!authorised(req)) {
    console.warn(`[paysvc] rejected an unauthenticated ${req.method} ${url.pathname}`);
    return json(res, 401, { ok: false, error: 'bad secret' });
  }

  try {
    /* GET /health — is the inbox being watched, and does the QR have a payee? */
    if (url.pathname === '/health') {
      return json(res, 200, {
        ok: true,
        upiConfigured: Boolean(config.upi.id && config.upi.payee),
        autoConfirm: autoConfirmEnabled,
        upiId: config.upi.id || null,
        payee: config.upi.payee || null,
        windowMinutes: Math.round(config.paymentWindowMs / 60000),
        poller: gateway.status(),
        orders: store.counts(),
      });
    }

    if (req.method !== 'POST') return json(res, 405, { ok: false, error: 'POST only' });
    const body = await readJson(req);

    /* POST /order {orderId, rupees} — reserve an amount and hand back the QR. */
    if (url.pathname === '/order') {
      const orderId = String(body.orderId || '').trim();
      const rupees = Number(body.rupees);
      if (!orderId) return json(res, 400, { ok: false, error: 'orderId is required' });
      if (!Number.isFinite(rupees) || rupees <= 0) {
        return json(res, 400, { ok: false, error: 'rupees must be a positive number' });
      }

      const made = gateway.createIntent({ id: orderId, priceRupees: rupees });
      if (!made.ok) return json(res, 400, { ok: false, error: made.error });

      const { intent } = made;
      return json(res, 200, {
        ok: true,
        reused: Boolean(made.reused),
        orderId: intent.id,
        status: intent.status,
        amountPaise: intent.amountPaise,
        listedPaise: intent.listedPaise,
        amountDue: displayAmount(intent.amountPaise),
        upiId: intent.upiId,
        payee: intent.payee,
        upiUri: intent.upiUri,
        reference: intent.reference,
        expiresAt: intent.expiresAt,
        autoConfirm: autoConfirmEnabled,
        qr: qrCells(intent.upiUri),
      });
    }

    /*
     * POST /check {orderId} — "the user says they have paid."
     *
     * Scans the inbox now instead of waiting for the next 15-second tick, then
     * answers with the order's state. `force: true` because the poller skips a
     * scan when nothing is holding, and by the time an impatient user presses
     * this their window may already have closed.
     */
    if (url.pathname === '/check') {
      const orderId = String(body.orderId || '').trim();
      const before = store.get(orderId);
      if (!before) return json(res, 404, { ok: false, error: 'no such order' });

      let scan = { ok: false, reason: 'automatic confirmation is off' };
      if (before.status === 'holding') scan = await gateway.verifyOnce({ force: true });

      const intent = store.get(orderId) || before;
      return json(res, 200, {
        ok: true,
        orderId,
        status: intent.status,
        amountPaise: intent.amountPaise,
        listedPaise: intent.listedPaise,
        amountDue: displayAmount(intent.amountPaise),
        reference: intent.reference,
        expiresAt: intent.expiresAt,
        paidInfo: intent.paidInfo || null,
        autoConfirm: autoConfirmEnabled,
        scan,
      });
    }

    /* POST /cancel {orderId} — release the amount for the next buyer. */
    if (url.pathname === '/cancel') {
      const orderId = String(body.orderId || '').trim();
      const intent = store.get(orderId);
      if (!intent) return json(res, 404, { ok: false, error: 'no such order' });
      if (intent.status === 'paid') {
        // Cancelling something already settled would strand a real payment.
        return json(res, 409, { ok: false, error: 'already paid', status: 'paid' });
      }
      store.cancel(orderId);
      return json(res, 200, { ok: true, orderId, status: store.get(orderId).status });
    }

    return json(res, 404, { ok: false, error: 'no such endpoint' });
  } catch (err) {
    console.error(`[paysvc] ${req.method} ${url.pathname} failed: ${err.stack || err.message}`);
    return json(res, 400, { ok: false, error: err.message });
  }
});

/* --------------------------------- boot ---------------------------------- */

gateway.start();       // a no-op unless IMAP is configured; it says so if not

server.listen(PORT, HOST, () => {
  const counts = store.counts();
  console.log(`\n  paysvc → http://${HOST}:${PORT}   (localhost only — keep it that way)`);
  console.log(`  journal: ${JOURNAL}  (${counts.holding} holding, ${counts.paid} paid)`);
  console.log(`  window:  ${Math.round(config.paymentWindowMs / 60000)} min`
    + `  ·  bot callback: 127.0.0.1:${BOT_CALLBACK_PORT}`);
  if (!config.upi.id || !config.upi.payee) {
    console.log('  ⚠  UPI_ID / UPI_PAYEE_NAME are not set — no QR can be built yet.');
  } else {
    console.log(`  ✓  paying into ${config.upi.id} (${config.upi.payee})`);
  }
  if (!autoConfirmEnabled) {
    console.log('  ⚠  IMAP_USER / IMAP_APP_PASSWORD are not set — orders are quoted but'
      + ' will never settle on their own.\n');
  } else {
    console.log(`  ✓  watching ${config.imap.user} for mail from ${config.imap.sender}\n`);
  }
});

server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`\n  ✖  port ${PORT} is already in use — paysvc may already be running.\n`);
    process.exit(2);
  }
  throw err;
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    console.log('\n[paysvc] stopping…');
    gateway.stop();
    server.close(() => process.exit(0));
    // A held-open keep-alive socket must not stop this from ever exiting.
    setTimeout(() => process.exit(0), 3000).unref();
  });
}
