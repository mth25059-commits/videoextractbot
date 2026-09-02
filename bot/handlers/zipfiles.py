"""
The ZIP flow: archive in, playable videos out.

Charging is per archive, not per video, so the whole thing is one queue job. That
also makes the refund rule easy to hold: if the archive turns out to hold nothing
sendable, the job fails and the credits go back.

The upload progress bar is edited into one status message that is reused for
every video in the archive, so a 12-video ZIP does not leave 12 dead progress
messages in the chat.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import archive, credits, keyboards as kb, media, queue as jobq, state, ui, uploader
from ..config import cfg

log = logging.getLogger(__name__)

PROMPT = (
    "🗂 <b>Send me a ZIP file</b>\n\n"
    "I will open it here, pull out every video inside, and send each one as a "
    "playable video — so nothing has to be downloaded or unpacked on your phone.\n\n"
    f"💰 <b>Price</b>\n"
    f"  • up to 1 GB — <b>{cfg.cost_zip_upto_1gb:g} credits</b>\n"
    f"  • 1 – 2 GB — <b>{cfg.cost_zip_upto_2gb:g} credits</b>\n\n"
    "<i>One price for the whole archive, however many videos are in it.</i>"
)


def _work_dir(user_id: int, tag: str) -> Path:
    return cfg.work_dir / f"zip-{user_id}-{tag}"

async def _deliver(client: Client, job: jobq.Job) -> None:
    """
    The queue runner: extract → remux if needed → send → delete, one video at a time.

    Only one extracted file is on disk at any moment. That is what lets a 4 GB
    archive be processed on a box with 8 GB of free space.
    """
    zip_path: Path = job.payload["zip_path"]
    password: str | None = job.payload.get("password")
    entries: list[archive.Entry] = job.payload["videos"]
    status: Message = job.payload["status"]
    out_dir = zip_path.parent / "out"

    sent = 0
    failed: list[str] = []
    try:
        for index, entry in enumerate(entries, start=1):
            if job.cancelled:
                raise asyncio.CancelledError
            name = archive.safe_name(entry.name)
            head = f"🗂 <b>{index}/{len(entries)}</b> — {ui.esc(name)}"

            try:
                await status.edit_text(f"{head}\n\n📤 <i>unpacking…</i>")
            except Exception:
                pass

            try:
                extracted = archive.extract_one(zip_path, entry, out_dir, password)
            except archive.ArchiveError as exc:
                failed.append(f"{name} — {exc}")
                continue

            try:
                # A .mkv or .avi arrives as a grey document unless it is
                # repackaged. `-c copy` makes that a few seconds, not a re-encode.
                to_send = extracted
                if extracted.suffix.lower() != ".mp4":
                    try:
                        await status.edit_text(f"{head}\n\n🔄 <i>making it playable…</i>")
                    except Exception:
                        pass
                    try:
                        to_send = await media.remux_to_mp4(
                            extracted, out_dir / f"{extracted.stem}.mp4",
                            cancelled=lambda: job.cancelled,
                        )
                    except media.MediaError as exc:
                        log.info("remux refused for %s (%s), sending as-is", name, exc)
                        to_send = extracted

                result = await uploader.send_best_effort(
                    client, job.chat_id, to_send,
                    caption=f"🎬 <b>{ui.esc(name)}</b>\n<i>{index} of {len(entries)}</i>",
                    status=status, title=name, cancelled=lambda: job.cancelled,
                )
                sent += 1
                job.file_name = name
                job.size_bytes += result.size_bytes
            except uploader.TooLarge as exc:
                failed.append(f"{name} — too big for Telegram")
                await client.send_message(job.chat_id, exc.user_message())
            finally:
                extracted.unlink(missing_ok=True)
                if to_send != extracted:
                    to_send.unlink(missing_ok=True)

        if sent == 0:
            raise archive.ArchiveError(
                "None of the videos in that archive could be sent.\n"
                + ("\n".join(f"• {ui.esc(f)}" for f in failed[:5]) if failed else "")
            )

        tail = ""
        if failed:
            tail = "\n\n⚠️ <b>Skipped:</b>\n" + "\n".join(f"• {ui.esc(f)}" for f in failed[:5])
        await status.edit_text(
            f"✅ <b>Done — {sent} video{'s' if sent != 1 else ''} sent</b>\n"
            f"📦 {ui.human_bytes(job.size_bytes)} total{tail}",
            reply_markup=kb.back_to_menu(),
        )
    finally:
        shutil.rmtree(zip_path.parent, ignore_errors=True)

async def _accept_archive(client: Client, message: Message, jobs: jobq.Queue,
                          password: str | None = None) -> None:
    """Download the archive from Telegram, price it, and queue it."""
    doc = message.document
    user_id = message.from_user.id
    size = doc.file_size or 0
    cost = archive.price_for(size)

    if not credits.can_afford(user_id, cost):
        have = credits.balance(user_id)
        await message.reply_text(ui.insufficient(cost, have),
                                 reply_markup=kb.not_enough_credit(cost - have))
        return

    ceiling = cfg.max_upload_mb * 1024 * 1024
    if size > ceiling:
        await message.reply_text(uploader.TooLarge(size, ceiling).user_message())
        return

    work = _work_dir(user_id, str(message.id))
    work.mkdir(parents=True, exist_ok=True)
    zip_path = work / (archive.safe_name(doc.file_name or "archive.zip"))

    status = await message.reply_text("🗂 <b>Getting your archive…</b>")
    throttle = ui.Throttle(4.0)
    started = time.monotonic()

    async def on_progress(current: int, total: int) -> None:
        if not throttle.ready(force=current >= total):
            return
        try:
            await status.edit_text(ui.progress_block(
                "🗂 Receiving archive", current, total, started,
                stage="downloading from Telegram"))
        except Exception:
            pass

    try:
        await client.download_media(message, file_name=str(zip_path),
                                    progress=on_progress)
    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        log.warning("archive download failed: %s", exc)
        await status.edit_text("❌ Could not receive that file. Please send it again.",
                               reply_markup=kb.back_to_menu())
        return

    try:
        entries = archive.inspect(zip_path, password)
    except archive.NeedsPassword:
        state.set_mode(user_id, "await_zip_password",
                       zip_path=str(zip_path), message_id=message.id)
        await status.edit_text(
            "🔒 <b>That archive is locked</b>\n\nSend me the password and I will open it.",
            reply_markup=kb.back_to_menu("✖  Cancel"))
        return
    except archive.ArchiveError as exc:
        shutil.rmtree(work, ignore_errors=True)
        await status.edit_text(f"❌ {ui.esc(exc)}", reply_markup=kb.back_to_menu())
        return

    videos = archive.videos_in(entries)
    if not videos:
        shutil.rmtree(work, ignore_errors=True)
        await status.edit_text(
            "🤷 <b>No videos in that archive.</b>\n\nNothing was charged.",
            reply_markup=kb.back_to_menu())
        return

    state.clear_mode(user_id)
    await status.edit_text(archive.summary(entries, videos, size, cost)
                           + "\n\n⏳ <i>starting…</i>")

    job = jobq.Job(
        user_id=user_id, chat_id=message.chat.id, kind="zip",
        runner=lambda j: _deliver(client, j),
        cost=cost, title=zip_path.stem, source=zip_path.name,
        payload={"zip_path": zip_path, "password": password,
                 "videos": videos, "status": status},
    )
    try:
        jobs.submit(job)
    except (credits.InsufficientCredits, jobq.Rejected) as exc:
        shutil.rmtree(work, ignore_errors=True)
        text = getattr(exc, "user_message", None)
        await status.edit_text(text if isinstance(text, str) else ui.insufficient(cost, credits.balance(user_id)),
                               reply_markup=kb.not_enough_credit(cost))

def register(app: Client, jobs: jobq.Queue) -> None:

    @app.on_callback_query(filters.regex(r"^mode:zip$"))
    async def open_zip(client: Client, cq: CallbackQuery) -> None:
        state.set_mode(cq.from_user.id, "zip")
        await cq.answer()
        await cq.message.edit_text(PROMPT, reply_markup=kb.back_to_menu())

    @app.on_message(filters.private & filters.document)
    async def got_document(client: Client, message: Message) -> None:
        name = (message.document.file_name or "").lower()
        mime = (message.document.mime_type or "").lower()
        if not (name.endswith(".zip") or "zip" in mime):
            await message.reply_text(
                "🗂 I can only open <b>.zip</b> archives right now.",
                reply_markup=kb.back_to_menu())
            return
        await _accept_archive(client, message, jobs)

    @app.on_message(filters.private & filters.text & ~filters.command(["start", "balance"]))
    async def maybe_password(client: Client, message: Message) -> None:
        """Only claims the message when a locked archive is waiting on a password."""
        entry = state.get_mode(message.from_user.id)
        if not entry or entry[0] != "await_zip_password":
            return
        _, payload = entry
        zip_path = Path(payload["zip_path"])
        password = (message.text or "").strip()
        state.clear_mode(message.from_user.id)

        if not zip_path.exists():
            await message.reply_text("That archive expired. Please send it again.",
                                     reply_markup=kb.back_to_menu())
            return
        try:
            entries = archive.inspect(zip_path, password)
        except archive.ArchiveError as exc:
            state.set_mode(message.from_user.id, "await_zip_password",
                           zip_path=str(zip_path), message_id=payload.get("message_id"))
            await message.reply_text(f"❌ {ui.esc(exc)}\n\nSend the password again, or tap Cancel.",
                                     reply_markup=kb.back_to_menu("✖  Cancel"))
            return

        videos = archive.videos_in(entries)
        if not videos:
            shutil.rmtree(zip_path.parent, ignore_errors=True)
            await message.reply_text("🤷 No videos in that archive. Nothing was charged.",
                                     reply_markup=kb.back_to_menu())
            return

        size = zip_path.stat().st_size
        cost = archive.price_for(size)
        status = await message.reply_text(
            archive.summary(entries, videos, size, cost) + "\n\n⏳ <i>starting…</i>")
        job = jobq.Job(
            user_id=message.from_user.id, chat_id=message.chat.id, kind="zip",
            runner=lambda j: _deliver(client, j),
            cost=cost, title=zip_path.stem, source=zip_path.name,
            payload={"zip_path": zip_path, "password": password,
                     "videos": videos, "status": status},
        )
        try:
            jobs.submit(job)
        except (credits.InsufficientCredits, jobq.Rejected):
            shutil.rmtree(zip_path.parent, ignore_errors=True)
            have = credits.balance(message.from_user.id)
            await status.edit_text(ui.insufficient(cost, have),
                                  reply_markup=kb.not_enough_credit(cost - have))
