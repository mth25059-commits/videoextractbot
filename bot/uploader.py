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

    @property
    def speed(self) -> float:
        return self.size_bytes / max(0.001, self.seconds)


def limit_bytes() -> int:
    return cfg.max_upload_mb * 1024 * 1024


def check_size(path: Path) -> int:
    """Raise TooLarge before wasting an upload attempt. Returns the size."""
    size = path.stat().st_size
    if size > limit_bytes():
        raise TooLarge(size, limit_bytes())
    return size


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
                                  current, total, started, stage=stage)
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
            caption=caption[:1024] if caption else None,
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

    return Sent(message=message, size_bytes=size, seconds=time.monotonic() - started)

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
    """
    size = check_size(path)
    started = time.monotonic()
    message = await client.send_document(
        chat_id=chat_id,
        document=str(path),
        caption=caption[:1024] if caption else None,
        force_document=True,
        progress=_progress_reporter(status, title or path.name, started,
                                    "sending as file", cancelled),
    )
    return Sent(message=message, size_bytes=size, seconds=time.monotonic() - started)


async def send_best_effort(
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
    Try as a video, fall back to a document.

    Some containers Telegram accepts as a file but rejects as a video. Retrying
    as a document is the difference between "your video is here" and a hard
    failure the user has to be refunded for.
    """
    try:
        return await send_video(client, chat_id, path, caption=caption,
                                status=status, title=title, cancelled=cancelled)
    except (TooLarge, asyncio.CancelledError):
        raise
    except Exception as exc:
        log.warning("send_video failed for %s (%s), retrying as document", path.name, exc)
        return await send_as_document(client, chat_id, path, caption=caption,
                                      status=status, title=title, cancelled=cancelled)
