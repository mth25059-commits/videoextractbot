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
"""

from __future__ import annotations

from pyrogram import filters

from .. import state


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
