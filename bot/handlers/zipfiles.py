"""
The archive flow: ZIP, RAR or 7z in, playable videos out.

Charging is per archive, not per video, so the whole thing is one queue job. The
refund rule follows from that: if nothing sendable comes out the job fails and the
credits go back in full, and if only some of the videos arrive the missing share
is refunded — `Job.expected` / `Job.delivered`, settled in `queue._refund_part`.

A video inside the archive that is over Telegram's per-file ceiling is **cut into
parts and sent** rather than skipped, so "8 videos found" means 8 videos delivered.

Nothing stays on the VPS. The archive is deleted the moment it has handed over its
last video — not at the end of the job — one extracted video is on disk at a time,
and `bot/scratch.py` owns the directory so that a crash between those steps is
swept up rather than left there. It cannot be done without touching the disk at
all: every archive format keeps its table of contents at the *end* of the file, so
opening one means seeking backwards through it and a Telegram download only goes
forwards.

The upload progress bar is edited into one status message that is reused for
every video in the archive, so a 12-video ZIP does not leave 12 dead progress
messages in the chat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import (archive, credits, expiry, keyboards as kb, media, queue as jobq, scratch,
                state, ui, uploader)
from ..config import cfg
from . import _gate

log = logging.getLogger(__name__)

def prompt() -> str:
    """
    The 🗂 File screen, built per message.

    A function rather than a module constant because every figure on it comes from
    `archive.price_for`, which now reads the two ZIP rungs out of `settings` — a
    constant would have frozen the price list at import and gone stale the first time
    the admin changed a rung.

    The ladder is rendered from `price_for` rather than written out, so the screen and
    the charge cannot drift apart.
    """
    gb = archive.GB
    per_extra = archive.price_for(3 * gb) - archive.price_for(2 * gb)
    return (
        "🗂 <b>Send me a ZIP, RAR or 7z file</b>\n\n"
        "I will open it here, pull out every video inside, and send each one as a "
        "playable video — so nothing has to be downloaded or unpacked on your phone.\n\n"
        f"💰 <b>Price</b>\n"
        f"  • up to 1 GB — <b>{archive.price_for(gb):g} credits</b>\n"
        f"  • 1 – 2 GB — <b>{archive.price_for(2 * gb):g} credits</b>\n"
        f"  • 2 – 3 GB — <b>{archive.price_for(3 * gb):g} credits</b>\n"
        f"  • 3 – 4 GB — <b>{archive.price_for(4 * gb):g} credits</b>\n"
        f"  • bigger — <b>+{per_extra:g} credits</b> per extra GB\n\n"
        "<i>One price for the whole archive, however many videos are in it. A video too "
        "big for Telegram is sent in parts instead of being skipped.</i>"
    )



def _work_dir(user_id: int, tag: str) -> Path:
    return cfg.work_dir / f"zip-{user_id}-{tag}"

async def _deliver(client: Client, job: jobq.Job) -> None:
    """
    The queue runner: extract → remux if needed → send → delete, one video at a time.

    Only one extracted file is on disk at any moment, and the archive itself is
    deleted the instant it has handed over its last video rather than at the end of
    the job. That is what lets a 4 GB archive be processed on a box with 8 GB free,
    and it is why a one-video archive — the common case — never needs twice its own
    size in scratch.
    """
    zip_path: Path = job.payload["zip_path"]
    password: str | None = job.payload.get("password")
    entries: list[archive.Entry] = job.payload["videos"]
    status: Message = job.payload["status"]
    out_dir = zip_path.parent / "out"

    sent = 0
    parts_used = 0
    failed: list[str] = []
    job.expected = len(entries)
    try:
        for index, entry in enumerate(entries, start=1):
            if job.cancelled:
                raise asyncio.CancelledError
            # `name` is the archive's own name for this file. It is used for the
            # "Skipped:" list and the job row — never for the panel and never for what
            # is written to disk, because the on-disk name is what Telegram is handed.
            name = archive.safe_name(entry.name)
            head = f"🗂 <b>{ui.panel_title(index - 1, len(entries))}</b>"

            try:
                await status.edit_text(f"{head}\n\n📤 <i>unpacking…</i>")
            except Exception:
                pass

            try:
                extracted = archive.extract_one(zip_path, entry, out_dir, password,
                                                as_name=f"{index:02d}")
            except archive.ArchiveError as exc:
                failed.append(f"{name} — {exc}")
                continue
            finally:
                if index == len(entries):
                    # Last one out. The archive has given up everything it is going
                    # to — succeeded or not — and carrying it through the final
                    # upload doubles the disk this job needs for nothing. It cannot
                    # go any earlier than this: ZIP, RAR and 7z all keep their
                    # directory at the end of the file, so every extraction seeks
                    # back into it and the whole archive has to be there to do it.
                    zip_path.unlink(missing_ok=True)

            to_send = extracted        # assigned before the try: the finally reads it
            try:
                # A .mkv or .avi arrives as a grey document unless it is
                # repackaged. `-c copy` makes that a few seconds, not a re-encode.
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
                        log.info("remux refused for item %d of job %s (%s), sending as-is",
                                 index, job.row_id, exc)
                        to_send = extracted

                result = await uploader.send_best_effort(
                    client, job.chat_id, to_send,
                    status=status, title=ui.panel_title(index - 1, len(entries)),
                    cancelled=lambda: job.cancelled,
                )
                sent += 1
                job.delivered = sent
                if result.parts > 1:
                    parts_used += 1
                job.file_name = name
                job.size_bytes += result.size_bytes
                # The ZIP route is on the same clock as the other two. A video that went
                # out in pieces has every piece and its rejoin note scheduled, which is
                # what `Sent.message_ids` is for — half a split video left behind is
                # worse than none of it.
                expiry.remember(result, job.chat_id, job.row_id)
            except uploader.TooLarge as exc:
                # Only reachable for a zero-byte file now — anything over the
                # ceiling is cut into parts by `send_best_effort` instead.
                failed.append(f"{name} — Telegram would not take it")
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
        if parts_used:
            tail += (f"\n✂️ {parts_used} video{'s' if parts_used != 1 else ''} "
                     "went out in parts — join them as described above.")
        if failed:
            tail += "\n\n⚠️ <b>Skipped:</b>\n" + "\n".join(f"• {ui.esc(f)}" for f in failed[:5])
        await status.edit_text(
            f"✅ <b>Done — {sent} of {len(entries)} video"
            f"{'s' if len(entries) != 1 else ''} sent</b>\n"
            f"📦 {ui.human_bytes(job.size_bytes)} total{tail}",
            reply_markup=kb.back_to_menu(),
        )
    finally:
        scratch.release(zip_path.parent)


async def _accept_archive(client: Client, message: Message, jobs: jobq.Queue,
                          password: str | None = None) -> bool:
    """
    Download the archive from Telegram, price it, and queue it.

    Returns **True once the bytes are on the box**, which is the caller's signal that
    the user's upload can be taken back out of the chat. It is deliberately not True
    on the busy or too-poor paths: nothing was received there, and the message is the
    only copy the user can forward back to the bot when they are ready. `False` also
    covers a failed download, where the reply asks them to send it again.
    """
    doc = message.document
    user_id = message.from_user.id
    size = doc.file_size or 0
    cost = archive.price_for(size)

    # The archive lane only. A link of theirs downloading right now is a different
    # pipe — Terabox's CDN, not Telegram's — so it does not stand in the way of this,
    # and the refusal below says which one is actually busy.
    if jobs.busy(user_id, jobq.ZIP_LANE):
        await message.reply_text(
            "⏳ <b>One archive at a time, please</b>\n\nYour last ZIP is still "
            "unpacking. I will be free the moment it finishes.\n\n"
            "🔗 <b>Links are not blocked by this.</b> Paste a Terabox link right now "
            "and it will run alongside this archive.",
            reply_markup=kb.back_to_menu())
        return False

    if not credits.can_afford(user_id, cost):
        have = credits.balance(user_id)
        await message.reply_text(ui.insufficient(cost, have),
                                 reply_markup=kb.not_enough_credit(cost - have))
        return False

    # No size refusal here on purpose. Telegram's per-file ceiling applies to what
    # goes *out*, and an oversized video now leaves in parts rather than being
    # skipped, so an archive bigger than one upload is a normal job. Scratch space is
    # the real constraint and `Queue._wait_for_disk` owns it.

    work = scratch.claim(_work_dir(user_id, str(message.id)))
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
                stage="downloading from Telegram",
                expires_minutes=cfg.auto_delete_minutes))
        except Exception:
            pass

    try:
        await client.download_media(message, file_name=str(zip_path),
                                    progress=on_progress)
    except Exception as exc:
        scratch.release(work)
        log.warning("archive download failed: %s", exc)
        await status.edit_text("❌ Could not receive that file. Please send it again.",
                               reply_markup=kb.back_to_menu())
        return False

    try:
        entries = archive.inspect(zip_path, password)
    except archive.NeedsPassword:
        # Kept on disk — the password is coming — but no longer claimed, because
        # if it never comes nothing else will ever delete this. The prompt itself
        # expires after `state.TTL_SECONDS`, and `scratch.STALE_SECONDS` is set
        # past that, so the janitor takes the file once it is unreachable.
        scratch.unclaim(work)
        state.set_mode(user_id, "await_zip_password",
                       zip_path=str(zip_path), message_id=message.id)
        await status.edit_text(
            "🔒 <b>That archive is locked</b>\n\nSend me the password and I will open it.",
            reply_markup=kb.back_to_menu("✖  Cancel"))
        return True
    except archive.ArchiveError as exc:
        scratch.release(work)
        await status.edit_text(f"❌ {ui.esc(exc)}", reply_markup=kb.back_to_menu())
        return True

    videos = archive.videos_in(entries)
    if not videos:
        scratch.release(work)
        await status.edit_text(
            "🤷 <b>No videos in that archive.</b>\n\nNothing was charged.",
            reply_markup=kb.back_to_menu())
        return True

    state.clear_mode(user_id)
    await status.edit_text(archive.summary(entries, videos, size, cost,
                                          archive.kind_or_blank(zip_path))
                           + "\n\n⏳ <i>starting…</i>")

    job = jobq.Job(
        user_id=user_id, chat_id=message.chat.id, kind="zip",
        runner=lambda j: _deliver(client, j),
        cost=cost, title=zip_path.stem, source=zip_path.name,
        expected=len(videos),
        payload={"zip_path": zip_path, "password": password,
                 "videos": videos, "status": status},
    )
    try:
        jobs.submit(job)
    except (credits.InsufficientCredits, jobq.Rejected) as exc:
        scratch.release(work)
        text = getattr(exc, "user_message", None)
        await status.edit_text(text if isinstance(text, str) else ui.insufficient(cost, credits.balance(user_id)),
                               reply_markup=kb.not_enough_credit(cost))
    return True

def register(app: Client, jobs: jobq.Queue) -> None:

    @app.on_callback_query(filters.regex(r"^mode:zip$"))
    async def open_zip(client: Client, cq: CallbackQuery) -> None:
        state.set_mode(cq.from_user.id, "zip")
        await cq.answer()
        await cq.message.edit_text(prompt(), reply_markup=kb.back_to_menu())

    @app.on_message(filters.private & filters.document)
    async def got_document(client: Client, message: Message) -> None:
        """
        Accept anything that might be an archive; the real check is the first bytes.

        The name is only a filter for what to bother downloading — `archive.kind_of`
        decides the format once the file is here, so a RAR sent as `movie.zip` still
        opens. Refusing on the name alone was the old bug: Telegram and WhatsApp
        rename freely and the user gets blamed for it.
        """
        name = (message.document.file_name or "").lower()
        mime = (message.document.mime_type or "").lower()
        looks_right = (any(name.endswith(s) for s in archive.ARCHIVE_SUFFIXES)
                       or any(word in mime for word in ("zip", "rar", "7z", "compressed")))
        if not looks_right:
            await message.reply_text(
                "🗂 <b>That is not an archive</b>\n\nSend me a <b>.zip</b>, "
                "<b>.rar</b> or <b>.7z</b> file and I will pull the videos out of it.",
                reply_markup=kb.back_to_menu())
            return
        # The upload goes back out of the chat once the box has it — the same rule as
        # the pasted link and the pressed key, and this was the one input still left
        # sitting there. It is deliberately after the download: `download_media` reads
        # the message, so deleting first would take the file with it. `_accept_archive`
        # answers False when there is nothing on disk yet, and then the user's copy
        # stays put — it is the only thing they can forward back to the bot.
        if await _accept_archive(client, message, jobs):
            await _gate.forget(message, "the uploaded archive")

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode("await_zip_password"))
    async def maybe_password(client: Client, message: Message) -> None:
        """Only reached while a locked archive is waiting on a password (see _gate)."""
        entry = state.get_mode(message.from_user.id)
        if not entry:            # swept between the filter and here — very unlikely
            return
        _, payload = entry
        zip_path = Path(payload["zip_path"])
        password = (message.text or "").strip()
        state.clear_mode(message.from_user.id)

        # **The password goes first.** It is the most sensitive thing anyone types into
        # this bot and it was staying in the chat for ever; it is read into a local
        # before this line and never logged. Everything below therefore posts with
        # `send_message` rather than `reply_text`: there is no longer a message to
        # reply to.
        await _gate.forget(message, "the archive password")

        if not zip_path.exists():
            await client.send_message(message.chat.id,
                                      "That archive expired. Please send it again.",
                                      reply_markup=kb.back_to_menu())
            return
        try:
            entries = archive.inspect(zip_path, password)
        except archive.ArchiveError as exc:
            state.set_mode(message.from_user.id, "await_zip_password",
                           zip_path=str(zip_path), message_id=payload.get("message_id"))
            await client.send_message(
                message.chat.id,
                f"❌ {ui.esc(exc)}\n\nSend the password again, or tap Cancel.",
                reply_markup=kb.back_to_menu("✖  Cancel"))
            return

        videos = archive.videos_in(entries)
        if not videos:
            scratch.release(zip_path.parent)
            await client.send_message(message.chat.id,
                                      "🤷 No videos in that archive. Nothing was charged.",
                                      reply_markup=kb.back_to_menu())
            return

        # The password worked, so this directory has an owner again — claimed back
        # from the janitor before the job that needs it is queued.
        scratch.claim(zip_path.parent)
        size = zip_path.stat().st_size
        cost = archive.price_for(size)
        status = await client.send_message(
            message.chat.id,
            archive.summary(entries, videos, size, cost,
                            archive.kind_or_blank(zip_path)) + "\n\n⏳ <i>starting…</i>")
        job = jobq.Job(
            user_id=message.from_user.id, chat_id=message.chat.id, kind="zip",
            runner=lambda j: _deliver(client, j),
            cost=cost, title=zip_path.stem, source=zip_path.name,
            expected=len(videos),
            payload={"zip_path": zip_path, "password": password,
                     "videos": videos, "status": status},
        )
        try:
            jobs.submit(job)
        except (credits.InsufficientCredits, jobq.Rejected):
            scratch.release(zip_path.parent)
            have = credits.balance(message.from_user.id)
            await status.edit_text(ui.insufficient(cost, have),
                                  reply_markup=kb.not_enough_credit(cost - have))
