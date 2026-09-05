# videoextractbot

A Telegram bot that takes a link or an archive and sends back **playable video**,
paid for with credits. Built on Pyrogram/MTProto, so a single file can be up to
**2 GB** (4 GB on Premium) — the cloud Bot API caps out at 50 MB, which is why most
bots of this kind only ever send you a link instead of the video.

```
  link / .zip  ──►  ffmpeg (-c copy)  ──►  faststart .mp4  ──►  Telegram
                    container change,          index at             up to 2 GB
                    never a re-encode          the front            per file
```

## Install it

On a fresh Ubuntu 24.04 box, two commands. Everything else is asked, not edited.

```bash
git clone https://github.com/mth25059-commits/videoextractbot.git && cd videoextractbot
```

```bash
sudo bash install.sh
```

`install.sh` installs the system packages (ffmpeg, Node, `unar` for RAR), builds
the virtualenv, installs the payment service's Node packages — and then hands over
to a wizard that asks thirteen questions and **checks each answer as you give it**.
Every Terabox cookie is tested against Terabox before it is accepted; every proxy
is dialled; the Telegram credentials are used to call `get_me()` for real; every
force-join channel is opened to confirm the bot is an admin there. Nothing is
written until you have seen it all on one review page and accepted it.

Re-run `sudo bash install.sh` any time to change an answer. Every current value is
offered as the default, so Enter keeps it, and your old `.env` is copied to
`env-before-N.bak` before the new one is written.

### What you need an account for

