"""
The thirty-minute clock on a delivered video.

Two properties are what this file is for:

1. **The timer survives a restart.** It is a row in `deletions`, not a sleeping task,
   so a sweep started fresh still deletes what came due while the bot was down.
2. **A split video goes whole or not at all.** A 3 GB file leaves as `.001`/`.002`
   plus a note explaining how to rejoin them; deleting only the last piece would leave
   the user holding fragments and instructions for a file that is gone.

pyrogram is stubbed — nothing in here needs the real thing. Run:
python tests/test_expiry.py
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

# --- stub pyrogram so uploader/ui can be imported without the real dependency ---
_pyrogram = types.ModuleType("pyrogram")
_pyrogram.Client = type("Client", (), {})
_errors = types.ModuleType("pyrogram.errors")
_errors.FloodWait = type("FloodWait", (Exception,), {})
_types = types.ModuleType("pyrogram.types")
_types.Message = type("Message", (), {})
_pyrogram.errors, _pyrogram.types = _errors, _types
sys.modules.update({"pyrogram": _pyrogram, "pyrogram.errors": _errors,
                    "pyrogram.types": _types})

from bot import db, expiry, ui, uploader     # noqa: E402
from bot.config import cfg                   # noqa: E402

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
    path = Path(tempfile.mkdtemp(prefix="terabot-expiry-")) / "test.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn
    return path


def set_minutes(value):
    """`cfg` is a frozen dataclass; the suites are the only thing allowed to poke it."""
    object.__setattr__(cfg, "auto_delete_minutes", value)


def rows():
    return db.query("SELECT * FROM deletions ORDER BY id", ())


class Msg:
    """Just enough of a pyrogram Message for `_ids_of` to read an id off it."""

    def __init__(self, ident):
        self.id = ident


class FakeClient:
    """Records every delete_messages call, and can be told to refuse one."""

    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    async def delete_messages(self, chat_id, message_ids, **_kw):
        self.calls.append((chat_id, list(message_ids)))
        if self.raises is not None:
            raise self.raises


def test_the_warning():
    print("\nthe warning on screen")
    off = ui.delete_notice(0)
    check("off renders as nothing at all, not as a special case per caller", off, "")

    note = ui.delete_notice(30)
    check("it is bold", "<b>AUTO-DELETE IN 30 MINUTES</b>" in note, True)
    check("and capitals, which is the only other emphasis Telegram has",
          note.upper() == note or "SAVE THE VIDEO" in note, True)
    check("it says what to do about it", "SAVE THE VIDEO AS SOON AS IT ARRIVES" in note,
          True)
    check("one minute is not '1 MINUTES'", "1 MINUTE</b>" in ui.delete_notice(1), True)

    # `tests/test_fap.py` asserts that the HLS panel contains no byte counts at all —
    # " B" and "MB" both fail it. The notice is appended to that panel, so its wording
    # has to stay clear of both or a green suite turns red for a reason nobody guesses.
    check("no byte-count substring sneaks into the panel via the notice",
          " B" in note or "MB" in note, False)


def test_it_reaches_both_panels():
    print("\nboth live panels carry it")
    import time
    began = time.monotonic() - 30

    hls = ui.assembling_block("clip", 45.0, 300.0, began, expires_minutes=30)
    bytes_panel = ui.progress_block("up", 500, 1000, began, stage="sending",
                                    expires_minutes=30)
    check("the HLS panel (Fap, and Terabox's HLS fallback)",
          "AUTO-DELETE IN 30 MINUTES" in hls, True)
    check("the byte panel (download, upload, ZIP)",
          "AUTO-DELETE IN 30 MINUTES" in bytes_panel, True)
    check("neither carries it when the feature is off",
          "AUTO-DELETE" in ui.assembling_block("clip", 45.0, 300.0, began)
          or "AUTO-DELETE" in ui.progress_block("up", 500, 1000, began),
          False)
    check("the HLS panel still has no byte counts with the notice on it",
          " B" in hls or "MB" in hls, False)


def test_scheduling():
    print("\nwhat a delivery writes down")
    fresh_db()
    set_minutes(30)

    one = uploader.Sent(message=Msg(41), size_bytes=10, seconds=1.0,
                        message_ids=(41,))
    check("one video is one row", expiry.remember(one, chat_id=777, job_id=5), 1)
    row = rows()[0]
    check("in the right chat", row["chat_id"], 777)
    check("naming the right message", row["message_id"], 41)
    check("and the job it came from, for the log", row["job_id"], 5)
    due_in = row["due_at"] - db.now()
    check("due in half an hour, give or take the second it took to get here",
          1770 <= due_in <= 1800, True)

    # A split delivery: the rejoin note plus every piece. `send_in_parts` returns the
    # *last* message, so a `Sent.message`-only implementation would delete one of four.
    fresh_db()
    split = uploader.Sent(message=Msg(94), size_bytes=10, seconds=1.0, parts=3,
                          message_ids=(91, 92, 93, 94))
    check("a three-part video schedules all four messages",
          expiry.remember(split, chat_id=777), 4)
    check("the note that explains how to rejoin them included",
          [r["message_id"] for r in rows()], [91, 92, 93, 94])

    fresh_db()
    legacy = uploader.Sent(message=Msg(7), size_bytes=10, seconds=1.0)
    check("a Sent with no message_ids falls back to its single message",
          expiry.remember(legacy, chat_id=777), 1)

    fresh_db()
    blind = uploader.Sent(message=None, size_bytes=10, seconds=1.0)
    check("and one with nothing to point at schedules nothing rather than raising",
          expiry.remember(blind, chat_id=777), 0)
    check("no row either", len(rows()), 0)

    set_minutes(0)
    check("AUTO_DELETE_MINUTES=0 turns the whole thing off",
          expiry.remember(one, chat_id=777), 0)
    check("still nothing in the table", len(rows()), 0)
    set_minutes(30)


async def test_the_sweep():
    print("\nthe sweep")
    fresh_db()
    set_minutes(30)

    # Two chats, and one row that is not due yet.
    now = db.now()
    for chat, message, due in ((10, 101, now - 5), (10, 102, now - 5),
                               (20, 201, now - 5), (10, 999, now + 600)):
        db.execute("INSERT INTO deletions (chat_id, message_id, job_id, due_at, "
                   "created_at) VALUES (?, ?, ?, ?, ?)", (chat, message, None, due, now))

    check("only the ripe rows are picked up", len(expiry.due()), 3)

    client = FakeClient()
    check("three messages went", await expiry.sweep(client), 3)
    check("one call per chat, not one per message", len(client.calls), 2)
    check("the ids are batched together",
          sorted(client.calls, key=lambda c: c[0]),
          [(10, [101, 102]), (20, [201])])
    check("their rows are gone", [r["message_id"] for r in rows()], [999])
    check("and the one that is not due yet is untouched", len(rows()), 1)

    check("a second sweep with nothing ripe does nothing at all",
          await expiry.sweep(FakeClient()), 0)


async def test_it_survives_a_restart():
    print("\nit survives a restart")
    fresh_db()
    set_minutes(30)

    # Written by a process that is now gone: due_at is in the past because the bot was
    # down through the half hour. This is the whole reason the timer is a row.
    db.execute("INSERT INTO deletions (chat_id, message_id, job_id, due_at, created_at) "
               "VALUES (?, ?, ?, ?, ?)", (55, 500, 9, db.now() - 3600, db.now() - 5400))

    client = FakeClient()
    check("a fresh sweep catches up on what came due while it was stopped",
          await expiry.sweep(client), 1)
    check("deleting the right message", client.calls, [(55, [500])])
    check("and the queue is empty again", len(rows()), 0)


async def test_when_telegram_refuses():
    print("\nwhen Telegram refuses")
    fresh_db()
    set_minutes(30)
    now = db.now()
    db.execute("INSERT INTO deletions (chat_id, message_id, job_id, due_at, created_at) "
               "VALUES (?, ?, ?, ?, ?)", (33, 300, None, now - 5, now))

    # Blocked bot, already-deleted message, chat gone. The delete is lost either way, so
    # the row still goes — otherwise it is retried every thirty seconds until the
    # database ends, and one dead chat becomes a permanent log flood.
    refused = FakeClient(raises=RuntimeError("MESSAGE_DELETE_FORBIDDEN"))
    check("nothing is reported as deleted", await expiry.sweep(refused), 0)
    check("but the row is dropped rather than retried for ever", len(rows()), 0)

    # A rate limit is the one failure worth keeping the work for: the next pass is
    # thirty seconds away, and arguing with a FloodWait is how a bot loses the ability
    # to send anything at all.
    fresh_db()
    db.execute("INSERT INTO deletions (chat_id, message_id, job_id, due_at, created_at) "
               "VALUES (?, ?, ?, ?, ?)", (34, 400, None, now - 5, now))
    from pyrogram.errors import FloodWait
    flooded = FakeClient(raises=FloodWait("wait 42"))
    check("a flood wait deletes nothing", await expiry.sweep(flooded), 0)
    check("and leaves the row for the next pass", [r["message_id"] for r in rows()],
          [400])

    check("a later pass with a healthy client finishes the job",
          await expiry.sweep(FakeClient()), 1)
    check("queue drained", len(rows()), 0)


def test_the_table_is_in_both_schemas():
    print("\nthe table exists in both flavours")
    check("sqlite has it", "CREATE TABLE IF NOT EXISTS deletions" in db.SCHEMA, True)
    check("postgres too", "CREATE TABLE IF NOT EXISTS deletions" in db.SCHEMA_PG, True)
    check("and connect() is told to expect it, so a Supabase project that is missing "
          "it is refused at boot instead of failing at delivery",
          "deletions" in db.TABLES, True)
    # `_connect_postgres` runs SCHEMA_PG on every connect and only refuses when a table
    # is genuinely absent, so an existing Supabase project grows this table by itself.
    check("BIGSERIAL, not AUTOINCREMENT, on the Postgres side",
          "AUTOINCREMENT" in db.SCHEMA_PG, False)
    check("the due_at index is in both",
          db.SCHEMA.count("idx_deletions_due") and db.SCHEMA_PG.count("idx_deletions_due"),
          1)


async def main():
    test_the_warning()
    test_it_reaches_both_panels()
    test_scheduling()
    await test_the_sweep()
    await test_it_survives_a_restart()
    await test_when_telegram_refuses()
    test_the_table_is_in_both_schemas()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
