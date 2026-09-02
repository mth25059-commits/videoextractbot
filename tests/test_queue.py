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
from bot.queue import Job, Queue, Rejected  # noqa: E402

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
    user, is_new = credits.ensure(USER, "the operator", "operator")
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
