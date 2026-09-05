"""
The top-up screens.

    pay:open ──► pay:amt:<rupees> ─┐
                 pay:custom ──► "await_amount" text ─┴─► QR + the exact amount
                                                            │
                                      pay:check:<order> ────┤
                                      pay:cancel:<order> ───┘

Three things this module is careful about:

* **Only `payments.settle()` moves credits**, and it returns non-None exactly
  once per order. Every screen here keys off that: `None` means "somebody else
  already settled this", and a screen that gets `None` must not tell the user
  credits were just added.
* **The "paid" message is a new message, not an edit.** Editing a caption does
  not make a phone buzz, and someone who has just handed over money should get a
  ping rather than a quietly changed screen further up the chat.
* **Order ownership is re-read from the database.** The user id inside an order
  id is for humans reading the journal; the row is what decides whose order it is.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import credits, keyboards as kb, payments, settings, state, ui
from ..config import cfg
from . import _gate

log = logging.getLogger(__name__)

# UPI itself refuses more than ₹1 lakh in one transfer on most apps. This is not a
# business ceiling — the user asked for none — it is the number above which the
# payment would be declined by the bank rather than by the bot.
UPI_MAX_RUPEES = 100_000

# --- where each order's screen is, so it can be closed off later -------------
#
# In memory on purpose. This is a place in a chat, not a fact about money, and
# the money table should not grow columns to hold cosmetics. If a restart loses
# it the user still gets the "payment received" message — only the tidy-up of the
# old QR is skipped.

_screens: dict[str, tuple[int, int]] = {}
SCREEN_LIMIT = 400


def _remember_screen(order_id: str, chat_id: int, message_id: int) -> None:
    if len(_screens) >= SCREEN_LIMIT:
        for stale in list(_screens)[: SCREEN_LIMIT // 4]:
            _screens.pop(stale, None)
    _screens[order_id] = (chat_id, message_id)


async def _close_screen(client: Client, order_id: str, note: str) -> None:
    """Strike out a finished order's QR. Best effort — it is decoration."""
    where = _screens.pop(order_id, None)
    if where is None:
        return
    chat_id, message_id = where
    try:
        await client.edit_message_caption(chat_id, message_id, note)
    except Exception:
        try:
            await client.edit_message_text(chat_id, message_id, note)
        except Exception:
            pass


# --- helpers -----------------------------------------------------------------

