"""
Terabox link and response parsing.

The network half of the provider cannot be tested here — it needs a cookie and a
live API. What *can* be tested is everything that has ever actually broken:
which links are recognised, how the short-url key is dug out of five URL shapes,
and how a `/share/list` body is turned into files. The response bodies below are
the shapes Terabox returns, trimmed to the fields the provider reads.

Run: python tests/test_terabox.py
"""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")
# The handler refuses to charge for a provider that cannot run, so the door tests
# need a cookie to be present. Its value is never used: nothing here goes near the
# network.
os.environ.setdefault("TERABOX_COOKIE", "ndus=not-a-real-cookie")

# The provider module itself needs no pyrogram; base.py needs none either. The
# *handler* does, and `register()` evaluates whole filter expressions, so the
# stub carries a filter object that answers `&` and `~` by shrugging.
class _Stub:
    """Stands in for a Pyrogram type: remembers how it was built, does nothing."""

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class _Filter:
    """Enough of a filter for `filters.private & filters.text & ~x` to evaluate."""

    def __and__(self, _other):
        return self

    __rand__ = __or__ = __ror__ = __and__

    def __invert__(self):
        return self


_pyrogram = types.ModuleType("pyrogram")
_pyrogram.Client = type("Client", (_Stub,), {})
_pg_filters = types.ModuleType("pyrogram.filters")
for _name in ("private", "text", "document", "group"):
    setattr(_pg_filters, _name, _Filter())
for _name in ("create", "regex", "command"):
    setattr(_pg_filters, _name, lambda *_a, **_kw: _Filter())
_pg_types = types.ModuleType("pyrogram.types")
for _name in ("Message", "CallbackQuery",
              "InlineKeyboardButton", "InlineKeyboardMarkup"):
    setattr(_pg_types, _name, type(_name, (_Stub,), {}))
_pg_errors = types.ModuleType("pyrogram.errors")
_pg_errors.FloodWait = type("FloodWait", (Exception,), {"value": 0})
_pyrogram.filters, _pyrogram.types, _pyrogram.errors = (
    _pg_filters, _pg_types, _pg_errors)
sys.modules.setdefault("pyrogram", _pyrogram)
sys.modules.setdefault("pyrogram.filters", _pg_filters)
sys.modules.setdefault("pyrogram.types", _pg_types)
sys.modules.setdefault("pyrogram.errors", _pg_errors)

from bot.providers import terabox as tb                     # noqa: E402
from bot.providers.base import ResolveError                 # noqa: E402

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def raises(name, fn):
    global passed, failed
    try:
        fn()
    except ResolveError:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} — expected ResolveError")


# --- recorded response shapes ------------------------------------------------

ONE_FILE = {
    "errno": 0,
    "list": [
        {"server_filename": "Movie 1080p.mkv", "size": "1503238553",
         "fs_id": "812345678901234", "isdir": "0", "path": "/Movie 1080p.mkv",
         "dlink": "https://d.terabox.com/file/abc?fid=1&sign=xyz"},
    ],
}

MIXED = {
    "errno": 0,
    "list": [
        {"server_filename": "cover.jpg", "size": "84120", "fs_id": "1",
         "isdir": "0", "path": "/cover.jpg", "dlink": "https://d/1"},
        {"server_filename": "small.mp4", "size": "10485760", "fs_id": "2",
         "isdir": "0", "path": "/small.mp4", "dlink": "https://d/2"},
        {"server_filename": "notes.pdf", "size": "9001", "fs_id": "3",
         "isdir": "0", "path": "/notes.pdf", "dlink": "https://d/3"},
        {"server_filename": "big.MP4", "size": "2147483648", "fs_id": "4",
         "isdir": "0", "path": "/big.MP4", "dlink": "https://d/4"},
        {"server_filename": "Season 1", "size": "0", "fs_id": "5",
         "isdir": "1", "path": "/Season 1"},
    ],
}

EXPIRED_COOKIE = {"errno": -6, "request_id": 12345}
GONE = {"errno": -9, "list": []}
NO_LIST = {"errno": 0}

