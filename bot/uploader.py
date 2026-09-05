"""
Sending the finished file to Telegram.

This is the module that justifies Pyrogram. Over the cloud Bot API a bot may
upload 50 MB; over MTProto the ceiling is 2 GB (4 GB with Premium). Every part of
the bot that handles "jitni bhi badi video ho" depends on being here and not on
`sendVideo`.

Two things that bite in production and are handled below:

* Telegram rate-limits message edits hard. Progress is throttled to one edit
  every few seconds, and a FloodWait during a progress edit is swallowed — it
  must never take down an upload that is 90% done.
* `send_video` with width/height/duration/thumb makes the file arrive as a
  playable video. Omit them and the same bytes arrive as a grey document.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from . import media, ui
from .config import cfg

log = logging.getLogger(__name__)


class TooLarge(Exception):
    """The file cannot be sent at all — Telegram's own ceiling, not a setting."""

    def __init__(self, size_bytes: int, limit_bytes: int):
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(f"{size_bytes} > {limit_bytes}")

    def user_message(self) -> str:
        return (
            "❌ <b>File is too big for Telegram</b>\n\n"
            f"📦 This file: <b>{ui.human_bytes(self.size_bytes)}</b>\n"
            f"🚧 Telegram limit: <b>{ui.human_bytes(self.limit_bytes)}</b>\n\n"
            "Telegram itself refuses anything larger — nothing on my side can "
            "raise it. Try a lower quality if the source offers one."
        )

@dataclass
class Sent:
    message: Message
    size_bytes: int
    seconds: float
    parts: int = 1
    #: Every message this delivery put in the chat, oldest first — the video itself,
    #: or all the `.001`/`.002` pieces plus the rejoin note that explains them.
    #:
    #: `message` is only ever the *last* of them, which was enough while nothing had to
    #: be taken back. `expiry.remember` has to delete the whole delivery, and a
    #: three-part video whose first two pieces stay in the chat for ever is worse than
    #: no auto-delete at all: the user is left with fragments they cannot rejoin.
    #:
    #: Defaults to empty so the suites' hand-built `Sent(message=None, …)` fakes keep
    #: working; `expiry.remember` falls back to `message.id` when it is.
    message_ids: tuple[int, ...] = ()

    @property
    def speed(self) -> float:
        return self.size_bytes / max(0.001, self.seconds)


def limit_bytes() -> int:
    return cfg.max_upload_mb * 1024 * 1024


