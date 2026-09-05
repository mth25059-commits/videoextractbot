"""
The filter that keeps the text handlers from eating each other's messages.

Several handlers need plain text from a private chat: the ZIP password, the
top-up amount, the admin's give-credit prompt, a batch of links. They cannot all
register `filters.private & filters.text` — Pyrogram runs **one** handler per
group and then stops, so the first one registered would swallow every message and
the rest would never fire. That is not a theoretical worry; it is what happens the
moment two handlers share a filter.

So a handler declares which mode it owns and is inert otherwise:

    @app.on_message(filters.private & filters.text & gate.in_mode("await_zip_password"))

Because the check happens in the *filter*, a handler that does not own the
current mode never runs at all and the dispatcher moves on to the next one. It
also means a branch can add its own text handler without knowing, or caring,
what order `register_all` uses.

Only one mode is ever set per user (`state.set_mode` overwrites), so at most one
of these handlers can match a given message.

It also holds `forget()`, for the same reason: every route needs to take the user's
own message back out of the chat, and four copies of the same five lines is where the
fifth one gets forgotten.
"""

from __future__ import annotations

import logging

from pyrogram import filters

from .. import state

log = logging.getLogger(__name__)


async def forget(message, why: str = "") -> bool:
    """
    Take one message back out of the chat. True if it went.

    the operator's rule — *"user bhje to cht se delete ho"* — and it is applied to every
    input the bot accepts: the pasted link, the pressed key, the uploaded archive.
    What is left on screen is the one panel the flow owns and then the video, which
    is the whole point of the single-panel design.

    **Never raises.** A delete can fail for reasons that are none of the flow's
    business — the message is older than 48 hours, the user deleted it first, the
    chat lost permissions — and none of them is a reason to fail a job the user has
    already been charged for.
    """
    try:
        await message.delete()
        return True
    except Exception:
        log.debug("could not delete %s", why or "a message", exc_info=True)
        return False


def owns(user_id: int | None, modes: frozenset[str] | set[str]) -> bool:
    """
    True when this user is mid-way through one of `modes`.

    Kept separate from the filter so it can be tested without Pyrogram.
    """
    if user_id is None:
        return False
    entry = state.get_mode(user_id)
    return bool(entry and entry[0] in modes)


def in_mode(*modes: str):
    """A Pyrogram filter that only matches while the sender is in one of `modes`."""
    if not modes:
        raise ValueError("in_mode() needs at least one mode")

    async def check(flt, _client, message) -> bool:
        user = getattr(message, "from_user", None)
        return owns(getattr(user, "id", None), flt.modes)

    return filters.create(check, "InMode", modes=frozenset(modes))
