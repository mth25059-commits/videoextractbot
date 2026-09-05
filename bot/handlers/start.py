"""
/start, the main menu, and the account card.

Also the place where a brand-new user is registered, given the joining bonus,
and reported to the admin — all three happen in `credits.ensure`, which returns
`is_new` so the alert fires exactly once per user, ever.
"""

from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import credits, db, keyboards as kb, state, ui
from ..config import cfg

log = logging.getLogger(__name__)


async def _touch(client: Client, user) -> tuple[credits.User, bool]:
    """Register or refresh the user, and alert admins the first time we see them."""
    record, is_new = credits.ensure(user.id, user.first_name or "", user.username)
    if is_new:
        total = int(db.scalar("SELECT COUNT(*) FROM users"))
        text = ui.new_user_alert(record, total)
        for admin_id in cfg.admin_ids:
            try:
                await client.send_message(admin_id, text)
            except Exception as exc:  # a blocked admin must not break /start
                log.warning("could not alert admin %s: %s", admin_id, exc)
    return record, is_new


def register(app: Client) -> None:

    @app.on_message(filters.command("start") & filters.private)
    async def start_cmd(client: Client, message: Message) -> None:
        state.clear_mode(message.from_user.id)
        user, is_new = await _touch(client, message.from_user)

        if user.banned:
            await message.reply_text("🚫 Your access to this bot has been disabled.")
            return

        # Two messages, on purpose. A reply keyboard and an inline keyboard cannot
        # ride on the same message, and the persistent one has to be installed at
        # least once or none of its keys exist. The first message is the one that
        # installs it; the second follows and reads as the reply to /start.
        await message.reply_text(
            "⌨️ <i>Buttons are in your keyboard now — no need to press "
            "/start again. Just paste a link whenever you like.</i>",
            reply_markup=kb.home_keys())

        if is_new:
            # A first-timer gets the manual instead of the menu, because a menu of
            # six buttons answers "what can I press" and not one of "what does this
            # cost", "what do I send it", or "why is it taking so long" — which are
            # the three questions that otherwise arrive by message. Its ▶ button
            # runs `nav:menu`, so the guide *becomes* the menu in place: one screen
            # to read, one tap, no second message to scroll past.
            await message.reply_text(ui.guide(is_new=True),
                                     reply_markup=kb.guide_nav(is_new=True),
                                     disable_web_page_preview=True)
            return

        await message.reply_text(
            ui.welcome(user.first_name, user.credits, is_new, cfg.free_credits_on_join),
            reply_markup=kb.main_menu(cfg.is_admin(user.user_id)),
            disable_web_page_preview=True,
        )

    @app.on_message(filters.command("balance") & filters.private)
    async def balance_cmd(client: Client, message: Message) -> None:
        user, _ = await _touch(client, message.from_user)
        await message.reply_text(f"💰 Balance: <b>{user.credits:g} credits</b>")

    @app.on_callback_query(filters.regex(r"^nav:menu$"))
    async def back_to_menu(client: Client, cq: CallbackQuery) -> None:
        state.clear_mode(cq.from_user.id)
        user, _ = await _touch(client, cq.from_user)
        await cq.answer()
        await cq.message.edit_text(
            ui.welcome(user.first_name, user.credits, False, 0),
            reply_markup=kb.main_menu(cfg.is_admin(user.user_id)),
        )

    @app.on_callback_query(filters.regex(r"^help:open$"))
    async def how_it_works(client: Client, cq: CallbackQuery) -> None:
        """
        The manual, from the menu. Same text a new user was shown.

        `state.clear_mode` is here because this is reachable while the bot is waiting
        for a link: reading the instructions is a way of backing out of a prompt, and
        leaving the mode set means the next thing typed is answered by a handler the
        user has already navigated away from.
        """
        state.clear_mode(cq.from_user.id)
        await cq.answer()
        await cq.message.edit_text(ui.guide(), reply_markup=kb.guide_nav(),
                                   disable_web_page_preview=True)

    @app.on_callback_query(filters.regex(r"^acct:open$"))
    async def account(client: Client, cq: CallbackQuery) -> None:
        user, _ = await _touch(client, cq.from_user)
        jobs_done = int(db.scalar(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'done'",
            (user.user_id,),
        ))
        has_history = bool(credits.history(user.user_id, 1))
        await cq.answer()
        await cq.message.edit_text(
            ui.account_card(user, jobs_done),
            reply_markup=kb.account(has_history),
        )

    @app.on_callback_query(filters.regex(r"^acct:history$"))
    async def history(client: Client, cq: CallbackQuery) -> None:
        rows = credits.history(cq.from_user.id, 15)
        if not rows:
            await cq.answer("No credit activity yet.", show_alert=True)
            return

        lines = ["🧾 <b>Credit History</b>", "──────────────────"]
        for row in rows:
            sign = "＋" if row["delta"] > 0 else "－"
            lines.append(
                f"{sign}{abs(row['delta']):g}  ·  {ui.esc(row['reason'])}"
                f"  <i>(bal {row['balance']:g})</i>"
            )
        await cq.answer()
        await cq.message.edit_text("\n".join(lines), reply_markup=kb.account(True))
