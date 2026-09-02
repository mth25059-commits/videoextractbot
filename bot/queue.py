"""
The job queue and its worker pool.

Ten links pasted at once are ten jobs, not ten downloads. `MAX_CONCURRENT_JOBS`
workers pull from one asyncio queue, and each file is delivered the moment it is
ready — so the user is watching video 1 while video 7 is still downloading.
Running all ten at once would just split the same bandwidth ten ways and make
every one of them slow.

The money rule lives here, and it is the whole reason this module owns credits:

* On accept, the cost is debited and the ledger row says "reserved". This is a
  real debit, not a soft hold — otherwise a user could queue ten links on two
  credits and the shortfall would only surface after the work was done.
* On failure, cancellation, or a file Telegram refuses, it is refunded in full.

A user is never charged for a video that did not arrive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pyrogram import Client

from . import credits, db, state
from .config import cfg

log = logging.getLogger(__name__)

#: What a module hands the queue: given the job, fetch and deliver it.
JobRunner = Callable[["Job"], Awaitable[None]]

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

    @property
    def cancelled(self) -> bool:
        return bool(self.token) and state.is_cancelled(self.token)

    @property
    def label(self) -> str:
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

class Queue:
    """One asyncio queue plus a fixed pool of workers, owned by `main`."""

    MAX_DEPTH = 200

    def __init__(self, client: Client, workers: int | None = None):
        self.client = client
        self.workers = max(1, workers or cfg.max_concurrent_jobs)
        self._q: asyncio.Queue[Job] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._active: dict[int, Job] = {}
        self._closing = False
        self.done_count = 0
        self.failed_count = 0

    # --- lifecycle ---

    async def start(self) -> None:
        if self._tasks:
            return
        self._closing = False
        self._tasks = [
            asyncio.create_task(self._worker(i), name=f"worker-{i}")
            for i in range(self.workers)
        ]
        log.info("queue up with %d workers", self.workers)

    async def stop(self) -> None:
        """
        Shut the pool down without eating credits.

        Refund ownership is split so nothing is refunded twice: whatever is still
        *queued* is refunded here, and whatever is *running* is refunded by its own
        worker as the cancellation unwinds it.
        """
        self._closing = True

        while not self._q.empty():
            try:
                job = self._q.get_nowait()
            except asyncio.QueueEmpty:
                break
            _mark(job, "cancelled", "bot restarted", charged=False)
            _refund(job, "bot restarted before it started")
            self._q.task_done()

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()

    # --- accepting work ---

    def submit(self, job: Job) -> Job:
        """
        Debit, record, enqueue. Raises `Rejected` with a user-facing message
        instead of accepting something it cannot pay for or cannot fit.
        """
        if self._q.qsize() >= self.MAX_DEPTH:
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
        self._q.put_nowait(job)
        return job

    def submit_many(self, jobs: list[Job]) -> tuple[list[Job], list[tuple[Job, Exception]]]:
        """
        Submit a batch, keeping whatever fits.

        Ten links with credit for six should send six, not refuse all ten. The
        caller reports the rejected tail.
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

    def depth(self) -> int:
        return self._q.qsize()

    def active(self) -> list[Job]:
        return list(self._active.values())

    def position_of(self, job: Job) -> int:
        """1-based place in line, counting the jobs already running."""
        return len(self._active) + max(1, self._q.qsize())

    def stats(self) -> dict[str, Any]:
        return {
            "workers": self.workers,
            "queued": self._q.qsize(),
            "running": len(self._active),
            "done": self.done_count,
            "failed": self.failed_count,
        }

    # --- running work ---

    async def _worker(self, index: int) -> None:
        while True:
            job = await self._q.get()      # cancelling here just ends the worker
            self._active[index] = job
            job.started_at = time.monotonic()
            try:
                await self._run_one(job)
            except Exception:
                log.exception("worker %d crashed on job %s", index, job.row_id)
            finally:
                self._active.pop(index, None)
                self._q.task_done()

    async def _run_one(self, job: Job) -> None:
        if job.cancelled:
            _mark(job, "cancelled", "cancelled while queued", charged=False)
            _refund(job, "cancelled before it started")
            state.clear_cancel(job.token)
            return

        _mark(job, "running")
        try:
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
        _mark(job, "done", charged=True)
        state.clear_cancel(job.token)
        log.info("job %s delivered (%s, waited %.0fs)", job.row_id, job.file_name, job.waited)

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
