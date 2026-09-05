"""
The job queue and its worker pool.

Ten links pasted at once are ten jobs, not ten downloads. Workers pull from a
queue, and each file is delivered the moment it is ready — so the user is watching
video 1 while video 7 is still downloading.

**There is one queue per lane, not one queue.** Terabox jobs and ZIP jobs are
limited by different things: a Terabox download is capped by Terabox's own
per-CDN-host shaping, while a ZIP job pulls from Telegram, unpacks locally and
uploads, never touching Terabox. Sharing a single pool meant four archives could
block three link jobs for no reason at all, so `Job.kind` picks the lane and the
two drain side by side.

Raising a lane's width does not make any one download faster — Terabox's shaping
decides that. It is so that six people all *start* immediately instead of watching
a queue, which is the part that feels slow. Contention here is graceful rather
than a wall: two streams on different CDN hosts kept full speed apiece.

The money rule lives here, and it is the whole reason this module owns credits:

* On accept, the cost is debited and the ledger row says "reserved". This is a
  real debit, not a soft hold — otherwise a user could queue ten links on two
  credits and the shortfall would only surface after the work was done.
* On failure, cancellation, or a file Telegram refuses, it is refunded in full.
* An archive is one price for many videos, so if some of them arrive and some do
  not, the missing share is refunded — `Job.expected` / `Job.delivered` and
  `_refund_part`. Nothing arriving is a failure and refunds everything.

A user is never charged for a video that did not arrive.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pyrogram import Client

from . import credits, db, scratch, state
from .config import cfg

log = logging.getLogger(__name__)

#: What a module hands the queue: given the job, fetch and deliver it.
JobRunner = Callable[["Job"], Awaitable[None]]

#: The two lanes. Anything that is not an archive drains down the link lane, so a
#: provider added later works without anyone remembering this file exists.
ZIP_LANE = "zip"
LINK_LANE = "terabox"


def lane_of(kind: str) -> str:
    """Which lane a job of this kind belongs in."""
    return ZIP_LANE if kind == ZIP_LANE else LINK_LANE


@dataclass
class Job:
    """One unit of work: one link, or one video found inside one archive."""

    user_id: int
    chat_id: int
    kind: str                       # "terabox" | "zip" | any provider name
    runner: JobRunner               # what actually fetches and sends
    cost: float = 0.0
    title: str = ""
    source: str = ""                # the link, for the jobs table
    quality: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    token: str = ""                 # cancel handle from state.park()
    index: int = 0                  # position in its batch, for "3/10" labels
    total_in_batch: int = 1

    # filled in as it runs
    row_id: int | None = None
    file_name: str = ""
    size_bytes: int = 0
    queued_at: float = field(default_factory=time.monotonic)
    started_at: float = 0.0

    #: For a job that promises several files — an archive — how many were promised
    #: and how many actually arrived. `expected <= 1` means the pro-rata rule does
    #: not apply and the job is all-or-nothing, which is every link job.
    expected: int = 0
    delivered: int = 0

    @property
    def cancelled(self) -> bool:
        return bool(self.token) and state.is_cancelled(self.token)

    @property
    def label(self) -> str:
        """
        A human-readable name for this job, **for the log and the admin only**.

        It carries `title` / `file_name`, which is the name of the video, so it must
        not reach anything the user's chat can see: a panel that shows it is a text
        message naming the content, which is the trail the operator asked to remove. The
        handlers build their screen text from `ui.panel_title` instead. Kept because
        an admin card and a log line are allowed to say which job is which, and both
        run on his own box.
        """
        name = self.title or self.file_name or self.source or self.kind
        if self.total_in_batch > 1:
            return f"[{self.index + 1}/{self.total_in_batch}] {name}"
        return name

    @property
    def waited(self) -> float:
        return max(0.0, (self.started_at or time.monotonic()) - self.queued_at)


class QueueFull(Exception):
    """Rejected at the door rather than accepted and silently starved."""


class Rejected(Exception):
    """The job cannot be accepted. `.user_message` is shown as-is."""

    def __init__(self, user_message: str):
        self.user_message = user_message
        super().__init__(user_message)


class NoSpace(RuntimeError):
    """
    The scratch disk never freed up, so the job is failed instead of hung.

    Raised from inside `_run_one`, on purpose: everything after it — mark the row,
    refund in full, tell the user — is the failure path that already exists, and
    a job that waits for ever is the one outcome the money rule cannot describe.
    """

# --- the jobs table ----------------------------------------------------------

def _insert_row(job: Job) -> int:
    cur = db.execute(
        """INSERT INTO jobs (user_id, kind, source, status, quality, cost, charged, created_at)
           VALUES (?, ?, ?, 'queued', ?, ?, 1, ?)""",
        (job.user_id, job.kind, job.source[:500], job.quality, job.cost, db.now()),
    )
    return int(cur.lastrowid)


def _mark(job: Job, status: str, error: str | None = None, charged: bool | None = None) -> None:
    if job.row_id is None:
        return
    fields = ["status = ?", "file_name = ?", "size_bytes = ?", "error = ?"]
    params: list[Any] = [status, job.file_name[:255] or None, job.size_bytes,
                         (error or "")[:500] or None]
    if charged is not None:
        fields.append("charged = ?")
        params.append(1 if charged else 0)
    if status in ("done", "failed", "cancelled"):
        fields.append("finished_at = ?")
        params.append(db.now())
    params.append(job.row_id)
    db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", params)


def _refund(job: Job, why: str) -> None:
    """Give the credits back. Never let a refund failure mask the original error."""
    if job.cost <= 0:
        return
    try:
        credits.refund(job.user_id, job.cost, f"refund — {why}",
                       ref=f"job:{job.row_id}" if job.row_id else None)
        log.info("refunded %.2f to %s (%s)", job.cost, job.user_id, why)
    except Exception:
        log.exception("REFUND FAILED for user %s job %s — needs manual fixing",
                      job.user_id, job.row_id)


def _refund_part(job: Job) -> float:
    """
    Give back the share of a multi-file job that never arrived. Returns the amount.

    An archive is priced as one thing, so a 10-video ZIP where 7 arrive used to
    charge for all 10 — and the other obvious rule, refund everything, hands over 7
    videos for free. Pro-rata is the rule the operator picked: pay for what arrived.

    Nothing arriving is still a full refund and is *not* handled here: the runner
    raises in that case, so it goes down the failure path with the rest.
    """
    if job.cost <= 0 or job.expected <= 1:
        return 0.0
    missing = max(0, job.expected - job.delivered)
    if missing <= 0:
        return 0.0
    back = round(job.cost * missing / job.expected, 2)
    if back <= 0:                       # a rounding floor, not worth a ledger row
        return 0.0
    try:
        credits.refund(job.user_id, back,
                       f"refund — {missing} of {job.expected} did not arrive",
                       ref=f"job:{job.row_id}" if job.row_id else None)
    except Exception:
        log.exception("PARTIAL REFUND FAILED for user %s job %s — needs manual fixing",
                      job.user_id, job.row_id)
        return 0.0
    log.info("part-refunded %.2f of %.2f to %s (%d/%d delivered)",
             back, job.cost, job.user_id, job.delivered, job.expected)
    return back


def refund_difference(job: Job, worth: float) -> float:
    """
    Give back the gap when what arrived is cheaper than what was charged. Returns it.

    `_refund_part`'s rule — *pay for what arrived* — seen from the other end, for a job
    that delivers one thing rather than a short batch. A Fap job is priced at the tap and
    its stream is looked up again just before ffmpeg opens it, because the CDN's URLs are
    signed and expire; the resolver can drop the exact rung between those two moments, and
    the runner then delivers the best copy still within the price. Without this, a 1080p
    tap that comes back as 720p keeps the 1080p charge — the bot quietly billing for one
    thing and handing over another, which is the practice the operator's own menu rule exists to
    prevent.

    `job.cost` comes down with it, so the failure path stays whole: ffmpeg falling over
    after this refunds the reduced cost, and the two together are exactly what was taken.
    Cheaper is the only direction it moves. A `worth` above `job.cost` is a no-op, because
    raising a price after the button was tapped is not a thing this bot does — `re_pick`
    is what makes sure it cannot arise in the first place.
    """
    if job.cost <= 0 or worth <= 0:
        return 0.0
    back = round(job.cost - worth, 2)
    if back <= 0:
        return 0.0
    try:
        credits.refund(job.user_id, back, "refund — a cheaper copy than was paid for",
                       ref=f"job:{job.row_id}" if job.row_id else None)
    except Exception:
        log.exception("DOWNGRADE REFUND FAILED for user %s job %s — needs manual fixing",
                      job.user_id, job.row_id)
        return 0.0
    job.cost = round(job.cost - back, 2)
    if job.row_id:
        db.execute("UPDATE jobs SET cost = ? WHERE id = ?", (job.cost, job.row_id))
    log.info("refunded %.2f of a downgrade to %s (job %s now costs %.2f)",
             back, job.user_id, job.row_id, job.cost)
    return back


def charge_more(job: Job, per_item: float, items: int) -> int:
    """
    Charge for the extra items a job turned out to hold. Returns how many it covered.

    A folder link is priced per video, and how many videos it holds is not knowable
    until it has been read — so the confirm screen takes one video's worth and this
    settles the rest, *before* any of them is fetched. Charging afterwards would hand
    out free work to anyone who blocks the bot mid-job; holding ten videos' worth up
    front would freeze credits for videos that usually are not there.

    The return value is the number of items the balance actually covers, always at
    least the one already paid for. A user who can afford 4 of 7 gets 4 videos and a
    line saying so, which is better than a refusal that leaves them with nothing —
    and `job.cost` is updated to match, so `_refund_part` keeps working from a price
    that is true.
    """
    if items <= 1 or per_item <= 0:
        return max(1, items)
    want = items - 1                                  # item one is already paid for
    have = credits.balance(job.user_id)
    afford = min(want, int(have // per_item)) if per_item else want
    if afford <= 0:
        return 1
    amount = round(afford * per_item, 2)
    try:
        credits.charge(job.user_id, amount, f"{afford} more video(s) in one link",
                       ref=f"job:{job.row_id}" if job.row_id else None)
    except credits.InsufficientCredits:
        # Raced with another job of theirs finishing. One video is paid for; deliver
        # that and let the rest be told about rather than failing the whole link.
        log.info("job %s could not take the extra %s cr", job.row_id, amount)
        return 1
    job.cost = round(job.cost + amount, 2)
    if job.row_id:
        db.execute("UPDATE jobs SET cost = ? WHERE id = ?", (job.cost, job.row_id))
    log.info("job %s charged %.2f more for %d extra item(s)",
             job.row_id, amount, afford)
    return afford + 1


class Queue:
    """One asyncio queue *per lane* plus a fixed pool of workers, owned by `main`."""

    MAX_DEPTH = 200                 # per lane

    #: Refuse to start a job with less than this much scratch space free, in
    #: multiples of one upload's worth. A link job can hold the raw download and
    #: its remux target at once, and an archive holds the zip plus the video being
    #: extracted from it, so one job's true worst case is about two files.
    DISK_HEADROOM = 2.5
    DISK_WAIT_SECONDS = 600.0
    DISK_POLL_SECONDS = 5.0

    def __init__(self, client: Client, workers: int | None = None):
        self.client = client
        #: lane -> how many workers pull from it. An explicit `workers=` applies to
        #: every lane, which is what the tests want and what a one-worker debug run
        #: means.
        self.lanes: dict[str, int] = {
            LINK_LANE: max(1, workers or cfg.max_concurrent_jobs),
            ZIP_LANE: max(1, workers or cfg.max_concurrent_zip_jobs),
        }
        self._q: dict[str, asyncio.Queue[Job]] = {
            lane: asyncio.Queue() for lane in self.lanes
        }
        self._tasks: list[asyncio.Task] = []
        self._active: dict[tuple[str, int], Job] = {}
        #: user id -> how many of their jobs are queued or running. Kept as a count
        #: rather than read off the lane queues because an `asyncio.Queue` cannot be
        #: inspected without draining it, and this is asked on every message.
        self._inflight: dict[int, int] = {}
        self._closing = False
        self.done_count = 0
        self.failed_count = 0

    def busy(self, user_id: int) -> int:
        """
        How much work this user already has in flight.

        One person, one process at a time: a second batch while the first is still
        running would have them watching two progress bars race for the same
        throttled pipe, and neither would finish sooner. A whole batch counts as
        the work it is — ten links submitted together are ten, and the door checks
        this once before submitting any of them.
        """
        return self._inflight.get(user_id, 0)

    def _hold(self, job: Job) -> None:
        self._inflight[job.user_id] = self._inflight.get(job.user_id, 0) + 1

    def _release(self, job: Job) -> None:
        """Called exactly once per accepted job, whatever ended it."""
        left = self._inflight.get(job.user_id, 0) - 1
        if left > 0:
            self._inflight[job.user_id] = left
        else:
            self._inflight.pop(job.user_id, None)

    @property
    def workers(self) -> int:
        """Total width, for the admin card and the startup log."""
        return sum(self.lanes.values())

    # --- lifecycle ---

    async def start(self) -> None:
        if self._tasks:
            return
        self._closing = False
        self._tasks = [
            asyncio.create_task(self._worker(lane, i), name=f"worker-{lane}-{i}")
            for lane, count in self.lanes.items()
            for i in range(count)
        ]
        log.info("queue up with %s workers (%s)", self.workers,
                 ", ".join(f"{lane}={n}" for lane, n in self.lanes.items()))

    async def stop(self) -> None:
        """
        Shut the pool down without eating credits.

        Refund ownership is split so nothing is refunded twice: whatever is still
        *queued* is refunded here, and whatever is *running* is refunded by its own
        worker as the cancellation unwinds it. Every lane has to be drained — a
        lane missed here is a lane whose queued jobs keep the money.
        """
        self._closing = True

        for lane, queue in self._q.items():
            while not queue.empty():
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                _mark(job, "cancelled", "bot restarted", charged=False)
                _refund(job, "bot restarted before it started")
                self._release(job)
                queue.task_done()

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()
        self._inflight.clear()


    # --- accepting work ---

    def submit(self, job: Job) -> Job:
        """
        Debit, record, enqueue. Raises `Rejected` with a user-facing message
        instead of accepting something it cannot pay for or cannot fit.
        """
        lane = lane_of(job.kind)
        if self._q[lane].qsize() >= self.MAX_DEPTH:
            raise Rejected(
                "🚦 The queue is unusually long right now. Please try again in a few minutes."
            )

        job.row_id = _insert_row(job)
        if job.cost > 0:
            try:
                credits.charge(job.user_id, job.cost,
                               f"{job.kind} — reserved", ref=f"job:{job.row_id}")
            except credits.InsufficientCredits:
                _mark(job, "failed", "not enough credits", charged=False)
                raise
            except Exception as exc:
                # Anything else — an id with no row in `users`, a locked database —
                # must not leave this row saying "queued". It was never enqueued, so
                # no worker will finish it and `stop()` will never refund it: it
                # reads as work in flight for ever. One really did. The Terabox door
                # charged the bot's own id, and jobs row 1 on the live box claimed
                # to be a queued, charged job that had never existed.
                _mark(job, "failed", f"{type(exc).__name__}: {exc}", charged=False)
                raise
        self._q[lane].put_nowait(job)
        self._hold(job)
        return job

    def submit_many(self, jobs: list[Job]) -> tuple[list[Job], list[tuple[Job, Exception]]]:
        """
        Submit a batch, keeping whatever fits.

        Ten links with credit for six should send six, not refuse all ten. The
        caller reports the rejected tail.

        Only the two *expected* refusals are caught. Anything else is a bug in the
        caller and is left to escape: the id the Terabox door passed was wrong for
        every link, and a crash naming the id is what made that a five-minute fix
        rather than a bot that quietly rejected every batch.
        """
        accepted: list[Job] = []
        rejected: list[tuple[Job, Exception]] = []
        for job in jobs:
            try:
                accepted.append(self.submit(job))
            except (credits.InsufficientCredits, Rejected) as exc:
                rejected.append((job, exc))
        return accepted, rejected

    # --- introspection, for the admin panel and the queue message ---

    def depth(self, lane: str | None = None) -> int:
        """Jobs waiting — in one lane, or across all of them."""
        if lane is None:
            return sum(q.qsize() for q in self._q.values())
        return self._q[lane].qsize()

    def active(self, lane: str | None = None) -> list[Job]:
        if lane is None:
            return list(self._active.values())
        return [job for (key, _), job in self._active.items() if key == lane]

    def position_of(self, job: Job) -> int:
        """1-based place in line within the job's own lane, counting what runs."""
        lane = lane_of(job.kind)
        return len(self.active(lane)) + max(1, self._q[lane].qsize())

    def stats(self) -> dict[str, Any]:
        return {
            "workers": self.workers,
            "queued": self.depth(),
            "running": len(self._active),
            "done": self.done_count,
            "failed": self.failed_count,
            "lanes": {
                lane: {"workers": count,
                       "queued": self._q[lane].qsize(),
                       "running": len(self.active(lane))}
                for lane, count in self.lanes.items()
            },
        }

    # --- running work ---

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(cfg.work_dir).free
        except OSError:               # pragma: no cover - unreadable work dir
            log.warning("could not read free space on %s", cfg.work_dir)
            return 1 << 62            # unknown is not a reason to refuse work

    async def _wait_for_disk(self, job: Job) -> None:
        """
        Hold a job at the gate until there is room to run it.

        Six link jobs and four archives can each want a couple of gigabytes of
        scratch at the same moment, which is more than the box has. Waiting is much
        better than what happens without this — two jobs filling the disk and
        *both* failing, plus every unrelated write on the box. The wait is bounded
        because a job that never starts cannot be explained to the person who paid
        for it.

        This is a floor, not a reservation: several workers can pass the same check
        in the same instant. It exists to keep the disk off empty, which is the
        failure that takes everything down with it, not to guarantee any one job
        its space.
        """
        need = int(cfg.max_upload_mb * self.DISK_HEADROOM) * 1024 * 1024
        if self._free_bytes() >= need:
            return

        # Being short on space is exactly the moment a leftover from a killed job
        # matters, so look for one before making somebody who has paid wait on it.
        # A running job's directory is claimed and `scratch.sweep` will not touch
        # it, so this is safe to call from a worker; it runs in a thread because
        # walking and unlinking is blocking work and every other job is waiting on
        # this same loop.
        try:
            dirs, freed = await asyncio.to_thread(scratch.sweep)
        except Exception:                 # pragma: no cover - sweeping is optional
            dirs, freed = 0, 0
            log.warning("job %s: sweep failed", job.row_id, exc_info=True)
        if dirs:
            log.info("job %s: swept %d stale dir(s), %d MB back",
                     job.row_id, dirs, freed >> 20)
            if self._free_bytes() >= need:
                return

        waited = 0.0
        while waited < self.DISK_WAIT_SECONDS:
            if job.cancelled:
                return            # `_run_one` deals with cancellation properly
            log.info("job %s waiting for disk space (%d MB free, wants %d MB)",
                     job.row_id, self._free_bytes() >> 20, need >> 20)
            await asyncio.sleep(self.DISK_POLL_SECONDS)
            waited += self.DISK_POLL_SECONDS
            if self._free_bytes() >= need:
                log.info("job %s got its space after %.0fs", job.row_id, waited)
                return

        raise NoSpace(
            "The server ran out of scratch space for this one. Nothing was "
            "charged — please try again in a few minutes.")

    async def _worker(self, lane: str, index: int) -> None:
        queue = self._q[lane]
        while True:
            job = await queue.get()        # cancelling here just ends the worker
            self._active[(lane, index)] = job
            job.started_at = time.monotonic()
            try:
                await self._run_one(job)
            except Exception:
                log.exception("worker %s-%d crashed on job %s", lane, index, job.row_id)
            finally:
                self._active.pop((lane, index), None)
                self._release(job)
                queue.task_done()

    async def _run_one(self, job: Job) -> None:
        if job.cancelled:
            _mark(job, "cancelled", "cancelled while queued", charged=False)
            _refund(job, "cancelled before it started")
            state.clear_cancel(job.token)
            return

        _mark(job, "running")
        try:
            await self._wait_for_disk(job)
            if job.cancelled:
                _mark(job, "cancelled", "cancelled while queued", charged=False)
                _refund(job, "cancelled before it started")
                state.clear_cancel(job.token)
                return
            await job.runner(job)
        except asyncio.CancelledError:
            # Two very different things arrive here: the user pressed Cancel (the
            # runner raises it), or the pool is shutting down (the task is being
            # cancelled). Both refund — but only the second may swallow the
            # cancellation, or `stop()` would wait on a worker that never dies.
            _mark(job, "cancelled", "cancelled", charged=False)
            _refund(job, "bot restarted mid-job" if self._closing else "you cancelled it")
            state.clear_cancel(job.token)
            if self._closing:
                raise
            return
        except Exception as exc:
            self.failed_count += 1
            _mark(job, "failed", f"{type(exc).__name__}: {exc}", charged=False)
            _refund(job, "the download failed")
            log.warning("job %s failed: %s", job.row_id, exc, exc_info=True)
            await self._tell_user_it_failed(job, exc)
            return

        self.done_count += 1
        back = _refund_part(job)
        _mark(job, "done", charged=True)
        state.clear_cancel(job.token)
        log.info("job %s delivered (%s, waited %.0fs)", job.row_id, job.file_name, job.waited)
        if back > 0:
            await self._tell_user_about_part_refund(job, back)

    async def _tell_user_about_part_refund(self, job: Job, back: float) -> None:
        """A partial charge has to be said out loud, or it reads as a silent overcharge."""
        try:
            await self.client.send_message(
                job.chat_id,
                f"💰 <b>{back:g} credit(s) refunded</b>\n\n"
                f"{job.delivered} of {job.expected} files were delivered, so you were "
                f"only charged for those.",
            )
        except Exception:
            log.debug("could not deliver part-refund notice", exc_info=True)


    async def _tell_user_it_failed(self, job: Job, exc: Exception) -> None:
        """One clear message with the refund stated, never a stack trace."""
        from . import ui  # local import: ui pulls in nothing, but keeps the cycle obvious

        detail = getattr(exc, "user_message", None)
        text = detail() if callable(detail) else f"❌ <b>Could not finish that one.</b>\n\n{ui.esc(exc)}"
        if job.cost > 0:
            text += f"\n\n💰 <b>{job.cost:g} credit(s) refunded.</b>"
        try:
            await self.client.send_message(job.chat_id, text,
                                          disable_web_page_preview=True)
        except Exception:
            log.debug("could not deliver failure notice", exc_info=True)
