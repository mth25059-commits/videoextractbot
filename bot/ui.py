"""
Text and animation. Kept away from the handlers so wording can be changed
without touching logic.

Two things worth knowing:

* Telegram rate-limits message edits hard. `Throttle` makes a progress bar that
  updates at most once every few seconds; without it a 1 GB upload gets the bot
  flood-waited halfway through.
* All user text goes out as HTML, not Markdown. Filenames routinely contain
  `_` and `*`, which silently break Markdown parsing and make the bot look bugged.
"""

from __future__ import annotations

import html
import time

BAR_FULL = "█"
BAR_EMPTY = "░"
BAR_WIDTH = 12

SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def bar(done: int, total: int) -> str:
    if total <= 0:
        return BAR_EMPTY * BAR_WIDTH
    filled = int(BAR_WIDTH * min(done, total) / total)
    return BAR_FULL * filled + BAR_EMPTY * (BAR_WIDTH - filled)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def human_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def human_speed(bytes_per_sec: float) -> str:
    return f"{human_bytes(bytes_per_sec)}/s"


class Throttle:
    """Allows an action at most once every `every` seconds. Always allows the last call."""

    def __init__(self, every: float = 4.0):
        self.every = every
        self._last = 0.0

    def ready(self, force: bool = False) -> bool:
        now = time.monotonic()
        if force or now - self._last >= self.every:
            self._last = now
            return True
        return False


def progress_block(title: str, done: int, total: int, started: float,
                   stage: str = "") -> str:
    """The live download/upload panel. Safe to call with total=0 (unknown size)."""
    elapsed = max(0.001, time.monotonic() - started)
    speed = done / elapsed
    pct = (done / total * 100) if total else 0
    eta = ((total - done) / speed) if (total and speed > 0) else 0

    lines = [f"<b>{esc(title)}</b>"]
    if stage:
        lines.append(f"<i>{esc(stage)}</i>")
    lines.append("")
    if total:
        lines.append(f"<code>{bar(done, total)}</code>  {pct:5.1f}%")
        lines.append(f"📦 {human_bytes(done)} / {human_bytes(total)}")
        lines.append(f"⚡ {human_speed(speed)}   ⏳ {human_time(eta)} left")
    else:
        lines.append(f"📦 {human_bytes(done)} transferred")
        lines.append(f"⚡ {human_speed(speed)}")
    return "\n".join(lines)


def assembling_block(title: str, done: float, total: float, started: float) -> str:
    """
    The live panel for an HLS fetch, where progress is measured in **seconds of
    video** rather than bytes.

    A renderer of its own rather than a flag on `progress_block`, because every line
    of that one is a byte count: seconds run through it read as "45 B / 300 B" moving
    at "12 B/s". What is honest to say here instead is how much of the video exists so
    far and how fast it is being pulled — `×2.4` meaning two and a bit minutes of
    video a minute, which is the only speed an HLS stream can report before the file
    it becomes has a size at all.

    Safe with `total=0`: a master playlist carries no `#EXTINF` of its own, so the
    length is sometimes genuinely unknown until the fetch ends.
    """
    elapsed = max(0.001, time.monotonic() - started)
    rate = done / elapsed
    pct = (done / total * 100) if total else 0
    eta = ((total - done) / rate) if (total and rate > 0) else 0

    lines = [f"<b>{esc(title)}</b>", "<i>downloading the video…</i>", ""]
    if total:
        lines.append(f"<code>{bar(int(done), int(total))}</code>  {pct:5.1f}%")
        lines.append(f"🎞 {human_time(done)} / {human_time(total)} of video")
        lines.append(f"⚡ ×{rate:.1f} speed   ⏳ {human_time(eta)} left")
    else:
        lines.append(f"🎞 {human_time(done)} of video ready")
        lines.append(f"⚡ ×{rate:.1f} speed")
    return "\n".join(lines)


