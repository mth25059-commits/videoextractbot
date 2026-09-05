/**
 * The bank's own alert, checked against the real thing. Run: node test/alert.test.js
 *
 * `fixtures/famapp-received.eml` is a genuine FamApp credit alert from
 * 2 September 2026 with the names, ids, UTR and balance replaced. The headers,
 * the MIME structure and the quoted-printable wrapping are exactly what Gmail
 * delivered. The gateway ships its own sample from a week earlier; this one is
 * here because the mail is the only input to the money path that nobody in this
 * repo controls, and a wording change at the bank's end fails silently — orders
 * would simply stop settling, with no error anywhere to notice.
 *
 * The assertion that matters most is the decoy: two orders one paise apart, and
 * this ₹1.06 alert has to settle exactly one of them. That is the whole design.
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { parseMessage, settlePayment, senderIsAuthentic, describeSettlement }
  from 'upi-fampay-gateway';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const RAW = fs.readFileSync(path.join(HERE, 'fixtures', 'famapp-received.eml'), 'utf8');
const SENDER = 'no-reply@famapp.in';

let n = 0;
const ok = (name) => { n += 1; console.log(`  ok   ${name}`); };

const mail = parseMessage(RAW);

/* --- reading the alert ----------------------------------------------------- */

assert.equal(mail.subject, 'You received ₹1.06 in your FamX account');
ok('the =?UTF-8?Q? subject decodes, rupee sign and all');

assert.equal(mail.date, Date.parse('Wed, 2 Sep 2026 09:40:42 +0000'));
ok('the Date header parses to the instant the bank sent it');

assert.equal(mail.credit.direction, 'in');
ok('money coming in reads as a credit');

assert.equal(mail.credit.amountPaise, 106);
ok('₹1.06 is 106 paise — the paise are what identify the order');

assert.equal(mail.credit.bankRef, '999999999999');
ok('the UTR is kept, so a settled order can be traced at the bank');

assert.equal(mail.credit.at, mail.date);
ok('the credit is stamped with the mail date, not the time we read it');

/*
 * Not a bug, but not obvious either, and worth failing loudly if it ever changes:
 * the payment note never survives. `makeReference` emits SF-20260902-XXXXXX and
 * FamApp prints `Purpose: SF20260902XXXXXX` with the hyphens gone, so the
 * reference can never match and every settlement here rides on the amount alone.
 * If this assertion starts failing, reference matching has come alive — which is
 * good news, and means the pattern in upi.js finally agrees with the bank.
 */
assert.equal(mail.credit.reference, null);
ok('the payment note does not survive FamApp — matching is on amount alone');

/* --- is it really the bank? ------------------------------------------------ */

assert.deepEqual(senderIsAuthentic(mail.headers, SENDER), { ok: true, problems: [] });
ok('a real alert passes the DKIM check for famapp.in');

const forged = senderIsAuthentic(mail.headers, 'no-reply@some-other-bank.in');
assert.equal(forged.ok, false);
ok('the same mail is not authentic for a bank that did not sign it');

const unsigned = senderIsAuthentic({ from: mail.headers.from }, SENDER);
assert.equal(unsigned.ok, false);
assert.match(unsigned.problems.join(' '), /no Authentication-Results/);
ok('a From header on its own proves nothing — anyone can type one');

/* --- which order does it pay? ---------------------------------------------- */

/*
 * `setAt` is derived from the mail rather than from `Date.now()`. The matcher
 * refuses a credit that predates the order it claims to pay, so orders pinned to
 * the current clock would stop matching this fixture the day after it was saved.
 */
const order = (id, amountPaise) => ({
  id,
  status: 'holding',
  amountPaise,
  listedPaise: 200,
  reference: 'SF-20260902-XXXXXX',
  setAt: mail.credit.at - 3 * 60 * 1000,
  expiresAt: mail.credit.at + 7 * 60 * 1000,
});

const decoy = order('TB-DECOY', 105);
const real = order('TB-REAL', 106);

