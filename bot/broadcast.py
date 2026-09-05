"""
One message to every user the bot has ever seen.

Built for one specific job — *"sabko naye bot p set krde"* — and it is the only way
to do it: a bot cannot start a conversation, so the only chance to tell people the
address has changed is a message from the bot they are already talking to.

The whole design is about **not failing halfway**. A broadcast to a few thousand
chats will hit blocked users, deleted accounts and Telegram's own rate limits, and
every one of those is normal rather than exceptional. So each send is wrapped
individually, the loop never raises, and what comes back is a count of what actually
happened:

    Result(sent=812, blocked=141, failed=3, total=956)

`blocked` is separated from `failed` on purpose. Someone who blocked the bot is not
a problem to investigate — it is the expected state of a third of any user list — and
folding the two together turns a healthy run into an alarming one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pyrogram import Client

from . import db

log = logging.getLogger(__name__)

#: Between sends. Telegram's documented ceiling for messages to *different* chats is
#: around 30 a second; a fifth of that is slow enough to never see a FloodWait and
#: still clears a thousand users inside a minute. A broadcast is not urgent — being
#: throttled mid-run, and not knowing who got it, would be much worse than waiting.
GAP_SECONDS = 0.05

#: What Telegram says when the user has blocked the bot, never started it, or is
#: gone. Matched on the text because pyrogram raises several different classes for
#: these and they are all the same fact: this chat is not reachable, and never will
#: be. Anything else is a real failure and worth a log line.
GONE = ("blocked", "user_is_deactivated", "chat not found", "peer_id_invalid",
        "user_deactivated", "bot was blocked", "chat_write_forbidden",
        "input_user_deactivated")


@dataclass
class Result:
    """What a run did. `sent + blocked + failed == total`, always."""

    total: int = 0
    sent: int = 0
    blocked: int = 0
    failed: int = 0

    @property
    def card(self) -> str:
        lines = [f"📢 <b>Announcement sent</b>",
                 f"✅ Delivered: <b>{self.sent}</b> of {self.total}",
                 f"🚫 Blocked / gone: <b>{self.blocked}</b>"]
        if self.failed:
            lines.append(f"⚠️ Failed: <b>{self.failed}</b> — see the log")
        return "\n".join(lines)


def audience() -> list[int]:
    """
    Everyone to send to: every user row that is not banned, oldest first.

    Oldest first so that if a run is interrupted, the people who have been here
    longest — the ones with credits on the books — are the ones already told.
    """
    rows = db.query("SELECT user_id FROM users WHERE banned = 0 ORDER BY user_id")
    return [int(row["user_id"]) for row in rows]


def _is_gone(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(mark in text for mark in GONE)


async def send_to_all(client: Client, text: str,
                      on_progress=None) -> Result:
    """
    Deliver `text` to every user. Never raises — the count is the report.

    `on_progress(done, total)` is called as it goes, for an admin card that updates
    while a long run is in flight; it is called inside the same `try` as nothing else
    so a failed panel edit cannot stop the broadcast.
    """
    everyone = audience()
    result = Result(total=len(everyone))

    for done, user_id in enumerate(everyone, start=1):
        try:
            await client.send_message(user_id, text,
                                      disable_web_page_preview=True)
            result.sent += 1
        except Exception as exc:
            if _is_gone(exc):
                result.blocked += 1
            else:
                result.failed += 1
                log.warning("broadcast: %s did not go through (%s)", user_id, exc)

        if on_progress is not None:
            try:
                await on_progress(done, result.total)
            except Exception:
                log.debug("broadcast: progress callback failed", exc_info=True)

        await asyncio.sleep(GAP_SECONDS)

    log.info("broadcast: %d sent, %d blocked, %d failed, of %d",
             result.sent, result.blocked, result.failed, result.total)
    return result