# What a signed-out session actually gets today, measured against a live share on
# 3 September 2026. Note the field: `code`, and no `errno` at all — reading only
# `errno` turned this into "no files found".
NEED_VERIFY = {"code": 460020, "errmsg": "need verify",
               "request_id": 9143944801021818863}
NEED_VERIFY_V2 = {"errno": 400210, "errmsg": "need verify_v2"}


# --- the door: who pays for the batch ----------------------------------------

def test_batch_charges_whoever_pressed_the_button():
    """
    A batch starts from a *button*, and the message a button hangs on was sent by
    the bot — so `cq.message.from_user` is the bot, never the human. Reading the
    id off it charged every link to the bot's own id, which owns no row in
    `users`, so `submit()` raised `ValueError: no such user: 7200000002` after
    writing the jobs row and before enqueuing anything. No credit moved, no
    worker ever saw the job, and the status message sat on "waiting for a free
    worker…" for ever. The whole Terabox flow had never worked once.

    Driven through the registered handlers, the real queue and a real (temporary)
    database — paste, then press — because what broke was the wiring between the
    callback handler and `credits.charge`. Calling `_queue_batch` directly with a
    correct id would have passed on the broken code too.
    """
    import asyncio
    import sqlite3
    import tempfile

    from bot import credits, db, state
    from bot.config import cfg
    from bot.handlers import terabox as handler
    from bot.queue import Queue

    path = Path(tempfile.mkdtemp(prefix="terabot-door-")) / "test.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn

    USER, BOT = 6100000001, 7200000002          # the human, and @videoextractkro_bot
    credits.ensure(USER, "the operator", "operator")
    opening = credits.balance(USER)

    class Sent:
        """Whatever a reply hands back. Records the last text and its buttons."""

        def __init__(self, text, markup=None):
            self.text, self.markup = text, markup

        async def edit_text(self, text, reply_markup=None, **_kw):
            self.text, self.markup = text, reply_markup

        def buttons(self):
            rows = self.markup.args[0] if self.markup else []
            return [b.kwargs.get("callback_data") for row in rows for b in row]

    class Chat:
        def __init__(self, text, sender):
            self.chat = types.SimpleNamespace(id=USER)
            self.from_user = types.SimpleNamespace(id=sender)
            self.text = text
            self.replies: list[Sent] = []

        async def reply_text(self, text, reply_markup=None, **_kw):
            self.replies.append(Sent(text, reply_markup))
            return self.replies[-1]

        async def edit_text(self, text, reply_markup=None, **_kw):
            self.text, self.markup = text, reply_markup

    class Press:
        """A CallbackQuery. `.message` is the bot's own message — the whole trap."""

        def __init__(self, data):
            self.data = data
            self.from_user = types.SimpleNamespace(id=USER)
            self.message = Chat("(the bot's own message)", BOT)
            self.answers: list[str] = []

        async def answer(self, text="", **_kw):
            self.answers.append(text)

    class FakeApp:
        """Collects whatever `register()` decorates, by function name."""

        def __init__(self):
            self.handlers = {}

        def _collect(self, *_a, **_kw):
            def keep(fn):
                self.handlers[fn.__name__] = fn
                return fn
            return keep

        on_callback_query = on_message = _collect

    class FakeClient:
        async def send_message(self, *_a, **_kw):
            pass

    links = ["https://terabox.com/s/1aaa", "https://terabox.com/s/1bbb"]
    client = FakeClient()
    queue = Queue(client, workers=1)            # never started: nothing runs them
    app = FakeApp()
    handler.register(app, queue)

    # 1. the human pastes the links — this message really is theirs
    pasted = Chat(" and ".join(links), USER)
    asyncio.run(app.handlers["got_links"](client, pasted))
    offer = pasted.replies[-1]
    check("the links are counted back", f"{len(links)} link(s) ready" in offer.text, True)
    go = [d for d in offer.buttons() if d.startswith("job:go:")]
    check("with one Start button", len(go), 1)

    # 2. the human presses Start. `cq.message` is now the *bot's* message.
    press = Press(go[0])
    asyncio.run(app.handlers["start_batch"](client, press))
    check("the press was acknowledged", press.answers, ["Starting…"])

    rows = db.query("SELECT * FROM jobs ORDER BY id")
    owners = sorted({r["user_id"] for r in rows})
    check("one job per link", len(rows), len(links))
    check("owned by the human who pressed Start", owners, [USER])
    check("and never by the bot", BOT in owners, False)
    check("both accepted, none failed at the door",
          sorted({r["status"] for r in rows}), ["queued"])
    check("both are waiting for a worker", queue.depth(), len(links))
    check("charged once per link",
          credits.balance(USER), opening - len(links) * cfg.cost_terabox_per_link)
    check("every link got its own status message plus one summary",
          len(press.message.replies), len(links) + 1)

    # The cancel token has to be parked under the human too, or Cancel silently
    # matches nobody — `cancel_job` looks it up with `state.peek(token, user_id)`.
    # Draining the queue is also the proof that the jobs were *enqueued*: under
    # the bug `submit()` raised before this line ever ran.
    queued = [queue._q.get_nowait() for _ in range(len(links))]
    check("each job carries a token owned by the human",
          [state.peek(j.token, USER) is not None for j in queued],
          [True] * len(links))
    check("and that token does not answer to the bot",
          [state.peek(j.token, BOT) for j in queued], [None] * len(links))
    check("each job kept its own link", [j.source for j in queued], links)

    # And the shape of the bug, so a revert cannot pass quietly: the id on the
    # message really is the bot's, and charging it really does raise.
    check("the message the button hangs on carries the bot's id",
          press.message.from_user.id, BOT)
    failure = None
    try:
        credits.charge(BOT, cfg.cost_terabox_per_link, "probe")
    except Exception as exc:                    # noqa: BLE001 — the type is the point
        failure = f"{type(exc).__name__}: {exc}"
    check("charging the bot's id is a hard error",
          failure, f"ValueError: no such user: {BOT}")

    # --- and with no cookie, the door charges nothing at all -----------------
    # Every link would fail on the worker and be refunded, so no money is lost
    # either way — but a credit that leaves and comes back looks like a bug to the
    # person watching, and this is the state the live bot is in until the operator
    # adds TERABOX_COOKIE.
    held = credits.balance(USER)
    rows_before = len(db.query("SELECT id FROM jobs"))
    object.__setattr__(cfg, "terabox_cookie", "")       # cfg is frozen
    try:
        again = Chat(links[0], USER)
        asyncio.run(app.handlers["got_links"](client, again))
        check("the user is told it is not set up",
              "not switched on" in again.replies[-1].text, True)
        check("no Start button is offered",
              [d for d in again.replies[-1].buttons() if d.startswith("job:go:")], [])
        check("no credit moved", credits.balance(USER), held)
        check("and no job row was written",
              len(db.query("SELECT id FROM jobs")), rows_before)

        press_dead = Press("mode:terabox")
        asyncio.run(app.handlers["open_terabox"](client, press_dead))
        check("the menu says so before asking for links",
              "not switched on" in press_dead.message.text, True)
    finally:
        object.__setattr__(cfg, "terabox_cookie", "ndus=not-a-real-cookie")

    conn.close()


