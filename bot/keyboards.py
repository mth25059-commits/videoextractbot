"""
Every inline keyboard the bot shows, in one place.

Callback data is namespaced `area:action:arg` and kept under Telegram's 64-byte
limit. Long values (like a URL) never go in callback data — they are held in the
per-user session in `state.py` and referenced by a short id.
"""

from __future__ import annotations

from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

from .config import cfg


def main_menu(is_admin: bool = False) -> Markup:
    rows = [
        [Btn("💳  Add Credit", callback_data="pay:open")],
        [
            Btn("📦  Terabox", callback_data="mode:terabox"),
            Btn("🗂  ZIP File", callback_data="mode:zip"),
        ],
    ]
    if cfg.show_soon_button:
        rows.append([Btn(cfg.soon_button_label, callback_data="mode:soon")])
    rows.append([Btn("👤  My Account", callback_data="acct:open")])
    if is_admin:
        rows.append([Btn("🛠  Admin Panel", callback_data="adm:open")])
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
    """Quick amounts, plus a free-text option. Anything below the floor is rejected."""
    floor = cfg.min_topup_rupees
    presets = [floor, floor * 5, floor * 10, floor * 25]
    rows, current = [], []
    for rupees in presets:
        current.append(Btn(f"₹{rupees:g}", callback_data=f"pay:amt:{rupees:g}"))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([Btn("✏️  Enter my own amount", callback_data="pay:custom")])
    rows.append([Btn("◀  Back", callback_data="nav:menu")])
    return Markup(rows)


def payment_screen(order_id: str, upi_uri: str) -> Markup:
    return Markup([
        [Btn("📲  Open in UPI app", url=upi_uri)],
        [Btn("✅  I have paid — please check", callback_data=f"pay:check:{order_id}")],
        [Btn("✖  Cancel order", callback_data=f"pay:cancel:{order_id}")],
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
        [Btn("💰  Payments", callback_data="adm:orders")],
        [Btn("◀  Back", callback_data="nav:menu")],
    ])


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