const settled = settlePayment(mail, { holding: [decoy, real], expectedSender: SENDER });
assert.equal(settled.settled, true);
assert.equal(settled.order.id, 'TB-REAL');
assert.equal(settled.matchedOn, 'amount');
assert.equal(settled.amountPaise, 106);
assert.equal(settled.bankRef, '999999999999');
ok('one paise apart is enough — ₹1.06 settles TB-REAL and leaves TB-DECOY holding');

assert.equal(describeSettlement(settled), 'TB-REAL paid ₹1.06 (matched on amount)');
ok('the settlement describes itself for the log and the notification');

const replay = settlePayment(mail, {
  holding: [order('TB-AGAIN', 106)],
  expectedSender: SENDER,
  isSettled: (id) => id === mail.messageId,
});
assert.equal(replay.settled, false);
assert.match(replay.reason, /already settled/);
ok('the same alert cannot be spent twice');

const stolen = settlePayment(mail, { holding: [real], expectedSender: 'no-reply@other.in' });
assert.equal(stolen.settled, false);
assert.equal(stolen.suspicious, true);
ok('a mail that fails the sender check is refused, and flagged as suspicious');

const collision = settlePayment(mail, {
  holding: [order('TB-A', 106), order('TB-B', 106)],
  expectedSender: SENDER,
});
assert.equal(collision.settled, false);
assert.match(collision.reason, /should have been unique/);
ok('two orders holding the same amount settles neither — guessing is worse');

const nobody = settlePayment(mail, { holding: [decoy], expectedSender: SENDER });
assert.equal(nobody.settled, false);
assert.match(nobody.reason, /no pending order is waiting/);
ok('a credit nobody is waiting for is left alone');

/* --- the wording traps ----------------------------------------------------- */

/*
 * Quoted-printable wraps at 76 characters wherever that lands, which can be in
 * the middle of the figure. The real fixture happens to break after "A PAY=";
 * this variant moves the break inside "1.06" and blanks the figure out of the
 * subject, so the amount has to come off a reassembled body line.
 */
const wrapped = parseMessage(RAW
  .replace('=?UTF-8?Q?You_received_=E2=82=B91.06_in_your_FamX_account?=',
    'You received money in your FamX account')
  .replace('=E2=82=B91.06 from A PAY=\nER', '=E2=82=B91.0=\n6 from A PAYER'));
assert.equal(wrapped.credit.amountPaise, 106);
ok('an amount split across a soft line break still reads as 106 paise');

/*
 * "Purpose: Paid via CRED" is what FamApp prints when the payer used CRED, and it
 * sits inside a mail that is unambiguously money coming *in*. Anything testing
 * for the bare word "paid" would classify this credit as a debit and never settle
 * the order — so both directions anchor on "you <verb>".
 */
const viaCred = parseMessage(RAW.replace('Purpose: SF2026=\n0902XXXXXX', 'Purpose: Paid via CRED'));
assert.equal(viaCred.credit.direction, 'in');
assert.equal(viaCred.credit.amountPaise, 106);
ok('"Purpose: Paid via CRED" is still a credit, not a payment going out');

/* A real debit, on the other hand, must never buy anything. */
const debit = parseMessage(RAW
  .replace('=?UTF-8?Q?You_received_=E2=82=B91.06_in_your_FamX_account?=',
    '=?UTF-8?Q?You_paid_=E2=82=B91.06_from_your_FamX_account?=')
  .replace('You have successfully received', 'You have successfully paid'));
assert.equal(debit.credit.direction, 'out');

const outgoing = settlePayment(debit, { holding: [real], expectedSender: SENDER });
assert.equal(outgoing.settled, false);
assert.match(outgoing.reason, /money going out/);
ok('money leaving the wallet settles nothing, however well it matches');

/* A mail that is not about money at all is ignored rather than argued with. */
const promo = parseMessage(RAW.replace(
  /Hey Account Holder[\s\S]*?@famapp\.in/,
  'Your FamX card is ready. Tap to activate it.',
).replace('=?UTF-8?Q?You_received_=E2=82=B91.06_in_your_FamX_account?=',
  'Your FamX card is ready'));
assert.equal(promo.credit, null);
assert.equal(settlePayment(promo, { holding: [real], expectedSender: SENDER }).settled, false);
ok('a promo in the same inbox is not a payment');

console.log(`\n${n} passed, 0 failed\n`);
