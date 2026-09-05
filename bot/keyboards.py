"""
Every inline keyboard the bot shows, in one place.

Callback data is namespaced `area:action:arg` and kept under Telegram's 64-byte
limit. Long values (like a URL) never go in callback data — they are held in the
per-user session in `state.py` and referenced by a short id.
"""

from __future__ import annotations

from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup
from pyrogram.types import KeyboardButton as Key
from pyrogram.types import ReplyKeyboardMarkup as Keys

from . import settings
from .config import cfg

#: The persistent keyboard's labels. They arrive as ordinary text messages, so the
#: catch-all handler matches on these exact strings — which is why they live here
#: as constants and not inline in two files that could drift apart.
KEY_TERABOX = "📦 Terabox"
KEY_FAP = "🔥 Fap"
KEY_FILE = "🗂 File"
KEY_MENU = "☰ Menu"

#: The four keys, plus a way back to the inline menu for everything else. Credit and
#: Account deliberately do *not* have keys: those screens are owned by other handler
#: modules — one of them on another branch entirely — and a key that has to reach
#: across that seam would be a key that works on the box and not in the branch.
#:
#: Every label in here must have an answer in `handlers/terabox._home_key`. A key
#: that matches nothing is worse than no key: the message goes to the catch-all and
#: the user is told their own button is a "wrong link".
HOME_LABELS = frozenset({KEY_TERABOX, KEY_FAP, KEY_FILE, KEY_MENU})


def home_keys() -> Keys:
    """
    The always-there keyboard in the typing area.

    Inline buttons live on one message, so once that message has scrolled away the
    only way back is `/start` — which is exactly what a user pasting their fifth
    link of the evening had to do. This one sits under the text box permanently,
    and `is_persistent` keeps it open rather than collapsing to an icon.

    Three keys on the top row and Menu full-width under them: the two services and
    the one that is coming are what people reach for, and Menu is the escape hatch,
    so it gets its own row rather than competing for a third of one.

    `placeholder` does real work here: the most common action is not any button at
    all, it is pasting a link, and the box now says so.
    """
    return Keys([[Key(KEY_TERABOX), Key(KEY_FAP), Key(KEY_FILE)], [Key(KEY_MENU)]],
                resize_keyboard=True, is_persistent=True,
                placeholder="Paste a Terabox link…")


def main_menu(is_admin: bool = False) -> Markup:
    rows = [
        [Btn("💳  Add Credit", callback_data="pay:open")],
        [
            Btn("📦  Terabox", callback_data="mode:terabox"),
            Btn("🗂  File", callback_data="mode:zip"),
        ],
    ]
    if cfg.show_soon_button:
        rows.append([Btn(cfg.soon_button_label, callback_data="mode:soon")])
    rows.append([
        Btn("📖  How it works", callback_data="help:open"),
        Btn("👤  My Account", callback_data="acct:open"),
    ])
    if is_admin:
        rows.append([Btn("🛠  Admin Panel", callback_data="adm:open")])
    return Markup(rows)


def guide_nav(is_new: bool = False) -> Markup:
    """
    Under the manual. One button, and which one depends on why it is being read.

    A first-time reader has not seen the menu yet, so the button is the way in; a
    returning one came from the menu and wants to go back to it. Same screen, and
    the difference is one word, but "Back" on the very first message a user ever
    sees points at nothing they remember.
    """
    return Markup([[Btn("▶️  Start using it" if is_new else "◀  Back to Menu",
                        callback_data="nav:menu")]])



def join_gate(channels) -> Markup:
    """
    One URL button per channel, then the verify button on its own row.

    A URL button, not a callback: tapping it opens the channel inside Telegram, and
    the user comes back to a chat that still has ✅ waiting. The verify button is
    last because it is the one thing they should press *after* the others.
    """
    rows = [[Btn(f"📢  Join {c.label}", url=c.link)] for c in channels]
    rows.append([Btn("✅  I've joined — check", callback_data="join:ok")])
    return Markup(rows)


def back_to_menu(label: str = "◀  Back to Menu") -> Markup:
    return Markup([[Btn(label, callback_data="nav:menu")]])


def cancel_only(token: str) -> Markup:
    return Markup([[Btn("✖  Cancel", callback_data=f"job:cancel:{token}")]])