def main():
    print("\n— which links are ours —")
    for url, want in [
        ("https://terabox.com/s/1abcDEF_-", True),
        ("https://www.terabox.com/s/1abc", True),
        ("https://1024terabox.com/s/1abc", True),
        ("https://www.4funbox.com/s/1abc", True),
        ("https://teraboxapp.com/sharing/link?surl=abcDEF", True),
        ("https://terabox.com/wap/share/filelist?surl=abc", True),
        ("https://terabox.com/", False),                     # no share key
        ("https://terabox.com.evil.io/s/1abc", False),        # look-alike host
        ("https://drive.google.com/file/d/1abc", False),
        ("ftp://terabox.com/s/1abc", False),                  # wrong scheme
        ("not a url at all", False),
    ]:
        check(f"matches({url[:44]}) = {want}", tb.terabox.matches(url), want)

    print("\n— digging out the short-url key —")
    check("/s/ path", tb.surl_of("https://terabox.com/s/1abcDEF"), "1abcDEF")
    check("surl= without the 1", tb.surl_of("https://x.com/x?surl=abcDEF"), "1abcDEF")
    check("surl= with the 1", tb.surl_of("https://x.com/x?surl=1abcDEF"), "1abcDEF")
    check("trailing query on /s/", tb.surl_of("https://terabox.com/s/1abc?pwd=zz"), "1abc")
    check("whitespace is trimmed", tb.surl_of("  https://terabox.com/s/1abc  "), "1abc")
    raises("a link with no key at all", lambda: tb.surl_of("https://terabox.com/hello"))

    print("\n— reading a file list —")
    items = tb.parse_list(ONE_FILE)
    check("one file comes back", len(items), 1)
    check("name", items[0].name, "Movie 1080p.mkv")
    check("size is an int", items[0].size, 1503238553)
    check("dlink kept", items[0].dlink.startswith("https://d.terabox.com/"), True)
    check("it is a video", items[0].is_video, True)

    items = tb.parse_list(MIXED)
    check("photos and pdfs dropped", [i.name for i in items if not i.is_dir],
          ["big.MP4", "small.mp4"])
    check("biggest first", items[0].name, "big.MP4")
    check("uppercase extension still a video", items[0].is_video, True)
    check("folders kept, and last", [i.name for i in items if i.is_dir], ["Season 1"])
    check("folder flagged", items[-1].is_dir, True)

    check("only_videos=False keeps everything",
          len(tb.parse_list(MIXED, only_videos=False)), 5)

    print("\n— what Terabox says when it says no —")
    raises("expired cookie (errno -6)", lambda: tb.parse_list(EXPIRED_COOKIE))
    raises("link gone (errno -9)", lambda: tb.parse_list(GONE))
    raises("not a dict at all", lambda: tb.parse_list("<html>login</html>"))
    try:
        tb.parse_list(EXPIRED_COOKIE)
    except ResolveError as exc:
        check("the -6 message names the cookie", "TERABOX_COOKIE" in str(exc), True)
        check("and stays plain text (it is html-escaped downstream)",
              "<" in str(exc), False)
    check("errno 0 with no list is empty, not an error", tb.parse_list(NO_LIST), [])
    raises("signed-out session (code 460020)", lambda: tb.parse_list(NEED_VERIFY))
    raises("signed-out session (errno 400210)", lambda: tb.parse_list(NEED_VERIFY_V2))
    for name, payload in (("code", NEED_VERIFY), ("errno", NEED_VERIFY_V2)):
        try:
            tb.parse_list(payload)
        except ResolveError as exc:
            check(f"the {name} form blames the cookie, not the link",
                  "TERABOX_COOKIE" in str(exc), True)

    print("\n— the HLS fallback url —")
    url = tb.hls_url("/some folder/big.MP4")
    check("path is percent-encoded", "%2Fsome%20folder%2Fbig.MP4" in url, True)
    check("capped at 1080", "M3U8_AUTO_1080" in url, True)
    check("carries the app id", f"app_id={tb.APP_ID}" in url, True)

    print("\n— pulling links out of a pasted blob —")
    blob = ("here you go https://terabox.com/s/1aaa and\n"
            "https://www.terabox.com/s/1bbb, plus https://terabox.com/s/1aaa again\n"
            "and https://youtube.com/watch?v=x which is not ours.")
    check("de-duplicated, punctuation stripped, ours only",
          tb.terabox.extract_links(blob),
          ["https://terabox.com/s/1aaa", "https://www.terabox.com/s/1bbb"])
    check("nothing in an empty message", tb.terabox.extract_links(""), [])

    print("\n— registered under its own name —")
    from bot.providers.base import REGISTRY, find_for
    check("in the registry", REGISTRY.get("terabox") is tb.terabox, True)
    check("found by url", find_for("https://terabox.com/s/1abc") is tb.terabox, True)
    check("not found for a foreign url", find_for("https://example.com/x"), None)

    print("\n— starting a batch charges the human, not the bot —")
    test_batch_charges_whoever_pressed_the_button()

    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
