"""
Admin panel — users, credits, and whether the box is healthy.

Every handler in here is gated on `cfg.is_admin`, checked on the callback's own
`from_user.id`. That matters more than it looks: callback data is guessable, so a
non-admin can and will try `adm:give:12345` by hand. The gate is on the server,
not on whether the button was shown.
"""

from __future__ import annotations

import logging
import shutil
import time

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import credits, db, keyboards as kb, queue as jobq, state, ui
from ..config import cfg
from . import _gate

log = logging.getLogger(__name__)

PAGE = 8
_BOOTED = time.monotonic()


def _is_admin(user_id: int) -> bool:
    return cfg.is_admin(user_id)


def _vps_card() -> str:
    """Load, memory, disk and uptime. psutil is optional — degrade, do not crash."""
    lines = ["🖥 <b>Server Status</b>", "──────────────────"]
    try:
        import psutil

        cpu = psutil.cpu_percent(interval=0.4)
        mem = psutil.virtual_memory()
        lines += [
            f"⚙️ CPU        {cpu:.0f}%  ({psutil.cpu_count()} cores)",
            f"🧠 Memory     {ui.human_bytes(mem.used)} / {ui.human_bytes(mem.total)}"
            f"  ({mem.percent:.0f}%)",
        ]
        net = psutil.net_io_counters()
        lines.append(f"🌐 Traffic    ↓{ui.human_bytes(net.bytes_recv)}  "
                     f"↑{ui.human_bytes(net.bytes_sent)}")
    except ImportError:
        lines.append("<i>psutil not installed — install it for CPU and memory.</i>")
    except Exception as exc:
        lines.append(f"<i>could not read system stats: {ui.esc(exc)}</i>")

    try:
        usage = shutil.disk_usage(cfg.work_dir)
        lines.append(f"💾 Disk free  {ui.human_bytes(usage.free)} of "
                     f"{ui.human_bytes(usage.total)}")
    except Exception:
        pass

    lines.append(f"⏱ Uptime     {ui.human_time(time.monotonic() - _BOOTED)}")
    return "\n".join(lines)

def _bot_card(jobs: jobq.Queue) -> str:
    users = int(db.scalar("SELECT COUNT(*) FROM users"))
    active = int(db.scalar(
        "SELECT COUNT(*) FROM users WHERE last_seen > ?", (db.now() - 86400,)))
    banned = int(db.scalar("SELECT COUNT(*) FROM users WHERE banned = 1"))
    done = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'done'"))
    failed = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'failed'"))
    sent_bytes = int(db.scalar("SELECT SUM(size_bytes) FROM jobs WHERE status = 'done'"))
    outstanding = float(db.scalar("SELECT SUM(credits) FROM users") or 0)
    topped = float(db.scalar("SELECT SUM(total_topup) FROM users") or 0)
    spent = float(db.scalar("SELECT SUM(total_spent) FROM users") or 0)
    q = jobs.stats()

    return (
        "📊 <b>Bot Stats</b>\n"
        "──────────────────\n"
        f"👥 Users        <b>{users}</b>   (24h active {active}, banned {banned})\n"
        f"✅ Delivered    <b>{done}</b>   ❌ failed {failed}\n"
        f"📦 Data sent    {ui.human_bytes(sent_bytes)}\n"
        "\n"
        f"⚙️ Queue        {q['running']} running · {q['queued']} waiting "
        f"· {q['workers']} workers\n"
        f"🧩 Sessions     {state.stats()['pending']} pending\n"
        "\n"
        f"💰 Credits out  <b>{outstanding:g}</b>  (unspent balances)\n"
        f"📈 Topped up    {topped:g}\n"
        f"📉 Used         {spent:g}\n"
    )


def _user_card(user: credits.User) -> str:
    done = int(db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'done'",
        (user.user_id,)))
    failed = int(db.scalar(
        "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'failed'",
        (user.user_id,)))
    data = int(db.scalar(
        "SELECT SUM(size_bytes) FROM jobs WHERE user_id = ? AND status = 'done'",
        (user.user_id,)))
    return (
        f"👤 <b>{ui.esc(user.first_name)}</b>  {ui.esc(user.handle)}\n"
        "──────────────────\n"
        f"🆔 <code>{user.user_id}</code>\n"
        f"📅 Joined     {time.strftime('%d %b %Y', time.localtime(user.joined_at))}\n"
        f"👀 Last seen  {ui.human_time(db.now() - user.last_seen)} ago\n"
        "\n"
        f"💰 Balance    <b>{user.credits:g} cr</b>\n"
        f"📈 Topped up  {user.total_topup:g} cr\n"
        f"📉 Used       {user.total_spent:g} cr\n"
        "\n"
        f"✅ Delivered  {done}   ❌ failed {failed}\n"
        f"📦 Data       {ui.human_bytes(data)}\n"
        + ("\n🚫 <b>BANNED</b>" if user.banned else "")
    )

