/**
 * The store the gateway plugs into — a JSON journal on disk.
 *
 * The gateway owns no persistence; it asks for six methods (see the contract in
 * `upi-fampay-gateway/src/store.js`) and calls every one of them
 * **synchronously**, inside `createIntent` and the poll loop. That single fact
 * rules out every async database driver, so this is `readFileSync` /
 * `writeFileSync` over one small file rather than SQLite: `better-sqlite3` is a
 * native build, and `node:sqlite` needs Node 22, while the bot's install notes
 * promise Node 18.
 *
 * It has to survive a restart. Two things in here are not recoverable if they are
 * lost:
 *
 *   - a live order's reserved amount. Forget it and the next order can be quoted
 *     the same figure, and then one payment matches two orders.
 *   - the ids of bank mails already acted on. Forget those and the next scan
 *     re-reads the same alert and pays the same order twice.
 *
 * The bot's SQLite `orders` table is still the money record — this file is the
 * gateway's working memory, keyed by the same order id.
 */

import fs from 'node:fs';
import path from 'node:path';

const SEEN_LIMIT = 500;              // matches MemoryStore; a scan looks back hours
const KEEP_CLOSED_MS = 7 * 24 * 3600 * 1000;

export class FileStore {
  constructor(file) {
    this.file = file;
    this.intents = new Map();
    this.seen = [];
    this._load();
  }

  /* ------------------------------- disk ---------------------------------- */

  _load() {
    try {
      const raw = JSON.parse(fs.readFileSync(this.file, 'utf8'));
      for (const intent of raw.intents || []) this.intents.set(intent.id, intent);
      this.seen = Array.isArray(raw.seen) ? raw.seen.slice(-SEEN_LIMIT) : [];
    } catch (err) {
      // A missing file is the first run. Anything else is a corrupt journal, and
      // starting from empty would re-pay old alerts — so say so and stop.
      if (err.code !== 'ENOENT') {
        throw new Error(`paysvc journal at ${this.file} is unreadable: ${err.message}`);
      }
    }
  }

  /**
   * Write via a temp file and `renameSync`, which is atomic on both POSIX and
   * NTFS. A half-written journal is worse than an old one: it reads as corrupt,
   * and the constructor above refuses to start on corrupt.
   */
  _save() {
    this._prune();
    const body = JSON.stringify({
      version: 1,
      savedAt: Date.now(),
      intents: [...this.intents.values()],
      seen: this.seen.slice(-SEEN_LIMIT),
    });
    const tmp = `${this.file}.tmp`;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    fs.writeFileSync(tmp, body, { mode: 0o600 });
    fs.renameSync(tmp, this.file);
  }

  /** Closed orders are history the gateway never looks at; the bot's DB keeps it. */
  _prune() {
    const cutoff = Date.now() - KEEP_CLOSED_MS;
    for (const [id, intent] of this.intents) {
      if (intent.status !== 'holding' && (intent.paidAt || intent.setAt || 0) < cutoff) {
        this.intents.delete(id);
      }
    }
  }

  /* --------------------------- the six methods ---------------------------- */

  reserve(intent) {
    this.intents.set(intent.id, intent);
    this._save();
    return intent;
  }

  /** Only orders still waiting for money — this is what the matcher iterates. */
  open() {
    return [...this.intents.values()].filter((i) => i.status === 'holding');
  }

  get(id) {
    return this.intents.get(id) || null;
  }

  markPaid(id, info) {
    const intent = this.intents.get(id);
    if (!intent) return null;
    intent.status = 'paid';
    intent.paidInfo = info;
    intent.paidAt = Date.now();
    this._save();
    return intent;
  }

  isSettled(messageId) {
    // No id means no replay guard is possible, so refuse the mail outright —
    // the same call the bundled MemoryStore makes, and for the same reason.
    if (!messageId) return true;
    return this.seen.includes(messageId);
  }

  rememberSettled(messageId) {
    if (!messageId || this.seen.includes(messageId)) return;
    this.seen.push(messageId);
    this._save();
  }

  /* ----------------------------- extra, ours ------------------------------ */

  /** Releases an order's amount early so the next buyer can be quoted it. */
  cancel(id) {
    const intent = this.intents.get(id);
    if (!intent || intent.status !== 'holding') return null;
    intent.status = 'cancelled';
    this._save();
    return intent;
  }

  counts() {
    let holding = 0;
    let paid = 0;
    for (const intent of this.intents.values()) {
      if (intent.status === 'holding') holding += 1;
      else if (intent.status === 'paid') paid += 1;
    }
    return { holding, paid, total: this.intents.size, seen: this.seen.length };
  }
}