#: What the wait says while it waits. Rotated rather than fixed, because one frozen
#: line under a moving bar still reads as a stuck bot — the words changing is what
#: says a real thing is happening on the other side.
WAIT_NOTES = (
    "getting it ready…",
    "talking to Terabox…",
    "lining up the download…",
    "almost there…",
)


def _band(tick: int, width: int = BAR_WIDTH, size: int = 3) -> str:
    """
    A `size`-cell band bouncing left and right across `width` cells.

    Bouncing, not wrapping: a band that wraps arrives at the right edge as two
    pieces and reads as a glitch. The triangle wave keeps all `size` cells inside
    the bar at every tick, so the lit count never changes.
    """
    span = max(1, width - size)
    phase = tick % (2 * span)
    start = phase if phase <= span else 2 * span - phase
    cells = [BAR_EMPTY] * width
    for offset in range(size):
        cells[start + offset] = BAR_FULL
    return "".join(cells)


def waiting_block(title: str, tick: int, note: str = "", seconds: float = 0.0) -> str:
    """
    The panel shown between "accepted" and the first byte.

    It deliberately does **not** say "queued", or show a position in line. A
    number that counts down teaches everyone watching that they are behind other
    people; a moving bar reads as work happening, and the wait is the same either
    way. `tick` advances the spinner, the band and the wording, so the caller owns
    the clock and this stays a pure function.

    `seconds` is shown when the caller knows it. An elapsed counter is the one
    honest thing that can be said during a wait of unknown length, and it is what
    tells someone at 40 seconds that they are not looking at a frozen screen.
    """
    spin = SPINNER[tick % len(SPINNER)]
    words = note or WAIT_NOTES[(tick // 3) % len(WAIT_NOTES)]
    clock = f"   ⏱ {human_time(seconds)}" if seconds > 0 else ""
    return (f"<b>{esc(title)}</b>\n"
            f"<i>{spin} {esc(words)}</i>\n\n"
            f"<code>{_band(tick)}</code>{clock}")


# --- static copy -------------------------------------------------------------

def panel_title(index: int = 0, total: int = 1) -> str:
    """
    What the working panel calls the video: `Video 2 of 10`. Never its name.

    **the operator's rule, and the reason is Telegram rather than taste** — *"tg ko n pta
    chle ki ye sb hora"*. The finished file has been anonymous all along (it goes up
    under its on-disk name, `01.mp4`), and there has never been a caption. The panel
    was the hole: a *text* message, live in the chat for the whole job, carrying the
    video's own title — which is far easier for anything reading the chat than the
    video ever was.

    A number is not a downgrade for the user either. The name told them nothing they
    did not already know, having just pasted the link; in a ten-link batch what they
    actually want to know is which one is moving, and that is what this says.

    One-based on the way in, because it is read by people: `panel_title(0, 3)` is
    "Video 1 of 3". A job that is alone in its batch is just "Video".
    """
    if total > 1:
        return f"Video {index + 1} of {total}"
    return "Video"


def rate_text(per_credit: float) -> str:
    """
    The exchange rate the way it is said out loud: `₹1 = <b>1.5 credits</b>`.

    Takes rupees-per-credit — what `payments.credits_for` divides by — and turns it
    the right way up, because the arithmetic and the sentence point in opposite
    directions. At 1.5 credits per rupee the stored number is 0.6667, and printing
    *that* gives "1 credit = ₹0.6667": true, useless, and the kind of line that gets
    read as a bug in the price.

    `:g` is what keeps it clean — 6 significant digits turns the 1.5000000000000002
    that comes back out of the round trip into `1.5`.
    """
    if per_credit <= 0:
        per_credit = 1
    credits = 1 / per_credit
    return f"₹1 = <b>{credits:g} credit{'' if credits == 1 else 's'}</b>"


#: Megabytes a second from one Terabox stream, measured on the box on 4 September
#: 2026 (1.29–1.87 MB/s over repeated 32 MiB windows). The guide quotes waiting
#: times, and a made-up number there is worse than no number: it is the one line a
#: user checks against a stopwatch. Timings are computed from this, so correcting
#: the measurement corrects every figure on the page.
GUIDE_MB_PER_SEC = 1.5


def _download_eta(megabytes: float) -> str:
    return human_time(megabytes / GUIDE_MB_PER_SEC)


def guide(is_new: bool = False) -> str:
    """
    The whole manual, on one screen.

    Shown twice: unprompted to a brand-new user before they ever see the menu, and
    from the menu's "How it works" button afterwards. Same text both times — a
    first-timer and someone coming back with a question want the same facts, and two
    versions would mean the one nobody reads is the one that goes stale.

    Every price and limit is read from config here rather than typed, because this
    page is the thing people quote back when they think they were overcharged. The
    imports are function-local on purpose: this module is imported by the queue and
    the providers, and it stays free of module-level bot imports so it cannot join
    an import cycle.

    Ordered by what a new user needs first, not by what is interesting: the two
    services and how to trigger them, then price, then how long it takes, then what
    happens to their file. The waiting-time section exists because "why is it slow"
    is the most common question and the honest answer — a per-file cap at the other
    end that no amount of credit changes — is not guessable from the outside.
    """
    from . import archive
    from .config import cfg

    gb = archive.GB
    per_extra_gb = archive.price_for(3 * gb) - archive.price_for(2 * gb)
    if is_new:
        # The joining gift is announced here rather than on a fourth message,
        # because for a new user this page *is* the first screen — `nav:menu`
        # renders the returning-user welcome, which has no gift line in it.
        gift = (f"\n🎁 <b>{cfg.free_credits_on_join:g} free credits</b> are already in "
                "your account.\n" if cfg.free_credits_on_join else "")
        head = ("👋 <b>Welcome! Worth one minute of reading.</b>\n"
                "<i>After this, everything here is one tap.</i>\n" + gift)
    else:
        head = "📖 <b>How this bot works</b>"

    return "\n".join([
        head,
        "──────────────────",
        "",
        "Send me a <b>Terabox link</b> or an <b>archive file</b> and the videos come "
        "back into this chat, already playable — nothing to download or unpack on "
        "your phone.",
        "",
        "<b>⌨️ The four keys under your typing box</b>",
        "📦 <b>Terabox</b> — paste a link",
        "🔥 <b>Fap</b> — one video link, then pick the quality",
        "🗂 <b>File</b> — send a ZIP, RAR or 7z",
        "☰ <b>Menu</b> — credits, account, this page",
        "",
        "<b>📦 Terabox — what to do</b>",
        "<b>1.</b> Tap 📦 <b>Terabox</b>, or just paste the link straight in. Both work.",
        f"<b>2.</b> Up to <b>{cfg.max_links_per_batch} links</b> in one message — "
        "one per line.",
        "<b>3.</b> Confirm the price, then wait. The bar moves while it works and the "
        "video arrives right here.",
        f"💰 <b>{cfg.cost_terabox_per_link:g} "
        f"credit{'' if cfg.cost_terabox_per_link == 1 else 's'}</b> per <b>video</b>. "
        f"A single link is one video. A folder link sends up to "
        f"<b>{cfg.terabox_max_files_per_link} videos</b> and is priced for what is "
        "actually inside it — counted after I have read it, so nothing is held for "
        "videos that are not there.",
        "",
        "<b>🔥 Fap — what to do</b>",
        "<b>1.</b> Tap 🔥 <b>Fap</b>, or paste the video link straight in.",
        "<b>2.</b> <b>One link</b> at a time. I read it and show you the qualities that "
        "video actually has.",
        "<b>3.</b> Tap one. That is when the credit is taken, and the video comes back "
        "here.",
        f"💰 <b>480p {cfg.cost_fap_480:g}</b>  ·  <b>720p {cfg.cost_fap_720:g}</b>  ·  "
        f"<b>1080p {cfg.cost_fap_1080:g}</b> credits. A quality the video does not have "
        "is not offered at all, so you never pay for one and get another.",
        "",
        "<b>🗂 File — what to do</b>",
        "Send the archive as a <b>file</b> (📎 → <i>File</i>), not as a photo or a video. "
        "ZIP, RAR and 7z all open — even one saved under the wrong name.",
        f"  • up to 1 GB — <b>{archive.price_for(gb):g} credits</b>",
        f"  • 1 – 2 GB — <b>{archive.price_for(2 * gb):g} credits</b>",
        f"  • 2 – 3 GB — <b>{archive.price_for(3 * gb):g} credits</b>",
        f"  • every extra GB — <b>+{per_extra_gb:g} credits</b>",
        "💰 One price for the whole archive, however many videos are inside it. A video "
        f"over Telegram's <b>{cfg.max_upload_mb} MB</b> ceiling is <b>split and sent in "
        "parts</b> instead of being skipped, with the rejoin steps posted next to it.",
        "",
        "<b>💬 Commands</b>",
        "<code>/start</code> — the menu, and the keys back if they vanish",
        "<code>/balance</code> — what you have left",
        "",
        "<b>💰 Credits</b>",
        f"💱 {rate_text(cfg.rupees_per_credit)} — so ₹{cfg.min_topup_rupees:g} is "
        f"<b>{cfg.min_topup_rupees / (cfg.rupees_per_credit or 1):g} credits</b>.",
        "<b>How to add them</b>",
        "<b>1.</b> ☰ <b>Menu</b> → 💳 <b>Add Credit</b>.",
        f"<b>2.</b> Pick an amount, or type your own — minimum "
        f"<b>₹{cfg.min_topup_rupees:g}</b>, no maximum.",
        "<b>3.</b> Pay with any UPI app — scan the QR, or copy the UPI id.",
        "<b>4.</b> Tap <b>I have paid</b>. The credits land on their own, usually in "
        "seconds.",
        "⚠️ <b>Pay the exact amount shown</b>, paise included. That figure is how your "
        "payment is recognised — a rounded-off amount matches nothing and has to be "
        "sorted out by hand.",
        "If something fails the credits come <b>back on their own</b> — all of them if "
        "nothing arrived, and the missing share if only some videos did. You never have "
        "to ask.",
        "",
        "<b>⏱ How long it takes</b>",
        f"Terabox hands out about <b>{GUIDE_MB_PER_SEC:g} MB/s</b> per video, to everyone — "
        "so size decides the wait:",
        f"  • 200 MB — about {_download_eta(200)}",
        f"  • 500 MB — about {_download_eta(500)}",
        f"  • 1 GB — about {_download_eta(1024)}",
        "…plus the upload back to you on top. Several links at once is fine: they run "
        "side by side, not one after another.",
        "<i>One batch at a time per person, though — let it finish, or cancel it, "
        "before starting the next.</i>",
        "",
        "<b>🔒 Your file is not kept</b>",
        "It is deleted from the server the moment the last video has been sent. Nothing "
        "stays behind.",
    ])


def fap_soon() -> str:
    """
    The 🔥 Fap key before there is anything behind it.

    A key that answers nothing is worse than no key, so it answers this. It says
    plainly that there is no charge and nothing to send yet, because the alternative
    is someone pasting a link into it and waiting.

    **Superseded here** — `handlers/fap.py` now answers that key for real, and the
    "not switched on" case is `providers.faphouse.Faphouse.unavailable`, which knows
    *why*. This stays because the standalone button branch still renders it, and
    deleting it would break that branch the next time it is merged forward.
    """
    return (
        "🔥 <b>Fap — not open yet</b>\n"
        "──────────────────\n\n"
        "This one is still being built. There is nothing to send it yet, and it "
        "cannot take any of your credits.\n\n"
        "📦 <b>Terabox</b> and 🗂 <b>File</b> both work right now — use those in the "
        "meantime, and this key will start answering as soon as it is ready."
    )


def welcome(name: str, credits: float, is_new: bool, bonus: float) -> str:
    head = (
        f"👋 <b>Welcome, {esc(name)}!</b>"
        if is_new
        else f"👋 <b>Welcome back, {esc(name)}!</b>"
    )
    gift = (
        f"\n🎁 <b>{bonus:g} free credits</b> added to get you started.\n"
        if is_new and bonus
        else "\n"
    )
    return (
        f"{head}\n"
        f"{gift}"
        f"💰 Balance: <b>{credits:g} credits</b>\n"
        f"\n"
        f"Pick a service below 👇"
    )


def account_card(u, jobs_done: int) -> str:
    return (
        "👤 <b>My Account</b>\n"
        "──────────────────\n"
        f"🆔 <code>{u.user_id}</code>\n"
        f"👋 {esc(u.first_name)}  {esc(u.handle)}\n"
        f"\n"
        f"💰 Balance      <b>{u.credits:g} cr</b>\n"
        f"📉 Total used    {u.total_spent:g} cr\n"
        f"📈 Total added   {u.total_topup:g} cr\n"
        f"✅ Files sent    {jobs_done}\n"
    )


def new_user_alert(u, total_users: int) -> str:
    return (
        "🆕 <b>New user started the bot</b>\n"
        "──────────────────\n"
        f"🆔 <code>{u.user_id}</code>\n"
        f"👋 {esc(u.first_name)}\n"
        f"🔗 {esc(u.handle)}\n"
        f"💰 Starting credits: {u.credits:g}\n"
        f"\n"
        f"👥 Total users now: <b>{total_users}</b>"
    )


def insufficient(needed: float, have: float) -> str:
    return (
        "❌ <b>Not enough credits</b>\n\n"
        f"This needs <b>{needed:g}</b> credits, you have <b>{have:g}</b>.\n"
        f"Short by <b>{needed - have:g}</b>.\n\n"
        "Tap below to top up."
    )


# --- top-ups ----------------------------------------------------------------
# The payment screens, folded in from the deployed box. They used to live on a
# branch of their own; from here there is one tree, so this is the only copy.

def topup_intro(balance: float, floor: int, per_credit: float) -> str:
    """
    The Add Credit screen.

    The rate goes through `rate_text`, not an f-string of its own. `per_credit`
    is rupees-per-credit, so at 1.5 credits per rupee it is 0.6667 and printing
    it directly reads "₹0.6667 = 1 credit" — which is true, unusable, and gets
    reported as a pricing bug. `rate_text` turns it back up the right way.
    """
    return (
        "💳 <b>Add Credit</b>\n"
        "──────────────────\n"
        f"💰 Balance now: <b>{balance:g} credits</b>\n"
        f"💱 Rate: {rate_text(per_credit)}\n"
        f"🔖 Minimum: <b>₹{floor:g}</b> · no maximum\n"
        "\n"
        "Pick an amount, or type your own 👇"
    )


def topup_ask_amount(floor: int) -> str:
    return (
        "✏️ <b>How much do you want to add?</b>\n\n"
        f"Send me the amount in rupees — minimum <b>₹{floor:g}</b>.\n"
        f"<i>Just the number, like</i> <code>{floor * 3}</code>"
    )


def topup_bad_amount(reason: str, floor: int) -> str:
    return (
        f"❌ {esc(reason)}\n\n"
        f"Send the amount in rupees — a whole number, at least <b>₹{floor:g}</b>."
    )


def payment_card(q, *, qr: bool = True, minutes: int = 10) -> str:
    """
    The pay screen. Caption of the QR photo, so it stays well under 1024 chars.

    The exact-amount warning is not decoration. Each live order is quoted a
    different figure a few paise below the listed price, and that figure is what
    identifies the payment — a user who rounds up to ₹20 has paid an amount that
    matches nothing, and someone has to sort it out by hand.
    """
    how = ("📲 Scan the QR with GPay / PhonePe / Paytm — any UPI app."
           if qr else "📲 Open any UPI app and pay to the ID below.")
    lines = [
        f"💳 <b>Pay ₹{q.amount_rupees:.2f}</b>",
        "──────────────────",
        how,
        "",
        f"💸 Amount   <code>{q.amount_rupees:.2f}</code>",
        f"🏦 UPI ID   <code>{esc(q.upi_id)}</code>",
    ]
    if q.payee:
        lines.append(f"👤 Payee     {esc(q.payee)}")
    lines.append("")
    lines.append(
        f"⚠️ <b>Pay exactly ₹{q.amount_rupees:.2f}</b>"
        + (f", not ₹{q.rupees:g}" if q.discount_paise else "")
        + f" — that exact figure is how I recognise your payment. "
          f"You still get <b>{q.credits:g} credits</b>."
    )
    lines.append("")
    lines.append(f"⏳ Valid for <b>{minutes} minutes</b>")
    if q.reference:
        lines.append(f"🧾 Ref <code>{esc(q.reference)}</code>")
    lines.append("")
    lines.append(
        "✅ Credits land on their own, usually within a minute. "
        "Tap below if you don't want to wait."
        if q.auto_confirm else
        "📩 Tap the button below after paying — the admin confirms these by hand "
        "at the moment, so it may take a few minutes."
    )
    return "\n".join(lines)


def payment_claim(row, user) -> str:
    """
    Admin-facing: "this user says they have paid." Sent with a confirm button.

    Everything here comes from the order row rather than from the user, so the
    amount an admin is about to approve is the amount the bot quoted.
    """
    paise = int(row["amount_paise"] or 0)
    return (
        "💰 <b>Payment claim</b>\n"
        "──────────────────\n"
        f"👤 {esc(getattr(user, 'first_name', '') or 'user')} "
        f"{esc(getattr(user, 'handle', ''))}\n"
        f"🆔 <code>{row['user_id']}</code>\n"
        "\n"
        f"💸 Expected  <b>₹{paise / 100:.2f}</b>"
        + (f"  <i>(listed ₹{float(row['rupees']):g})</i>\n" if paise else "\n")
        + f"🎁 Credits   {float(row['credits']):g}\n"
        + (f"🧾 Ref <code>{esc(row['reference'])}</code>\n" if row["reference"] else "")
        + f"📄 Order <code>{esc(row['order_id'])}</code>\n"
        "\n"
        "<i>Check the bank app for that exact amount before confirming.</i>"
    )


def payment_not_yet(minutes_left: int, auto_confirm: bool) -> str:
    when = (f"⏳ This order is valid for <b>{minutes_left} more minute"
            f"{'s' if minutes_left != 1 else ''}</b>.\n" if minutes_left else "")
    tail = ("I check the bank inbox every few seconds — if you have just paid, "
            "give it a minute and tap again."
            if auto_confirm else
            "The admin has been told. You will get the credits as soon as it is "
            "confirmed — no need to pay again.")
    return f"🔍 <b>Not showing as paid yet</b>\n\n{when}\n{tail}"


def payment_paid(s) -> str:
    return (
        "🎉 <b>Payment received!</b>\n"
        "──────────────────\n"
        f"💸 Paid        ₹{s.amount_paise / 100:.2f}\n"
        f"🎁 Added       <b>+{s.credits_added:g} credits</b>\n"
        f"💰 Balance     <b>{s.new_balance:g} credits</b>\n"
        + (f"🧾 Bank ref    <code>{esc(s.bank_ref)}</code>\n" if s.bank_ref else "")
        + "\nThank you! Pick a service below 👇"
    )
