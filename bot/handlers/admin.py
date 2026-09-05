"""
Admin panel — users, credits, and whether the box is healthy.

Every handler in here is gated on `cfg.is_admin`, checked on the callback's own
`from_user.id`. That matters more than it looks: callback data is guessable, so a
non-admin can and will try `adm:give:12345` by hand. The gate is on the server,
not on whether the button was shown.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import (broadcast, credits, db, egress, expiry, keyboards as kb, nightly,
                queue as jobq, scratch, settings, state, ui)
from ..config import cfg
from ..providers import terabox as tb
from . import _gate

log = logging.getLogger(__name__)

PAGE = 8
#: How many log entries the PDF carries. A report, not a database dump.
PDF_MAX_ROWS = 600
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

    held = scratch.report()
    lines.append(f"🗂 Scratch    {ui.human_bytes(held['bytes'])} in {held['dirs']} job dir"
                 f"{'s' if held['dirs'] != 1 else ''}"
                 + (f" · <b>{held['orphans']} orphaned</b>" if held["orphans"] else ""))

    if expiry.enabled():
        # Only worth a line while the feature is on: at AUTO_DELETE_MINUTES=0 there is
        # no clock and a permanent "0 messages" row is noise on a card read at a glance.
        waiting = expiry.pending()
        lines.append(f"🧹 Expiring   {expiry.minutes()} min · {waiting} message"
                     f"{'s' if waiting != 1 else ''} on the clock")

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
    lanes = " · ".join(
        f"{lane} {info['running']}/{info['workers']}"
        + (f" (+{info['queued']})" if info["queued"] else "")
        for lane, info in q["lanes"].items()
    )

    return (
        "📊 <b>Bot Stats</b>\n"
        "──────────────────\n"
        f"👥 Users        <b>{users}</b>   (24h active {active}, banned {banned})\n"
        f"✅ Delivered    <b>{done}</b>   ❌ failed {failed}\n"
        f"📦 Data sent    {ui.human_bytes(sent_bytes)}\n"
        "\n"
        f"⚙️ Queue        {q['running']} running · {q['queued']} waiting "
        f"· {q['workers']} workers\n"
        f"🛣 Lanes        {lanes}\n"
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

async def _cookie_lines() -> list[str]:
    """One line per configured cookie, plus what to do about a bad one."""
    pool = tb.cookies()
    if not pool:
        return ["🔑 <b>Cookies</b>   <i>none configured</i>",
                "<i>The signed route is off — every link goes to the fallback.</i>"]

    lines = [f"🔑 <b>Cookies</b>   {len(pool)} configured"]
    for health in await tb.terabox.health():
        if not health.ok:
            lines.append(f"  ✖ <b>#{health.index}</b> errno {health.errno} · "
                         f"{ui.esc(health.detail)}")
            continue
        room = ""
        if health.total_bytes:
            room = (f" · {ui.human_bytes(health.used_bytes)} of "
                    f"{ui.human_bytes(health.total_bytes)} used "
                    f"({health.full_percent:.0f}%)")
        lines.append(f"  ✅ <b>#{health.index}</b> {ui.esc(health.home.split('//')[-1])}"
                     f"{room}")
        if not health.tokens:
            lines.append("      <i>no bdstoken on /main — listings may say 4000020</i>")
        if health.detail:
            lines.append(f"      <i>{ui.esc(health.detail)}</i>")
    return lines


async def _proxy_lines(*, probe: bool) -> list[str]:
    """
    Proxy rotation, and optionally a live test of every address.

    Probing is off by default because it is ten outbound requests, and the card is
    opened to read the cookie state far more often than to re-test a proxy list.
    """
    configured = tuple(p for p in cfg.proxies if p)
    if not configured:
        return ["🌐 <b>Egress</b>   direct only — no proxies configured"]

    benched = egress.benched()
    lines = [f"🌐 <b>Egress</b>   {len(configured)} proxies, "
             f"{len(egress.pool())} in rotation (+ direct)"]
    if benched:
        for proxy, left in sorted(benched.items(), key=lambda kv: -kv[1]):
            lines.append(f"  ⏸ {ui.esc(egress.describe(proxy))} benched "
                         f"{ui.human_time(left)} more")
    if not probe:
        lines.append("<i>Tap “Test proxies” to check every address is alive.</i>")
        return lines

    results = await asyncio.gather(*(egress.probe(p) for p in configured),
                                   return_exceptions=True)
    alive = 0
    for proxy, result in zip(configured, results):
        if isinstance(result, BaseException):
            ok, detail = False, type(result).__name__
        else:
            ok, detail = result
        alive += ok
        lines.append(f"  {'✅' if ok else '✖'} {ui.esc(egress.describe(proxy))}"
                     f" · {ui.esc(detail)}")
    lines.append(f"<i>{alive} of {len(configured)} answered. A dead one costs time, "
                 "never a job — the download is retried directly.</i>")
    return lines


async def _terabox_card(*, probe: bool = False) -> str:
    """
    Why Terabox is or is not working right now, in the order it fails.

    Written for the one question an operator actually has at 2am — "is it the
    cookie?" — so the cookie state comes first and says which host answered, not
    merely yes or no. Extra cookies are described as failover on purpose: they buy
    nothing on speed, and the card is where that expectation gets set.
    """
    lines = ["🔑 <b>Terabox Health</b>", "──────────────────"]
    lines += await _cookie_lines()
    lines.append("")
    lines += await _proxy_lines(probe=probe)
    lines.append("")
    lines.append("🪃 <b>Fallback</b>   "
                 + ("on" if cfg.terabox_fallback else "off")
                 + f" · {len(cfg.terabox_fallback_tokens) or 'guest'} account(s)"
                 + f" · up to {cfg.terabox_fallback_attempts} tries a link")
    gate = tb.terabox.unavailable()
    if gate:
        lines.append("")
        lines.append(f"⛔ <b>Both routes are off</b> — {ui.esc(gate)}")
    return "\n".join(lines)


STATUS_ICONS = {"done": "✅", "failed": "❌", "cancelled": "✖",
                "running": "⏳", "queued": "⏳"}


def _link_rows(limit: int, offset: int = 0) -> list:
    """
    The log of what people have sent, newest first.

    `jobs.source` is written by both services — the URL for a link job, the file
    name for an archive — so this is one list and the icon says which. Rows with an
    empty source are skipped rather than shown blank: those are jobs from before
    the column was filled in.
    """
    return db.query(
        "SELECT * FROM jobs WHERE source != '' ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset))


def _links_card(page: int) -> tuple[str, int, int]:
    """The link log, one page of it. Returns (text, total rows, rows on this page)."""
    total = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE source != ''"))
    rows = _link_rows(PAGE, page * PAGE)

    lines = [f"🔗 <b>Shared Links</b>  <i>({total} total)</i>", "──────────────────"]
    for row in rows:
        when = time.strftime("%d %b %H:%M", time.localtime(row["created_at"]))
        icon = STATUS_ICONS.get(row["status"], "•")
        kind = "🔗" if row["kind"] != "zip" else "🗂"
        size = f" · {ui.human_bytes(row['size_bytes'])}" if row["size_bytes"] else ""
        source = str(row["source"])
        # Trimmed for the card only. The URL is long, the useful half is the front,
        # and the PDF is where the whole thing lives.
        shown = source if len(source) <= 52 else source[:49] + "…"
        lines.append(f"{icon} {when} · <code>{row['user_id']}</code> "
                     f"· {float(row['cost']):g} cr{size}")
        lines.append(f"   {kind} <code>{ui.esc(shown)}</code>")
    if not rows:
        lines.append("<i>Nothing yet — no link or archive has been sent.</i>")
    return "\n".join(lines), total, len(rows)


def _links_pdf(path) -> tuple[object, int]:
    """
    The whole log as a PDF. Returns (path, row count).

    Capped at `PDF_MAX_ROWS` because this is a report, not a database dump: fifty
    pages nobody scrolls costs an upload and buys nothing, and the cap is stated on
    the last page so it is never silently truncated.
    """
    from .. import pdf

    total = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE source != ''"))
    rows = _link_rows(PDF_MAX_ROWS)
    done = sum(1 for row in rows if row["status"] == "done")
    sent_bytes = sum(int(row["size_bytes"] or 0) for row in rows)

    doc = pdf.Writer("TeraBot - shared links",
                     f"exported {nightly.ist_stamp()} · {len(rows)} of {total} entries")
    doc.line(f"delivered {done} · data {ui.human_bytes(sent_bytes)} · "
             f"spent {sum(float(row['cost'] or 0) for row in rows):g} credits")
    doc.gap()

    for row in rows:
        when = time.strftime("%d %b %Y %H:%M", time.localtime(row["created_at"]))
        head = (f"#{row['id']:<6} {when}  {row['status']:<9} {row['kind']:<8} "
                f"{float(row['cost'] or 0):g} cr")
        if row["size_bytes"]:
            head += f"  {ui.human_bytes(row['size_bytes'])}"
        doc.line(head, bold=True)
        doc.line(f"   user {row['user_id']}"
                 + (f"  ·  {row['quality']}" if row["quality"] else ""))
        doc.wrapped(f"   {row['source']}", indent="      ")
        if row["file_name"]:
            doc.wrapped(f"   -> {row['file_name']}", indent="      ")
        if row["error"]:
            doc.wrapped(f"   ! {row['error']}", indent="      ")
        doc.gap(5.0)

    if total > len(rows):
        doc.gap()
        doc.line(f"Only the {len(rows)} most recent of {total} entries are listed.",
                 bold=True)
    return doc.save(path), len(rows)


async def daily_report(jobs: jobq.Queue) -> str:
    """
    The nightly card, composed from the same renderers the panel uses.

    One message, so it survives being read on a phone at midnight: the day's
    numbers first — which is the part that is actually new — then the server, the
    bot and the cookies underneath it. The cookie probe is network, so it is
    wrapped: a report that arrives without the cookie section still tells the
    operator the disk is filling up, which is the thing the report is for.
    """
    day = db.now() - 86400
    done = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'done' "
                         "AND COALESCE(finished_at, created_at) > ?", (day,)))
    failed = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'failed' "
                           "AND COALESCE(finished_at, created_at) > ?", (day,)))
    data = int(db.scalar("SELECT SUM(size_bytes) FROM jobs WHERE status = 'done' "
                         "AND COALESCE(finished_at, created_at) > ?", (day,)))
    links = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE kind != 'zip' "
                          "AND created_at > ?", (day,)))
    zips = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE kind = 'zip' "
                         "AND created_at > ?", (day,)))
    joined = int(db.scalar("SELECT COUNT(*) FROM users WHERE joined_at > ?", (day,)))
    spent = float(db.scalar("SELECT SUM(-delta) FROM ledger WHERE delta < 0 "
                            "AND created_at > ?", (day,)) or 0)

    parts = [
        "🌙 <b>Nightly report</b>",
        f"<i>{nightly.ist_stamp()}</i>",
        "──────────────────",
        "📆 <b>Last 24 hours</b>",
        f"✅ Delivered    <b>{done}</b>   ❌ failed {failed}",
        f"📦 Data sent    {ui.human_bytes(data)}",
        f"🔗 Sent in      {links} link job{'s' if links != 1 else ''} · "
        f"{zips} archive{'s' if zips != 1 else ''}",
        f"👥 New users    {joined}",
        f"📉 Credits used {spent:g}",
        "",
        _vps_card(),
        "",
        _bot_card(jobs),
    ]
    try:
        parts.append("\n".join(await _cookie_lines()))
    except Exception as exc:                 # noqa: BLE001 - a report, not a job
        log.warning("nightly cookie check failed", exc_info=True)
        parts.append(f"🔑 <i>cookie check did not finish: {ui.esc(exc)}</i>")
    return "\n".join(parts)


#: The mode names the price prompt uses, one per editable key. Built from
#: `settings.EDITABLE` rather than typed out so that adding a seventh price cannot
#: leave a button whose reply nothing is listening for.
PRICE_MODES = tuple(f"admin_price_{name}" for name in settings.EDITABLE)


def _prices_card() -> str:
    """
    Every editable price, and whether it has been changed since install.

    The rate is shown as both sentences — "₹1 = 1.5 credits" is what the operator
    thinks in, "1 credit = ₹0.67" is what a user asks about — because deriving the
    second one in the head is exactly how the wrong number ends up on a screen.
    """
    live = settings.all_prices()
    per_rupee = live["credits_per_rupee"]
    if per_rupee > 0:
        rate = (f"💱 ₹1 = <b>{per_rupee:g} credits</b>"
                f"  ·  1 credit = <b>₹{1 / per_rupee:.2f}</b>")
    else:
        rate = "💱 <i>the rate is not set</i>"
    lines = ["⚙️ <b>Prices</b>", "──────────────────", rate, ""]
    for name, (label, unit, _ceiling) in settings.EDITABLE.items():
        if name == "credits_per_rupee":
            continue
        mark = "" if settings.is_default(name) else "  ✏️"
        lines.append(f"• {label} — <b>{live[name]:g} {unit}</b>{mark}")
    lines += [
        "",
        "<i>✏️ marks a price changed from the installed default. Tap one to change "
        "it — it applies to the very next message, with no restart.</i>",
    ]
    return "\n".join(lines)


def _price_keys():
    """The ⚙️ Prices keyboard. One button per editable key, in `EDITABLE` order."""
    labels = {name: label for name, (label, _u, _c) in settings.EDITABLE.items()}
    return kb.admin_prices(list(settings.EDITABLE), labels)


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

    @app.on_callback_query(filters.regex(r"^adm:price$"))
    async def prices_panel(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        state.clear_mode(cq.from_user.id)
        await cq.answer()
        await cq.message.edit_text(_prices_card(), reply_markup=_price_keys())

    @app.on_callback_query(filters.regex(r"^adm:price:reset:([a-z0-9_]+)$"))
    async def price_reset(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        name = cq.data.split(":")[3]
        if name not in settings.EDITABLE:
            await cq.answer("No such price.", show_alert=True)
            return
        state.clear_mode(cq.from_user.id)
        value = settings.reset(name)
        log.info("admin %s reset %s to the installed default %g",
                 cq.from_user.id, name, value)
        await cq.answer(f"{settings.EDITABLE[name][0]} is back to {value:g}.")
        await cq.message.edit_text(_prices_card(), reply_markup=_price_keys())

    @app.on_callback_query(filters.regex(r"^adm:price:([a-z0-9_]+)$"))
    async def price_ask(client: Client, cq: CallbackQuery) -> None:
        """
        Ask for one new number.

        Declared *after* the reset handler above on purpose: `adm:price:reset:…` also
        matches this pattern, with `reset` captured as the name. Pyrogram stops at the
        first callback handler whose filter matches, so the specific one has to come
        first — the `EDITABLE` check below is the second line of defence, not the first.
        """
        if await _deny(cq):
            return
        name = cq.data.split(":")[2]
        if name not in settings.EDITABLE:
            await cq.answer("No such price.", show_alert=True)
            return
        label, unit, ceiling = settings.EDITABLE[name]
        now = settings.get(name)
        state.set_mode(cq.from_user.id, f"admin_price_{name}")
        await cq.answer()
        await cq.message.edit_text(
            f"⚙️ <b>{ui.esc(label)}</b>\n"
            "──────────────────\n"
            f"Now: <b>{now:g} {unit}</b>\n\n"
            f"Send the new number. Halves are fine (<code>1.5</code>), up to "
            f"<b>{ceiling:g}</b>.\n\n"
            "<i>It applies to the very next message — nothing restarts.</i>",
            reply_markup=kb.admin_price_edit(name, settings.is_default(name)))

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode(*PRICE_MODES))
    async def price_typed(client: Client, message: Message) -> None:
        """The number typed after ⚙️ Prices. The mode gate does the addressing."""
        user_id = message.from_user.id
        if not _is_admin(user_id):
            # Belt and braces, as in `admin_typing`: the mode is only ever set by an
            # admin-only callback, but authority is re-checked in every handler.
            return
        entry = state.get_mode(user_id)
        if not entry:            # swept between the filter and here — very unlikely
            return
        name = entry[0][len("admin_price_"):]
        if name not in settings.EDITABLE:
            state.clear_mode(user_id)
            return
        label, unit, _ceiling = settings.EDITABLE[name]
        # A comma for a decimal point, and a stray ₹, because that is how the number
        # gets typed on a phone. Anything else `settings.set` refuses in words.
        text = (message.text or "").strip().replace(",", ".").lstrip("₹").strip()
        try:
            value = settings.set(name, text)
        except settings.BadValue as exc:
            # The message was written to be read by the admin, so it is shown as-is.
            # The mode stays set: a typo should cost one more line, not another trip
            # through the menu.
            await message.reply_text(f"⚠️ {ui.esc(exc)}\n\nSend the number again.")
            return
        state.clear_mode(user_id)
        await _gate.forget(message, "the typed price")
        log.info("admin %s set %s = %g", user_id, name, value)
        await message.reply_text(
            f"✅ <b>{ui.esc(label)} is now {value:g} {unit}</b>\n\n{_prices_card()}",
            reply_markup=_price_keys())

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

    @app.on_callback_query(filters.regex(r"^adm:say$"))
    async def announce_start(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        await cq.answer()
        state.set_mode(cq.from_user.id, "admin_announce")
        await cq.message.edit_text(
            f"📢 <b>Announce to everyone</b>\n\n"
            f"Send me the message. It goes to all <b>{len(broadcast.audience())}</b> "
            "users, exactly as you type it.\n\n"
            "<i>HTML works: </i><code>&lt;b&gt;bold&lt;/b&gt;</code><i>, "
            "</i><code>&lt;a href=\"…\"&gt;link&lt;/a&gt;</code><i>. "
            "You will see it first and have to confirm.</i>",
            reply_markup=kb.back_to_menu("✖  Cancel"))

    @app.on_callback_query(filters.regex(r"^adm:say:go:(?P<token>[\w-]+)$"))
    async def announce_send(client: Client, cq: CallbackQuery) -> None:
        """
        The second tap. The draft was parked, so the text cannot have been edited
        between the preview and the send — and a stale button after a restart says so
        rather than broadcasting something nobody has looked at.
        """
        if await _deny(cq):
            return
        parked = state.take(cq.data.split(":")[3], cq.from_user.id)
        if parked is None:
            await cq.answer("That draft has expired — write it again.", show_alert=True)
            return
        text = str(parked.payload.get("text") or "")
        if not text:
            await cq.answer("That draft was empty.", show_alert=True)
            return

        await cq.answer("Sending…")
        panel = cq.message
        throttle = ui.Throttle(4.0)

        async def on_progress(done: int, total: int) -> None:
            if not throttle.ready(force=done >= total):
                return
            await panel.edit_text(
                f"📢 <b>Sending…</b>\n\n{ui.bar(done, total)}  {done}/{total}")

        result = await broadcast.send_to_all(client, text, on_progress=on_progress)
        try:
            await panel.edit_text(result.card, reply_markup=kb.admin_menu())
        except Exception:
            await client.send_message(cq.from_user.id, result.card,
                                      reply_markup=kb.admin_menu())

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode("admin_announce"))
    async def announce_typing(client: Client, message: Message) -> None:
        """The draft, shown back exactly as it will arrive, before anything is sent."""
        user_id = message.from_user.id
        if not _is_admin(user_id):
            return
        text = (message.text or "").strip()
        if not text:
            return
        state.clear_mode(user_id)
        parked = state.park(user_id, "admin_announce", text=text)
        count = len(broadcast.audience())
        try:
            await message.reply_text(text, disable_web_page_preview=True)
        except Exception as exc:
            # Bad HTML is the one mistake worth catching here: it would fail on every
            # single send, one user at a time, after the confirm.
            await message.reply_text(
                f"⚠️ Telegram would not accept that text: {ui.esc(exc)}\n\n"
                "Fix the formatting and send it again.",
                reply_markup=kb.admin_menu())
            state.take(parked.token, user_id)
            return
        await message.reply_text(
            f"👆 <b>This is what {count} people will get.</b> Send it?",
            reply_markup=kb.admin_announce(parked.token))

    @app.on_callback_query(filters.regex(r"^adm:links:(\d+)$"))
    async def links_page(client: Client, cq: CallbackQuery) -> None:
        if await _deny(cq):
            return
        page = int(cq.data.split(":")[2])
        text, total, _ = _links_card(page)
        await cq.answer()
        await cq.message.edit_text(
            text, reply_markup=kb.admin_links_page(
                page, page > 0, (page + 1) * PAGE < total),
            disable_web_page_preview=True)

    @app.on_callback_query(filters.regex(r"^adm:links:pdf$"))
    async def links_pdf(client: Client, cq: CallbackQuery) -> None:
        """
        Write the log to a PDF, send it, delete it.

        Written under `scratch` rather than straight into `work_dir` so the janitor
        owns it if the upload dies halfway: nothing this bot makes is allowed to
        outlive the message it was made for. It goes out as a *new* message and the
        card stays put, because the card is what the admin is reading.
        """
        if await _deny(cq):
            return
        if not int(db.scalar("SELECT COUNT(*) FROM jobs WHERE source != ''")):
            await cq.answer("Nothing to export yet.", show_alert=True)
            return

        await cq.answer("building the PDF…")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        work = scratch.claim(cfg.work_dir / f"pdf-{cq.from_user.id}-{stamp}")
        try:
            path, rows = await asyncio.to_thread(
                _links_pdf, work / f"terabot-links-{stamp}.pdf")
            await client.send_document(
                cq.message.chat.id, str(path),
                caption=f"🔗 <b>{rows} shared link{'s' if rows != 1 else ''}</b>\n"
                        f"<i>exported {nightly.ist_stamp()}</i>")
        except Exception as exc:              # noqa: BLE001 - an export, not a job
            log.warning("link export failed", exc_info=True)
            await client.send_message(cq.message.chat.id,
                                      f"❌ Could not build the PDF: {ui.esc(exc)}")
        finally:
            scratch.release(work)

    @app.on_callback_query(filters.regex(r"^adm:report$"))
    async def report_now(client: Client, cq: CallbackQuery) -> None:
        """The nightly report, on demand — the only way to check it before midnight."""
        if await _deny(cq):
            return
        await cq.answer("building the report…")
        try:
            text = await daily_report(jobs)
        except Exception as exc:              # noqa: BLE001 - a report, not a job
            log.warning("manual report failed", exc_info=True)
            text = f"🌙 <b>Nightly report</b>\n\nCould not finish it: {ui.esc(exc)}"
        try:
            await cq.message.edit_text(text, reply_markup=kb.admin_menu(),
                                       disable_web_page_preview=True)
        except Exception:
            await client.send_message(cq.message.chat.id, text,
                                      disable_web_page_preview=True)

    @app.on_callback_query(filters.regex(r"^adm:tbox(?::(proxies|unbench))?$"))
    async def terabox_health(client: Client, cq: CallbackQuery) -> None:
        """
        The cookie/proxy card. One handler for all three actions on it.

        `cq.answer()` goes out first and the card is edited afterwards, because
        probing five cookies and ten proxies is seconds of network and Telegram
        times a callback out in far less than that — an unanswered callback leaves
        the button spinning and the admin pressing it again, which starts the whole
        probe a second time.
        """
        if await _deny(cq):
            return
        action = (cq.data.split(":") + ["", ""])[2]
        if action == "unbench":
            egress.clear_bench()
            await cq.answer("Every proxy is back in the rotation.")
        else:
            await cq.answer("checking…")
        try:
            card = await _terabox_card(probe=action == "proxies")
        except Exception as exc:                # noqa: BLE001 - a card, not a job
            log.warning("terabox health card failed", exc_info=True)
            card = ("🔑 <b>Terabox Health</b>\n──────────────────\n"
                    f"Could not finish the check: {ui.esc(exc)}")
        try:
            await cq.message.edit_text(
                card, reply_markup=kb.admin_health(bool(egress.benched())))
        except Exception:
            # Same text as last time -> MESSAGE_NOT_MODIFIED. Nothing to report.
            pass

# __ADMIN_TAIL__
