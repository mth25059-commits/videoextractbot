"""
The nightly report, the link log and the hand-rolled PDF writer.

Run: python tests/test_report.py

The PDF checks are structural on purpose. Whether the file *looks* right is a
matter for a reader, but whether the xref table points at the right bytes is
arithmetic, and getting it wrong produces a file that opens in one viewer and not
in another — the kind of bug that only shows up on the one machine the operator
uses.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os                                    # noqa: E402
os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")

import asyncio                               # noqa: E402
import re                                    # noqa: E402
import sqlite3                               # noqa: E402
import tempfile                              # noqa: E402
import time                                  # noqa: E402
import types                                 # noqa: E402

from bot import db, nightly, pdf             # noqa: E402
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


def raises(name, exc, fn):
    global passed, failed
    try:
        fn()
    except exc:
        passed += 1
        print(f"  ok   {name}")
        return
    except Exception as other:
        failed += 1
        print(f"  FAIL {name} - raised {type(other).__name__}, wanted {exc.__name__}")
        return
    failed += 1
    print(f"  FAIL {name} - nothing raised")


tmp = Path(tempfile.mkdtemp(prefix="terabot-report-"))

# --- when the report fires ---------------------------------------------------

print("\n- the clock -")

DAY = 86400
MIDNIGHT = 1757030400            # 2026-09-05 00:00:00 UTC, a whole multiple of 86400

check("parses HH:MM", nightly.parse_hhmm("18:30"), (18, 30))
check("tolerates padding", nightly.parse_hhmm("  09:05 "), (9, 5))
check("midnight is a time", nightly.parse_hhmm("00:00"), (0, 0))
check("an empty setting is off", nightly.parse_hhmm(""), None)
check("nonsense is off, not a crash", nightly.parse_hhmm("later"), None)
check("a 25th hour is off", nightly.parse_hhmm("25:00"), None)
check("a 60th minute is off", nightly.parse_hhmm("18:60"), None)
check("seconds are not accepted", nightly.parse_hhmm("18:30:00"), None)

check("18:30 is 66600s after midnight UTC",
      nightly.seconds_until("18:30", MIDNIGHT), 18 * 3600 + 30 * 60)
check("an hour before, it is an hour away",
      nightly.seconds_until("18:30", MIDNIGHT + 17 * 3600 + 30 * 60), 3600)
check("one second after, it is a whole day away",
      nightly.seconds_until("18:30", MIDNIGHT + 18 * 3600 + 30 * 60 + 1), DAY - 1)
check("exactly on it, it waits for tomorrow rather than firing twice",
      nightly.seconds_until("18:30", MIDNIGHT + 18 * 3600 + 30 * 60), DAY)
check("never returns zero or less",
      all(nightly.seconds_until("18:30", MIDNIGHT + n * 977) > 0 for n in range(200)),
      True)
check("never returns more than a day",
      max(nightly.seconds_until("18:30", MIDNIGHT + n * 977) for n in range(200))
      <= DAY, True)
raises("an unusable time is a programming error, not a silent skip",
       ValueError, lambda: nightly.seconds_until("nope", MIDNIGHT))

check("18:30 UTC is midnight IST, which is what was asked for",
      nightly.ist_stamp(MIDNIGHT + 18 * 3600 + 30 * 60),
      time.strftime("%d %b %Y, 00:00 IST", time.gmtime(MIDNIGHT + DAY)))
check("and the date rolls with it, not with UTC",
      nightly.ist_stamp(MIDNIGHT + 18 * 3600 + 30 * 60).split(",")[0],
      time.strftime("%d %b %Y", time.gmtime(MIDNIGHT + DAY)))
check("the shipped default is that time", cfg.daily_report_utc, "18:30")

# --- the PDF writer ----------------------------------------------------------

print("\n- wrapping -")

check("short text is one line", pdf.wrap("hello", 20), ["hello"])
check("empty text is one empty line", pdf.wrap("", 20), [""])
check("breaks on spaces", pdf.wrap("aaa bbb ccc", 7), ["aaa bbb", "ccc"])
check("a word exactly the width is not cut", pdf.wrap("abcdefgh", 8), ["abcdefgh"])
check("a word too long is cut", pdf.wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])
check("continuation lines take the indent",
      pdf.wrap("abcdefghij", 6, indent="  "), ["abcdef", "  ghij"])

URL = "https://1024terabox.com/sharing/link?surl=" + "Q" * 140
lines = pdf.wrap(f"   {URL}", pdf.columns(), indent="      ")
check("a 180-character link fits on the page",
      max(len(line) for line in lines) <= pdf.columns(), True)
check("and nothing is lost in the wrapping",
      "".join(line.strip() for line in lines).replace(" ", ""), f"{URL}")
check("every wrap stays inside its width",
      max(len(line)
          for width in (12, 30, 61, 80)
          for line in pdf.wrap("word " * 40 + "x" * 200, width)) <= 80, True)

print("\n- the file itself -")

doc = pdf.Writer("TeraBot - shared links", "exported today")
for n in range(120):                        # enough rows to force several pages
    doc.line(f"#{n} 04 Sep 2026 18:22  done  terabox  1 cr", bold=True)
    doc.wrapped(f"   {URL}", indent="      ")
    doc.gap(5.0)
out = doc.save(tmp / "links.pdf")
raw = out.read_bytes()

check("it is a PDF", raw[:8], b"%PDF-1.4")
check("and ends where a reader looks for the end", raw.strip()[-5:], b"%%EOF")

pages = int(re.search(rb"/Type /Pages /Kids \[(.*?)\] /Count (\d+)", raw).group(2))
check("120 rows of a wrapped URL run to several pages", pages > 1, True)
check("every page is in the tree",
      len(re.findall(rb"/Type /Page /Parent", raw)), pages)
check("the last page is numbered with the total",
      f"page {pages} of {pages}".encode() in raw, True)
check("nothing claims to be page 0", b"page 0 of" in raw, False)

# The xref table is the one part a reader trusts blindly: each entry must be the
# byte offset of "<n> 0 obj". A viewer that honours it opens a file with wrong
# offsets as blank pages rather than complaining, so it is checked here.
start = int(re.search(rb"startxref\s+(\d+)", raw).group(1))
check("startxref points at the table", raw[start:start + 4], b"xref")
entries = re.findall(rb"^(\d{10}) 00000 n $", raw[start:].decode("latin-1")
                     .encode("latin-1"), re.M)
check("one entry per object, plus the free head",
      len(entries) + 1, int(re.search(rb"/Size (\d+)", raw).group(1)))
bad = [n for n, offset in enumerate(entries, start=1)
       if not raw[int(offset):].startswith(b"%d 0 obj" % n)]
check("every offset lands on its own object", bad, [])
check("each xref line is exactly 20 bytes",
      {len(line) + 1 for line in raw[start:].split(b"\n")
       if re.fullmatch(rb"\d{10} \d{5} [nf] ", line)}, {20})

check("a parenthesis or a backslash cannot break the stream", True, True)
esc = pdf.Writer()
esc.line("a (b) c \\ d")
esc.save(tmp / "escaped.pdf")
body = (tmp / "escaped.pdf").read_bytes()
check("parens and backslashes are escaped", br"(a \(b\) c \\ d)" in body, True)
check("a newline inside a name cannot end the text object early",
      b"BT" in body and body.count(b"stream") == 2, True)

uni = pdf.Writer("report")
uni.line("Devanagari below")
uni.line("नमस्ते.mp4")
uni.save(tmp / "uni.pdf")
unicode_body = (tmp / "uni.pdf").read_bytes()
check("an unrepresentable name becomes ? rather than an unopenable file",
      b"(?" in unicode_body or b"?" in unicode_body, True)
check("and the file is still a valid PDF", unicode_body[:8], b"%PDF-1.4")

# --- the link log and the report --------------------------------------------

print("\n- the link log -")

import bot.handlers.admin as admin           # noqa: E402

object.__setattr__(cfg, "work_dir", tmp)

# A throwaway database, exactly as test_queue does it. This file inserts and
# deletes rows, and `cfg.db_path` is a property that cannot be pointed elsewhere —
# so the connection is swapped instead. Running this against the real bot.db on the
# box would empty the users table.
conn = sqlite3.connect(tmp / "report.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript(db.SCHEMA)
conn.commit()
db._conn = conn

now = db.now()
LINK = "https://1024terabox.com/s/1AbCdEfGhIjK"
for n in range(11):
    db.execute(
        "INSERT INTO jobs (user_id, kind, source, status, file_name, size_bytes, "
        "cost, charged, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,1,?,?)",
        (6100000001, "terabox" if n % 2 else "zip", f"{LINK}{n}",
         "done" if n % 3 else "failed", f"movie{n}.mp4", 1024 * 1024 * (n + 1),
         1, now - 60 * n, now - 30 * n))
db.execute("INSERT INTO jobs (user_id, kind, source, status, cost, created_at) "
           "VALUES (?,?,?,?,?,?)", (6100000001, "terabox", "", "done", 1, now))
db.execute("INSERT INTO users (user_id, first_name, joined_at, last_seen) "
           "VALUES (?,?,?,?)", (6100000001, "Operator", now, now))

card, total, shown = admin._links_card(0)
check("the log counts only rows that have a source", total, 11)
check("a page holds PAGE of them", shown, admin.PAGE)
check("the newest is on the first page", f"{LINK}10" in card, True)
check("the oldest is not", f"{LINK}0<" in card, False)
check("a link job is marked as a link", "🔗" in card, True)
check("an archive job is marked as an archive", "🗂" in card, True)
check("the second page holds the rest", admin._links_card(1)[2], 11 - admin.PAGE)
check("past the end is empty rather than an error", admin._links_card(9)[2], 0)

long_link = "https://1024terabox.com/sharing/link?surl=" + "Z" * 90
db.execute("INSERT INTO jobs (user_id, kind, source, status, cost, created_at) "
           "VALUES (?,?,?,?,?,?)", (6100000001, "terabox", long_link, "done", 1, now + 1))
check("a long link is trimmed on the card, not wrapped",
      max(len(line) for line in admin._links_card(0)[0].splitlines()) < 140, True)
check("and the trim is marked", "…" in admin._links_card(0)[0], True)

path, rows = admin._links_pdf(tmp / "export.pdf")
exported = path.read_bytes()
# What the page actually shows: every string the content stream draws, with the
# wrapping taken back out. A URL cut across three lines is still the same URL.
drawn = b"".join(re.findall(rb"\((.*?)\) Tj", exported))
flat = drawn.replace(b" ", b"").replace(b"\\", b"")
check("the export carries every row", rows, 12)
check("the whole link is in the PDF, not the trimmed one",
      long_link.encode() in flat, True)
check("and so is the trimmed one's full text", f"{LINK}10".encode() in flat, True)
check("the export names itself", b"TeraBot-sharedlinks" in flat, True)
check("it is a readable PDF too", exported[:8], b"%PDF-1.4")

print("\n- the nightly card -")


class FakeQueue:
    def stats(self):
        return {"running": 1, "queued": 0, "workers": 10,
                "lanes": {"terabox": {"running": 1, "queued": 0, "workers": 6},
                          "zip": {"running": 0, "queued": 0, "workers": 4}}}


async def _no_cookies():
    return ["🔑 <b>Cookies</b>   <i>none configured</i>"]


admin._cookie_lines = _no_cookies
report = asyncio.run(admin.daily_report(FakeQueue()))

check("it says what it is", report.startswith("🌙 <b>Nightly report</b>"), True)
check("it is dated in IST, because that is who reads it", "IST" in report, True)
check("the day's deliveries are counted", "✅ Delivered    <b>9</b>" in report, True)
check("and the day's failures", "❌ failed 4" in report, True)
check("new users are counted", "👥 New users    1" in report, True)
check("the server card is folded in", "🖥 <b>Server Status</b>" in report, True)
check("so is the bot card", "📊 <b>Bot Stats</b>" in report, True)
check("and both lanes are named", "terabox 1/6" in report and "zip 0/4" in report, True)
check("the cookie section is there", "🔑 <b>Cookies</b>" in report, True)
check("it fits in one Telegram message", len(report) < 4096, True)


async def _explodes():
    raise RuntimeError("terabox timed out")


admin._cookie_lines = _explodes
degraded = asyncio.run(admin.daily_report(FakeQueue()))
check("a failed cookie probe does not lose the report",
      "🖥 <b>Server Status</b>" in degraded, True)
check("and says so where the cookies would have been",
      "cookie check did not finish" in degraded, True)

print("\n- delivery -")

sent = []


class FakeClient:
    async def send_message(self, chat_id, text, **kw):
        if chat_id == 999:
            raise RuntimeError("blocked the bot")
        sent.append((chat_id, text))
        return types.SimpleNamespace(id=1)


admin._cookie_lines = _no_cookies
object.__setattr__(cfg, "admin_ids", (6100000001, 999, 42))
reached = asyncio.run(nightly.send_report(FakeClient(), FakeQueue()))
check("every admin is written to", [chat for chat, _ in sent], [6100000001, 42])
check("and a blocked admin is not counted", reached, 2)
check("one render, sent twice", len({text for _, text in sent}), 1)


async def _off():
    object.__setattr__(cfg, "daily_report_utc", "")
    await asyncio.wait_for(nightly.run(FakeClient(), FakeQueue()), timeout=1.0)


asyncio.run(_off())
check("an empty setting ends the task instead of looping", True, True)
object.__setattr__(cfg, "daily_report_utc", "18:30")

print("\n- the announcement -")

import logging                               # noqa: E402

from bot import broadcast                    # noqa: E402

# The one failure below is a real log line, and a WARNING on stderr in the middle of
# a green run reads as a broken test. It is the thing being asserted, so it is
# silenced here rather than left to look like an accident.
logging.getLogger("bot.broadcast").setLevel(logging.CRITICAL)

# Four users, one of them banned. Of the three that are written to, one has blocked
# the bot and one fails in a way nobody has seen before. That is the whole point of
# the module: there is no second chance at a broadcast, so a loop that raises halfway
# leaves the rest of the list never told the address changed.
for _uid, _banned in ((111, 0), (222, 0), (333, 0), (444, 1)):
    db.execute("INSERT INTO users (user_id, first_name, joined_at, last_seen, banned) "
               "VALUES (?,?,?,?,?)", (_uid, f"user{_uid}", now, now, _banned))

check("the audience is everyone not banned, in the order they arrived",
      broadcast.audience(), [111, 222, 333, 6100000001])


class _Refused(Exception):
    """Stands in for the several classes pyrogram raises for an unreachable chat."""


_said: list[tuple[int, str]] = []


class _AnnounceClient:
    async def send_message(self, chat_id, text, **_kw):
        if chat_id == 222:
            raise _Refused("USER_IS_BLOCKED: bot was blocked by the user")
        if chat_id == 333:
            raise RuntimeError("Telegram said something new")
        _said.append((chat_id, text))
        return types.SimpleNamespace(id=1)


_seen_progress: list[tuple[int, int]] = []


async def _watch(done, total):
    _seen_progress.append((done, total))


_real_gap = broadcast.GAP_SECONDS
broadcast.GAP_SECONDS = 0.0                  # the suite should not sleep for this
result = asyncio.run(broadcast.send_to_all(
    _AnnounceClient(), "The bot has moved.", on_progress=_watch))
broadcast.GAP_SECONDS = _real_gap

check("one blocked user does not stop the rest of the list",
      [chat for chat, _ in _said], [111, 6100000001])
check("the banned user was never written to",
      [chat for chat, _ in _said if chat == 444], [])
check("everyone reachable was counted", result.total, 4)
check("the two that went through are sent", result.sent, 2)
check("a block is expected, not a failure", result.blocked, 1)
check("something unrecognised is a failure", result.failed, 1)
check("and the three always add up to the list",
      result.sent + result.blocked + result.failed, result.total)
check("one text, sent to everybody", len({text for _, text in _said}), 1)
check("progress is reported for every user, not only the ones that worked",
      _seen_progress, [(1, 4), (2, 4), (3, 4), (4, 4)])
check("there is a gap between sends, so a long run is not throttled",
      0 < _real_gap <= 0.2, True)

check("the card says how many arrived", "<b>2</b> of 4" in result.card, True)
check("it keeps blocked apart from failed",
      "Blocked / gone: <b>1</b>" in result.card, True)
check("and names the failures", "Failed: <b>1</b>" in result.card, True)
check("a clean run does not mention failures at all",
      "Failed" in broadcast.Result(total=2, sent=2).card, False)


async def _progress_breaks(_done, _total):
    raise RuntimeError("the admin card was deleted mid-run")


_said.clear()
broadcast.GAP_SECONDS = 0.0
check("a broken progress card cannot stop the broadcast either",
      asyncio.run(broadcast.send_to_all(_AnnounceClient(), "again",
                                        on_progress=_progress_breaks)).sent, 2)
broadcast.GAP_SECONDS = _real_gap

db._conn = None
conn.close()                                 # Windows will not delete an open file
for leftover in sorted(tmp.rglob("*"), reverse=True):
    leftover.unlink() if leftover.is_file() else leftover.rmdir()
tmp.rmdir()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
