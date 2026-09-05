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

**One message on screen, not five.** The prompt is edited into the confirm card,
the card into the first link's live panel, and the panel is deleted once the video
has been sent; the user's own pasted text and key presses are deleted too. So a
single link — which is most of them — leaves the chat holding the video and nothing
else. Every one of those steps is best-effort: a refused edit falls back to a new
message and a refused delete is ignored, because none of it is worth failing a job
the user has paid for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import (credits, download, egress, expiry, keyboards as kb, media, providers,
                queue as jobq, scratch, settings, state, ui, uploader)
from ..config import cfg
from ..providers.terabox import terabox
from . import _gate

log = logging.getLogger(__name__)

MODE = "await_links"


def prompt() -> str:
    """
    The 📦 Terabox screen, built per message.

    It used to be a module-level f-string, which meant the price in it was the price
    at *import* — so an admin who changed the rate saw the old number on this screen
    until the next restart, which is exactly the thing Prices exists to avoid.

    The wording is "per video", not "per link". A folder link is one link and up to
    `terabox_max_files_per_link` videos, and it is charged per video — the old text
    said "credit per link", which read as a promise that a 10-video folder cost one
    credit.
    """
    per_video = settings.get("cost_terabox_per_link")
    return (
        "📦 <b>Send me your Terabox links</b>\n\n"
        f"Up to <b>{cfg.max_links_per_batch}</b> at a time — one per line, or all in "
        "one message, whichever is easier.\n\n"
        f"💰 <b>{per_video:g} credit{'' if per_video == 1 else 's'} per video</b> — a "
        "plain link is one video, a folder link is charged for what is inside it. "
        "Highest quality is picked automatically.\n\n"
        "<i>They are fetched in the background and each video is sent the moment it "
        "is ready, so you do not have to wait for the whole batch.</i>"
    )


def _work_dir(job: jobq.Job) -> Path:
    return cfg.work_dir / f"tb-{job.user_id}-{job.row_id or int(time.time())}"


#: Seconds between animation frames, and how many messages one frame may touch.
#: Telegram tolerates about one edit a second in a chat, so the product of these
#: two is the real constraint — 4 edits per 4 seconds. With more waiting than
#: that the band moves slower rather than the bot getting flood-waited.
WAIT_TICK_SECONDS = 4.0
WAIT_EDITS_PER_TICK = 4
WAIT_GIVE_UP_SECONDS = 1800.0

#: Live animator tasks, kept referenced so the event loop cannot collect one.
_animators: set[asyncio.Task] = set()


def _waiting_title(job: jobq.Job) -> str:
    if job.total_in_batch > 1:
        return f"📦 Link {job.index + 1} of {job.total_in_batch}"
    return "📦 Your link"


async def _animate_waiting(pending: list[jobq.Job]) -> None:
    """
    Keep the not-yet-started jobs looking alive until a worker takes them.

    `job.started_at` is the handover: the worker sets it before it runs anything,
    and `_deliver` overwrites the message with its own panel a moment later. So
    this needs no coordination with the queue — it animates what has not started
    and stops when nothing is left, which also covers cancel and shutdown.

    Edits are spread across ticks instead of all being sent at once. Ten waiting
    messages every four seconds would be 2.5 edits a second in one chat, and the
    `FloodWait` that earns would land on the *jobs*, not on the decoration.
    """
    tick = 0
    cursor = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < WAIT_GIVE_UP_SECONDS:
            await asyncio.sleep(WAIT_TICK_SECONDS)
            tick += 1
            waiting = [j for j in pending if not j.started_at and not j.cancelled]
            if not waiting:
                return
            for _ in range(min(WAIT_EDITS_PER_TICK, len(waiting))):
                job = waiting[cursor % len(waiting)]
                cursor += 1
                status: Message | None = job.payload.get("status")
                if status is None:
                    continue
                try:
                    await status.edit_text(
                        ui.waiting_block(_waiting_title(job), tick,
                                         seconds=job.waited),
                        reply_markup=kb.cancel_only(job.token))
                except Exception as exc:
                    # FloodWait carries the seconds to wait in `.value`. Anything
                    # else (message deleted, not modified) is not worth a retry.
                    pause = getattr(exc, "value", None)
                    if isinstance(pause, int):
                        await asyncio.sleep(min(pause + 1, 60))
                    break
    except asyncio.CancelledError:
        pass