def account(has_history: bool = False) -> Markup:
    rows = [[Btn("💳  Add Credit", callback_data="pay:open")]]
    if has_history:
        rows.append([Btn("🧾  Credit History", callback_data="acct:history")])
    rows.append([Btn("◀  Back", callback_data="nav:menu")])
    return Markup(rows)


# --- topup ------------------------------------------------------------------

def topup_presets() -> Markup:
    """
    Quick amounts, plus a free-text option. Anything below the floor is rejected.

    Each button says what the money *buys*, not just what it costs — `₹20 → 30 cr`.
    The rate is on the screen above it as a sentence, but this is the moment someone
    is deciding how much to send, and a rate they have to do arithmetic on is a rate
    they will get wrong. It is computed from the live `credits_per_rupee`, so changing
    the rate — from the admin panel, with no restart — changes these buttons and there
    is no second number to keep in step.
    """
    floor = cfg.min_topup_rupees
    per_rupee = settings.get("credits_per_rupee")
    presets = [floor, floor * 5, floor * 10, floor * 25]
    rows, current = [], []
    for rupees in presets:
        gives = rupees * per_rupee
        current.append(Btn(f"₹{rupees:g}  →  {gives:g} cr",
                           callback_data=f"pay:amt:{rupees:g}"))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([Btn("✏️  Enter my own amount", callback_data="pay:custom")])
    rows.append([Btn("◀  Back", callback_data="nav:menu")])
    return Markup(rows)


def payment_screen(order_id: str, auto_confirm: bool = True) -> Markup:
    """
    The buttons under the QR.

    There is deliberately no "open in UPI app" button. Telegram only allows
    http/https/tg: in an inline button's URL, so a `upi://pay?…` link there is
    rejected outright (BUTTON_URL_INVALID) and the whole screen fails to send.
    The UPI id and the exact amount go in tap-to-copy <code> blocks instead,
    which also works for someone paying from a second device.
    """
    check = ("✅  I have paid — please check" if auto_confirm
             else "✅  I have paid — tell the admin")
    return Markup([
        [Btn(check, callback_data=f"pay:check:{order_id}")],
        [Btn("✖  Cancel order", callback_data=f"pay:cancel:{order_id}")],
    ])


def payment_pending(order_id: str) -> Markup:
    """Shown when a check found nothing yet — the same screen, minus the noise."""
    return Markup([
        [Btn("🔄  Check again", callback_data=f"pay:check:{order_id}")],
        [Btn("✖  Cancel order", callback_data=f"pay:cancel:{order_id}")],
    ])


def admin_confirm_payment(order_id: str) -> Markup:
    """
    Only reachable by an admin, and only while automatic confirmation is off.

    Without IMAP configured nothing can settle an order on its own, so the
    alternative to this button is a user who has paid and can never be credited.
    """
    return Markup([
        [Btn("✅  Confirm & add credits", callback_data=f"adm:paid:{order_id}")],
        [Btn("🚫  Not received", callback_data=f"adm:unpaid:{order_id}")],
    ])


def payment_done() -> Markup:
    return Markup([
        [Btn("📦  Terabox", callback_data="mode:terabox"),
         Btn("🗂  ZIP File", callback_data="mode:zip")],
        [Btn("◀  Menu", callback_data="nav:menu")],
    ])


# --- quality ----------------------------------------------------------------

def quality_choice(token: str, options: list[tuple[str, float]]) -> Markup:
    """
    options: [(label, credit_cost), ...] already filtered to what the source
    actually offers — never advertise a resolution that is not there.
    """
    rows, current = [], []
    for label, cost in options:
        text = f"{label}  ·  {cost:g} cr"
        current.append(Btn(text, callback_data=f"q:{token}:{label}"))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([Btn("✖  Cancel", callback_data=f"job:cancel:{token}")])
    return Markup(rows)


def confirm_batch(token: str, count: int, cost: float) -> Markup:
    return Markup([
        [Btn(f"▶️  Start  ({count} links · {cost:g} cr)", callback_data=f"job:go:{token}")],
        [Btn("✖  Cancel", callback_data=f"job:cancel:{token}")],
    ])


def not_enough_credit(needed: float) -> Markup:
    return Markup([
        [Btn(f"💳  Add Credit  (need {needed:g})", callback_data="pay:open")],
        [Btn("◀  Menu", callback_data="nav:menu")],
    ])


# --- admin ------------------------------------------------------------------

