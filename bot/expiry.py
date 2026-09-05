"""
The clock on a delivered video.

Every video this bot sends is deleted from the chat again after
`AUTO_DELETE_MINUTES` (30 by default), on all three routes — Terabox, ZIP and Fap.
Set it to 0 and nothing is ever deleted.

Two decisions are the whole module:

* **The timer is a database row, not a sleeping task.** `asyncio.sleep(1800)` would
  be four lines instead of this file, and a restart anywhere inside that half hour
  would silently leave the video in the chat for ever. The user was *told* it goes;
  a promise that a deploy can quietly cancel is worse than no promise. So a delivery
  writes rows into `deletions` and `janitor` drains them — which also means the sweep
  catches up on anything that came due while the bot was down.
* **The whole delivery goes, not just the last message.** A 3 GB video leaves as
  `.001`/`.002`/`.003` plus a note explaining how to rejoin them. Deleting only the
  message `Sent.message` points at would strip the last piece and leave the user
  holding fragments and instructions for a file that no longer exists. That is why
  `uploader.Sent` grew `message_ids`.

The warning itself is not in here — it is `ui.delete_notice`, appended to the live
progress panel by `ui.progress_block` and `ui.assembling_block`, so the user reads it
for the whole length of the download and upload rather than after the fact.

Nothing in here is allowed to break a delivery. `remember` swallows its own database
errors: a video that arrived and was paid for must not turn into a failed job because
a bookkeeping insert went wrong. The cost of that is a video that outstays its
welcome, which is the lesser fault by a distance.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Sequence

from . import db
from .config import cfg

log = logging.getLogger(__name__)

#: How often the janitor looks for work. Well under a minute so "30 minutes" is not
#: read as "up to 35" — the row carries an exact due time and this only decides how
#: soon after it the delete actually happens.
SWEEP_EVERY_SECONDS = 30.0

#: Telegram takes at most 100 message ids in one `delete_messages`.
BATCH = 100

#: How many rows one sweep will handle, so a backlog after a long outage is worked
#: through over several passes instead of in one burst that gets the bot flood-waited.
PER_SWEEP = 400


def minutes() -> int:
    """The configured lifetime in minutes. 0 means the feature is off."""
    return max(0, int(getattr(cfg, "auto_delete_minutes", 0) or 0))


def enabled() -> bool:
    return minutes() > 0


def _ids_from(sent: Any) -> tuple[int, ...]:
    """
    Which message ids a `uploader.Sent` put in the chat.

    Prefers its `message_ids`, and falls back to the single `message.id` so a caller
    holding an older-shaped `Sent` — or one of the suites' hand-built fakes — still
    schedules what it can instead of raising inside a finished delivery.
    """
    ids = tuple(getattr(sent, "message_ids", ()) or ())
    if ids:
        return ids
    ident = getattr(getattr(sent, "message", None), "id", None)
    return (ident,) if isinstance(ident, int) else ()


def remember(sent: Any, chat_id: int, job_id: int | None = None) -> int:
    """
    Put a delivery on the clock. Returns how many messages were scheduled.

    Called straight after the upload on every route. A no-op when the feature is off,
    when the chat id is missing, or when the delivery produced no id worth recording —
    all three of which are normal, not errors.
    """
    if not enabled():
        return 0
    ids = _ids_from(sent)
    if not ids or not isinstance(chat_id, int):
        return 0

    due_at = db.now() + minutes() * 60
    written = 0
    try:
        for message_id in ids:
            db.execute(
                "INSERT INTO deletions (chat_id, message_id, job_id, due_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, message_id, job_id, due_at, db.now()),
            )
            written += 1
    except Exception:                                     # noqa: BLE001
        # Deliberately swallowed — see the module docstring. The video is already in
        # the chat and already paid for; a failed insert must not become a failed job.
        log.warning("expiry: could not schedule %d message(s) in chat %s",
                    len(ids), chat_id, exc_info=True)
    return written


def due(limit: int = PER_SWEEP, at: int | None = None) -> list[db.Row]:
    """Rows whose time is up, oldest first. Read-only — nothing is removed here."""
    return db.query(
        "SELECT id, chat_id, message_id FROM deletions "
        "WHERE due_at <= ? ORDER BY due_at, id LIMIT ?",
        (db.now() if at is None else at, int(limit)),
    )


def forget(row_ids: Sequence[int]) -> None:
    """
    Drop rows from the queue, whether or not Telegram accepted the delete.

    A message that is already gone, in a chat the user has blocked the bot in, or one
    Telegram simply refuses, must not be retried every thirty seconds until the
    database ends — the row has done its job either way.
    """
    for row_id in row_ids:
        try:
            db.execute("DELETE FROM deletions WHERE id = ?", (int(row_id),))
        except Exception:                                 # noqa: BLE001
            log.debug("expiry: could not clear row %s", row_id, exc_info=True)


def pending() -> int:
    """How many messages are waiting on the clock, for the admin card."""
    try:
        return int(db.scalar("SELECT COUNT(*) FROM deletions", ()))
    except Exception:                                     # noqa: BLE001
        return 0


def _grouped(rows: Iterable[db.Row]) -> dict[int, list[tuple[int, int]]]:
    """`{chat_id: [(row_id, message_id), …]}` — one `delete_messages` call per chat."""
    out: dict[int, list[tuple[int, int]]] = {}
    for row in rows:
        out.setdefault(int(row["chat_id"]), []).append(
            (int(row["id"]), int(row["message_id"]))
        )
    return out


async def sweep(client: Any) -> int:
    """
    Delete everything that is due. Returns how many messages actually went.

    A `FloodWait` leaves its rows alone and gives up on the rest of the pass: the next
    sweep is thirty seconds away, and arguing with Telegram about a rate limit is how
    a bot loses the ability to send anything at all. Every other failure clears its
    rows — see `forget`.
    """
    if not enabled():
        return 0
    rows = due()
    if not rows:
        return 0

    gone = 0
    for chat_id, pairs in _grouped(rows).items():
        for start in range(0, len(pairs), BATCH):
            chunk = pairs[start:start + BATCH]
            row_ids = [row_id for row_id, _ in chunk]
            message_ids = [message_id for _, message_id in chunk]
            try:
                await client.delete_messages(chat_id=chat_id, message_ids=message_ids)
                gone += len(message_ids)
            except Exception as exc:                      # noqa: BLE001
                if type(exc).__name__ == "FloodWait":
                    log.info("expiry: flood-waited, leaving %d row(s) for the next pass",
                             len(row_ids))
                    return gone
                # Already deleted by the user, the bot blocked, the chat gone. Normal
                # enough to be an info line rather than a warning, and the rows still
                # go so this is not retried for ever.
                log.info("expiry: chat %s refused %d delete(s) (%s)",
                         chat_id, len(message_ids), exc)
            forget(row_ids)
    if gone:
        log.info("expiry: deleted %d delivered message(s)", gone)
    return gone


async def janitor(client: Any, interval: float = SWEEP_EVERY_SECONDS) -> None:
    """
    Sweep every `interval` seconds until cancelled.

    Started and cancelled by `main.run`, and shaped like `scratch.janitor` on purpose:
    a crash in here must never take the bot down with it, so the loop logs and carries
    on. The worst case of a failed sweep is a video that stays a little longer.
    """
    if not enabled():
        log.info("expiry: auto-delete is off (AUTO_DELETE_MINUTES=0)")
        return
    log.info("expiry: delivered videos are deleted after %d minute(s)", minutes())
    while True:
        try:
            await asyncio.sleep(interval)
            await sweep(client)
        except asyncio.CancelledError:
            raise
        except Exception:                                 # noqa: BLE001
            log.warning("expiry: sweep failed", exc_info=True)