async def _fetch_bytes(url: str, out_path: Path, *, headers: dict[str, str],
                       on_progress, cancelled) -> None:
    """
    Download one file, through a rotated egress address, falling back to direct.

    **One address per job, chosen here rather than in the provider.** The signed
    resolve leaves directly — Terabox does not bind a `dlink` to the address that
    asked for it, and resolving through a proxy measured flakier — so this is the
    only hop where the rotation pays. Measured 4 September 2026: three links at once
    total 3.11 MB/s direct and 3.92 MB/s with one address each, a 1.26x gain on
    *aggregate* throughput. Splitting one file across four routes was 1.03x, which
    is why nothing here does that.

    **A proxy is never allowed to fail a job.** Two of the ten live addresses
    measured 0.25-0.36 MB/s against a 1.38 MB/s direct baseline, and a whole earlier
    batch answered `CONNECT` with `HTTP 402 Payment Required` and carried nothing at
    all. So a failure benches the address and the same download is retried directly:
    the user's credit buys bytes, not a lottery ticket on someone else's proxy.
    `download.to_file` already retries three times with resume before it raises, so
    reaching the fallback means the route is genuinely bad, not merely unlucky.
    """
    proxy = egress.pick()
    limit = cfg.max_upload_mb * 1024 * 1024
    try:
        await download.to_file(
            url, out_path,
            headers=headers, proxy=proxy,
            on_progress=on_progress, cancelled=cancelled, max_bytes=limit,
        )
        return
    except download.DownloadError as exc:
        if proxy is None:
            raise
        egress.bench(proxy)
        log.info("terabox: %s failed through %s (%s) — retrying direct",
                 out_path.name, egress.describe(proxy), exc)

    # `to_file` removes its own `.part` before it raises, so the retry starts clean
    # rather than resuming bytes that came from the address just benched.
    await download.to_file(
        url, out_path,
        headers=headers, proxy=None,
        on_progress=on_progress, cancelled=cancelled, max_bytes=limit,
    )


