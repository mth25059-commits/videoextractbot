"""
The force-join gate: registered ahead of every other handler, and inert when
`FORCE_JOIN` is empty.

**Why one handler in group -1 rather than a check in twenty handlers.** Pyrogram
walks handler groups in ascending order, so a handler in group -1 sees every
private message and every callback before the flows in group 0 do, and
`StopPropagation` from there stops the rest. That is one choke point for the whole
bot — /start, a pasted link, an uploaded archive, a top-up button, a Fap quality
press — and no flow has to remember to ask. A gate that has to be added to each
new handler is a gate that will be missing from the next one.

**Nothing the user sent is deleted.** Every other route in this bot takes the
user's message back out of the chat, but the gate is the one place where the user
has not been served yet: deleting a pasted link they will have to paste again after
joining is a small punishment for arriving before joining. The gate answers and
gets out of the way.

`join:ok` is answered here too, in the same group, rather than in group 0 behind
the gate — otherwise the gate would swallow the very button that lets somebody
past it.
"""

from __future__ import annotations

import logging

from pyrogram import Client, StopPropagation, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import CallbackQuery, Message

from .. import credits, joingate, keyboards as kb, state, ui
from ..config import cfg

log = logging.getLogger(__name__)


async def _show(target, channels, name: str = "", *, edit: bool = False) -> None:
    text = ui.join_gate(channels, name)
    markup = kb.join_gate(channels)
    try:
        if edit:
            await target.edit_text(text, reply_markup=markup,
                                   disable_web_page_preview=True)
            return
        await target.reply_text(text, reply_markup=markup,
                                disable_web_page_preview=True)
    except Exception:
        # An edit fails when the text is byte-identical to what is already there —
        # which is exactly what happens when somebody taps ✅ twice without joining
        # anything. The alert already told them; there is nothing else to do.
        log.debug("join gate: could not put the card up", exc_info=True)


def register(app: Client) -> None:
    """
    Install the gate. Called before every other `register` in `main.register_all`.

    Registering it when `FORCE_JOIN` is empty would be harmless — `joingate.missing`
    answers empty without an API call — but a bot that is not gating anybody has no
    business owning a handler group at all.
    """
    if not joingate.is_on():
        log.info("force-join is off (FORCE_JOIN is empty)")
        return

    channels = joingate.configured()
    log.info("force-join is on: %s", ", ".join(str(c.ref) for c in channels))

    async def gate_message(client: Client, message: Message) -> None:
        user = getattr(message, "from_user", None)
        if user is None:                     # a channel post, not a person
            return
        missing = await joingate.missing(client, user.id)
        if not missing:
            return
        # Registered before anything else, so this is also where a brand-new user is
        # first seen. Without it a gated user would not exist in the database until
        # they joined, and the admin's "somebody new arrived" alert would only fire
        # for the ones who did — hiding exactly the number an operator wants.
        credits.ensure(user.id, user.first_name or "", user.username)
        state.clear_mode(user.id)
        await _show(message, missing, user.first_name or "")
        raise StopPropagation

    async def gate_callback(client: Client, cq: CallbackQuery) -> None:
        data = cq.data or ""
        if data == "join:ok":
            await _verify(client, cq)
            raise StopPropagation
        missing = await joingate.missing(client, cq.from_user.id)
        if not missing:
            return
        await cq.answer("Join the channel first 🙂", show_alert=True)
        await _show(cq.message, missing, cq.from_user.first_name or "", edit=True)
        raise StopPropagation

    async def _verify(client: Client, cq: CallbackQuery) -> None:
        # `use_cache=False` on purpose: the user pressed this *because* they just
        # joined, and a five-minute-old "no" is the one answer that is certainly
        # wrong here.
        missing = await joingate.missing(client, cq.from_user.id, use_cache=False)
        if missing:
            await cq.answer(ui.join_still_missing(missing), show_alert=True)
            await _show(cq.message, missing, cq.from_user.first_name or "", edit=True)
            return

        user, is_new = credits.ensure(cq.from_user.id, cq.from_user.first_name or "",
                                      cq.from_user.username)
        await cq.answer("✅ Verified — you're in", show_alert=False)
        state.clear_mode(cq.from_user.id)
        try:
            await cq.message.edit_text(
                ui.welcome(user.first_name, user.credits, is_new,
                           cfg.free_credits_on_join),
                reply_markup=kb.main_menu(cfg.is_admin(user.user_id)),
                disable_web_page_preview=True)
        except Exception:
            log.debug("join gate: could not open the menu after verifying",
                      exc_info=True)
        # The persistent keyboard is installed by /start, and a user who was gated on
        # their very first /start never got it. One line, once, and only for them.
        try:
            await client.send_message(
                cq.from_user.id,
                "⌨️ <i>Buttons are in your keyboard now — paste a link any time.</i>",
                reply_markup=kb.home_keys())
        except Exception:
            log.debug("join gate: could not install the home keys", exc_info=True)

    app.add_handler(MessageHandler(gate_message, filters.private), group=-1)
    app.add_handler(CallbackQueryHandler(gate_callback), group=-1)
