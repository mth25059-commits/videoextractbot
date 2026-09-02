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


# --- static copy -------------------------------------------------------------

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