async def _deliver(client: Client, job: jobq.Job) -> None:
    """
    The queue runner for one link: resolve → fetch → remux if needed → send.

    Everything lands in a per-job directory that is removed in `finally`, so a
    crash mid-fetch cannot leave a 1.9 GB `.part` file behind for ever. The
    directory is *claimed* while the job runs — see `bot/scratch.py` — so that the
    one crash the `finally` cannot survive, the process being killed outright, is
    cleaned up at the next boot instead.

    **The price is settled here, not at the confirm screen.** A video costs
    `COST_TERABOX_PER_LINK` and a folder link holds up to ten of them, so the real
    figure is only knowable once `resolve_all` has read it — see `jobq.charge_more`.
    """
    status: Message = job.payload["status"]
    url: str = job.source
    work = scratch.claim(_work_dir(job))
    # The panel's heading says where this job sits in the batch and nothing else —
    # never what the video is called. What it is *doing* goes on the line below.
    batch = f"Link {job.index + 1} of {job.total_in_batch}" if job.total_in_batch > 1 else ""
    head = f"📦 <b>{batch or 'Working'}</b>"

    try:
        try:
            await status.edit_text(f"{head}\n\n🔍 <i>reading the link…</i>",
                                   reply_markup=kb.cancel_only(job.token))
        except Exception:
            pass

        found = await terabox.resolve_all(url)
        if job.cancelled:
            raise asyncio.CancelledError

        # One video was paid for at the confirm; a folder holds more. Take the rest
        # now, before a byte is fetched, and cut the list to what the balance covers.
        #
        # At the price the *confirm screen quoted*, which is `job.cost` — one video's
        # worth, set when the job was made and not yet touched by this call. Reading
        # the live price here instead would let an admin's edit land between the tap
        # and the folder listing and charge more than the button said.
        covered = jobq.charge_more(job, job.cost, len(found))
        short = len(found) - covered
        found = found[:covered]
        job.expected = len(found)
        if short > 0:
            try:
                await client.send_message(
                    job.chat_id,
                    f"⚠️ That link holds <b>{short + covered}</b> videos, and your "
                    f"balance covers <b>{covered}</b>. Sending those — the rest are "
                    "not started and nothing is held for them.")
            except Exception:
                log.debug("could not explain the trim", exc_info=True)

        sent = 0
        for index, resolved in enumerate(found, start=1):
            if job.cancelled:
                raise asyncio.CancelledError

            stream = resolved.best
            name = resolved.safe_title
            # Neutral on screen, on purpose — see `ui.panel_title`. The real name is
            # still used for the file on disk and the admin's own log, never in the
            # chat and never on the upload.
            title = (f"{batch} · " if batch else "") + ui.panel_title(index - 1, len(found))
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
                                          stage="downloading",
                                          expires_minutes=cfg.auto_delete_minutes),
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
                await _fetch_bytes(
                    stream.url, raw,
                    headers=stream.headers,
                    on_progress=on_progress,
                    cancelled=lambda: job.cancelled,
                )
                if suffix == ".mp4" and media.looks_like_video(raw):
                    # Already an MP4; remuxing would cost a full pass over the
                    # file to gain a faststart flag Telegram tolerates without.
                    target = raw
                else:
                    try:
                        await status.edit_text(
                            f"📦 <b>{ui.esc(title)}</b>\n\n🔄 <i>making it playable…</i>",
                            reply_markup=kb.cancel_only(job.token))
                    except Exception:
                        pass
                    try:
                        target = await media.remux_to_mp4(
                            raw, target, cancelled=lambda: job.cancelled)
                        raw.unlink(missing_ok=True)
                    except media.MediaError as exc:
                        log.info("remux refused for item %d of job %s (%s), sending as-is",
                                 index, job.row_id, exc)
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
                    status=status, title=title, cancelled=lambda: job.cancelled,
                )
                sent += 1
                # What actually arrived, for the pro-rata refund: a folder of ten that
                # sends seven gives back three videos' worth — see `queue._refund_part`.
                job.delivered = sent
                job.file_name = name
                job.size_bytes += result.size_bytes
                # Each video in a folder gets its own half hour, timed from when it
                # landed rather than from the end of the batch — a ten-link job would
                # otherwise give the first video far longer than the last.
                expiry.remember(result, job.chat_id, job.row_id)
            except uploader.TooLarge as exc:
                await client.send_message(job.chat_id, exc.user_message())
            finally:
                target.unlink(missing_ok=True)

        if sent == 0:
            raise providers.ResolveError(
                "Telegram would not take that file — it is over the size limit.")

        # The panel goes away rather than turning into a receipt. It existed to say
        # "something is happening"; the video that just landed says it better, and a
        # ✅ card above every clip is what makes a ten-link batch unreadable. Only on
        # success: the cancel path sends the user nothing at all and the failure path
        # sends a *new* message, so in both of those the panel is the only trace.
        try:
            await status.delete()
        except Exception:
            # Older than 48 hours, or already gone. Fall back to the receipt — and it
            # names no video either, for the same reason the panel above it did not.
            try:
                await status.edit_text(
                    f"✅ <b>Done</b>\n"
                    f"🎬 {sent} video{'' if sent == 1 else 's'}\n"
                    f"📦 {ui.human_bytes(job.size_bytes)}",
                    reply_markup=kb.back_to_menu())
            except Exception:
                pass
    finally:
        scratch.release(work)


async def _queue_batch(client: Client, message: Message, user_id: int,
                       jobs: jobq.Queue, links: list[str],
                       panel: Message | None = None) -> None:
    """
    One status message and one job per link, keeping whatever the balance covers.

    `panel` is the confirm card the user just pressed. The first link's status takes
    it over instead of posting underneath it, so a single-link batch — which is most
    of them — never leaves a dead card above a live progress bar. Links 2..N get
    their own messages; there is nothing to reuse for those.

    `user_id` is a parameter and not `message.from_user.id`, which is the bug this
    signature exists to prevent. The only caller is a callback handler, and
    `cq.message` is the message the *bot* sent — the one the confirm button is
    attached to — so its `from_user` is the bot. Every link was then charged to the
    bot's own id, which owns no row in `users`:

        ValueError: no such user: 7200000002        credits.py:96, from submit()

    and that lands after the jobs row is written and before anything is enqueued, so
    no credit moves, no worker ever sees the job, and the status message below
    animates for ever. `message` is still the right thing to reply to; it is only
    the identity that cannot come from it.
    """
    pending: list[jobq.Job] = []

    for index, url in enumerate(links):
        parked = state.park(user_id, "terabox", url=url)
        job = jobq.Job(
            user_id=user_id,
            chat_id=message.chat.id,
            kind="terabox",
            runner=lambda job: _deliver(client, job),
            cost=settings.get("cost_terabox_per_link"),
            title="",
            source=url,
            token=parked.token,
            index=index,
            total_in_batch=len(links),
            payload={"url": url},
        )
        first = ui.waiting_block(_waiting_title(job), 0)
        markup = kb.cancel_only(parked.token)
        status = None
        if index == 0 and panel is not None:
            try:
                await panel.edit_text(first, reply_markup=markup)
                status = panel
            except Exception:
                log.debug("could not reuse the confirm card", exc_info=True)
        job.payload["status"] = status or await message.reply_text(first,
                                                                  reply_markup=markup)
        pending.append(job)

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
        # One link and nothing rejected needs no summary: the confirm card the user
        # just pressed already showed the cost and the balance, and its own panel is
        # right there animating. The receipt was the second message that made a
        # one-link job look like paperwork.
        if len(accepted) > 1 or rejected:
            await message.reply_text(
                f"▶️ <b>{len(accepted)} link(s) started</b>\n"
                f"💰 {spent:g} credits held · balance "
                f"<b>{credits.balance(user_id):g}</b>\n\n"
                "<i>Each video arrives on its own as it finishes. Anything that fails "
                "is refunded automatically.</i>" + note)

        # Fire-and-forget, but held in a set: a bare create_task can be collected
        # mid-flight, and this one has to outlive the handler that made it.
        animator = asyncio.create_task(_animate_waiting(accepted), name="tb-waiting")
        _animators.add(animator)
        animator.add_done_callback(_animators.discard)


