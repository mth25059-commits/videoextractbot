"""
The Terabox service: paste up to ten links, get ten videos back.

    paste links ──► confirm N × 1 cr ──► one queue job per link
                                          │
                                          ├─ resolve (share/list → dlink)
                                          ├─ fetch   (direct file, or HLS)
                                          ├─ remux   (-c copy, only if needed)
                                          └─ upload  (video, streamable)

Deliberate choices:

**No quality menu.** The original upload is the best quality there is, so the
provider returns one stream and the bot takes it. A picker would only ever offer
"worse".

**Resolution happens in the worker, not at the door.** Ten links would mean ten
API round-trips before the user saw anything, and a link that has been deleted
would hold up the other nine. The door only counts links and checks the balance;
everything slow happens on a worker with a live progress bar.

**One job per link.** They finish out of order — a 40 MB clip should not wait
behind a 1.8 GB film — and each one is charged, refunded and cancelled on its own.
A link holding a folder still costs one credit; the provider caps how many files
that can be.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import (credits, download, keyboards as kb, media, providers,
                queue as jobq, state, ui, uploader)
from ..config import cfg
from ..providers.terabox import terabox
from . import _gate

log = logging.getLogger(__name__)

MODE = "await_links"

PROMPT = (
    "📦 <b>Send me your Terabox links</b>\n\n"
    f"Up to <b>{cfg.max_links_per_batch}</b> at a time — one per line, or all in "
    "one message, whichever is easier.\n\n"
    f"💰 <b>{cfg.cost_terabox_per_link:g} credit per link</b>. Highest quality is "
    "picked automatically.\n\n"
    "<i>They are fetched in the background and each video is sent the moment it "
    "is ready, so you do not have to wait for the whole batch.</i>"
)


def _work_dir(job: jobq.Job) -> Path:
    return cfg.work_dir / f"tb-{job.user_id}-{job.row_id or int(time.time())}"


async def _deliver(client: Client, job: jobq.Job) -> None:
    """
    The queue runner for one link: resolve → fetch → remux if needed → send.

    Everything lands in a per-job directory that is removed in `finally`, so a
    crash mid-fetch cannot leave a 1.9 GB `.part` file behind for ever.
    """
    status: Message = job.payload["status"]
    url: str = job.source
    work = _work_dir(job)
    work.mkdir(parents=True, exist_ok=True)
    head = f"📦 <b>{ui.esc(job.label)}</b>"

    try:
        try:
            await status.edit_text(f"{head}\n\n🔍 <i>reading the link…</i>",
                                   reply_markup=kb.cancel_only(job.token))
        except Exception:
            pass

        found = await terabox.resolve_all(url)
        if job.cancelled:
            raise asyncio.CancelledError

        sent = 0
        for index, resolved in enumerate(found, start=1):
            if job.cancelled:
                raise asyncio.CancelledError

            stream = resolved.best
            name = resolved.safe_title
            of = f" ({index}/{len(found)})" if len(found) > 1 else ""
            title = f"{name}{of}"
            job.title = job.title or name
            job.quality = stream.label

            started = time.monotonic()
            throttle = ui.Throttle(5.0)

            async def on_progress(done: int, total: int, _t=throttle, _s=started,
                                  _title=title) -> None:
                if not _t.ready(force=bool(total) and done >= total):
                    return
                try:
                    await status.edit_text(
                        ui.progress_block(f"📥 {_title}", done, total, _s,
                                          stage="downloading"),
                        reply_markup=kb.cancel_only(job.token))
                except Exception:
                    pass

            target = work / f"{index:02d}.mp4"
            if stream.kind == "file":
                # Keep the real extension: ffmpeg needs it to pick a demuxer, and
                # an .mp4 that arrives as an .mp4 can skip the remux entirely.
                suffix = Path(name).suffix.lower()
                if suffix not in media.VIDEO_SUFFIXES:
                    suffix = ".bin"
                raw = work / f"{index:02d}{suffix}"
                await download.to_file(
                    stream.url, raw,
                    headers=stream.headers,
                    proxy=(cfg.proxies[0] if cfg.proxies else None),
                    on_progress=on_progress,
                    cancelled=lambda: job.cancelled,
                    max_bytes=cfg.max_upload_mb * 1024 * 1024,
                )
                if suffix == ".mp4" and media.looks_like_video(raw):
                    # Already an MP4; remuxing would cost a full pass over the
                    # file to gain a faststart flag Telegram tolerates without.
                    target = raw
                else:
                    try:
                        await status.edit_text(f"{head}\n\n🔄 <i>making it playable…</i>",
                                              reply_markup=kb.cancel_only(job.token))
                    except Exception:
                        pass
                    try:
                        target = await media.remux_to_mp4(
                            raw, target, cancelled=lambda: job.cancelled)
                        raw.unlink(missing_ok=True)
                    except media.MediaError as exc:
                        log.info("remux refused for %s (%s), sending as-is", name, exc)
                        target = raw
            else:
                # HLS: ffmpeg pulls the playlist straight into a faststart MP4.
                await media.fetch_to_mp4(
                    stream.url, target,
                    total_seconds=resolved.duration_seconds or 0.0,
                    headers=stream.headers,
                    on_progress=on_progress,
                    cancelled=lambda: job.cancelled,
                )

            try:
                result = await uploader.send_best_effort(
                    client, job.chat_id, target,
                    caption=f"🎬 <b>{ui.esc(name)}</b>",
                    status=status, title=name, cancelled=lambda: job.cancelled,
                )
                sent += 1
                job.file_name = name
                job.size_bytes += result.size_bytes
            except uploader.TooLarge as exc:
                await client.send_message(job.chat_id, exc.user_message())
            finally:
                target.unlink(missing_ok=True)

        if sent == 0:
            raise providers.ResolveError(
                "Telegram would not take that file — it is over the size limit.")

        await status.edit_text(
            f"✅ <b>Done</b>\n🎬 {ui.esc(job.file_name)}\n"
            f"📦 {ui.human_bytes(job.size_bytes)}"
            + (f"\n<i>{sent} videos from this link</i>" if sent > 1 else ""),
            reply_markup=kb.back_to_menu(),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def _queue_batch(client: Client, message: Message, jobs: jobq.Queue,
                       links: list[str]) -> None:
    """One status message and one job per link, keeping whatever the balance covers."""
    user_id = message.from_user.id
    pending: list[jobq.Job] = []

    for index, url in enumerate(links):
        status = await message.reply_text(
            f"📦 <b>Queued</b> — <code>{index + 1}/{len(links)}</code>\n"
            f"<i>waiting for a free worker…</i>")
        parked = state.park(user_id, "terabox", url=url)
        pending.append(jobq.Job(
            user_id=user_id,
            chat_id=message.chat.id,
            kind="terabox",
            runner=lambda job: _deliver(client, job),
            cost=cfg.cost_terabox_per_link,
            title="",
            source=url,
            token=parked.token,
            index=index,
            total_in_batch=len(links),
            payload={"status": status, "url": url},
        ))

    accepted, rejected = jobs.submit_many(pending)

    for job, exc in rejected:
        status: Message = job.payload["status"]
        text = getattr(exc, "user_message", None)
        await status.edit_text(
            text if isinstance(text, str)
            else ui.insufficient(job.cost, credits.balance(user_id)),
            reply_markup=kb.not_enough_credit(job.cost))
        state.clear_cancel(job.token)

    if accepted:
        spent = sum(j.cost for j in accepted)
        note = ""
        if rejected:
            note = (f"\n\n⚠️ <b>{len(rejected)} link(s) not started</b> — "
                    "your balance covered the rest. Top up and send those again.")
        await message.reply_text(
            f"▶️ <b>{len(accepted)} link(s) started</b>\n"
            f"💰 {spent:g} credits held · balance <b>{credits.balance(user_id):g}</b>\n\n"
            "<i>Each video arrives on its own as it finishes. Anything that fails "
            "is refunded automatically.</i>" + note)


def register(app: Client, jobs: jobq.Queue) -> None:

    @app.on_callback_query(filters.regex(r"^mode:terabox$"))
    async def open_terabox(client: Client, cq: CallbackQuery) -> None:
        state.set_mode(cq.from_user.id, MODE)
        await cq.answer()
        await cq.message.edit_text(PROMPT, reply_markup=kb.back_to_menu("◀  Menu"),
                                   disable_web_page_preview=True)

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode(MODE))
    async def got_links(client: Client, message: Message) -> None:
        """Only reached while the user is in the Terabox flow (see _gate)."""
        user_id = message.from_user.id
        links = terabox.extract_links(message.text or "")

        if not links:
            await message.reply_text(
                "🤔 <b>No Terabox link in that message</b>\n\n"
                "They look like <code>https://terabox.com/s/1abc…</code> — paste "
                "the whole link, one per line.",
                reply_markup=kb.back_to_menu("◀  Menu"),
                disable_web_page_preview=True)
            return

        state.clear_mode(user_id)
        trimmed = links[:cfg.max_links_per_batch]
        cost = len(trimmed) * cfg.cost_terabox_per_link
        have = credits.balance(user_id)

        extra = ""
        if len(links) > len(trimmed):
            extra = (f"\n\n<i>You sent {len(links)}; the first "
                     f"{cfg.max_links_per_batch} are taken — send the rest after.</i>")

        if have <= 0:
            await message.reply_text(ui.insufficient(cost, have),
                                     reply_markup=kb.not_enough_credit(cost - have))
            return

        parked = state.park(user_id, "terabox_batch", links=trimmed)
        affordable = min(len(trimmed), int(have // cfg.cost_terabox_per_link))
        short = ""
        if affordable < len(trimmed):
            short = (f"\n\n⚠️ Your balance covers <b>{affordable}</b> of them. "
                     "The rest will not be started, and nothing is held for them.")

        await message.reply_text(
            f"📦 <b>{len(trimmed)} link(s) ready</b>\n\n"
            f"💰 Cost: <b>{cost:g} credits</b> · you have <b>{have:g}</b>\n"
            f"🎚 Quality: <b>highest available</b>{short}{extra}",
            reply_markup=kb.confirm_batch(parked.token, len(trimmed), cost),
            disable_web_page_preview=True)

    @app.on_callback_query(filters.regex(r"^job:go:(?P<token>[\w-]+)$"))
    async def start_batch(client: Client, cq: CallbackQuery) -> None:
        parked = state.take(cq.data.split(":")[2], cq.from_user.id)
        if parked is None or parked.kind != "terabox_batch":
            await cq.answer("That has expired — send the links again.", show_alert=True)
            return
        await cq.answer("Starting…")
        try:
            await cq.message.edit_text("📦 <b>Starting…</b>")
        except Exception:
            pass
        await _queue_batch(client, cq.message, jobs, parked.payload["links"])

    @app.on_callback_query(filters.regex(r"^job:cancel:(?P<token>[\w-]+)$"))
    async def cancel_job(client: Client, cq: CallbackQuery) -> None:
        token = cq.data.split(":")[2]
        state.cancel(token)
        parked = state.peek(token, cq.from_user.id)
        if parked is not None and parked.kind == "terabox_batch":
            state.take(token, cq.from_user.id)
            await cq.answer("Cancelled — nothing was charged.")
            await cq.message.edit_text("✖ <b>Cancelled.</b> Nothing was charged.",
                                       reply_markup=kb.back_to_menu())
            return
        await cq.answer("Stopping — your credits come back automatically.")
