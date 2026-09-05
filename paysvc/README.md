# paysvc

The UPI half of TeraBot's top-ups. It does the three things the bot cannot do
itself: quote each order an amount no other live order has, watch the bank's
alert mail over IMAP, and say *"that credit was order X"*.

```bash
cd paysvc && npm install && node server.js
```

It is a thin wrapper around
[`upi-fampay-gateway`](https://github.com/mth25059-commits/upi-fampay-gateway) —
that package holds the matching and the mail parsing; `server.js` is the HTTP
door the bot knocks on, and `store.js` is the order journal on disk.

**The bot keeps the money record. paysvc keeps the matching.** No credit is ever
moved here, and nothing in this directory can grant one.

## Why an amount, and not a reference

UPI lets a payer type whatever note they like, and most apps do not pass a
reference through to the bank's alert at all. What the alert *always* contains is
the amount, to the paise. So each order is quoted a figure a few paise below the
listed price — ₹19.67 for a ₹20 top-up — and that figure is the order's identity
for as long as it is live.

Two consequences you can see in the UI:

- The pay screen says **"pay exactly ₹19.67, not ₹20"**, and means it. A user who
  rounds up has paid an amount matching nothing, and a human has to sort it out.
- Reserved amounts are held for `PAYMENT_WINDOW_MINUTES` plus
  `PAYMENT_GRACE_MINUTES`, so a credit landing on the buzzer cannot be
  re-attributed to the next person quoted the same figure.

## Two doors, on purpose

```
bot ──POST /order──►  unique amount + QR matrix ──► the user pays
                                                         │
                                 bank alert mail ◄────────┘
                                       │
bot ◄──POST /paid────  gateway matched it   (or the bot asks: POST /check)
```

The **push** (`/paid` → the bot) is what makes credits appear without the user
pressing anything. The **pull** (`/check`) is what makes it correct anyway: if
the bot was restarting when the money landed, the push is lost — and `/check`,
from the "I have paid" button or the bot's boot-time reconcile, finds the settled
order in the journal and credits it then.

The push is retried at widening gaps (0s, 2s, 5s, 15s, 30s, 60s) because the
usual reason it fails is a bot that is restarting. If every attempt fails the
order stays `paid` in the journal and the bot picks it up on `/check`, so giving
up costs the user a wait, never their money.

## What you have to fill in

Everything lives in the **bot's** `.env` at the repo root — paysvc reads that file
too, so there is one place to edit and one place a mistyped secret can hide.
`paysvc/.env.example` exists only for running this service on its own box.

Four values make top-ups fully automatic:

| Key | What it is |
| --- | --- |
| `UPI_ID` | where the money lands, e.g. `yourname@okhdfcbank` |
| `UPI_PAYEE_NAME` | the name a payer sees before they confirm |
| `IMAP_USER` | the Gmail inbox the bank alerts arrive in |
| `IMAP_APP_PASSWORD` | a Gmail **app password**, not the account password |

An app password comes from *myaccount.google.com → Security → 2-Step
Verification → App passwords*. Spaces in it are ignored, so paste it either way.

Plus the shared secret, which must be byte-identical to the bot's:

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

`UPI_ID` and `UPI_PAYEE_NAME` are printed on every QR, so neither is a secret —
but a typo in `UPI_ID` silently pays a stranger. Scan your own QR once before
letting a customer near it.

## Half-configured is the state to avoid

Leaving the IMAP pair blank is *supported*: orders are still quoted and QRs still
appear, they simply never settle on their own. The bot notices — `GET /health`
reports `autoConfirm: false` — and changes what its screens promise: "I have
paid" then messages the admin with a one-tap confirm button instead of claiming
the credits will arrive by themselves.

That path goes quiet on its own the moment IMAP is configured. There is no flag
to remember to turn off.

## Is it working?

```bash
curl -s -H "x-paysvc-secret: $PAYSVC_SECRET" http://127.0.0.1:4400/health
```

```json
{
  "ok": true,
  "upiConfigured": true,
  "autoConfirm": true,
  "windowMinutes": 10,
  "poller": { "on": true, "scans": 214, "settled": 12, "failing": null },
  "orders": { "holding": 1, "paid": 12 }
}
```

`autoConfirm: false` with `upiConfigured: true` means the inbox half is missing.
The boot log says which key it is.

## The endpoints

Every one of them requires the `x-paysvc-secret` header and answers `401`
without it. All are `POST` except `/health`.

| | |
| --- | --- |
| `GET /health` | config, poller state, order counts |
| `POST /order` `{orderId, rupees}` | reserve an amount; returns `amountPaise`, `upiUri`, `reference`, `expiresAt`, and the QR as `{size, cells}` |
| `POST /check` `{orderId}` | scan the inbox **now** and report the order's state |
| `POST /cancel` `{orderId}` | release the amount; `409 already paid` if it settled first |

`/order` is idempotent on `orderId` — re-posting returns the same amount with
`reused: true`, so a retry cannot burn two figures on one order.

The QR is handed over as a flat grid rather than an image because the bot draws
it with Pillow. One encoder in the system is one that cannot disagree with
itself, and a QR encoding the wrong amount is a payment nobody can match.

## Security

- **Binds `127.0.0.1` only.** Nothing here authenticates a *user*, only the bot.
  Anything that can reach this port can quote amounts and read order state — do
  not put it behind nginx, do not open the port.
- The shared secret is compared in constant time, on both sides.
- **A `From` header proves nothing.** An alert mail must also carry a passing
  DKIM signature for the sender's domain, and one that fails is logged loudly as
  `REJECTED an unauthentic payment mail` — the only innocent explanation is the
  bank changing its mail setup.
- `IMAP_LOOKBACK_HOURS` bounds how far back a scan reads, so an alert from last
  week cannot be replayed.
- **Money is never derived from a request body.** The credits a top-up is worth
  are written into the order row when it is created; a callback can settle an
  order but cannot change what it pays out.
- `paysvc/data/` is gitignored. It holds order ids, amounts and bank references —
  no credentials and no card details.

## If your bank is not FamApp

`IMAP_SENDER` defaults to FamApp's alert address and the parser is built against
FamApp's wording. For a different provider, change `IMAP_SENDER` and the credit
regexes in `node_modules/upi-fampay-gateway/src/mailbox.js` (`RECEIVED`,
`IS_CREDIT`, `IS_DEBIT`); a sanitised example alert ships in that package's
`samples/`.

One thing to check first: **your alert must print the paise in full.** The whole
matching scheme rests on them. If your bank rounds to the rupee, drop to
single-digit steps (`STEP_PAISE = 10` in `src/upi.js`) and matching keeps
working with ten times fewer simultaneous orders.

## Tests

```bash
npm test
```

Covers the journal, where two assertions are the reason the file exists: a
reserved amount and a settled mail id both have to survive a restart, because
forgetting either is how one payment gets counted twice. Also the replay guard,
`cancel()` refusing to un-pay a paid order, and a corrupt journal refusing to
start rather than re-crediting old alerts.

Then `test/alert.test.js` runs the matcher against a real FamApp credit alert —
`test/fixtures/famapp-received.eml`, a genuine mail with the names, ids, UTR and
balance replaced but the headers, MIME structure and quoted-printable wrapping
left exactly as Gmail delivered them. The mail is the one input to the money path
that nobody here controls, and a change in the bank's wording fails silently:
orders would just stop settling, with no error to notice. The assertion that
matters most puts two orders one paise apart and requires the ₹1.06 alert to
settle exactly one of them. The rest are the ways it must refuse — a forged
sender, a replayed alert, a debit, two orders holding the same amount.