async def _edit(message: Message, text: str, markup: Any = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


def _parse_rupees(text: str) -> tuple[int, str]:
    """(rupees, "") on success, (0, reason) otherwise. Whole rupees only."""
    raw = (text or "").strip().lower()
    for junk in ("₹", ",", "rs.", "rs", "inr", "rupees", "rupee", "/-", " "):
        raw = raw.replace(junk, "")
    if not raw:
        return 0, "That did not look like an amount."
    try:
        value = float(raw)
    except ValueError:
        return 0, f"“{text.strip()[:24]}” is not a number."
    if value != value or value in (float("inf"), float("-inf")):
        return 0, "That is not an amount I can charge."
    if abs(value - round(value)) > 1e-9:
        return 0, "Please use whole rupees, no paise."
    rupees = int(round(value))
    if rupees < cfg.min_topup_rupees:
        return 0, f"The minimum top-up is ₹{cfg.min_topup_rupees:g}."
    if rupees > UPI_MAX_RUPEES:
        return 0, (f"₹{UPI_MAX_RUPEES:,} is the most UPI will move in one payment. "
                   "Send a smaller amount, twice.")
    return rupees, ""


def _mine(order_id: str, user_id: int):
    """The order row, but only if it belongs to this user. Otherwise None."""
    row = payments.get_order(order_id)
    if row is None or int(row["user_id"]) != user_id:
        return None
    return row


async def _open_order(client: Client, user_id: int, chat_id: int, rupees: int,
                      status: Message) -> None:
    """
    Quote the order with paysvc and put the pay screen up.

    `status` is a message already on screen showing progress; it becomes the pay
    screen when there is no QR to send, and is deleted when there is — a QR has
    to be a photo, and a text message cannot be edited into one.
    """
    if not cfg.payments_enabled:
        await _edit(status, "⚠️ Top-ups are switched off on this bot at the moment.",
                    kb.back_to_menu())
        return

    try:
        quote = await payments.quote(user_id, rupees)
    except payments.PaySvcError as exc:
        await _edit(status, f"⚠️ {ui.esc(exc)}", kb.back_to_menu())
        return
    except Exception:
        log.exception("could not quote ₹%s for user %s", rupees, user_id)
        await _edit(status, f"⚠️ {ui.esc(payments.DOWN)}", kb.back_to_menu())
        return

    photo = payments.qr_png(quote.qr_size, quote.qr_cells)
    minutes = round(quote.seconds_left / 60) or cfg.payment_window_minutes
    caption = ui.payment_card(quote, qr=photo is not None, minutes=minutes)
    buttons = kb.payment_screen(quote.order_id, quote.auto_confirm)

    if photo is None:
        await _edit(status, caption, buttons)
        _remember_screen(quote.order_id, chat_id, status.id)
        return

    try:
        sent = await client.send_photo(chat_id, photo, caption=caption,
                                       reply_markup=buttons)
    except Exception:
        # Falling back to text keeps the order payable — the UPI id and the exact
        # amount are in the caption, and they are what the money needs.
        log.exception("could not send the QR for %s", quote.order_id)
        await _edit(status, caption, buttons)
        _remember_screen(quote.order_id, chat_id, status.id)
        return

    _remember_screen(quote.order_id, chat_id, sent.id)
    try:
        await status.delete()
    except Exception:
        pass


async def announce(client: Client, done: payments.Settlement) -> None:
    """
    Tell the user their credits landed, then the admins. Never raises.

    Called from exactly one place per settlement — whichever caller got the
    non-None `Settlement` back — so it cannot double-message. Failures here are
    swallowed: the money has already moved, and letting an exception out would
    make paysvc retry a callback that has nothing left to do.
    """
    await _close_screen(client, done.order_id,
                        f"✅ Paid — {done.credits_added:g} credits added.")
    try:
        await client.send_message(done.user_id, ui.payment_paid(done),
                                  reply_markup=kb.payment_done())
    except Exception as exc:
        log.warning("could not tell user %s about %s: %s",
                    done.user_id, done.order_id, exc)

    note = (f"💰 <b>Top-up settled</b>\n"
            f"₹{done.amount_paise / 100:.2f} from <code>{done.user_id}</code>  ·  "
            f"+{done.credits_added:g} cr  ·  balance {done.new_balance:g}")
    for admin_id in cfg.admin_ids:
        try:
            await client.send_message(admin_id, note)
        except Exception:
            pass


def make_on_paid(app: Client) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """
    The handler `callback_server.serve()` calls when paysvc pushes a settlement.

    Anything raised out of here becomes a 500, which is paysvc's cue to retry —
    so only genuinely retryable failures (a locked database) are allowed out. A
    body the bot cannot use is logged and accepted, because retrying will not
    make it parse.
    """
    async def on_paid(payload: dict[str, Any]) -> None:
        order_id = str(payload.get("orderId") or "").strip()
        if not order_id:
            log.warning("callback: a body with no orderId — ignoring it")
            return

        done = payments.settle(
            order_id,
            amount_paise=int(payload.get("amountPaise") or 0) or None,
            bank_ref=str(payload.get("bankRef") or ""),
            matched_on=str(payload.get("matchedOn") or ""),
            source="callback",
        )
        if done is None:
            return          # unknown order, or one somebody else already settled
        await announce(app, done)

    return on_paid


# --- handlers ----------------------------------------------------------------

async def _show(cq: CallbackQuery, text: str, markup: Any = None) -> None:
    """Replace the screen the button sits on — or add one, if it is a photo."""
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except Exception:
        try:
            await cq.message.reply_text(text, reply_markup=markup)
        except Exception:
            pass


WORKING = ("💳 <b>Creating your order…</b>\n\n"
           "⠋ <i>reserving an amount that is yours alone</i>")


async def _working(cq: CallbackQuery) -> Message:
    try:
        return await cq.message.edit_text(WORKING)
    except Exception:
        return await cq.message.reply_text(WORKING)


def _minutes_left(row) -> int:
    return max(0, int((int(row["expires_at"] or 0) - time.time()) // 60))


def register(app: Client) -> None:

    @app.on_callback_query(filters.regex(r"^pay:open$"))
    async def open_topup(client: Client, cq: CallbackQuery) -> None:
        state.clear_mode(cq.from_user.id)
        await cq.answer()
        await _show(cq, ui.topup_intro(credits.balance(cq.from_user.id),
                                       cfg.min_topup_rupees,
                                       settings.rupees_per_credit()),
                    kb.topup_presets())

    @app.on_callback_query(filters.regex(r"^pay:amt:(\d+(?:\.\d+)?)$"))
    async def preset_amount(client: Client, cq: CallbackQuery) -> None:
        rupees, why = _parse_rupees(cq.matches[0].group(1))
        if not rupees:
            await cq.answer(why, show_alert=True)
            return
        state.clear_mode(cq.from_user.id)
        await cq.answer()
        status = await _working(cq)
        await _open_order(client, cq.from_user.id, cq.message.chat.id, rupees, status)

    @app.on_callback_query(filters.regex(r"^pay:custom$"))
    async def custom_amount(client: Client, cq: CallbackQuery) -> None:
        state.set_mode(cq.from_user.id, "await_amount")
        await cq.answer()
        await _show(cq, ui.topup_ask_amount(cfg.min_topup_rupees),
                    kb.back_to_menu("✖  Cancel"))

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode("await_amount"))
    async def typed_amount(client: Client, message: Message) -> None:
        """Only reached while this user is being asked for an amount (see _gate)."""
        rupees, why = _parse_rupees(message.text or "")
        if not rupees:
            # The mode stays set, so the next thing they type is read as an amount too.
            await message.reply_text(ui.topup_bad_amount(why, cfg.min_topup_rupees),
                                     reply_markup=kb.back_to_menu("✖  Cancel"))
            return
        state.clear_mode(message.from_user.id)
        status = await message.reply_text(WORKING)
        await _open_order(client, message.from_user.id, message.chat.id, rupees, status)

    @app.on_callback_query(filters.regex(r"^pay:check:(\S{1,48})$"))
    async def check_payment(client: Client, cq: CallbackQuery) -> None:
        order_id = cq.matches[0].group(1)
        row = _mine(order_id, cq.from_user.id)
        if row is None:
            await cq.answer("That order is not on your account.", show_alert=True)
            return
        if row["status"] == "paid":
            await cq.answer("Already paid — the credits are in your balance ✅",
                            show_alert=True)
            return

        await cq.answer("🔍 Checking…")
        try:
            status, done = await payments.check(order_id)
        except payments.PaySvcError as exc:
            await cq.message.reply_text(f"⚠️ {ui.esc(exc)}",
                                        reply_markup=kb.payment_pending(order_id))
            return

        if done is not None:
            await announce(client, done)
            return
        if status == "paid":
            # Settled a moment ago by the push, which has already messaged them.
            await cq.message.reply_text("✅ That is already paid — the credits are in.",
                                        reply_markup=kb.payment_done())
            return

        auto = await payments.auto_confirm_enabled()
        if not auto:
            await _claim(client, row, cq.from_user)
        await cq.message.reply_text(ui.payment_not_yet(_minutes_left(row), auto),
                                    reply_markup=kb.payment_pending(order_id))

    @app.on_callback_query(filters.regex(r"^pay:cancel:(\S{1,48})$"))
    async def cancel_payment(client: Client, cq: CallbackQuery) -> None:
        order_id = cq.matches[0].group(1)
        row = _mine(order_id, cq.from_user.id)
        if row is None:
            await cq.answer("That order is not on your account.", show_alert=True)
            return
        if row["status"] == "paid":
            await cq.answer("That one is already paid — nothing to cancel.",
                            show_alert=True)
            return

        await cq.answer("Cancelling…")
        try:
            cancelled, done = await payments.cancel(order_id)
        except payments.PaySvcError as exc:
            await cq.message.reply_text(f"⚠️ {ui.esc(exc)}",
                                        reply_markup=kb.payment_pending(order_id))
            return

        if done is not None:                 # the money landed as they tapped Cancel
            await announce(client, done)
            return
        if not cancelled:
            await cq.message.reply_text("✅ That payment has arrived — credits added.",
                                        reply_markup=kb.payment_done())
            return

        await _close_screen(client, order_id, "✖ Order cancelled. Nothing was charged.")
        await cq.message.reply_text("✖ <b>Order cancelled.</b> Nothing was charged.",
                                    reply_markup=kb.back_to_menu())

    # --- the manual path, for while there is no inbox to watch ---------------

    @app.on_callback_query(filters.regex(r"^adm:paid:(\S{1,48})$"))
    async def admin_confirm(client: Client, cq: CallbackQuery) -> None:
        if not cfg.is_admin(cq.from_user.id):
            await cq.answer("Not for you.", show_alert=True)
            return
        order_id = cq.matches[0].group(1)
        done = payments.settle(order_id, matched_on="confirmed by admin", source="admin")
        if done is None:
            await cq.answer("Already settled, or no such order.", show_alert=True)
            await _edit(cq.message, "☑️ <b>Nothing to do</b> — that order was already settled.")
            return
        await cq.answer("Credits added ✅")
        await _edit(cq.message,
                    f"✅ <b>Confirmed</b>\n+{done.credits_added:g} cr to "
                    f"<code>{done.user_id}</code>  ·  balance {done.new_balance:g}")
        await announce(client, done)

    @app.on_callback_query(filters.regex(r"^adm:unpaid:(\S{1,48})$"))
    async def admin_reject(client: Client, cq: CallbackQuery) -> None:
        if not cfg.is_admin(cq.from_user.id):
            await cq.answer("Not for you.", show_alert=True)
            return
        order_id = cq.matches[0].group(1)
        _claimed.discard(order_id)      # so a later tap can ask again
        row = payments.get_order(order_id)
        await cq.answer("Marked as not received.")
        await _edit(cq.message, "🚫 <b>Not received</b> — the order is still open.")
        if row is None:
            return
        try:
            await client.send_message(
                int(row["user_id"]),
                "🔍 <b>Your payment has not arrived yet</b>\n\n"
                "Nothing has been taken from you. If you did pay, check that the "
                "amount was <b>exact</b> — and tap “I have paid” again in a few "
                "minutes.",
                reply_markup=kb.payment_pending(order_id))
        except Exception as exc:
            log.warning("could not tell user %s about %s: %s",
                        row["user_id"], order_id, exc)


# --- manual confirmation -----------------------------------------------------
#
# With no IMAP inbox configured nothing can settle an order by itself, and the
# alternative to asking a human is a user who has paid and can never be credited.
# So "I have paid" turns into a message to the admin with a confirm button. Once
# IMAP is set up this path goes quiet on its own — `auto_confirm_enabled()` is
# what decides, so there is no flag to remember to turn off.

_claimed: set[str] = set()
CLAIM_LIMIT = 500


async def _claim(client: Client, row, user) -> None:
    """Ask the admins to confirm one order. Once per order, however many taps."""
    order_id = str(row["order_id"])
    if order_id in _claimed:
        return
    if len(_claimed) >= CLAIM_LIMIT:
        _claimed.clear()
    _claimed.add(order_id)

    record = credits.get(int(row["user_id"]))
    text = ui.payment_claim(row, record or user)
    for admin_id in cfg.admin_ids:
        try:
            await client.send_message(admin_id, text,
                                      reply_markup=kb.admin_confirm_payment(order_id))
        except Exception as exc:
            log.warning("could not ask admin %s to confirm %s: %s",
                        admin_id, order_id, exc)

