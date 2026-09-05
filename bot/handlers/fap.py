"""
Fap — one link in, a quality menu, one video back.

    🔥  ──►  "send the link"  ──►  the link is deleted  ──►  reading it…
                                                                  │
                                       480p · 1 cr    720p · 1.5 cr    1080p · 2 cr
                                                                  │
                                          assemble ──► upload ──► the file is gone

Three deliberate differences from the Terabox flow next door.

**The link is resolved at the door, not on a worker.** Terabox resolves inside the job
because ten links would be ten round trips before the user saw anything at all. Here
there is exactly one link and the resolve *is* the menu — there are no buttons to draw
until the service has answered — so it happens immediately, and it happens for free. A
private or deleted video costs nothing, because nothing has been charged by the time we
find out.

**Only the qualities the video actually has are offered.** The operator's rule, in their words:
*"if video 1080 m nhi hai available toh opt hi do dega"*. A rung the video does not
carry gets no button at all, rather than a button that quietly serves something
shorter. The card names which ones are missing, so an absent 1080p reads as the video's
limit and not as the bot holding it back.

**One message, start to finish.** The prompt is edited into the menu, the menu into the
live panel, and the panel is deleted once the video lands; the user's own message goes
as soon as the link is accepted. Every one of those steps is best effort — an edit
Telegram refuses is a cosmetic loss, and none of them may touch a job that has been
paid for.

Nothing survives the job. The chosen rendition is an HLS playlist rather than a file,
so ffmpeg has to assemble it before Telegram will take it; that file lives in a
`scratch.claim`ed directory for as long as the upload needs and is deleted in a
`finally`, with the janitor as the backstop for a process that dies mid-upload.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, Message

from .. import (credits, expiry, keyboards as kb, media, providers, queue as jobq,
                scratch, state, ui, uploader)
from ..config import cfg
from ..providers.faphouse import faphouse, menu, price_of, re_pick, rungs
from . import _gate

log = logging.getLogger(__name__)

MODE = "await_fap_link"

#: The quality buttons' callback shape, `q:<token>:<label>`.
#:
#: A module constant rather than a literal in the decorator so that a test can prove
#: every button `kb.quality_choice` renders is actually routable. A label carrying a
#: character this pattern rejects would not fail loudly — it would render a button that
#: does nothing at all, which is why `faphouse._safe_label` exists.
PICK_PATTERN = r"^q:(?P<token>[\w-]+):(?P<label>[\w.-]+)$"

def prompt() -> str:
    """
    The "send me the link" card.

    Built on each call rather than frozen at import time, so the ladder it quotes is
    the ladder `menu()` will charge — the prices are settings, and a card that
    disagrees with the buttons under it is worse than no card.
    """
    ladder = "\n".join(
        f"  •  <b>{label}</b> — {cost:g} credit" + ("" if cost == 1 else "s")
        for _height, label, cost in rungs())
    return (
        "🔥 <b>Send me the video link</b>\n"
        "──────────────────\n\n"
        "<b>One link</b> — paste it straight in.\n\n"
        "I read it first and then show you which qualities that video has:\n"
        f"{ladder}\n\n"
        "<i>Only the ones it actually has are offered, and nothing is charged until "
        "you tap one.</i>"
    )


WRONG_LINK = (
    "🚫 <b>That is not a link</b>\n\n"
    "This key takes <b>one video link</b> — the address of the video page itself, "
    "starting with <code>https://</code>\n\n"
    "<i>Open the video, copy the address from the bar, and paste it here. Nothing "
    "was charged.</i>"
)

#: How often the "waiting for a free worker" panel redraws itself. One message, so
#: unlike the Terabox animator there is no batch to spread across ticks.
WAIT_TICK_SECONDS = 4.0

#: When the animator gives up redrawing. The job is untouched — this only stops the
#: decoration, for a queue so long that something else is wrong.
WAIT_GIVE_UP_SECONDS = 30 * 60

#: What the wait panel says while it waits. `ui.WAIT_NOTES` talks about Terabox, and
#: this route never touches it; the rotation is kept because a line that changes is
#: what tells the user the bot is alive rather than stuck.
WAIT_NOTES = ("getting in line…", "finding a free worker…",
              "lining up the video…", "almost there…")

#: Live animator tasks, held so the garbage collector cannot take one mid-flight.
_animators: set[asyncio.Task] = set()

def _panel_title(job: jobq.Job) -> str:
    """
    The one-line head of every live panel for this job.

    **Plain text, not HTML** — `ui.waiting_block` and `ui.assembling_block` escape
    whatever they are given, so a tag put in here would be shown to the user as
    `&lt;b&gt;`.

    **It does not name the video.** It used to print `job.title`, the name the service
    reported, and that put the title of the video in a text message that sits in the
    chat for the whole job — the easiest thing in this flow for an automated reader to
    pick up, and the one the operator asked to close. `ui.panel_title` gives the neutral
    label instead; the quality stays, because the user chose it a moment ago and it is
    the only way to tell a 720p run from the 1080p one they meant to press.
    """
    label = ui.panel_title()
    return f"🔥 {label} · {job.quality}" if job.quality else f"🔥 {label}"


def _work_dir(job: jobq.Job) -> Path:
    """
    This job's own scratch directory. `fap-` is in `scratch.PREFIXES`, which is the
    only reason the janitor can clean up after a process that is killed mid-upload.
    """
    return cfg.work_dir / f"fap-{job.user_id}-{job.row_id or int(time.time())}"


def _busy(count: int) -> str:
    """
    One wording for both services, imported where it is used.

    A user with a Terabox batch running should be told the same thing here as there,
    because it is the same limit stopping them: Fap and Terabox share the link lane
    (`queue.lane_of` sends everything that is not an archive down it), so one link
    of either kind is one link. An archive is a different lane and does not count —
    the sentence saying both of those things already exists next door.
    """
    from .terabox import _busy_note

    return _busy_note(count)


def _menu_card(resolved: providers.Resolved, options: list[tuple[providers.Stream, float]],
               have: float, sent: int = 1) -> str:
    """
    The card the quality buttons hang under.

    It names what is *missing* rather than staying quiet about it. Someone who came
    for 1080p and is shown two buttons will otherwise assume the bot is holding the
    third back; one line saying the video does not have it turns a suspicion into a
    fact about the video.

    What it does **not** name is the video. The title used to head this card, and a
    card is a text message that stays in the chat until the job ends — see
    `_panel_title`. The user pasted the link one message ago, so the name is telling
    them what they already know while telling Telegram something it had no other way
    to learn.
    """
    offered = {stream.label.lower() for stream, _cost in options}
    missing = [label for _h, label, _c in rungs() if label.lower() not in offered]

    lines = ["🔥 <b>Your video</b>", "──────────────────", ""]
    if resolved.duration_seconds:
        lines.append(f"⏱ {ui.human_time(resolved.duration_seconds)} long")
    lines += [
        "🎚 <b>Pick a quality</b> — tap one and it starts.",
        f"💰 You have <b>{have:g} credit(s)</b>.",
    ]
    if missing:
        lines += ["", f"<i>This video does not have {', '.join(missing)}, so there is "
                      "no button for it — what is below is everything it has.</i>"]
    if sent > 1:
        lines += ["", f"<i>You sent {sent} links; this is the first one. Send the "
                      "next after this finishes.</i>"]
    lines += ["", "<i>Nothing is charged until you tap a button.</i>"]
    return "\n".join(lines)

async def _animate_waiting(job: jobq.Job) -> None:
    """
    Keep the panel moving until a worker picks this job up.

    **It stops itself, and needs no cancelling.** `job.started_at` is set by the worker
    before it runs anything, so this loop sees the handover on its next tick and
    returns — which also covers a cancel, a shutdown and a job that was refused, with
    no second party having to remember to stop the task. `_deliver` overwrites the
    panel a moment later either way.

    A `FloodWait` here is a decoration being rate-limited, never a reason to disturb
    the job: it is waited out and the next tick tries again.
    """
    status: Message | None = job.payload.get("status")
    if status is None:                                  # pragma: no cover - defensive
        return
    began = time.monotonic()
    tick = 0
    try:
        while time.monotonic() - began < WAIT_GIVE_UP_SECONDS:
            await asyncio.sleep(WAIT_TICK_SECONDS)
            if job.started_at or job.cancelled:
                return
            tick += 1
            try:
                await status.edit_text(
                    ui.waiting_block(_panel_title(job), tick,
                                     note=WAIT_NOTES[(tick // 3) % len(WAIT_NOTES)],
                                     seconds=job.waited),
                    reply_markup=kb.cancel_only(job.token))
            except Exception as exc:
                pause = getattr(exc, "value", None)      # FloodWait carries seconds
                if isinstance(pause, int) and pause > 0:
                    await asyncio.sleep(min(pause + 1, 60))
                else:
                    log.debug("fap: wait panel edit refused", exc_info=True)
    except asyncio.CancelledError:
        pass

async def _fresh_stream(job: jobq.Job, hint: providers.Stream) -> providers.Stream:
    """
    Look the video up again and return the rendition this job paid for.

    **The URL in the payload is a hint, not the thing fetched.** These CDN URLs are
    signed and time-limited — the token on the operator's own sample ends `,1788552000`, a
    Unix timestamp — so the address resolved when the link was pasted has a shelf life
    measured in hours, and there is no guarantee the job runs inside it: work ahead of
    it in the queue, or a user who reads the menu and wanders off before choosing a
    quality, is enough. Handed a lapsed URL, ffmpeg fails on a video the bot could
    perfectly well have fetched, and the refund message reads like a bug on this side.

    A second resolve that *fails* falls back to the hint rather than giving up. The
    resolver being down for the thirty seconds a job waited is not a reason to refuse a
    paid job whose parked URL may well still be inside its window — and if it is not,
    ffmpeg raises and `Queue._run_one` refunds anyway. What is not allowed is delivering
    a different rung than the one that was paid for, and `re_pick` is what holds that
    line: never dearer than `job.cost`, or nothing at all.
    """
    label = job.quality or hint.label
    try:
        resolved = await faphouse.resolve(job.source)
    except Exception:
        # Including ResolveError. The user id, never the link — `bot.log` is not a list
        # of what people watch.
        log.warning("fap: re-resolve failed for user %s, using the parked url",
                    job.user_id, exc_info=True)
        return hint

    stream = re_pick(resolved, label, hint.height, job.cost)
    if stream is None:
        # Every rung on offer now costs more than was charged. Refunding is the only
        # honest answer: the cheap copy the user paid for is not there to deliver.
        raise providers.ResolveError(
            "That video no longer has the quality you picked, and the ones it does "
            "have cost more than you were charged. Send the link again to see what "
            "it has now.")
    if stream.url != hint.url:
        log.info("fap: job %s re-resolved %s (%s)", job.row_id, label, stream.label)
    return stream


async def _deliver(client: Client, job: jobq.Job) -> None:
    """
    The queue runner: assemble the chosen rendition, hand it to Telegram, delete it.

    **The stream is looked up again here, immediately before ffmpeg opens it.** The one
    parked at the door priced the job and is still what the user is owed, but its URL is
    signed and expires — see `_fresh_stream`, which re-resolves and re-picks the same
    rung under a hard rule that the second answer can never cost more than the first.

    Raising is how this reports failure: `Queue._run_one` catches it, refunds the
    credit, marks the row and tells the user. The one piece of money code here is the
    other side of that coin — a rung that vanished between the tap and the fetch is
    delivered one rung down, and `jobq.refund_difference` gives back the gap.
    """
    status: Message = job.payload["status"]
    hint: providers.Stream = job.payload["stream"]
    work = scratch.claim(_work_dir(job))
    head = f"<b>{ui.esc(_panel_title(job))}</b>"

    try:
        try:
            await status.edit_text(f"{head}\n\n🔍 <i>lining up the video…</i>",
                                   reply_markup=kb.cancel_only(job.token))
        except Exception:
            log.debug("fap: could not open the live panel", exc_info=True)

        stream = await _fresh_stream(job, hint)
        # Never up, sometimes down — `re_pick` holds the ceiling, this settles the price
        # of what actually came back. Paying the 1080p rate for the 720p copy is the same
        # bug as paying for a video that never arrived, only smaller.
        jobq.refund_difference(job, price_of(stream.height))
        # And the job now says what it is delivering rather than what was tapped, so the
        # file name in the admin's own log is not a claim about a rung that never went out.
        # The `quality` column keeps the tapped label; between the two, the row says what
        # was asked for, what arrived, and what was finally charged for it.
        job.quality = stream.label
        if job.cancelled:
            raise asyncio.CancelledError

        # A total is what makes the bar a bar rather than a rising number, and for an
        # HLS variant the only place its length is written down is the playlist
        # itself. Best effort in the strongest sense — `playlist_seconds` answers 0.0
        # rather than raising, because a missing total costs the user nothing.
        total = float(job.payload.get("seconds") or 0.0)
        if not total:
            total = await faphouse.playlist_seconds(stream)
        if job.cancelled:
            raise asyncio.CancelledError

        began = time.monotonic()
        throttle = ui.Throttle(5.0)

        async def on_progress(done: float, whole: float) -> None:
            """
            ffmpeg reports **seconds of video**, not bytes.

            Which is why this renders through `ui.assembling_block` and not
            `ui.progress_block`: every line of that one is a byte count, and seconds
            fed through it read as "45 B / 300 B" at "12 B/s".
            """
            if not throttle.ready(force=bool(whole) and done >= whole):
                return
            try:
                await status.edit_text(
                    ui.assembling_block(_panel_title(job), done, whole, began,
                                        expires_minutes=cfg.auto_delete_minutes),
                    reply_markup=kb.cancel_only(job.token))
            except Exception:
                log.debug("fap: progress edit refused", exc_info=True)

        target = work / f"{job.row_id or 'video'}.mp4"
        await media.fetch_to_mp4(stream.url, target,
                                 total_seconds=total,
                                 headers=stream.headers or None,
                                 on_progress=on_progress,
                                 cancelled=lambda: job.cancelled)
        # The real name, for the job row and the admin's own log on his own box —
        # never for anything on screen.
        name = f"{job.title or 'video'} [{job.quality}]" if job.quality else (job.title or "video")
        try:
            # No caption. *"only video"* — the clip arrives with nothing written under
            # it on any route. `title` is only the progress panel's own wording, and it
            # gets the neutral label rather than `name`: that panel is a text message
            # living in the chat for the length of the upload, which is precisely where
            # the video's title must not appear. The panel is deleted below anyway.
            result = await uploader.send_best_effort(
                client, job.chat_id, target,
                status=status, title=_panel_title(job),
                cancelled=lambda: job.cancelled,
            )
            job.file_name = name
            job.size_bytes = result.size_bytes
            # On the clock from the moment it lands. The panel above has been saying
            # so for the whole download, which is the only warning the user gets —
            # it is deleted a few lines below.
            expiry.remember(result, job.chat_id, job.row_id)
        except uploader.TooLarge as exc:
            # Only reachable above Telegram's own ceiling, since `send_best_effort`
            # splits anything over the *configured* limit into parts. Its message
            # already suggests a lower quality, which on this route is a real option.
            await client.send_message(job.chat_id, exc.user_message())
            raise providers.ResolveError(
                "Telegram would not take that file — it is over its own size limit. "
                "Try a lower quality.") from exc
        finally:
            # The file goes the moment it is sent, not when the job ends: the panel
            # delete below can take a second and there is no reason for gigabytes to
            # sit on the box for it.
            target.unlink(missing_ok=True)

        # The panel has done its job. Deleting it leaves the chat holding the video
        # and nothing else, which is the whole point of the single-panel flow; if
        # Telegram refuses the delete, it becomes a one-line receipt instead — and
        # that receipt names the quality, not the video.
        try:
            await status.delete()
        except Exception:
            try:
                await status.edit_text(
                    "✅ <b>Sent</b>\n"
                    + (f"🎚 {ui.esc(job.quality)}\n" if job.quality else "")
                    + f"📦 {ui.human_bytes(job.size_bytes)}",
                    reply_markup=kb.back_to_menu())
            except Exception:
                log.debug("fap: could not clear the panel", exc_info=True)
    finally:
        scratch.release(work)

async def _offer(client: Client, message: Message, user_id: int, jobs: jobq.Queue,
                 url: str, sent: int = 1) -> None:
    """
    Read one link and put the quality menu on screen. **Charges nothing.**

    Shared by both doors — the 🔥 key and a link pasted with no flow open. As in the
    Terabox flow, the prompt's message id travels in the mode payload, so every answer
    below is edited *into* that message instead of landing under it; with no prompt to
    edit (the pasted case) `say` posts a new one.
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
                log.debug("fap: could not edit the prompt panel", exc_info=True)
        return await message.reply_text(text, reply_markup=markup,
                                        disable_web_page_preview=True)

    # "link manga, link gayab" — the pasted link goes now, not at the end. From here
    # on the flow owns exactly one message on screen.
    await _gate.forget(message, "fap: the pasted link")

    blocked = faphouse.unavailable()
    if blocked:
        await say(blocked, kb.back_to_menu("◀  Menu"))
        return

    running = jobs.busy(user_id, jobq.LINK_LANE)
    if running:
        await say(_busy(running), kb.back_to_menu("◀  Menu"))
        return

    # The cheapest rung, not a fixed 480p: these are settings and nothing stops an
    # operator pricing them in another order. Someone who cannot afford even the
    # cheapest button is told before the service is troubled for a link.
    cheapest = min(cost for _h, _l, cost in rungs())
    have = credits.balance(user_id)
    if have < cheapest:
        await say(ui.insufficient(cheapest, have),
                  kb.not_enough_credit(cheapest - have))
        return
    await say("🔍 <b>Reading that link…</b>\n\n"
              "<i>Finding out which qualities the video has. Nothing is charged for "
              "this.</i>")
    try:
        resolved = await faphouse.resolve(url)
    except providers.ResolveError as exc:
        await say(f"🚫 <b>Could not read that link</b>\n\n{ui.esc(exc)}\n\n"
                  "<i>Nothing was charged.</i>", kb.back_to_menu("◀  Menu"))
        return
    except Exception:
        # Anything the provider did not turn into a sentence is a bug on this side,
        # and the user is owed a reply either way. **The user id goes in the log, not
        # the link.** The traceback is what makes this debuggable; the URL only made
        # `bot.log` a list of what people watch, and the admin's own Shared Links card
        # already holds the link for the one person who is meant to see it.
        log.warning("fap: resolve failed for user %s", user_id, exc_info=True)
        await say("🚫 <b>That link could not be read right now</b>\n\n"
                  "The video service did not answer properly. Try again in a minute.\n\n"
                  "<i>Nothing was charged.</i>", kb.back_to_menu("◀  Menu"))
        return

    options = menu(resolved)
    # The `Stream` objects themselves are parked, not just their labels: the pick has
    # to submit the exact URL the price was quoted for, and a second resolve at that
    # point could answer differently.
    parked = state.park(user_id, "fap_pick", url=url, resolved=resolved,
                        priced={stream.label: cost for stream, cost in options})
    await say(_menu_card(resolved, options, have, sent),
              kb.quality_choice(parked.token,
                                [(stream.label, cost) for stream, cost in options]))


