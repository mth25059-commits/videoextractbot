# TeraBot

A Telegram bot that takes a link or an archive and sends back **playable video**,
paid for with credits. Built on Pyrogram/MTProto so a single file can be up to
**2 GB** (4 GB on Premium) — the cloud Bot API caps out at 50 MB, which is why
most bots of this kind only ever send a link instead of the video.

```
  link / .zip  ──►  ffmpeg (-c copy)  ──►  faststart .mp4  ──►  Telegram
                    container change,          index at             up to 2 GB
                    never a re-encode          the front            per file
```

## What it does

| Service | What happens | Cost |
|---|---|---|
| 📦 **Terabox** | Paste up to 10 links at once. Highest available quality, fetched in the background, each video delivered as it finishes. | 1 credit / link |
| 🗂 **ZIP File** | Send an archive. It is opened on the server and every video inside is sent as its own playable video — nothing is unpacked on your phone. | 2 cr up to 1 GB · 4 cr up to 2 GB |
| 💳 **Add Credit** | UPI QR, ₹1 = 1 credit, ₹20 minimum. Paid automatically by reading the bank's own alert mail. | — |

New users get **2 free credits**. Admins are notified the first time anyone
starts the bot.

## Why it is built this way

**Never re-encode.** An HLS stream is already H.264 in MPEG-TS segments, so
turning it into an MP4 is a *container* change. With `-c copy` a 1 GB 1080p video
is repackaged in well under a minute on one core; re-encoding the same file takes
20+ minutes and pins every core. There are no quality or bitrate settings in this
bot because nothing is ever encoded.

Two flags carry more weight than they look:

- `-bsf:a aac_adtstoasc` — HLS carries AAC in ADTS frames, MP4 needs it in ASC
  form. Omit it and the video arrives with silent audio.
- `-movflags +faststart` — moves the MP4 index to the front so Telegram can
  stream it. Without it the video must be fully downloaded before it plays.

**Bandwidth is the constraint, not CPU or RAM.** Every 1 GB video costs ~2 GB of
traffic (down, then up). Size the host on its traffic allowance, not its core
count.

**Nobody pays for a video that did not arrive.** Credits are debited when a job
is accepted and refunded in full on failure, cancellation, a file Telegram
refuses, or a restart that interrupts the job. The ledger records every movement
in the same transaction as the balance change, so any balance can be explained by
adding up its history.

**A batch bigger than the balance keeps what fits.** Ten links with six credits
sends six videos and says so, rather than refusing all ten.

## Branches

`main` is the bot plus the ZIP service. The rest are separate so each can be
merged, or not, on its own:

| Branch | Contents |
|---|---|
| `main` | Core bot, queue, media engine, ZIP service, admin panel |
| `feat/terabox` | The Terabox link resolver and its 10-link batch handler |
| `feat/payments` | `paysvc/` UPI gateway, QR screen, verification flow |
| `feat/faphouse-button` | A menu button that replies "not available yet". No downloader, nothing charged. |

```bash
git checkout main && git merge feat/terabox feat/payments
```

## Install

Needs Python 3.11+, ffmpeg, and (for payments) Node 18+.

```bash
apt-get update && apt-get install -y ffmpeg python3-pip
pip install -r requirements.txt
cp .env.example .env    # then fill it in
python -m bot.main
```

The bot refuses to start if ffmpeg is missing or a required variable is unset —
a bad deploy fails in the first second instead of on the first paying user.

## Configuration

Everything lives in `.env`; nothing is hardcoded. See [.env.example](.env.example)
for the full list with comments. The ones that matter most:

| Variable | Why |
|---|---|
| `API_ID` / `API_HASH` | From my.telegram.org. Required for MTProto — this is what buys the 2 GB limit. |
| `BOT_TOKEN` | From @BotFather. |
| `ADMIN_IDS` | Your own numeric id. Without it there is no admin panel. |
| `MAX_CONCURRENT_JOBS` | Keep at 3–4. Ten at once just splits the same uplink ten ways. |
| `MAX_UPLOAD_MB` | 2000, or 4000 if the account has Premium. |
| `WORK_DIR` | Scratch space. Needs `MAX_CONCURRENT_JOBS × MAX_UPLOAD_MB × 1.5` free. |

## Security

- `.env`, `*.session`, `data/` and `downloads/` are gitignored from the first
  commit. **No token, cookie or UPI id is ever in a tracked file.**
- If a bot token has ever been pasted into a chat or a commit, run `/revoke` in
  @BotFather and use the fresh one.
- `paysvc` binds to `127.0.0.1` only. It must never be reachable from the
  internet — it can move money into accounts.
- Admin checks run on `from_user.id` inside every handler, not on whether the
  button was shown. Callback data is guessable.
- ZIP entry names are flattened to a bare filename before extraction: an archive
  is allowed to contain `../../etc/passwd`.

## Tests

```bash
python tests/test_phase2.py     # media/provider helpers
python tests/test_archive.py    # ZIP pricing and path safety
python tests/test_queue.py      # the money path: charge, refund, cancel, restart
```

`test_queue.py` stubs Pyrogram, so it runs without the dependency installed. It
exists to hold one property: **a user is never charged for a video they did not
receive.**

## Layout

```
bot/
  config.py      every tunable, read from .env, fails loudly
  db.py          SQLite in WAL mode; users, ledger, jobs, orders
  credits.py     the only module allowed to change a balance
  queue.py       worker pool + the charge/refund rules
  media.py       ffmpeg/ffprobe — probe, fetch, remux, thumbnail
  download.py    direct-file downloader with resume
  uploader.py    MTProto upload, video-not-document, live progress
  archive.py     ZIP inspection, pricing, safe extraction
  state.py       parked interactions (callback data is only 64 bytes)
  ui.py          text, progress bars, throttling
  keyboards.py   every inline keyboard
  providers/     the plug-in slot for link sources
  handlers/      /start, ZIP, admin
```