def part_count(size_bytes: int) -> int:
    """How many pieces `size_bytes` has to be cut into to get through Telegram."""
    limit = limit_bytes()
    return max(1, -(-size_bytes // limit))          # ceil


def check_size(path: Path) -> int:
    """Raise TooLarge before wasting an upload attempt. Returns the size."""
    size = path.stat().st_size
    if size > limit_bytes():
        raise TooLarge(size, limit_bytes())
    return size


def rejoin_note(name: str, parts: int) -> str:
    """
    How to put the pieces back. Sent once, with the first part.

    Worth spelling out: a `.001` file on a phone opens in nothing at all, and without
    this the user reasonably concludes the bot sent them garbage.
    """
    return (
        f"✂️ <b>Too big for Telegram — sent in {parts} parts</b>\n\n"
        f"📦 <code>{ui.esc(name)}</code>\n"
        f"🚧 Telegram stops at {ui.human_bytes(limit_bytes())} per file, so this one "
        f"is cut into {parts} pieces.\n\n"
        "<b>To join them back into one video:</b>\n"
        "• <b>Phone</b> — open ZArchiver, long-press the <code>.001</code> file, "
        "choose <i>Join parts</i>.\n"
        "• <b>Windows</b> — put all parts in one folder, then in that folder run\n"
        f"<code>copy /b \"{ui.esc(name)}.*\" \"{ui.esc(name)}\"</code>\n"
        "• <b>Mac / Linux</b> —\n"
        f"<code>cat \"{ui.esc(name)}\".* &gt; \"{ui.esc(name)}\"</code>\n\n"
        "<i>Download every part first — one missing piece and the video will not play.</i>"
    )



def _ids_of(*messages: Message | None) -> tuple[int, ...]:
    """The message ids of whatever actually got sent, skipping anything that did not.

    Tolerant of `None` and of a stub with no `id` because the suites hand this module
    fakes, and a missing id has to mean "nothing to delete later" rather than an
    `AttributeError` in the middle of a delivery that already succeeded.
    """
    out = []
    for message in messages:
        ident = getattr(message, "id", None)
        if isinstance(ident, int):
            out.append(ident)
    return tuple(out)


def _progress_reporter(status: Message | None, title: str, started: float, stage: str,
                       cancelled: Callable[[], bool] | None):
    """Build the callback Pyrogram calls on every uploaded chunk."""
    throttle = ui.Throttle(5.0)

    async def on_progress(current: int, total: int) -> None:
        if cancelled and cancelled():
            raise asyncio.CancelledError
        if status is None or not throttle.ready(force=current >= total):
            return
        try:
            await status.edit_text(
                ui.progress_block(f"📤 Uploading — {ui.esc(title)}",
                                  current, total, started, stage=stage,
                                  expires_minutes=cfg.auto_delete_minutes)
            )
        except FloodWait:
            # Back off instead of dying: the upload itself is still healthy.
            throttle.every = min(30.0, throttle.every * 2)
        except Exception:
            pass  # a stale or deleted status message must not abort a live upload

    return on_progress

async def send_video(
    client: Client,
    chat_id: int,
    path: Path,
    *,
    caption: str = "",
    status: Message | None = None,
    title: str = "",
    cancelled: Callable[[], bool] | None = None,
) -> Sent:
    """
    Upload one file as a playable video, editing `status` with a live progress bar.

    Probes for duration/resolution and cuts a thumbnail first — without those
    Telegram delivers a grey document instead of a video with a preview frame.

    **`caption` is accepted and ignored.** The operator's rule, and it applies to every
    route: *"only video"* — no title under the clip, on Terabox, ZIP or the link
    route. The parameter stays so callers, tests and `send_in_parts` keep their
    shape, and so the decision lives in one place instead of three call sites that
    each have to remember it. `title` is still used, but only for the progress
    panel's own text, which is deleted when the job ends.
    """
    size = check_size(path)
    started = time.monotonic()
    label = title or path.stem

    info = media.MediaInfo()
    try:
        info = await media.probe(path)
    except media.MediaError as exc:
        log.warning("probe failed for %s, sending without metadata: %s", path.name, exc)

    thumb_path: Path | None = None
    try:
        thumb_path = await media.thumbnail(
            path, path.with_suffix(".thumb.jpg"), duration=info.duration
        )
    except Exception:
        log.debug("thumbnail failed", exc_info=True)

    try:
        message = await client.send_video(
            chat_id=chat_id,
            video=str(path),
            caption=None,
            duration=int(info.duration) or 0,
            width=info.width or 0,
            height=info.height or 0,
            thumb=str(thumb_path) if thumb_path else None,
            supports_streaming=True,
            progress=_progress_reporter(status, label, started,
                                        "sending to Telegram", cancelled),
        )
    finally:
        if thumb_path:
            thumb_path.unlink(missing_ok=True)

    return Sent(message=message, size_bytes=size, seconds=time.monotonic() - started,
                message_ids=_ids_of(message))

async def send_as_document(
    client: Client,
    chat_id: int,
    path: Path,
    *,
    caption: str = "",
    status: Message | None = None,
    title: str = "",
    cancelled: Callable[[], bool] | None = None,
) -> Sent:
    """
    Fallback for files ffmpeg could not repackage as MP4.

    Reached when a ZIP holds something exotic. Sending it as a document is worse
    UX than an inline video, but it is honest: the user still gets their file, and
    re-encoding it on the box would cost 20 minutes of pinned CPU per video.

    `caption` is accepted and ignored, as in `send_video` — a document already shows
    its own file name, so a caption here would be the title twice.
    """
    size = check_size(path)
    started = time.monotonic()
    message = await client.send_document(
        chat_id=chat_id,
        document=str(path),
        caption=None,
        force_document=True,
        progress=_progress_reporter(status, title or path.name, started,
                                    "sending as file", cancelled),
    )
    return Sent(message=message, size_bytes=size, seconds=time.monotonic() - started,
                message_ids=_ids_of(message))


#: Copy buffer for cutting parts. Small enough that a cancel is noticed quickly.
PART_CHUNK = 1 << 20


def _write_part(src, target: Path, want: int) -> int:
    """Copy at most `want` bytes from an open handle into `target`. Returns the count."""
    written = 0
    with target.open("wb") as out:
        while written < want:
            chunk = src.read(min(PART_CHUNK, want - written))
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
    return written


async def send_in_parts(
    client: Client,
    chat_id: int,
    path: Path,
    *,
    caption: str = "",
    status: Message | None = None,
    title: str = "",
    cancelled: Callable[[], bool] | None = None,
) -> Sent:
    """
    Cut a file Telegram will not accept whole and send the pieces.

    A video that is over the ceiling is still the video the user paid for, so it
    leaves as `name.001`, `name.002`, … instead of as a refusal.

    Each piece is written, sent and deleted before the next one is cut, so a 6 GB
    video needs one part's worth of spare disk rather than another 6 GB — which is
    what keeps this safe to run on all ten workers at once. The cutting itself goes
    through `asyncio.to_thread`: a synchronous 2 GB copy on the event loop would
    stall every other job's progress edits and heartbeat.
    """
    size = path.stat().st_size
    limit = limit_bytes()
    total = part_count(size)
    started = time.monotonic()
    label = title or path.name

    try:
        note = await client.send_message(chat_id, rejoin_note(path.name, total))
    except Exception:
        note = None
        log.debug("rejoin note failed", exc_info=True)   # never fatal, it is a caption

    last: Message | None = None
    pieces: list[Message | None] = [note]
    index = 0
    with path.open("rb") as src:
        while True:
            if cancelled and cancelled():
                raise asyncio.CancelledError
            index += 1
            part = path.with_name(f"{path.name}.{index:03d}")
            try:
                written = await asyncio.to_thread(_write_part, src, part, limit)
                if not written:
                    index -= 1                            # exact multiple of the limit
                    break
                # No title here either, but the part number stays: a `.002` document
                # with nothing on it is unidentifiable, and "which piece is this" is
                # not a caption in the sense the operator ruled out — it is the only way to
                # tell three files apart. The name is in `<code>` so it can be copied
                # into the rejoin command.
                last = await client.send_document(
                    chat_id=chat_id,
                    document=str(part),
                    file_name=part.name,
                    caption=(f"✂️ Part <b>{index}</b> of <b>{total}</b>\n"
                             f"<code>{ui.esc(path.name)}</code>")[:1024],
                    force_document=True,
                    progress=_progress_reporter(
                        status, f"{label} — part {index}/{total}", started,
                        f"part {index} of {total}", cancelled),
                )
            finally:
                part.unlink(missing_ok=True)
            pieces.append(last)
            if written < limit:
                break

    if last is None:                                      # a zero-byte file
        raise TooLarge(size, limit)
    return Sent(message=last, size_bytes=size,
                seconds=time.monotonic() - started, parts=index,
                # The rejoin note goes with them. It is instructions for files that
                # will not exist any more, and leaving it behind is a message telling
                # the user to join pieces that are gone.
                message_ids=_ids_of(*pieces))


async def send_best_effort(
    client: Client,
    chat_id: int,
    path: Path,
    *,
    caption: str = "",
    status: Message | None = None,
    title: str = "",
    cancelled: Callable[[], bool] | None = None,
    allow_split: bool = True,
) -> Sent:
    """
    Get the file to the user by whatever route works: video, document, or in parts.

    Some containers Telegram accepts as a file but rejects as a video. Retrying
    as a document is the difference between "your video is here" and a hard
    failure the user has to be refunded for.

    Over the size ceiling the file is cut into parts rather than refused. The check
    happens up front so an over-limit file does not burn a doomed upload attempt
    first, and `allow_split=False` is left for callers that genuinely want the
    refusal (a preview, say) rather than 4 GB of documents.
    """
    if allow_split and path.stat().st_size > limit_bytes():
        return await send_in_parts(client, chat_id, path, caption=caption,
                                  status=status, title=title, cancelled=cancelled)
    try:
        return await send_video(client, chat_id, path, caption=caption,
                                status=status, title=title, cancelled=cancelled)
    except (TooLarge, asyncio.CancelledError):
        raise
    except Exception as exc:
        log.warning("send_video failed for %s (%s), retrying as document", path.name, exc)
        return await send_as_document(client, chat_id, path, caption=caption,
                                      status=status, title=title, cancelled=cancelled)