async def offer_pasted(client: Client, message: Message, jobs: jobq.Queue) -> bool:
    """
    A Fap link pasted with no flow open. `True` if it was one and has been answered.

    Called from `terabox.loose_text`, which is registered last and is therefore the
    only text handler a user standing in no flow reaches at all. Without this, a link
    this bot can certainly fetch would be answered "wrong link — no video found",
    because that handler only ever looked for Terabox ones.
    """
    links = faphouse.extract_links(message.text or "")
    if not links:
        return False
    await _offer(client, message, message.from_user.id, jobs, links[0], sent=len(links))
    return True


async def open_from_key(client: Client, message: Message) -> None:
    """
    The 🔥 key was pressed. Called from `terabox._home_key`, which owns the labels.

    The prompt's ids go into the mode payload — ids and not the `Message`, because a
    mode entry lives for half an hour and a pyrogram object holds a client behind it
    for all of that time.
    """
    user_id = message.from_user.id
    blocked = faphouse.unavailable()
    sent = await message.reply_text(blocked or prompt(),
                                    reply_markup=kb.back_to_menu("◀  Menu"),
                                    disable_web_page_preview=True)
    if blocked:
        state.clear_mode(user_id)
    else:
        state.set_mode(user_id, MODE, panel=sent.id, chat=sent.chat.id)

