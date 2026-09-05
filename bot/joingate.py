"""
Force-join: the bot answers nobody until they are in every channel you name.

`FORCE_JOIN` is a comma-separated list and an empty one switches the whole thing
off, which is the shipped default — a fresh clone should not gate anybody behind
channels its operator has not created yet.

Three things here are decisions rather than mechanics:

* **A pass is cached for five minutes; a failure is never cached.** Membership is
  one API call per channel per message, and with four channels on a busy bot that
  is the difference between one call and forty. But the moment after somebody joins
  is exactly when they press *"I've joined"*, and answering that from a cache would
  tell them they had not — so only the yes is remembered.
* **An error means joined.** If the bot is thrown out of a channel, or Telegram is
  having a bad minute, `get_chat_member` raises for every user at once. Treating
  that as "not joined" would lock every single person out of a bot that works,
  including paying ones. It is logged loudly instead and the door stays open.
* **Admins are never gated.** An operator who has not joined their own channel
  would otherwise be unable to reach the panel that fixes it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import cfg

log = logging.getLogger(__name__)

#: How long a verified user is trusted for, in seconds. Short enough that leaving a
#: channel costs them access again within the same session.
PASS_TTL = 300

#: Statuses that mean "in the channel". Pyrogram spells them as an enum; its `.value`
#: is the lowercase string, and comparing on the string keeps this module importable
#: without pyrogram (which is what makes it testable).
IN = frozenset({"member", "administrator", "owner", "creator"})

#: The subset that can *ask* who is in a channel. The gate never needs this — it asks
#: about a user, not about itself — but the setup wizard does: a bot that is only a
#: member cannot call `get_chat_member` at all, and the gate's fail-open then quietly
#: lets everybody through. Kept here so there is one spelling of these words.
ADMIN = frozenset({"administrator", "owner", "creator"})

_passed: dict[int, float] = {}


@dataclass(frozen=True)
class Channel:
    """One channel a user has to be in."""

    #: What `get_chat_member` is given: `@name` for a public channel, the numeric id
    #: for a private one.
    ref: str | int
    #: Where the join button points. A private channel has no `t.me/name`, so its
    #: invite link has to be configured beside the id.
    link: str
    #: What the button says.
    label: str


def _one(entry: str) -> Channel | None:
    """
    Parse one configured entry, or None if it cannot be used.

    Accepted, because all four turn up in practice when somebody copies a channel
    out of Telegram:

        @mychannel
        mychannel
        https://t.me/mychannel
        -1001234567890|https://t.me/+AbCdEf      (private: id and its invite link)
    """
    entry = entry.strip()
    if not entry:
        return None

    ref, _, link = entry.partition("|")
    ref, link = ref.strip(), link.strip()

    bare = ref
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "telegram.me/"):
        if bare.lower().startswith(prefix):
            bare = bare[len(prefix):]
            break
    bare = bare.lstrip("@").strip("/")

    # A private channel: numeric id, and an invite link that has to be given because
    # there is no public one to derive. Without the link there is nothing to put on a
    # button, so the entry is refused rather than shown as a dead end.
    if bare.lstrip("-").isdigit():
        if not link:
            log.warning("FORCE_JOIN: %r is a numeric id with no invite link "
                        "after a '|' — skipped, nobody could join it", entry)
            return None
        return Channel(ref=int(bare), link=link, label=_label_of(link))

    if not bare:
        return None
    # A `+hash` or `joinchat/…` link pasted on its own: joinable, but membership
    # cannot be checked against it — `get_chat_member` needs the id.
    if bare.startswith("+") or bare.lower().startswith("joinchat/"):
        log.warning("FORCE_JOIN: %r is an invite link with no channel id — write it "
                    "as -100xxxxxxxxxx|%s so membership can be checked", entry, entry)
        return None

    return Channel(ref=f"@{bare}", link=link or f"https://t.me/{bare}",
                   label=f"@{bare}")


def _label_of(link: str) -> str:
    tail = link.rstrip("/").rsplit("/", 1)[-1]
    return f"@{tail}" if tail and not tail.startswith("+") else "Channel"


def configured() -> tuple[Channel, ...]:
    """Every usable channel from `FORCE_JOIN`, in the order it was written."""
    out = []
    for entry in cfg.force_join:
        one = _one(entry)
        if one and one not in out:
            out.append(one)
    return tuple(out)


def is_on() -> bool:
    return bool(configured())


def status_name(status: object) -> str:
    """
    A `ChatMember.status` as one plain lowercase word: `member`, `administrator`,
    `left`, `banned`.

    Pyrogram hands over an enum whose `.value` is already that word, but a caller
    holding the enum itself stringifies as `ChatMemberStatus.ADMINISTRATOR`, so the
    tail after the last dot is what is taken. `str.lstrip("chatmemberstatus.")`
    cannot do this job even though it looks like it can — `lstrip` takes a *set of
    characters*, so it eats the leading `a` off `administrator` as well, and an
    admin then reads as not joined.
    """
    value = str(getattr(status, "value", status)).lower().strip()
    return value.rsplit(".", 1)[-1]


def joined(status: object) -> bool:
    """
    True when this `ChatMember.status` counts as being in the channel.

    Everything unrecognised counts as *not* joined: a new status Telegram invents is
    far more likely to be a way of not being in a channel than a new way of being in
    one. `restricted` is the exception that proves it — a muted member is still in the
    channel, and `in_channel` answers that from `is_member` rather than from here,
    because the flag lives on the member and not on the status.
    """
    return status_name(status) in IN


def remember(user_id: int) -> None:
    _passed[user_id] = time.time() + PASS_TTL


def forget(user_id: int) -> None:
    """Drop a cached pass. Called when the answer must be fresh."""
    _passed.pop(user_id, None)


def cached_pass(user_id: int) -> bool:
    until = _passed.get(user_id)
    if until is None:
        return False
    if until < time.time():
        del _passed[user_id]
        return False
    return True


def clear_cache() -> None:
    _passed.clear()


async def in_channel(client, channel: Channel, user_id: int) -> bool:
    """
    Is this user in this one channel? Errors answer yes — see the module docstring.
    """
    try:
        member = await client.get_chat_member(channel.ref, user_id)
    except Exception as exc:
        name = type(exc).__name__
        if name in ("UserNotParticipant", "UserNotParticipantError"):
            return False
        # ChatAdminRequired, ChannelPrivate, PeerIdInvalid, a flood wait: all of them
        # are the operator's problem and none of them is the user's fault.
        log.warning("force-join: cannot check %s (%s: %s) — letting the user "
                    "through", channel.ref, name, exc)
        return True
    # `is_member` is only set for a restricted member, and it is the whole answer for
    # one: somebody the channel has muted is still *in* the channel, and their status
    # reads `restricted` rather than `member`. Asking the status alone would gate them
    # forever — the join button cannot fix a mute, so ✅ would never turn green.
    flag = getattr(member, "is_member", None)
    if flag is not None:
        return bool(flag)
    return joined(getattr(member, "status", None))


async def missing(client, user_id: int, *, use_cache: bool = True) -> list[Channel]:
    """
    The channels this user still has to join. Empty means let them in.

    Empty for an admin and for a bot with `FORCE_JOIN` unset, both without a single
    API call — this runs before every message the bot receives.
    """
    channels = configured()
    if not channels or cfg.is_admin(user_id):
        return []
    if use_cache and cached_pass(user_id):
        return []

    out = [c for c in channels if not await in_channel(client, c, user_id)]
    if not out:
        remember(user_id)
    else:
        forget(user_id)
    return out