def admin_menu() -> Markup:
    return Markup([
        [Btn("👥  Users", callback_data="adm:users:0"),
         Btn("🎁  Give Credit", callback_data="adm:give")],
        [Btn("📊  Bot Stats", callback_data="adm:bot"),
         Btn("🖥  VPS Status", callback_data="adm:vps")],
        [Btn("🔗  Shared Links", callback_data="adm:links:0"),
         Btn("📨  Send Report", callback_data="adm:report")],
        [Btn("🔑  Terabox Health", callback_data="adm:tbox")],
        [Btn("⚙️  Prices", callback_data="adm:price"),
         Btn("💰  Payments", callback_data="adm:orders")],
        [Btn("📢  Announce to everyone", callback_data="adm:say")],
        [Btn("◀  Back", callback_data="nav:menu")],
    ])


def admin_prices(names: list[str], labels: dict[str, str]) -> Markup:
    """
    One button per editable price, two to a row.

    The order is `settings.EDITABLE`'s, which is the order the wizard asks in and the
    order the card above lists them — three screens reading the same dict rather than
    three hand-kept lists.
    """
    rows, current = [], []
    for name in names:
        current.append(Btn(labels.get(name, name), callback_data=f"adm:price:{name}"))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([Btn("🛠  Admin", callback_data="adm:open")])
    return Markup(rows)


def admin_price_edit(name: str, is_default: bool) -> Markup:
    """
    Under the "send me the new number" prompt for one price.

    The reset button is hidden while the price still *is* the installed default,
    because a button that does nothing invites a second press and then a bug report.
    """
    rows = []
    if not is_default:
        rows.append([Btn("↩  Back to the installed default",
                         callback_data=f"adm:price:reset:{name}")])
    rows.append([Btn("✖  Cancel", callback_data="adm:price")])
    return Markup(rows)


def admin_announce(token: str) -> Markup:
    """
    The confirm under a drafted announcement.

    A broadcast is the one admin action that cannot be taken back — it is thousands
    of notifications on other people's phones — so it gets a preview and a second
    tap, and the count is on the button so nobody sends to 4,000 people thinking it
    was 40.
    """
    return Markup([
        [Btn("📢  Send it", callback_data=f"adm:say:go:{token}")],
        [Btn("✖  Cancel", callback_data="adm:open")],
    ])


def admin_links_page(page: int, has_prev: bool, has_next: bool) -> Markup:
    """
    Under the link log. The export is one tap and always exports the *whole* log,
    not the page on screen — a paginated PDF would be a worse copy of the screen,
    and the reason to ask for a file at all is to have the lot in one place.
    """
    nav = []
    if has_prev:
        nav.append(Btn("◀", callback_data=f"adm:links:{page - 1}"))
    nav.append(Btn("🛠  Admin", callback_data="adm:open"))
    if has_next:
        nav.append(Btn("▶", callback_data=f"adm:links:{page + 1}"))
    return Markup([[Btn("📄  Export all as PDF", callback_data="adm:links:pdf")], nav])


def admin_health(has_benched: bool = False) -> Markup:
    """Under the Terabox health card. Re-probing is a network call, so it is a
    button rather than something the card does on every open."""
    rows = [[Btn("🔄  Check again", callback_data="adm:tbox"),
             Btn("🌐  Test proxies", callback_data="adm:tbox:proxies")]]
    if has_benched:
        rows.append([Btn("♻️  Put benched proxies back",
                         callback_data="adm:tbox:unbench")])
    rows.append([Btn("◀  Admin", callback_data="adm:open")])
    return Markup(rows)


def admin_user_row(user_id: int) -> Markup:
    return Markup([
        [Btn("🎁  Give Credit", callback_data=f"adm:give:{user_id}"),
         Btn("🚫  Ban / Unban", callback_data=f"adm:ban:{user_id}")],
        [Btn("🧾  Their History", callback_data=f"adm:hist:{user_id}")],
        [Btn("◀  Users", callback_data="adm:users:0")],
    ])


def admin_users_page(page: int, has_prev: bool, has_next: bool) -> list[Btn]:
    nav = []
    if has_prev:
        nav.append(Btn("◀", callback_data=f"adm:users:{page - 1}"))
    nav.append(Btn("🛠  Admin", callback_data="adm:open"))
    if has_next:
        nav.append(Btn("▶", callback_data=f"adm:users:{page + 1}"))
    return nav