def register(app: Client, jobs: jobq.Queue) -> None:
    """
    Wire the flow up.

    **Register this before `terabox.register`.** That module ends with `loose_text`, a
    catch-all text handler, and pyrogram runs the *first* matching handler in a group:
    registered after it, the gated handler below would never see a message and this
    flow would take no links at all.
    """

    @app.on_callback_query(filters.regex(r"^mode:(fap|soon)$"))
    async def open_fap(client: Client, cq: CallbackQuery) -> None:
        """
        Both names on purpose. `kb.main_menu` has always emitted `mode:soon` for this
        button, and every menu card already sitting on someone's screen still points
        there — renaming the callback alone would turn those into dead buttons.
        """
        blocked = faphouse.unavailable()
        if blocked:
            await cq.answer()
            await cq.message.edit_text(blocked, reply_markup=kb.back_to_menu("◀  Menu"))
            return
        # This message is on screen and about to say "send me the link", so it is the
        # panel the menu will be edited into. See `_offer`.
        state.set_mode(cq.from_user.id, MODE,
                       panel=cq.message.id, chat=cq.message.chat.id)
        await cq.answer()
        await cq.message.edit_text(prompt(), reply_markup=kb.back_to_menu("◀  Menu"),
                                   disable_web_page_preview=True)

    @app.on_message(filters.private & filters.text
                    & ~filters.command(["start", "balance"])
                    & _gate.in_mode(MODE))
    async def got_link(client: Client, message: Message) -> None:
        """Only reached while the user is in the Fap flow (see `_gate`)."""
        text = (message.text or "").strip()
        if text in kb.HOME_LABELS:
            # A reply-keyboard press arrives as an ordinary text message, and one
            # place owns every label — including this flow's own key. Imported here
            # rather than at the top: a module-level import between two handlers is
            # the start of a cycle.
            from . import terabox

            if await terabox._home_key(client, message, text):
                return
        links = faphouse.extract_links(text)
        if not links:
            await message.reply_text(WRONG_LINK,
                                     reply_markup=kb.back_to_menu("◀  Menu"),
                                     disable_web_page_preview=True)
            return
        await _offer(client, message, message.from_user.id, jobs, links[0],
                     sent=len(links))
    @app.on_callback_query(filters.regex(PICK_PATTERN))
    async def pick_quality(client: Client, cq: CallbackQuery) -> None:
        """
        A quality button. **This is where the credit moves.**

        `state.take` rather than `peek`, so a double tap cannot charge twice: the
        second one finds nothing parked and is told the menu is spent. The busy check
        is repeated here for the same reason it is in the Terabox confirm — two menus
        can be on screen before either is tapped, and it is the tap that spends.
        """
        _prefix, token, label = cq.data.split(":", 2)
        parked = state.take(token, cq.from_user.id)
        if parked is None or parked.kind != "fap_pick":
            await cq.answer("That menu is no longer live — send the link again.",
                            show_alert=True)
            return

        resolved: providers.Resolved = parked.payload["resolved"]
        priced: dict[str, float] = parked.payload["priced"]
        stream = resolved.by_label(label)
        cost = priced.get(label)
        if stream is None or cost is None:
            # Not reachable from the keyboard — it is built from exactly these labels
            # — so this is a hand-made callback, answered and dropped.
            await cq.answer("That quality is not on offer for this video.",
                            show_alert=True)
            return

        running = jobs.busy(cq.from_user.id, jobq.LINK_LANE)
        if running:
            await cq.answer("You already have a link running.", show_alert=True)
            await cq.message.edit_text(_busy(running),
                                       reply_markup=kb.back_to_menu("◀  Menu"))
            return

        await cq.answer("Starting…")
        # A second parked entry, holding only the token: it is what `job:cancel:`
        # cancels and what `Queue` clears when the job ends. The menu's own entry has
        # already been taken above, which is what makes a double tap harmless.
        held = state.park(cq.from_user.id, "fap", url=parked.payload["url"])
        job = jobq.Job(
            user_id=cq.from_user.id,
            chat_id=cq.message.chat.id,
            kind="fap",
            runner=lambda j: _deliver(client, j),
            cost=cost,
            title=resolved.title,
            source=parked.payload["url"],
            quality=stream.label,
            token=held.token,
            payload={"stream": stream,
                     "seconds": float(resolved.duration_seconds or 0.0)},
        )
        job.payload["status"] = cq.message

        # The card becomes the live panel, and it is turned into one *before* the job
        # is submitted. The other order is a race: a free worker can start the job
        # inside `submit`, and this edit would then land on top of a panel `_deliver`
        # had already claimed, replacing real progress with a stale first frame.
        try:
            await cq.message.edit_text(
                ui.waiting_block(_panel_title(job), 0, note=WAIT_NOTES[0]),
                reply_markup=kb.cancel_only(held.token))
        except Exception:
            log.debug("fap: could not reuse the quality card", exc_info=True)

        try:
            jobs.submit(job)
        except credits.InsufficientCredits:
            state.clear_cancel(held.token)
            have = credits.balance(cq.from_user.id)
            await cq.message.edit_text(ui.insufficient(cost, have),
                                       reply_markup=kb.not_enough_credit(cost - have))
            return
        except jobq.Rejected as exc:
            state.clear_cancel(held.token)
            await cq.message.edit_text(exc.user_message,
                                       reply_markup=kb.back_to_menu("◀  Menu"))
            return

        # Held in a set so the loop cannot be collected mid-flight; it ends itself the
        # moment a worker sets `started_at`.
        animator = asyncio.create_task(_animate_waiting(job), name="fap-waiting")
        _animators.add(animator)
        animator.add_done_callback(_animators.discard)

        log.info("fap: queued job %s at %s for %s (%g cr)",
                 job.row_id, stream.label, cq.from_user.id, cost)
