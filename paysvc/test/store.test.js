/**
 * The journal. Run: node test/store.test.js
 *
 * Two of these assertions are the reason the file exists at all: a reserved
 * amount and a settled mail id both have to survive a restart, because forgetting
 * either one is how a payment gets attributed twice.
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { FileStore } from '../store.js';

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'paysvc-'));
const file = path.join(dir, 'nested', 'orders.json');
let n = 0;
const ok = (name) => { n += 1; console.log(`  ok   ${name}`); };

const intent = (id, over = {}) => ({
  id,
  priceRupees: 20,
  status: 'holding',
  amountPaise: 1973,
  listedPaise: 2000,
  reference: `PAY-20260902-${id}`,
  upiUri: 'upi://pay?pa=x@y&am=19.73',
  setAt: Date.now(),
  expiresAt: Date.now() + 600000,
  ...over,
});

/* --- a first run has no file, and does not mind ---------------------------- */
let store = new FileStore(file);
assert.deepEqual(store.open(), []);
ok('a missing journal is an empty journal, not an error');

store.reserve(intent('TB-1'));
assert.equal(fs.existsSync(file), true);
ok('reserve() creates the file, and its parent directory');

/* --- what has to survive a restart ---------------------------------------- */
store.rememberSettled('<alert-1@bank>');
store = new FileStore(file);

assert.equal(store.open().length, 1);
assert.equal(store.open()[0].amountPaise, 1973);
ok('a live order keeps its reserved amount across a restart');

assert.equal(store.isSettled('<alert-1@bank>'), true);
ok('a settled mail id is still burnt after a restart');

assert.equal(store.isSettled('<alert-2@bank>'), false);
ok('an unseen mail id is not');

assert.equal(store.isSettled(''), true);
assert.equal(store.isSettled(null), true);
ok('a mail with no id is refused — no id means no replay guard');

/* --- open() is the matcher's input, so it must be only live orders -------- */
store.reserve(intent('TB-2'));
store.markPaid('TB-2', { amountPaise: 1973, matchedOn: 'amount', bankRef: 'X1' });
assert.deepEqual(store.open().map((i) => i.id), ['TB-1']);
ok('open() hides paid orders');

assert.equal(store.get('TB-2').status, 'paid');
assert.equal(store.get('TB-2').paidInfo.bankRef, 'X1');
ok('markPaid() keeps the bank reference');

assert.equal(store.markPaid('TB-nope', {}), null);
ok('markPaid() on an unknown order is null, not a throw');

store.reserve(intent('TB-3'));
store.cancel('TB-3');
assert.deepEqual(store.open().map((i) => i.id), ['TB-1']);
assert.equal(store.get('TB-3').status, 'cancelled');
ok('cancel() releases the amount');

assert.equal(store.cancel('TB-2'), null);
assert.equal(store.get('TB-2').status, 'paid');
ok('cancel() cannot un-pay a paid order');

/* --- housekeeping --------------------------------------------------------- */
const old = intent('TB-old', { status: 'paid', paidAt: Date.now() - 30 * 24 * 3600 * 1000 });
store.intents.set(old.id, old);
store.reserve(intent('TB-4'));                 // any write prunes
assert.equal(store.get('TB-old'), null);
assert.equal(store.get('TB-2') !== null, true);
ok('closed orders age out, recent ones stay');

const counts = store.counts();
assert.equal(counts.holding, 2);               // TB-1, TB-4
assert.equal(counts.paid, 1);                  // TB-2
ok('counts() reports what /health shows');

/* --- a corrupt journal must not read as an empty one ---------------------- */
fs.writeFileSync(file, '{not json');
assert.throws(() => new FileStore(file), /unreadable/);
ok('a corrupt journal refuses to start rather than re-paying old alerts');

fs.rmSync(dir, { recursive: true, force: true });
console.log(`\n${n} passed, 0 failed\n`);
