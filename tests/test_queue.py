"""
Queue + credit-safety checks.

The point of this file is one property: **a user is never charged for a video
they did not receive.** Everything else in the bot can be re-run; a wrong balance
is a support ticket.

pyrogram and curl_cffi are not needed to prove that, so they are stubbed — the
queue only ever calls `client.send_message`. Run: python tests/test_queue.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")

# --- stub pyrogram so the queue can be imported without the real dependency ---
_pyrogram = types.ModuleType("pyrogram")
_pyrogram.Client = type("Client", (), {})
_errors = types.ModuleType("pyrogram.errors")
_errors.FloodWait = type("FloodWait", (Exception,), {})
_types = types.ModuleType("pyrogram.types")
_types.Message = type("Message", (), {})
_pyrogram.errors, _pyrogram.types = _errors, _types
sys.modules.update({"pyrogram": _pyrogram, "pyrogram.errors": _errors,
                    "pyrogram.types": _types})

from bot import credits, db, state          # noqa: E402
from bot.config import cfg                  # noqa: E402
from bot.queue import (LINK_LANE, ZIP_LANE, Job, NoSpace,  # noqa: E402
                       Queue, Rejected, lane_of)

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def fresh_db():
    """Point db at a throwaway file rather than the real bot.db."""
    path = Path(tempfile.mkdtemp(prefix="terabot-test-")) / "test.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn
    return path


class FakeClient:
    """Records what the bot would have said to the user."""

    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text))


def job_row(row_id):
    return db.one("SELECT * FROM jobs WHERE id = ?", (row_id,))

async def main():
    fresh_db()
    client = FakeClient()
    q = Queue(client, workers=2)
    await q.start()

    USER, CHAT = 111, 111
    user, is_new = credits.ensure(USER, "Operator", "operator")
    check("new user got the joining bonus", user.credits, 2.0)
    check("is_new flag", is_new, True)

    # --- a job that succeeds keeps the credit spent -------------------------
    print("\nsuccessful job")
    delivered = []

    async def good(job):
        job.file_name = "movie.mp4"
        job.size_bytes = 1234
        delivered.append(job.label)

    accepted = q.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                            runner=good, cost=1.0, title="Movie",
                            source="https://x.test/1"))
    check("debited at accept", credits.balance(USER), 1.0)
    check("row is queued", job_row(accepted.row_id)["status"], "queued")

    await asyncio.sleep(0.3)
    check("runner ran", delivered, ["Movie"])
    check("still charged after success", credits.balance(USER), 1.0)
    row = job_row(accepted.row_id)
    check("row is done", row["status"], "done")
    check("row marked charged", row["charged"], 1)
    check("file name recorded", row["file_name"], "movie.mp4")

    # --- a job that fails gives the credit back -----------------------------
    print("\nfailed job")

    async def bad(job):
        raise RuntimeError("the source dropped the connection")

    failed_job = q.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                              runner=bad, cost=1.0, title="Broken"))
    check("debited at accept", credits.balance(USER), 0.0)
    await asyncio.sleep(0.3)
    check("refunded after failure", credits.balance(USER), 1.0)
    row = job_row(failed_job.row_id)
    check("row is failed", row["status"], "failed")
    check("row marked not charged", row["charged"], 0)
    check("user was told once", len(client.messages), 1)
    check("refund mentioned in the message",
          "refunded" in client.messages[0][1], True)

    # --- cancelling refunds too ---------------------------------------------
    print("\ncancelled job")
    ran = []

    async def slow(job):
        ran.append(job.token)

    pending = state.park(USER, "test")
    state.cancel(pending.token)
    cancelled_job = q.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                                 runner=slow, cost=1.0, token=pending.token))
    await asyncio.sleep(0.3)
    check("runner never started", ran, [])
    check("refunded after cancel", credits.balance(USER), 1.0)
    check("row is cancelled", job_row(cancelled_job.row_id)["status"], "cancelled")
    check("cancel flag cleared", state.is_cancelled(pending.token), False)

    # --- a batch bigger than the balance keeps what fits --------------------
    print("\nbatch larger than the balance")
    credits.grant(USER, 5.0, "test top-up")
    check("balance topped up to 6", credits.balance(USER), 6.0)

    done = []

    async def counter(job):
        done.append(job.index)

    batch = [Job(user_id=USER, chat_id=CHAT, kind="terabox", runner=counter,
                 cost=1.0, index=i, total_in_batch=10,
                 source=f"https://x.test/{i}") for i in range(10)]
    ok, rejected = q.submit_many(batch)
    check("six accepted", len(ok), 6)
    check("four rejected", len(rejected), 4)
    check("rejections are InsufficientCredits",
          all(isinstance(e, credits.InsufficientCredits) for _, e in rejected), True)
    check("balance emptied, not negative", credits.balance(USER), 0.0)

    await asyncio.sleep(0.6)
    check("all six delivered", sorted(done), [0, 1, 2, 3, 4, 5])
    check("nothing refunded on success", credits.balance(USER), 0.0)
    check("labels number within the batch", batch[2].label,
          "[3/10] https://x.test/2")

    # --- a restart must not eat credits ------------------------------------
    print("\nshutdown with work in flight")
    credits.grant(USER, 3.0, "test top-up")

    async def never(job):
        await asyncio.sleep(30)

    stalled = [q.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                            runner=never, cost=1.0)) for _ in range(3)]
    check("debited at accept", credits.balance(USER), 0.0)
    await asyncio.sleep(0.15)
    await q.stop()
    check("everything refunded on shutdown", credits.balance(USER), 3.0)
    check("rows are cancelled",
          {job_row(j.row_id)["status"] for j in stalled}, {"cancelled"})

    # --- a charge that fails for an unexpected reason ------------------------
    print("\ncharge blows up in a way nobody planned for")

    async def unused(job):
        raise AssertionError("a job that was never enqueued must never run")

    ghost = Job(user_id=999_999_001, chat_id=1, kind="terabox",
                runner=unused, cost=1.0, source="https://terabox.com/s/1ghost")
    before = q.depth()
    boom = None
    try:
        q.submit(ghost)
    except Exception as exc:                    # noqa: BLE001 — the type is the point
        boom = type(exc).__name__
    check("an id with no user row raises, it is not swallowed", boom, "ValueError")
    check("and nothing was enqueued", q.depth(), before)
    row = job_row(ghost.row_id)
    check("the row says failed, not queued", row["status"], "failed")
    check("and admits it was never charged", row["charged"], 0)
    check("the reason is on the row",
          "no such user" in (row["error"] or ""), True)

    # --- the two lanes are genuinely separate -------------------------------
    #
    # Every job here is submitted to a pool ONE worker wide per lane. On a single
    # shared queue the archive could not possibly finish while the link job is
    # still inside its runner, so the ordering below is the whole proof.
    print("\nlanes drain side by side")
    fresh = Queue(client)
    check("link lane takes its width from config",
          fresh.lanes[LINK_LANE], cfg.max_concurrent_jobs)
    check("zip lane has its own width",
          fresh.lanes[ZIP_LANE], cfg.max_concurrent_zip_jobs)
    check("and `workers` still reads as the total",
          Queue(client, workers=2).workers, 4)

    credits.grant(USER, 2.0, "test top-up")
    lanes = Queue(client, workers=1)
    await lanes.start()

    order: list[str] = []
    hold = asyncio.Event()

    async def blocks(job):
        order.append("link-start")
        await hold.wait()
        order.append("link-end")

    async def quick(job):
        order.append("zip-done")

    lanes.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                     runner=blocks, cost=1.0))
    await asyncio.sleep(0.1)
    lanes.submit(Job(user_id=USER, chat_id=CHAT, kind="zip",
                     runner=quick, cost=1.0))
    await asyncio.sleep(0.2)
    check("the archive finished while the link was still running",
          order, ["link-start", "zip-done"])
    check("each lane reports its own occupancy",
          [len(lanes.active(LINK_LANE)), len(lanes.active(ZIP_LANE))], [1, 0])

    hold.set()
    await asyncio.sleep(0.1)
    check("and the link finished on its own clock", order[-1], "link-end")
    await lanes.stop()

    # --- a restart has to refund every lane, not just the first one ----------
    print("\nshutdown refunds both lanes")
    credits.grant(USER, 4.0, "test top-up")
    before = credits.balance(USER)

    async def never_finishes(job):
        await asyncio.sleep(30)

    both = Queue(client, workers=1)
    await both.start()
    stuck = [both.submit(Job(user_id=USER, chat_id=CHAT, kind=kind,
                             runner=never_finishes, cost=1.0))
             for kind in ("terabox", "terabox", "zip", "zip")]
    check("all four debited", credits.balance(USER), before - 4.0)
    await asyncio.sleep(0.15)
    check("one running and one waiting in each lane",
          [both.depth(LINK_LANE), both.depth(ZIP_LANE)], [1, 1])
    await both.stop()
    check("every lane gave its credits back", credits.balance(USER), before)
    check("all four rows cancelled",
          {job_row(j.row_id)["status"] for j in stuck}, {"cancelled"})

    # --- no disk means refund, never a job that hangs for ever ---------------
    print("\ndisk guard")
    credits.grant(USER, 2.0, "test top-up")
    before = credits.balance(USER)

    starved = Queue(client, workers=1)
    starved.DISK_WAIT_SECONDS = 0.2
    starved.DISK_POLL_SECONDS = 0.05
    starved._free_bytes = lambda: 0
    await starved.start()

    never_ran: list[int] = []

    async def should_not_run(job):
        never_ran.append(1)

    starved_job = starved.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                                     runner=should_not_run, cost=1.0))
    await asyncio.sleep(0.6)
    check("the runner never started", never_ran, [])
    check("the credit came back", credits.balance(USER), before)
    check("the row records a failure, not a hang",
          job_row(starved_job.row_id)["status"], "failed")
    check("and names the reason, so the log is diagnosable",
          job_row(starved_job.row_id)["error"].startswith(NoSpace.__name__), True)
    check("and the user was told, with the refund on it",
          "refunded" in client.messages[-1][1], True)
    await starved.stop()

    # Space appearing while it waits is the common case, and it must just run.
    polls = {"n": 0}

    def frees_up() -> int:
        polls["n"] += 1
        return 0 if polls["n"] < 3 else 1 << 60

    patient = Queue(client, workers=1)
    patient.DISK_WAIT_SECONDS = 5.0
    patient.DISK_POLL_SECONDS = 0.05
    patient._free_bytes = frees_up
    await patient.start()

    waited_then_ran: list[str] = []

    async def eventually(job):
        waited_then_ran.append("ran")

    patient.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                       runner=eventually, cost=1.0))
    await asyncio.sleep(0.5)
    check("a job that waits for space still runs", waited_then_ran, ["ran"])
    check("and stays charged", credits.balance(USER), before - 1.0)
    await patient.stop()

    # --- one process per person, per lane ------------------------------------
    #
    # The door asks `busy()` before it charges anything, so this count has to be
    # exact in both directions: too low and a second batch slips through, too high
    # and the user is locked out of the bot for ever with nothing running.
    print("\none process per person, per lane")
    credits.grant(USER, 3.0, "test top-up")

    counted = Queue(client, workers=1)
    await counted.start()
    check("nobody is busy on a fresh queue", counted.busy(USER), 0)
    check("and no lane of theirs is either",
          (counted.busy(USER, LINK_LANE), counted.busy(USER, ZIP_LANE)), (0, 0))

    gate = asyncio.Event()

    async def held(job):
        await gate.wait()

    held_jobs = [counted.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                                    runner=held, cost=1.0)) for _ in range(2)]
    check("both count as in flight, queued or running", counted.busy(USER), 2)
    check("and they are counted in the lane they will run in",
          counted.busy(USER, LINK_LANE), 2)
    check("not in the other one", counted.busy(USER, ZIP_LANE), 0)
    check("and a different person is still free", counted.busy(USER + 1), 0)

    gate.set()
    await asyncio.sleep(0.3)
    check("finishing releases every one of them", counted.busy(USER), 0)
    check("both really did finish",
          {job_row(j.row_id)["status"] for j in held_jobs}, {"done"})

    # A job that fails, and one that is never enqueued at all, must both release —
    # each is a way for a user to end up permanently "busy" with nothing running.
    async def explodes(job):
        raise RuntimeError("nope")

    counted.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                       runner=explodes, cost=1.0))
    await asyncio.sleep(0.3)
    check("a failed job releases too", counted.busy(USER), 0)

    try:
        counted.submit(Job(user_id=999_999_002, chat_id=1, kind="terabox",
                           runner=explodes, cost=1.0))
    except Exception:
        pass
    check("a submit that raised holds nothing", counted.busy(999_999_002), 0)
    await counted.stop()

    # Shutdown is the third way out, and it refunds — so it must release as well.
    credits.grant(USER, 2.0, "test top-up")
    shut = asyncio.Event()                      # a fresh gate: the first one is set

    async def held_again(job):
        await shut.wait()

    restarted = Queue(client, workers=1)
    await restarted.start()
    for _ in range(2):
        restarted.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                             runner=held_again, cost=1.0))
    await asyncio.sleep(0.1)
    check("two in flight before the restart", restarted.busy(USER), 2)
    await restarted.stop()
    check("a restart leaves nobody busy", restarted.busy(USER), 0)
    check("and clears the lanes with it",
          (restarted.busy(USER, LINK_LANE), restarted.busy(USER, ZIP_LANE)), (0, 0))

    # The rule the operator asked for, in the one place it is decided: a ZIP takes so
    # long that being locked out of links for its whole duration is most of the wait a
    # user ever sees. An archive is pulled from Telegram, unpacked here and uploaded,
    # while a link is pulled from the provider's CDN — different pipes, so both run at
    # full speed and there is nothing to protect the user from.
    print("\nan archive and a link, at once, for one person")
    credits.grant(USER, 4.0, "test top-up")
    both = Queue(client, workers=1)
    await both.start()

    pair = asyncio.Event()
    entered: list[str] = []

    async def holds(job):
        entered.append(job.kind)
        await pair.wait()

    zip_side = both.submit(Job(user_id=USER, chat_id=CHAT, kind="zip",
                               runner=holds, cost=1.0))
    check("the archive is in the archive lane", both.busy(USER, ZIP_LANE), 1)
    check("and the link lane is still open to them",
          both.busy(USER, LINK_LANE), 0)

    link_side = both.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                                runner=holds, cost=1.0))
    check("so a link goes in beside it rather than being refused",
          both.busy(USER, LINK_LANE), 1)
    check("two jobs, one person", both.busy(USER), 2)
    check("a second link is still refused, which is the part that stays",
          both.busy(USER, LINK_LANE), 1)

    # Both counted *and* both actually started. The counts above would read the same
    # if the second job were sitting in a queue behind the first, and sitting in a
    # queue is the thing this change exists to stop — so the runners say so
    # themselves, from inside a lane worker each, while the other is still held.
    await asyncio.sleep(0.1)
    check("and both are running, not one behind the other",
          sorted(entered), ["terabox", "zip"])
    check("with nothing left waiting in either queue", both.depth(), 0)

    pair.set()
    await asyncio.sleep(0.3)
    check("both finished", {job_row(j.row_id)["status"]
                            for j in (zip_side, link_side)}, {"done"})
    check("and each lane released its own",
          (both.busy(USER, ZIP_LANE), both.busy(USER, LINK_LANE)), (0, 0))
    check("with no key left behind for them at all", both._inflight, {})
    await both.stop()

    # `lane_of` maps everything that is not an archive onto the link lane, so a Fap
    # job and a Terabox job are one lane and remain mutually exclusive for one user.
    # That is deliberate: they both come down the same provider pipe, and splitting
    # them would be a third lane with its own worker count and disk headroom.
    check("Fap shares the link lane, not a third one", lane_of("fap"), LINK_LANE)
    check("and an archive is the only thing that is not a link", lane_of("zip"),
          ZIP_LANE)

    # --- pro-rata: an archive is one price for many videos -------------------
    #
    # The operator approved this rule directly ("han ye plan sahi hai"): pay for what
    # arrived. Charging for 10 when 7 came is an overcharge; refunding all 10 hands
    # over 7 videos free. Nothing arriving is still a *failure*, and that path is
    # untouched — the runner raises, so the full refund above still applies.
    print("\npro-rata charging for a part-delivered archive")
    credits.grant(USER, 20.0, "test top-up")
    prorata = Queue(client, workers=1)
    await prorata.start()

    async def delivers(n):
        async def runner(job):
            job.delivered = n
        return runner

    before = credits.balance(USER)
    part = prorata.submit(Job(user_id=USER, chat_id=CHAT, kind="zip",
                              runner=await delivers(7), cost=10.0, expected=10))
    await asyncio.sleep(0.25)
    check("charged only for what arrived", credits.balance(USER), before - 7.0)
    check("and the job still counts as done",
          job_row(part.row_id)["status"], "done")
    check("the user is told how many arrived",
          "7 of 10" in client.messages[-1][1], True)
    check("and told what came back", "3 credit" in client.messages[-1][1], True)

    before = credits.balance(USER)
    prorata.submit(Job(user_id=USER, chat_id=CHAT, kind="zip",
                       runner=await delivers(4), cost=8.0, expected=4))
    await asyncio.sleep(0.25)
    check("a full delivery refunds nothing", credits.balance(USER), before - 8.0)

    # A link job is one file: `expected` is 0 or 1 and the rule must not fire, or a
    # runner that never sets `delivered` would refund every single link job.
    before = credits.balance(USER)
    prorata.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                       runner=await delivers(0), cost=2.0))
    await asyncio.sleep(0.25)
    check("a single-file job is untouched by the rule",
          credits.balance(USER), before - 2.0)

    before = credits.balance(USER)
    prorata.submit(Job(user_id=USER, chat_id=CHAT, kind="terabox",
                       runner=await delivers(0), cost=2.0, expected=1))
    await asyncio.sleep(0.25)
    check("expected=1 is not an archive either",
          credits.balance(USER), before - 2.0)

    # Thirds do not divide: the refund is rounded to the cent the ledger stores.
    before = credits.balance(USER)
    prorata.submit(Job(user_id=USER, chat_id=CHAT, kind="zip",
                       runner=await delivers(2), cost=2.0, expected=3))
    await asyncio.sleep(0.25)
    check("an uneven share is rounded, not dropped",
          credits.balance(USER), round(before - 2.0 + 0.67, 2))

    # Nothing delivered is a failure, not a 100% pro-rata refund — the archive
    # handler raises in that case and the full-refund path owns it.
    before = credits.balance(USER)

    async def delivers_nothing(job):
        job.delivered = 0
        raise RuntimeError("nothing in that archive could be sent")

    empty = prorata.submit(Job(user_id=USER, chat_id=CHAT, kind="zip",
                               runner=delivers_nothing, cost=6.0, expected=6))
    await asyncio.sleep(0.25)
    check("nothing delivered is refunded in full", credits.balance(USER), before)
    check("and reads as failed, not done",
          job_row(empty.row_id)["status"], "failed")
    await prorata.stop()

    # --- the ledger explains the balance ------------------------------------
    print("\nledger")
    rows = credits.history(USER, 100)
    total = round(sum(float(r["delta"]) for r in rows), 2)
    check("ledger sums to the balance", total, credits.balance(USER))
    check("last row records the running balance",
          float(rows[0]["balance"]), credits.balance(USER))

if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