| | | |
|---|---|---|
| **Telegram** `api_id` / `api_hash` | [my.telegram.org](https://my.telegram.org) → API development tools | required — this pair is what buys the 2 GB limit |
| **Bot token** | [@BotFather](https://t.me/BotFather) → `/mybots` | required |
| **Your Telegram id** | [@userinfobot](https://t.me/userinfobot) → `/start` | required — it is who the admin panel obeys |
| **A Terabox `ndus` cookie** | terabox.com, signed in → F12 → Cookies | for the Terabox service |
| **A channel of your own** | Telegram → new channel → add the bot as **admin** | only to make joining it compulsory; leave blank and the bot answers everybody |
| **A UPI id** | any UPI app | only to sell credits; leave blank and top-ups are simply off |
| **A Gmail app password** | Google account → Security → App passwords | only to settle payments automatically |
| **A Supabase project** | free tier | only if you want credits to outlive this VPS |

Skipping any of the optional ones leaves that feature politely off rather than
half-wired, and the review page says which ones ended up off.

### The box

**2 vCPU · 4 GiB RAM · ~40 GB disk · Ubuntu 24.04** runs this comfortably.

Bandwidth is the constraint, not cores. Every 1 GB video costs ~2 GB of traffic —
down, then up — so size the host on its traffic allowance. Nothing here is
CPU-bound: remuxing is a container change, not an encode.

The disk number is not a guess. Scratch space has to hold
`(MAX_CONCURRENT_JOBS + MAX_CONCURRENT_ZIP_JOBS) × MAX_UPLOAD_MB × 2`, because a
job can hold two files at once (a download and its remux target, or an archive and
the video being pulled out of it). At the shipped defaults that is
`(6 + 4) × 2000 MB × 2` = 40 GB. Below `MAX_UPLOAD_MB × 2.5` free the queue makes
new jobs wait rather than filling the disk and failing several at once.

## What it does

| Service | What happens | Cost |
|---|---|---|
| 📦 **Terabox** | Paste up to 10 links at once. Highest available quality, fetched in the background, each video delivered as it finishes. | 0.5 cr **per video** |
| 🗂 **ZIP File** | Send a ZIP, RAR or 7z. It is opened on the server and every video inside is sent as its own playable video — nothing is unpacked on your phone. | 2 cr up to 1 GB · 4 cr up to 2 GB |
| 🔥 **Fap** | One video page → every quality it has, assembled from HLS. Needs a resolver of your own (`FAP_API`); with that blank the key says it is not switched on and charges nobody. | 1 / 1.5 / 2 cr for 480 / 720 / 1080 |
| 💳 **Add Credit** | UPI QR. ₹1 = 1.5 credits, ₹20 minimum, credited automatically by reading the bank's own alert mail. | — |

New users get **2 free credits**. Admins are notified the first time anyone starts
the bot.

A Terabox link that points at a **folder** is charged per video inside it, not per
link: the floor is taken when the batch is confirmed and the rest once the folder
has been read, so nothing is held for videos that turn out not to be there.
`TERABOX_MAX_FILES_PER_LINK` (default 10) bounds how many one link may pull.

### Join a channel first (optional)

`FORCE_JOIN` is a list of channels a user has to be in before the bot answers them
at all. Blank — the shipped default — and there is no gate: no handler, no extra API
call, nothing. Filled in, the first thing anyone sees is one card naming the
channels, with a button each and **✅ I've joined** under them; pressing it asks
Telegram again then and there, and the bot opens up.

```
FORCE_JOIN=@myupdates,-1001234567890|https://t.me/+AbCdEf
```

A public channel can be written `@name`, `name`, or pasted as `https://t.me/name`.
A private one needs both halves — the numeric id is the only thing membership can be
checked against, the invite link the only thing that can go on a button — and the
wizard refuses the half that cannot be checked rather than putting up a dead end.

**The bot must be an administrator in every channel**, not just a member: Telegram
refuses to say who is in a channel to anyone else. The wizard checks that at install
time, because the gate itself deliberately does not. Three decisions worth knowing:

- **A pass is cached for five minutes; a refusal is never cached.** The moment after
  somebody joins is exactly when they press ✅.
- **If the check fails, the user gets in.** Remove the bot from a channel and
  `get_chat_member` fails for everyone at once — gating on that would lock out your
  whole userbase, paying users included. It is logged loudly and the door stays open.
- **`ADMIN_IDS` are never gated**, so you cannot lock yourself out of your own panel.

### Prices change while it runs

The rate and all four prices are editable from **⚙️ Prices** in the admin panel and
from the wizard, and a change is live on the next message — no restart, no `.env`
edit, nothing to keep in step. `.env` holds the *install-time default*; the
`settings` table holds the current truth.

## Where the credits live

By default: one SQLite file, `data/bot.db`, on this box.

Credits are keyed on the **Telegram user id**, not on the bot token — so changing
the bot, or moving to a new box with the same database, keeps everybody's balance.

Fill in `DATABASE_URL` with a Postgres URL (the wizard offers Supabase and writes a
`supabase.txt` for you to paste into its SQL editor) and users, credits, the ledger
and the job history live there instead, surviving this VPS being resized, moved or
shut down. Use the **pooler** string, port 6543 — a VPS behind NAT will usually not
hold Supabase's direct 5432 session open.

Two things that do **not** move with it, said plainly because they are money:

- `paysvc/data/orders.json` is the in-flight order journal, on local disk. A box
  lost mid-payment loses only the orders that had not settled yet; your bank
  mailbox is still the record of truth for those.
- `data/*.session` stays local, and should — it is an MTProto credential belonging
  to one bot token.

## Why it is built this way

**Never re-encode.** An HLS stream is already H.264 in MPEG-TS segments, so turning
it into an MP4 is a *container* change. With `-c copy` a 1 GB 1080p video is
repackaged in well under a minute on one core; re-encoding the same file takes 20+
minutes and pins every core. There are no quality or bitrate settings in this bot
because nothing is ever encoded.

Two flags carry more weight than they look:

- `-bsf:a aac_adtstoasc` — HLS carries AAC in ADTS frames, MP4 needs it in ASC
  form. Omit it and the video arrives with silent audio.
- `-movflags +faststart` — moves the MP4 index to the front so Telegram can stream
  it. Without it the video must be fully downloaded before it plays.

**Nobody pays for a video that did not arrive.** Credits are debited when a job is
accepted and refunded in full on failure, cancellation, a file Telegram refuses, or
a restart that interrupts the job. The ledger records every movement in the same
transaction as the balance change, so any balance can be explained by adding up its
history.

**A batch bigger than the balance keeps what fits.** Ten links with six credits
sends six videos and says so, rather than refusing all ten.

**Two worker lanes, not one pool.** Terabox jobs are limited by Terabox's own
per-CDN-host shaping (~1.5 MB/s a stream); ZIP jobs pull from Telegram, unpack
locally and never touch Terabox at all. Sharing one pool let four archives block
three link jobs for no reason. Raising either number does not make any single
download faster — it is so that six people all *start* at once instead of watching
a queue, which is the part that actually feels slow.

## The Terabox resolver

```
/s/1abc…  ──►  surl  ──►  <home>/main  ──►  /share/list  ──►  dlink  ──►  the file
                          jsToken +          bare surl        signed,
                          bdstoken           + tokens         minutes
```

Three things it depends on, in the order they usually break:

- **`TERABOX_COOKIE`** — your own `ndus`, from a browser that is signed in. Listing
  a share works without one; the **download** does not (`dlink` comes back present
  and empty to a guest), so with this empty the service says it is not set up
  instead of charging anyone. Terabox hands an `ndus` to guests too, so its presence
  proves nothing — the wizard checks the one you paste against `/api/quota` and
  tells you whether it is really signed in.
- **The cookie is bound to one host, and the bot finds out which.** A Terabox
  session is valid on exactly the domain that issued it and answers `errno -6 user
  not login` on all the others, even though the cookie is scoped `.1024terabox.com`
  and a browser sends it everywhere. So the host is discovered, never configured —
  `errno -6` in the log after that means the cookie really has expired.
- **A browser TLS fingerprint** — requests go through `curl_cffi` with
  `impersonate="chrome124"`. Terabox's WAF rejects Python's handshake before it
  reads a single header, and the failure looks exactly like a bad cookie.

Two smaller traps, both of which cost a day:

- **`dlink` is not the file** — it is a signed redirect, valid for minutes, tied to
  the cookie and User-Agent that asked for it. It is resolved per job, never cached.
- **`dp-logid` must never be sent.** It is in the page bundle's own parameter
  builder, so it looks mandatory; send it and every call is refused with
  `code 460020 need verify`, cookie or no cookie.

**And the `dlink` has to be fetched on its own session.** The session that just
made the API calls carries `browserid`, `csrfToken` and `lang` in its jar, and the
CDN answers a flat `403 text/plain` to `ndus` arriving beside them — the same link,
same second, on a clean session gives 200. It is not the `Accept` header and not
Range; both were ruled out by measurement.

**Spare cookies buy failover, not speed.** `TERABOX_COOKIE_2..6` are numbered and
never comma-joined, because an `ndus` legitimately contains `=`, `;` and sometimes
`,`. Terabox shapes per CDN host, not per account, so a second cookie makes no
download faster — it keeps the bot working when the first is rate-limited
(`errno 400210`) or logged out, which one cookie cannot survive at all.

**No quality menu.** The original upload is the highest quality there is, so a link
resolves to one stream and the bot takes it. The `/api/streaming` HLS transcode is
a fallback for when `dlink` is refused, and Terabox caps it at 1080p.

Verified against the live API on 4 September 2026 with a signed-in cookie: three
shares resolved and served **HTTP 206 `video/mp4`** — 3.0 MB, 38.8 MB and 42.7 MB,
byte-for-byte the sizes Terabox reports, with `ftyp`/`moov` inside the first MiB
and `Range` honoured, so [download.py](bot/download.py) resume works.
`tests/live_check.py` is that check, kept runnable by hand and needing a real cookie.

## Security

- `.env`, `*.session`, `data/`, `downloads/`, `paysvc/data/`, `fampay.txt` and
  `supabase.txt` are gitignored. **No token, cookie or UPI id is ever in a tracked
  file** — every credential-shaped string in this repo, tests included, is invented.
- **`paysvc` binds `127.0.0.1` only, and must never be reachable from the
  internet.** It is the process that decides money has arrived, so anything that can
  reach it can grant credits. `install.sh` deliberately opens no firewall port; if
  you run one, leave 4400 and 8081 closed.
- **Never publish your `api_id`.** Unlike a bot token it cannot be revoked and
  reissued — there is one per phone number — and a published one earns a permanent
  `API_ID_PUBLISHED_FLOOD`.
- If a bot token has ever been pasted into a chat or a commit, run `/revoke` in
  @BotFather and use the fresh one.
- Admin checks run on `from_user.id` inside every handler, not on whether the button
  was shown. Callback data is guessable.
- ZIP entry names are flattened to a bare filename before extraction: an archive is
  allowed to contain `../../etc/passwd`.
- The bot runs as your login, not root — a process that unpacks strangers' archives
  is the last one to hand a uid of 0 to.

## Running it

The wizard starts the bot and enables it at boot for you. Afterwards:

```bash
deploy/run.sh status
```

`run.sh` is the supervisor: process-group pidfiles, 20 MB log rotation, and a
`stop` that signals the group and **waits up to 60 seconds**. That wait is what
lets the queue refund every job still in flight, which is why the systemd unit is
`Type=oneshot` with a real `ExecStop` rather than something that kills the process.

## Tests

1590 assertions, no network, no credentials. From the repo:

```bash
for t in queue terabox fap archive gate phase2 config report payments db_pg join setup; do .venv/bin/python tests/test_$t.py || break; done
```

`test_db_pg.py` skips itself unless `DATABASE_URL` is set, so nobody needs Postgres
installed to run the suite.

Three of them exist to hold one property each, and all three are easy to break by
accident:

- `test_queue.py` — **a user is never charged for a video they did not receive.**
- `test_gate.py` — Pyrogram runs a single message handler per group, so every
  handler that wants private text is gated on the mode it owns
  ([_gate.py](bot/handlers/_gate.py)). Without that the first one registered
  swallows every message and the rest are dead code.
- `test_join.py` — the force-join gate lets a user in when the check itself fails,
  caches a yes and never a no, and never gates an admin. Each of those is one line
  of code and each one, inverted, locks people out of a working bot.

## Layout

```
install.sh       one command: packages, venv, node deps, then the wizard
setup/           the wizard — questions, checks, review, .env, systemd unit
bot/
  config.py      every tunable, read from .env, fails loudly at import
  settings.py    the six prices, DB over .env, changeable while running
  db.py          SQLite in WAL mode, or Postgres if DATABASE_URL is set
  credits.py     the only module allowed to change a balance
  queue.py       worker pool + the charge/refund rules
  media.py       ffmpeg/ffprobe — probe, fetch, remux, thumbnail
  download.py    direct-file downloader with resume
  uploader.py    MTProto upload, video-not-document, live progress
  archive.py     ZIP/RAR/7z inspection, pricing, safe extraction
  payments.py    orders, the paysvc callback, atomic top-ups
  egress.py      outbound proxy rotation, benching, and probing
  nightly.py     the health report to the admins, at DAILY_REPORT_UTC
  broadcast.py   one message to every user, paced so Telegram does not refuse it
  state.py       parked interactions (callback data is only 64 bytes)
  joingate.py    force-join: who is in which channel, and the pass cache
  ui.py          text, progress bars, throttling
  keyboards.py   every inline keyboard
  providers/     the plug-in slot for link sources
  handlers/      /start, Terabox, Fap, ZIP, payments, admin — gated by _gate.py
                 join.py stands in front of them all when FORCE_JOIN is set
paysvc/          the Node UPI gateway: QR, order journal, bank-mail settlement
deploy/run.sh    the supervisor: pidfiles, log rotation, a stop that waits
```