def register(app: Client, jobs: jobq.Queue) -> None:

    async def _deny(cq: CallbackQuery) -> bool:
        if _is_admin(cq.from_user.id):
            return False
        await cq.answer("Not available.", show_alert=True)
        log.warning("non-admin %s tried %s", cq.from_user.id, cq.data)
        return True

    @app.on_callback_query(filters.regex(r"^adm:open$"))
    async def panel(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        await cq.answer()
        await cq.message.edit_text("🛠 <b>Admin Panel</b>", reply_markup=kb.admin_menu())

    @app.on_callback_query(filters.regex(r"^adm:bot$"))
    async def bot_stats(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        await cq.answer()
        await cq.message.edit_text(_bot_card(jobs), reply_markup=kb.admin_menu())

    @app.on_callback_query(filters.regex(r"^adm:vps$"))
    async def vps_stats(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        await cq.answer("reading…")
        await cq.message.edit_text(_vps_card(), reply_markup=kb.admin_menu())

    @app.on_callback_query(filters.regex(r"^adm:users:(\d+)$"))
    async def users_page(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        page = int(cq.data.split(":")[2])
        total = int(db.scalar("SELECT COUNT(*) FROM users"))
        rows = db.query(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?",
            (PAGE, page * PAGE))

        lines = [f"👥 <b>Users</b>  <i>({total} total)</i>", "──────────────────"]
        buttons = []
        for row in rows:
            flag = "🚫" if row["banned"] else "👤"
            handle = f"@{row['username']}" if row["username"] else f"id{row['user_id']}"
            lines.append(f"{flag} {ui.esc(row['first_name'])} · {ui.esc(handle)} "
                         f"· <b>{float(row['credits']):g} cr</b>")
            buttons.append([kb.Btn(f"{flag} {row['first_name'][:18] or row['user_id']}",
                                   callback_data=f"adm:user:{row['user_id']}")])
        buttons.append(kb.admin_users_page(page, page > 0, (page + 1) * PAGE < total))

        await cq.answer()
        await cq.message.edit_text("\n".join(lines), reply_markup=kb.Markup(buttons))

    @app.on_callback_query(filters.regex(r"^adm:user:(\d+)$"))
    async def user_detail(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        user_id = int(cq.data.split(":")[2])
        user = credits.get(user_id)
        if user is None:
            await cq.answer("No such user.", show_alert=True)
            return
        await cq.answer()
        await cq.message.edit_text(_user_card(user),
                                   reply_markup=kb.admin_user_row(user_id))

    @app.on_callback_query(filters.regex(r"^adm:ban:(\d+)$"))
    async def ban_toggle(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        user_id = int(cq.data.split(":")[2])
        if cfg.is_admin(user_id):
            await cq.answer("You cannot ban an admin.", show_alert=True)
            return
        user = credits.get(user_id)
        if user is None:
            await cq.answer("No such user.", show_alert=True)
            return
        now_banned = 0 if user.banned else 1
        db.execute("UPDATE users SET banned = ? WHERE user_id = ?", (now_banned, user_id))
        await cq.answer("Banned." if now_banned else "Unbanned.")
        await cq.message.edit_text(_user_card(credits.get(user_id)),
                                   reply_markup=kb.admin_user_row(user_id))

    @app.on_callback_query(filters.regex(r"^adm:hist:(\d+)$"))
    async def their_history(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        user_id = int(cq.data.split(":")[2])
        rows = credits.history(user_id, 15)
        if not rows:
            await cq.answer("No credit activity.", show_alert=True)
            return
        lines = [f"🧾 <b>Credit history</b> · <code>{user_id}</code>", "──────────────────"]
        for row in rows:
            sign = "＋" if row["delta"] > 0 else "－"
            lines.append(f"{sign}{abs(row['delta']):g}  ·  {ui.esc(row['reason'])}"
                         f"  <i>(bal {row['balance']:g})</i>")
        await cq.answer()
        await cq.message.edit_text("\n".join(lines),
                                   reply_markup=kb.admin_user_row(user_id))

    @app.on_callback_query(filters.regex(r"^adm:give(?::(\d+))?$"))
    async def give_start(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        parts = cq.data.split(":")
        target = parts[2] if len(parts) > 2 and parts[2] else ""
        await cq.answer()
        if target:
            state.set_mode(cq.from_user.id, "admin_give_amount", target=int(target))
            await cq.message.edit_text(
                f"🎁 <b>Give credits</b>\n\nTo <code>{target}</code>.\n"
                "Send the number of credits (a minus sign takes them away).",
                reply_markup=kb.back_to_menu("✖  Cancel"))
        else:
            state.set_mode(cq.from_user.id, "admin_give_user")
            await cq.message.edit_text(
                "🎁 <b>Give credits</b>\n\nSend the user id first.",
                reply_markup=kb.back_to_menu("✖  Cancel"))

    @app.on_callback_query(filters.regex(r"^adm:orders$"))
    async def orders(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        rows = db.query("SELECT * FROM orders ORDER BY created_at DESC LIMIT 12")
        if not rows:
            await cq.answer("No orders yet.", show_alert=True)
            return
        icons = {"paid": "✅", "pending": "⏳", "holding": "⏳",
                 "expired": "⌛", "cancelled": "✖"}
        lines = ["💰 <b>Recent top-ups</b>", "──────────────────"]
        for row in rows:
            when = time.strftime("%d %b %H:%M", time.localtime(row["created_at"]))
            lines.append(
                f"{icons.get(row['status'], '•')} ₹{float(row['rupees']):g} "
                f"→ {float(row['credits']):g} cr · <code>{row['user_id']}</code> · {when}")
        await cq.answer()
        await cq.message.edit_text("\n".join(lines), reply_markup=kb.admin_menu())

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode("admin_give_user", "admin_give_amount"))
    async def admin_typing(client: Client, message: Message) -> None:
        """Two-step give-credit prompt. The mode gate does the addressing (see _gate)."""
        user_id = message.from_user.id
        if not _is_admin(user_id):
            # Belt and braces: the mode is only ever set by an admin-only callback,
            # but authority is checked on from_user.id in every handler regardless.
            return
        entry = state.get_mode(user_id)
        if not entry:            # swept between the filter and here — very unlikely
            return
        mode, payload = entry
        text = (message.text or "").strip()

        if mode == "admin_give_user":
            if not text.lstrip("-").isdigit():
                await message.reply_text("That is not a user id. Send digits only.")
                return
            target = int(text)
            if credits.get(target) is None:
                await message.reply_text(
                    "No such user — they have to press /start once first.",
                    reply_markup=kb.admin_menu())
                state.clear_mode(user_id)
                return
            state.set_mode(user_id, "admin_give_amount", target=target)
            await message.reply_text(
                f"🎁 How many credits for <code>{target}</code>?\n"
                "<i>A minus sign takes them away.</i>")
            return

        target = int(payload["target"])
        try:
            amount = float(text.replace(",", "."))
        except ValueError:
            await message.reply_text("Send a number, like <code>10</code> or <code>-2.5</code>.")
            return
        if amount == 0:
            await message.reply_text("Zero does nothing.")
            return

        state.clear_mode(user_id)
        try:
            if amount > 0:
                new_balance = credits.grant(target, amount, f"gift from admin {user_id}")
            else:
                new_balance = credits.charge(target, -amount, f"removed by admin {user_id}")
        except credits.InsufficientCredits as exc:
            await message.reply_text(
                f"They only have {exc.available:g} credits.", reply_markup=kb.admin_menu())
            return

        await message.reply_text(
            f"✅ <b>{'Gave' if amount > 0 else 'Removed'} {abs(amount):g} credits</b>\n"
            f"<code>{target}</code> now has <b>{new_balance:g} cr</b>",
            reply_markup=kb.admin_menu())
        try:
            if amount > 0:
                await client.send_message(
                    target,
                    f"🎁 <b>{amount:g} credits added to your account!</b>\n\n"
                    f"💰 New balance: <b>{new_balance:g} credits</b>",
                    reply_markup=kb.payment_done())
        except Exception as exc:
            log.info("could not notify %s: %s", target, exc)

# __ADMIN_TAIL__