WRONG_LINK = (
    "🚫 <b>Wrong link — no video found</b>\n\n"
    "This bot only takes <b>Terabox</b> share links. They look like\n"
    "<code>https://terabox.com/s/1abc…</code>\n\n"
    "<i>Paste one straight into the chat — one per line for several at once.</i>"
)


def _busy_note(count: int) -> str:
    return (
        f"⏳ <b>You already have {count} link(s) running</b>\n\n"
        "One job at a time per person — that way yours gets the full speed instead "
        "of racing itself. Send this again the moment the last video lands.\n\n"
        "<i>Nothing was charged for this message.</i>"
    )


async def _offer_batch(client: Client, message: Message, user_id: int,
                       jobs: jobq.Queue, links: list[str]) -> None:
    """
    The confirm card for a pasted batch, shared by both doors into this flow.

    **The prompt becomes the card.** If the user got here by opening the Terabox
    prompt, its message id was recorded in the mode payload, and every answer below
    is edited into it rather than posted under it. The user's own message is deleted
    as soon as the links are accepted. Together those two mean the chat holds exactly
    one bot message for the whole flow — prompt, then card, then progress, then the
    video — instead of the four-message trail it used to leave. A failed edit or
    delete is ignored and falls back to a reply: Telegram only allows deleting for
    48 hours, and this is cosmetic either way.
    """
    entry = state.get_mode(user_id)
    panel = chat = None
    if entry and entry[0] == MODE:
        panel, chat = entry[1].get("panel"), entry[1].get("chat")
    state.clear_mode(user_id)

    async def say(text: str, markup=None) -> Message:
        """Into the prompt if there is one, otherwise a new message."""
        if panel:
            try:
                return await client.edit_message_text(
                    chat, panel, text, reply_markup=markup,
                    disable_web_page_preview=True)
            except Exception:
                log.debug("could not edit the prompt panel", exc_info=True)
        return await message.reply_text(text, reply_markup=markup,
                                        disable_web_page_preview=True)

    blocked = terabox.unavailable()
    if blocked:
        # The mode may have been opened before the cookie was cleared, and this is
        # the last point before a credit moves.
        await say(blocked, kb.back_to_menu("◀  Menu"))
        return

    running = jobs.busy(user_id)
    if running:
        await say(_busy_note(running), kb.back_to_menu("◀  Menu"))
        return

    trimmed = links[:cfg.max_links_per_batch]
    # **The floor, not the bill.** The price is per video and a folder link can hold
    # ten of them, but how many is not knowable until the link has been read — so the
    # confirm holds one video per link and `_deliver` takes the rest through
    # `jobq.charge_more` before it fetches anything. Quoting ten videos' worth up
    # front would freeze credits for videos that in most cases are not there.
    #
    # Read once, into a local, and used for the sum, the trim and every figure on the
    # screen. Calling `settings.get` four separate times would leave a window in which
    # an admin's edit made the quoted total disagree with the per-video figure printed
    # under it, on the same card.
    per_video = settings.get("cost_terabox_per_link")
    cost = len(trimmed) * per_video
    have = credits.balance(user_id)

    extra = ""
    if len(links) > len(trimmed):
        extra = (f"\n\n<i>You sent {len(links)}; the first "
                 f"{cfg.max_links_per_batch} are taken — send the rest after.</i>")

    if have <= 0:
        await say(ui.insufficient(cost, have), kb.not_enough_credit(cost - have))
        return

    parked = state.park(user_id, "terabox_batch", links=trimmed)
    affordable = min(len(trimmed), int(have // per_video))
    short = ""
    if affordable < len(trimmed):
        short = (f"\n\n⚠️ Your balance covers <b>{affordable}</b> of them. "
                 "The rest will not be started, and nothing is held for them.")

    await say(
        f"📦 <b>{len(trimmed)} link(s) ready</b>\n\n"
        f"💰 <b>{per_video:g} credit(s) per video</b> · "
        f"you have <b>{have:g}</b>\n"
        f"🎚 Quality: <b>highest available</b>\n\n"
        f"<i>A single link is one video, so this starts at {cost:g}. A folder link is "
        f"{per_video:g} for each video inside it — counted once I have "
        f"read the folder, never before.</i>{short}{extra}",
        kb.confirm_batch(parked.token, len(trimmed), cost))

    await _gate.forget(message, "the pasted links")


async def _home_key(client: Client, message: Message, text: str) -> bool:
    """
    Answer one of the persistent keyboard's keys. `True` if this was one.

    **Every label in `kb.HOME_LABELS` is answered here, and this is called from both
    text handlers.** A reply-keyboard press arrives as an ordinary text message, so
    without this it fell wherever the user happened to be standing: pressing 🗂 File
    while the bot was waiting for a link got "Wrong link — no video found" from
    `got_links`, and pressing it outside a flow got the inline menu from `loose_text`
    because only Terabox had a branch there. Both are the same bug — a key that
    answers something other than itself — and both are fixed by having one place that
    owns the labels and two callers that defer to it.

    The press itself is deleted. `📦 Terabox` in the chat history is not information;
    it is the word the user pressed, and leaving it there is what turns a session of
    six links into a scroll of alternating labels and panels.
    """
    user_id = message.from_user.id

    if text == kb.KEY_TERABOX:
        blocked = terabox.unavailable()
        sent = await message.reply_text(blocked or prompt(),
                                       reply_markup=kb.back_to_menu("◀  Menu"),
                                       disable_web_page_preview=True)
        if blocked:
            state.clear_mode(user_id)
        else:
            # The prompt's id travels in the mode payload so that the confirm card
            # can replace this very message instead of landing under it. Ids, not the
            # `Message` itself: a mode entry lives for half an hour, and a pyrogram
            # object holds a client and a chat behind it for all of that time.
            state.set_mode(user_id, MODE, panel=sent.id, chat=sent.chat.id)

    elif text == kb.KEY_FAP:
        # The 🔥 flow owns its own prompt and mode, so the key is handed to it rather
        # than answered here. Imported locally for the same reason as `zipfiles`
        # below: two handlers that only need one string from each other should not
        # import each other at module level.
        from . import fap

        await fap.open_from_key(client, message)

    elif text == kb.KEY_FILE:
        # One string from a sibling handler, imported here rather than at the top:
        # the two modules have no other reason to know about each other, and a
        # module-level import between handlers is the start of a cycle.
        from . import zipfiles

        state.clear_mode(user_id)
        await message.reply_text(zipfiles.prompt(),
                                 reply_markup=kb.back_to_menu("◀  Menu"))

    elif text == kb.KEY_MENU:
        state.clear_mode(user_id)
        await message.reply_text(
            "☰ <b>Menu</b>", reply_markup=kb.main_menu(cfg.is_admin(user_id)))

    else:
        return False

    await _gate.forget(message, "the key press")
    return True


def register(app: Client, jobs: jobq.Queue) -> None:

    @app.on_callback_query(filters.regex(r"^mode:terabox$"))
    async def open_terabox(client: Client, cq: CallbackQuery) -> None:
        blocked = terabox.unavailable()
        if blocked:
            # Say so before the prompt, not after ten links have been pasted.
            await cq.answer()
            await cq.message.edit_text(blocked, reply_markup=kb.back_to_menu("◀  Menu"))
            return
        # This message is already on screen and about to say "send me your links",
        # so it is the panel the confirm card will be edited into. See `_offer_batch`.
        state.set_mode(cq.from_user.id, MODE,
                       panel=cq.message.id, chat=cq.message.chat.id)
        await cq.answer()
        await cq.message.edit_text(prompt(), reply_markup=kb.back_to_menu("◀  Menu"),
                                   disable_web_page_preview=True)

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode(MODE))
    async def got_links(client: Client, message: Message) -> None:
        """Only reached while the user is in the Terabox flow (see _gate)."""
        text = (message.text or "").strip()
        if text in kb.HOME_LABELS and await _home_key(client, message, text):
            return
        links = terabox.extract_links(text)
        if not links:
            await message.reply_text(WRONG_LINK,
                                     reply_markup=kb.back_to_menu("◀  Menu"),
                                     disable_web_page_preview=True)
            return
        await _offer_batch(client, message, message.from_user.id, jobs, links)


    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"]))
    async def loose_text(client: Client, message: Message) -> None:
        """
        Anything typed outside a flow — which is where most links actually arrive.

        Registered last of all the text handlers, so every mode-owning handler has
        already had its chance (see `_gate`); reaching here means nobody owns this
        message. Before this existed a pasted link with no mode set matched no
        handler at all and the bot answered with **silence**, which is why users
        were pressing /start before every single link.
        """
        text = (message.text or "").strip()

        if text.startswith("/"):
            # Only /start and /balance exist. Anything else is a typo, and telling
            # someone their link is wrong when they typed /help is worse than
            # saying nothing.
            await message.reply_text(
                "🤖 <b>I only know /start and /balance</b>\n\n"
                "<i>Everything else is a button, or a link pasted straight in.</i>",
                reply_markup=kb.home_keys())
            return

        if text in kb.HOME_LABELS and await _home_key(client, message, text):
            return

        # A pasted Fap link is answered here too. This is the last text handler, so it
        # is the only one a user standing in no flow reaches at all — without this, a
        # link the bot can certainly fetch would be told "no video found".
        from . import fap

        if await fap.offer_pasted(client, message, jobs):
            return

        links = terabox.extract_links(text)
        if not links:
            await message.reply_text(WRONG_LINK, reply_markup=kb.home_keys(),
                                     disable_web_page_preview=True)
            return
        await _offer_batch(client, message, message.from_user.id, jobs, links)

    @app.on_callback_query(filters.regex(r"^job:go:(?P<token>[\w-]+)$"))
    async def start_batch(client: Client, cq: CallbackQuery) -> None:
        parked = state.take(cq.data.split(":")[2], cq.from_user.id)
        if parked is None or parked.kind != "terabox_batch":
            await cq.answer("That has expired — send the links again.", show_alert=True)
            return
        # Checked again here, not only at the door: two batches can be pasted before
        # either is confirmed, and it is the confirm that spends the credit.
        running = jobs.busy(cq.from_user.id)
        if running:
            await cq.answer("You already have work running.", show_alert=True)
            await cq.message.edit_text(_busy_note(running))
            return
        await cq.answer("Starting…")
        # No "Starting…" edit in between: `_queue_batch` turns this same card into
        # the first link's live panel, and a frame of placeholder text before it is
        # the flicker this flow exists to remove.
        await _queue_batch(client, cq.message, cq.from_user.id, jobs,
                           parked.payload["links"], panel=cq.message)

    @app.on_callback_query(filters.regex(r"^job:cancel:(?P<token>[\w-]+)$"))
    async def cancel_job(client: Client, cq: CallbackQuery) -> None:
        """
        Cancel, for every flow that shows a Cancel button.

        **`fap_pick` is handled here on purpose, not in `fap.py`.** Pyrogram runs only
        the first handler that matches a callback, so a second `job:cancel:` handler
        registered anywhere else would simply never fire — the choice is one handler
        that knows both kinds, or a Cancel button that silently does nothing.

        The two kinds behave the same way for the same reason: a parked entry means the
        user never confirmed, so nothing has been charged and the card can say so. Any
        other token belongs to a job that is already running, where the refund is the
        queue's to make.
        """
        token = cq.data.split(":")[2]
        state.cancel(token)
        parked = state.peek(token, cq.from_user.id)
        if parked is not None and parked.kind in ("terabox_batch", "fap_pick"):
            state.take(token, cq.from_user.id)
            await cq.answer("Cancelled — nothing was charged.")
            await cq.message.edit_text("✖ <b>Cancelled.</b> Nothing was charged.",
                                       reply_markup=kb.back_to_menu())
            return
        await cq.answer("Stopping — your credits come back automatically.")
