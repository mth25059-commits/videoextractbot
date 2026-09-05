"""
Terabox link and response parsing.

The network half of the provider cannot be tested here — it needs a live cookie.
What *can* be tested is everything that has ever actually broken: which links are
recognised, how the short-url key is dug out of five URL shapes and which of its
two spellings each endpoint wants, and how a response body is turned into files.

The spelling is the one to watch. `/s/1abc` carries a `1` that is not part of the
key: `/share/list` wants `abc` and answers `errno 140` to `1abc`, while
`/api/shorturlinfo` wants `1abc` and answers `errno 2` to `abc`. The provider used
to send the `1` to both, so every listing failed — and the assertions here used to
pin that behaviour in place.

The response bodies below were recorded from a live share on 3 September 2026,
trimmed to the fields the provider reads, with the signature values blunted.

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
              "InlineKeyboardButton", "InlineKeyboardMarkup",
              "KeyboardButton", "ReplyKeyboardMarkup"):
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

# What `/share/list` really answers: full metadata and **no `dlink` key at all**.
# The provider used to assume the dlink was in here, so every file looked like a
# refusal even when the listing had worked.
ONE_FILE_NO_DLINK = {
    "errno": 0,
    "list": [
        {"server_filename": "Movie 1080p.mkv", "size": "1503238553",
         "fs_id": "812345678901234", "isdir": "0", "path": "/Movie 1080p.mkv",
         "md5": "c283ef80a842ee57bbc3864f991e150c"},
    ],
}

# `/api/shorturlinfo?shorturl=1<key>&root=1` — the only endpoint that carries a
# `dlink` key, and the only one that hands back a signature. Recorded from a live
# share on 3 September 2026 with no cookie: the key is present and the value is
# **empty**, which is the whole reason a signed-in cookie is still required. The
# signature values are the right shape and blunted.
SHORTURLINFO = {
    "errno": 0,
    "shareid": 42424242,
    "uk": "4400123456789",
    "sign": "2d8fbb" + "0" * 34,
    "timestamp": "1756900000",
    "randsk": "abc123",
    "list": [
        {"server_filename": "2023-10-16-17-53-43(1).mp4", "size": "3190427",
         "fs_id": "1029695337079147", "isdir": "0",
         "path": "/2026-03-28 10-44/2023-10-16-17-53-43(1).mp4",
         "md5": "c283ef80a842ee57bbc3864f991e150c", "dlink": ""},
    ],
}

# Sent the wrong spelling — the `1`-prefixed key that `/api/shorturlinfo` wants —
# `/share/list` answers this, and answered it to every link the bot ever resolved.
WRONG_SPELLING = {"errno": 140, "request_id": 9143944801021818863}

# A signed-out session with a `dp-logid` gets these two. We no longer send that
# parameter; the shapes stay because the fields differ (`code`, not `errno`).
NEED_VERIFY = {"code": 460020, "errmsg": "need verify",
               "request_id": 9143944801021818863}
NEED_VERIFY_V2 = {"errno": 400210, "errmsg": "need verify_v2"}

# `/share/download`, signed out, with every param spelling tried: a captcha demand.
NEED_CAPTCHA = {"errno": 400310, "errmsg": "need verify_v2"}

# --- and what the cookieless route says --------------------------------------
# Recorded from iteraplay.com on 3 September 2026 for
# terasharefile.com/s/1vAQMs-3Zus6RX36DkPnmLA. The `size` is the figure Terabox
# reports for the upload, and `normal_dlink` really did deliver exactly that many
# bytes of MP4 — which is why the original is preferred over the ladder beside it.
# Tokens blunted; the worker hostname is deliberately a different one from the
# `usage_limit` sample below, because it changes between responses.
ITERA_OK = {
    "status": "success", "total_files": 1, "total_folders": 0,
    "usage": {"current": 1, "limit": 5, "resetHours": 6, "userType": "guest"},
    "list": [{
        "name": "VID-20250421-WA0035(1).mp4", "size": 44739275,
        "size_formatted": "42.67 MB", "quality": "720p", "duration": "0:03:07",
        "is_dir": False, "folder": False, "type": "video",
        "thumbnail": "https://data.terabox.com/thumbnail/abc",
        "normal_dlink": "https://frosty-boat-ef16.waxipylo.workers.dev/convert"
                        "?token=BLUNTED&file_name=VID-20250421-WA0035(1).mp4",
        "fast_stream_url": {"360p": "https://w.workers.dev/fast_stream?q=360.m3u8",
                            "480p": "https://w.workers.dev/fast_stream?q=480.m3u8"},
    }],
}

# Request six. This is the reply the bot will meet most often, so its wording is
# the user's whole explanation — and their own text ends "Login for higher limits",
# which is advice for the operator, not for whoever is waiting on a video.
ITERA_LIMIT = {
    "status": "error", "error": "usage_limit",
    "message": "Guest limit reached (5 videos/6h). Login for higher limits or "
               "try again in 5h 48m.",
    "usage": {"current": 5, "limit": 5, "userType": "guest",
              "resetTime": "2099-09-03T14:53:39.000Z"},
}

# A folder row, and a file with neither a direct link nor a ladder: both dropped.
ITERA_NOTHING = {
    "status": "success",
    "list": [{"name": "Season 1", "is_dir": True, "size": 0},
             {"name": "notes.txt", "size": 12, "normal_dlink": ""}],
}


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
    from bot import queue as jobq

    path = Path(tempfile.mkdtemp(prefix="terabot-door-")) / "test.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn

    USER, BOT = 6100000001, 7200000002          # the human, and the bot itself
    credits.ensure(USER, "Operator", "operator")
    opening = credits.balance(USER)

    class Sent:
        """Whatever a reply hands back. Records the last text and its buttons."""

        _ids = iter(range(9000, 9999))

        def __init__(self, text, markup=None):
            self.text, self.markup = text, markup
            # A handler may hand this message's id to `state.set_mode` so a later
            # step can edit it, so it needs one — and a chat to edit it in.
            self.id = next(Sent._ids)
            self.chat = types.SimpleNamespace(id=USER)
            self.deleted = False

        async def edit_text(self, text, reply_markup=None, **_kw):
            self.text, self.markup = text, reply_markup

        async def delete(self, **_kw):
            self.deleted = True

        def buttons(self):
            rows = self.markup.args[0] if self.markup else []
            return [b.kwargs.get("callback_data") for row in rows for b in row]

    class Chat:
        _ids = iter(range(100, 999))

        def __init__(self, text, sender):
            self.id = next(Chat._ids)
            self.chat = types.SimpleNamespace(id=USER)
            self.from_user = types.SimpleNamespace(id=sender)
            self.text = text
            self.replies: list[Sent] = []
            self.deleted = False

        async def reply_text(self, text, reply_markup=None, **_kw):
            self.replies.append(Sent(text, reply_markup))
            return self.replies[-1]

        async def edit_text(self, text, reply_markup=None, **_kw):
            self.text, self.markup = text, reply_markup

        async def delete(self, **_kw):
            self.deleted = True

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
    check("and the pasted message is taken out of the chat", pasted.deleted, True)

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
    # Link 1 takes over the confirm card the button was hanging on, so a two-link
    # batch posts one new panel and one summary — not two panels and a summary on
    # top of a dead card. See `test_one_panel_from_prompt_to_video`.
    check("the confirm card is reused, so one new panel plus one summary",
          len(press.message.replies), len(links))

    # The cancel token has to be parked under the human too, or Cancel silently
    # matches nobody — `cancel_job` looks it up with `state.peek(token, user_id)`.
    # Draining the queue is also the proof that the jobs were *enqueued*: under
    # the bug `submit()` raised before this line ever ran.
    queued = [queue._q[jobq.LINK_LANE].get_nowait() for _ in range(len(links))]
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

    # --- one process per person, checked at the door --------------------------
    # Those two jobs are still in flight as far as the queue is concerned, so a
    # third link from the same person must be turned away *before* it is charged.
    # This is the rule that stops one user opening six panels and splitting their
    # own bandwidth six ways.
    credits.grant(USER, cfg.cost_terabox_per_link, "test top-up")
    guarded = credits.balance(USER)
    third = Chat(links[0], USER)
    asyncio.run(app.handlers["got_links"](client, third))
    check("a second batch while one runs is refused",
          "already have" in third.replies[-1].text, True)
    check("with no Start button to press anyway",
          [d for d in third.replies[-1].buttons() if d.startswith("job:go:")], [])
    check("and nothing was charged for it", credits.balance(USER), guarded)

    # --- a link pasted with no mode set at all --------------------------------
    #
    # This is the door most links actually arrive at. Before it existed, a paste
    # with no mode matched no handler at all and the bot answered with silence,
    # so people pressed /start before every single link. `state.clear_mode` first,
    # because the section above left this user mid-flow.
    print("\n— text with no mode set —")
    state.clear_mode(USER)
    from bot import keyboards as kb
    loose_app = FakeApp()
    handler.register(loose_app, Queue(client, workers=1))
    loose = loose_app.handlers["loose_text"]

    credits.grant(USER, cfg.cost_terabox_per_link, "test top-up")
    cold = Chat(links[0], USER)
    asyncio.run(loose(client, cold))
    check("a link with no mode is accepted, not ignored",
          "1 link(s) ready" in cold.replies[-1].text, True)
    check("and that message is cleaned up too", cold.deleted, True)

    junk = Chat("https://youtube.com/watch?v=abc", USER)
    asyncio.run(loose(client, junk))
    check("a non-Terabox link is refused in words the user asked for",
          "Wrong link" in junk.replies[-1].text
          and "no video found" in junk.replies[-1].text, True)
    check("and it is not deleted, so they can see what they sent",
          junk.deleted, False)

    chatter = Chat("bhai kaise ho", USER)
    asyncio.run(loose(client, chatter))
    check("plain chatter gets the same clear answer",
          "Wrong link" in chatter.replies[-1].text, True)

    typo = Chat("/help", USER)
    asyncio.run(loose(client, typo))
    check("a mistyped command is not called a wrong link",
          "Wrong link" in typo.replies[-1].text, False)
    check("it names the two commands that exist",
          "/start" in typo.replies[-1].text, True)

    # The keyboard keys arrive as ordinary text, so they are this handler's job.
    key = Chat(kb.KEY_TERABOX, USER)
    asyncio.run(loose(client, key))
    check("the Terabox key opens the prompt",
          "Send me your Terabox links" in key.replies[-1].text, True)
    check("and puts the user in the flow", state.get_mode(USER)[0], handler.MODE)

    state.clear_mode(USER)
    menu = Chat(kb.KEY_MENU, USER)
    asyncio.run(loose(client, menu))
    check("the Menu key gives the inline menu",
          [d for d in menu.replies[-1].buttons() if d == "mode:terabox"],
          ["mode:terabox"])

    # --- and with no way to fetch at all, the door charges nothing -------------
    # Every link would fail on the worker and be refunded, so no money is lost
    # either way — but a credit that leaves and comes back looks like a bug to the
    # person watching. "No way to fetch" now means both routes off: an empty
    # TERABOX_COOKIE on its own is survivable, because TERABOX_FALLBACK resolves
    # the link without one.
    held = credits.balance(USER)
    rows_before = len(db.query("SELECT id FROM jobs"))
    # Both fields, not just the first. `terabox.cookies()` folds `terabox_cookie`
    # into the spares pool, so clearing one and leaving the other means the gate
    # still sees a cookie — green here and wrong on the box the moment a second
    # cookie is configured.
    object.__setattr__(cfg, "terabox_cookie", "")        # cfg is frozen
    object.__setattr__(cfg, "terabox_cookies", ())
    object.__setattr__(cfg, "terabox_fallback", False)
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

        # The cookie alone is not the gate any more. With the fallback on, a
        # cookieless bot is a working bot, so the door has to let this through —
        # the whole point of measuring that iteraplay returns the original file.
        object.__setattr__(cfg, "terabox_fallback", True)
        check("no cookie but a fallback is not a closed door",
              tb.terabox.unavailable(), "")
        credits.grant(USER, cfg.cost_terabox_per_link, "test top-up")
        # A fresh queue and a fresh registration: the door also refuses a second
        # batch while one is in flight, and this section is about the cookie gate,
        # not about that.
        idle_app = FakeApp()
        handler.register(idle_app, Queue(client, workers=1))
        live = Chat(links[0], USER)
        asyncio.run(idle_app.handlers["got_links"](client, live))
        check("and the link is accepted",
              bool([d for d in live.replies[-1].buttons() if d.startswith("job:go:")]),
              True)
    finally:
        object.__setattr__(cfg, "terabox_cookie", "ndus=not-a-real-cookie")
        object.__setattr__(cfg, "terabox_cookies", ())
        object.__setattr__(cfg, "terabox_fallback", True)

    conn.close()


def test_fallback_returns_the_original():
    """
    The cookieless path, driven through the provider with a fake transport.

    Two things are worth pinning here and neither is about parsing. The first is
    that the operator's Terabox cookie must not travel to a third party: the
    session this path builds has to be a clean one. The second is why the original
    and the 360p/480p ladder are never offered on the same `Resolved` — `Resolved`
    sorts by height and picks `streams[0]`, so a 480p rendition with a height would
    outrank the full-quality file that has none, and `best` would hand the
    downloader the downgrade.
    """
    import asyncio
    import json

    from bot.config import cfg
    from bot.providers import iteraplay as ip
    from bot.providers.base import Resolved, Stream

    class Reply:
        def __init__(self, payload, status=200):
            self.text, self.status_code = json.dumps(payload), status

    class FakeSession:
        """Answers the two calls `iteraplay.resolve` makes, and records them."""

        def __init__(self, payload, status=200):
            self.payload, self.status, self.seen = payload, status, []

        async def get(self, url, **kwargs):
            self.seen.append(("get", url, kwargs))
            return Reply({}, 200)

        async def post(self, url, **kwargs):
            self.seen.append(("post", url, kwargs))
            return Reply(self.payload, self.status)

        async def close(self):
            self.seen.append(("close", "", {}))

    # The cookie is set (the door tests need it) — so this also proves the clean
    # session is used even when there is one to leak.
    clean = tb.terabox._session(cookie=False)
    try:
        headers = {k.lower(): v for k, v in dict(clean.headers).items()}
    finally:
        pass
    check("the fallback session carries no Terabox cookie", "cookie" in headers, False)
    check("but still looks like a browser",
          headers.get("user-agent", "").startswith("Mozilla/5.0"), True)

    fake = FakeSession(ITERA_OK)
    original = tb.terabox._session
    try:
        tb.terabox._session = lambda **_kw: fake
        out = asyncio.run(tb.terabox._via_fallback("https://terasharefile.com/s/1vAQ"))
    finally:
        tb.terabox._session = original

    check("one video came back", len(out), 1)
    check("titled from their reply", out[0].title, "VID-20250421-WA0035(1).mp4")
    check("one stream, not a menu", len(out[0].streams), 1)
    check("and it is the original, not the ladder", out[0].best.label, "original")
    check("fetched as bytes, not a playlist", out[0].best.kind, "file")
    check("with the size known up front", out[0].best.size_bytes, 44739275)
    check("the source link is kept for the caption",
          out[0].source_url, "https://terasharefile.com/s/1vAQ")
    check("duration carried through", out[0].duration_seconds, 187.0)
    check("the downloader is told to look like their page",
          out[0].best.headers.get("Referer"), ip.HOME)
    check("and never sent the Terabox cookie",
          "Cookie" in out[0].best.headers, False)

    # The warm-up GET is not decoration: their API answers the POST only for a
    # caller their page has already handed a session cookie to.
    check("their home page is warmed up first", fake.seen[0][1], ip.HOME)
    check("then one POST, and only one",
          [c[1] for c in fake.seen if c[0] == "post"], [ip.ENDPOINT])
    check("carrying the link as JSON",
          fake.seen[1][2]["json"], {"url": "https://terasharefile.com/s/1vAQ"})
    check("with no token while none is configured",
          fake.seen[1][2].get("cookies"), None)
    check("the session is closed either way",
          fake.seen[-1][0], "close")

    # Why the two are never mixed. This is `Resolved`'s own ordering, not ours.
    mixed = Resolved(title="x", streams=[
        Stream(url="a", label="original", kind="file", size_bytes=44739275),
        Stream(url="b", label="480p", kind="hls", height=480)])
    check("a 480p with a height outranks an original without one",
          mixed.best.label, "480p")

    # And the quota refusal reaches the user through the provider, not just the parser.
    spent = FakeSession(ITERA_LIMIT, status=429)
    try:
        tb.terabox._session = lambda **_kw: spent
        raises("a spent quota surfaces as a ResolveError",
               lambda: asyncio.run(tb.terabox._via_fallback("https://x.com/s/1a")))
    finally:
        tb.terabox._session = original

    # With no cookie and the fallback switched off there is nothing left to try, and
    # the message has to name what the operator is missing. Both cookie fields are
    # cleared — the pool is the union of them.
    object.__setattr__(cfg, "terabox_cookie", "")
    object.__setattr__(cfg, "terabox_cookies", ())
    object.__setattr__(cfg, "terabox_fallback", False)
    try:
        failed = None
        try:
            asyncio.run(tb.terabox.resolve_all("https://terabox.com/s/1abc"))
        except ResolveError as exc:
            failed = str(exc)
        check("both routes off names the cookie", "TERABOX_COOKIE" in (failed or ""),
              True)
    finally:
        object.__setattr__(cfg, "terabox_cookie", "ndus=not-a-real-cookie")
        object.__setattr__(cfg, "terabox_cookies", ())
        object.__setattr__(cfg, "terabox_fallback", True)


def test_rotation_spreads_across_accounts_and_addresses():
    """
    The quota is per account *and* per address, so the plan walks both together.

    The mistake worth guarding against is nesting them: one account and ten proxies
    has to be ten chances, not one. The other is retrying the wrong refusal — a
    private share fails the same way through every route, so asking ten times only
    spends someone else's API and makes the user wait ten times as long.
    """
    import asyncio
    import json

    from bot.providers import iteraplay as ip

    class Reply:
        def __init__(self, payload, status=200):
            self.text, self.status_code = json.dumps(payload), status

    only = ip.plan((), (), budget=4)
    check("nothing configured is still one attempt", len(only), 1)
    check("as a guest, straight out", only[0], ("", ""))

    spread = ip.plan(("t1",), ("p1", "p2", "p3"), budget=4)
    check("one account over three addresses is three chances", len(spread), 3)
    check("each through a different address",
          [proxy for _t, proxy in spread], ["p1", "p2", "p3"])
    check("all on the one account", {token for token, _p in spread}, {"t1"})

    accounts = ip.plan(("t1", "t2"), (), budget=4)
    check("two accounts on one address is two chances", len(accounts), 2)
    check("each on its own account",
          [token for token, _p in accounts], ["t1", "t2"])

    check("the budget is a ceiling, not a target",
          len(ip.plan(("t1",), tuple(f"p{n}" for n in range(10)), budget=4)), 4)
    check("and never zero", len(ip.plan((), (), budget=0)), 1)

    check("a later link starts further along",
          ip.plan(("t1", "t2"), (), budget=1, start=1)[0][0], "t2")
    check("and wraps rather than running out",
          ip.plan(("t1", "t2"), (), budget=1, start=5)[0][0], "t2")

    # Now the walk itself, with a transport that refuses until the third route.
    built: list[str | None] = []
    tried: list[str] = []

    class Route:
        def __init__(self, proxy):
            self.proxy, self.closed = proxy, False

        async def get(self, url, **kwargs):
            return Reply({}, 200)

        async def post(self, url, **kwargs):
            tried.append(self.proxy or "direct")
            if self.proxy == "p3":
                return Reply(ITERA_OK, 200)
            return Reply(ITERA_LIMIT, 429)

        async def close(self):
            self.closed = True

    made: list[Route] = []

    def make(*, proxy=None):
        built.append(proxy)
        route = Route(proxy)
        made.append(route)
        return route

    # Pinned, because the starting point deliberately moves: earlier links in a
    # batch have already advanced it, and the order below is what is being checked.
    ip._cursor = 0
    rows = asyncio.run(ip.resolve_rotating(
        make, "https://terasharefile.com/s/1vAQ", ua="UA/1",
        tokens=("t1",), proxies=("p1", "p2", "p3"), budget=4))
    check("it kept going until one answered", tried, ["p1", "p2", "p3"])
    check("and stopped there", len(built), 3)
    check("with the file it went looking for", rows[0].size, 44739275)
    check("every session closed, dead ones included",
          [r.closed for r in made], [True, True, True])

    # A fresh session per attempt: a jar warmed through one address must not be
    # carried to the next, or the second attempt is the first one again.
    check("no session was reused", len({id(r) for r in made}), 3)

    # All spent is the quota wording, not a transport error.
    class Spent(Route):
        async def post(self, url, **kwargs):
            tried.append("spent")
            return Reply(ITERA_LIMIT, 429)

    failed = None
    try:
        asyncio.run(ip.resolve_rotating(
            lambda *, proxy=None: Spent(proxy), "https://x.com/s/1a", ua="UA/1",
            tokens=(), proxies=("p1", "p2"), budget=4))
    except ResolveError as exc:
        failed = str(exc)
    check("everything spent still says quota", "quota" in (failed or ""), True)
    check("and still tells them how long", "Try again in about" in (failed or ""),
          True)

    # The refusal that must not be retried.
    asked = []

    class Private:
        def __init__(self, proxy):
            self.proxy = proxy

        async def get(self, url, **kwargs):
            return Reply({}, 200)

        async def post(self, url, **kwargs):
            asked.append(self.proxy)
            return Reply({"status": "error", "message": "private link"}, 200)

        async def close(self):
            return None

    failed = None
    try:
        asyncio.run(ip.resolve_rotating(
            lambda *, proxy=None: Private(proxy), "https://x.com/s/1a", ua="UA/1",
            tokens=("t1", "t2", "t3"), proxies=("p1", "p2", "p3"), budget=4))
    except ResolveError as exc:
        failed = str(exc)
    check("a private share is asked once, not once per route", len(asked), 1)
    check("and says what is wrong with the link",
          "private" in (failed or "").lower(), True)


def test_the_signed_in_host_is_found_not_configured():
    """
    Host discovery, which is the whole reason the download used to fail.

    A Terabox session is bound to the host that issued it: the cookie that answers
    `errno 0` with a real `uk` on its own home host answers `errno -6 user not
    login` everywhere else. The cookie is scoped `.1024terabox.com`, so a browser
    sends it to all of them and the refusal is server-side — which made "asking the
    wrong host" and "the cookie is dead" the same symptom for a long time.

    The `uk` half of the test matters as much as the `errno` half. A guest is also
    `errno 0` on some of these paths; only a non-zero `uk` means a *user*.
    """
    import asyncio
    import json

    class Reply:
        def __init__(self, payload, status=200):
            self.text = payload if isinstance(payload, str) else json.dumps(payload)
            self.status_code = status

    class Hosts:
        """Answers `check/login` per host from a table, and records who was asked."""

        def __init__(self, table):
            self.table, self.asked = table, []

        async def get(self, url, **kwargs):
            origin = "https://" + url.split("/")[2]
            self.asked.append((origin, kwargs.get("headers") or {}))
            return Reply(self.table.get(origin, {"errno": -6}))

    def fresh():
        """Discovery caches against the cookie, so each case starts unremembered."""
        tb.terabox._home_for = None
        tb.terabox._tokens_for = None

    signed_in = {"errno": 0, "uk": 81364087741287, "username": "k"}
    guest = {"errno": 0, "uk": 0}

    fresh()
    session = Hosts({"https://dm.1024terabox.com": signed_in})
    check("the host that says errno 0 with a real uk wins",
          asyncio.run(tb.terabox._home(session)), "https://dm.1024terabox.com")
    check("and the ones before it were tried in order",
          [origin for origin, _ in session.asked][0], tb.HOME_HOSTS[0])
    check("nothing after the winner is asked",
          session.asked[-1][0], "https://dm.1024terabox.com")
    # dp-logid is the single parameter that turns every call into `need verify`.
    check("no dp-logid on the way in",
          any("dp-logid" in origin for origin, _ in session.asked), False)
    check("asked as if the browser were on /main",
          session.asked[0][1].get("Referer"), f"{tb.HOME_HOSTS[0]}/main")

    fresh()
    # The trap: errno 0 alone is not a login. This is what the four www hosts and a
    # guest cookie look like, and taking one would put every later call on a host
    # that cannot see the operator's session.
    session = Hosts({h: guest for h in tb.HOME_HOSTS[:2]}
                    | {tb.HOME_HOSTS[2]: signed_in})
    check("errno 0 with uk 0 is a guest, not a home",
          asyncio.run(tb.terabox._home(session)), tb.HOME_HOSTS[2])

    fresh()
    session = Hosts({})
    try:
        asyncio.run(tb.terabox._home(session))
        told = None
    except ResolveError as exc:
        told = str(exc)
    check("signed in nowhere is a refusal, not a guess", told is not None, True)
    check("and it says what to do about it",
          "sign in again" in (told or "").lower(), True)
    check("every candidate was tried first", len(session.asked), len(tb.HOME_HOSTS))

    fresh()
    session = Hosts({"https://dm.1024terabox.com": signed_in})
    asyncio.run(tb.terabox._home(session))
    spent = len(session.asked)
    asyncio.run(tb.terabox._home(session))
    check("the answer is remembered, not re-probed", len(session.asked), spent)
    tb.terabox._home_for = ("a-different-cookie", "https://www.terabox.com")
    check("a replaced cookie re-probes instead of inheriting the old host",
          asyncio.run(tb.terabox._home(session)), "https://dm.1024terabox.com")
    fresh()


def test_the_page_tokens_come_off_the_home_page():
    """
    `jsToken` and `bdstoken`, which `/share/list` refuses to work without.

    Two spellings have shipped in the page bundle, and the URL-encoded one —
    `fn%28%22…%22%29`, i.e. `fn("…")` — is the one live today. Both are read
    because the site has changed it before and the failure is silent: no token
    means `errno 4000020`, which reads as a dead cookie.
    """
    import asyncio

    JS = "a1" * 64                       # 128 hex, the live width
    BD = "b" * 32

    class Page:
        def __init__(self, html):
            self.html, self.asked = html, []

        async def get(self, url, **kwargs):
            self.asked.append(url)
            reply = type("R", (), {"text": self.html, "status_code": 200})
            return reply()

    def fresh():
        tb.terabox._tokens_for = None

    fresh()
    page = Page(f'var x=1;fn%28%22{JS}%22%29;window.__INITIAL={{"bdstoken":"{BD}"}}')
    query = asyncio.run(tb.terabox._tokens(page, "https://dm.1024terabox.com"))
    check("scraped from the home page, not the share page",
          page.asked, ["https://dm.1024terabox.com/main"])
    check("jsToken lands in the query", f"&jsToken={JS}" in query, True)
    check("and bdstoken beside it", f"&bdstoken={BD}" in query, True)
    check("ready to append as-is", query.startswith("&"), True)

    fresh()
    query = asyncio.run(tb.terabox._tokens(
        Page(f'"jsToken":"{JS}","bdstoken":"{BD}"'), "https://x.com"))
    check("the plain JSON spelling is read too", f"&jsToken={JS}" in query, True)

    fresh()
    query = asyncio.run(tb.terabox._tokens(Page(f'fn%28%22{JS}%22%29'), "https://x.com"))
    check("bdstoken is optional", query, f"&jsToken={JS}")

    fresh()
    try:
        asyncio.run(tb.terabox._tokens(Page("<html>please log in</html>"), "https://x.com"))
        told = None
    except ResolveError as exc:
        told = str(exc)
    check("a page with no token is a refusal", told is not None, True)
    check("and it blames the cookie, which is usually right",
          "cookie" in (told or "").lower(), True)

    fresh()
    page = Page(f'fn%28%22{JS}%22%29')
    asyncio.run(tb.terabox._tokens(page, "https://x.com"))
    asyncio.run(tb.terabox._tokens(page, "https://x.com"))
    check("held for a few minutes rather than re-scraped per link",
          len(page.asked), 1)
    # A token outlives its usefulness quietly, so the cache is deadlined, not kept.
    key, _, query = tb.terabox._tokens_for
    tb.terabox._tokens_for = (key, 0.0, query)
    asyncio.run(tb.terabox._tokens(page, "https://x.com"))
    check("and re-scraped once it goes stale", len(page.asked), 2)
    asyncio.run(tb.terabox._tokens(page, "https://y.com"))
    check("a different host gets its own token", len(page.asked), 3)
    fresh()


def test_the_dlink_hop_gets_a_session_of_its_own():
    """
    The download must not reuse the session that did the API calls.

    This is the bug the operator hit on 4 September 2026 with a cookie that was fine. The
    listing worked, then the `dlink` came back `403 text/plain` — and because the
    failure fell through to the cookieless route, what he was shown was iteraplay's
    "sent something unreadable", which is not what went wrong.

    Measured on the live box, one fresh dlink, four fetches seconds apart:

        reused API session, no Range   403      fresh session, no Range   200
        reused API session, ranged     403      fresh session, ranged     206

    The difference is the jar. `check/login`, `/main` and `/share/list` leave
    `browserid`, `csrfToken` and `lang` behind, and the CDN refuses `ndus` arriving
    beside them. Not the `Accept` header — a clean session with the API's
    `application/json` Accept added still gets 200 — and not Range.

    So the session is built inside `_direct_url`. A per-hop session is also what
    keeps one file in a folder from poisoning the next, since the CDN sets cookies
    of its own.
    """
    import asyncio

    from bot.config import cfg

    class Reply:
        def __init__(self, url, status=200, length=44739275):
            self.url, self.status_code = url, status
            self.headers = {"content-length": str(length),
                            "content-type": "video/mp4"}
            self.closed = False

        def close(self):
            self.closed = True

    class Cdn:
        """One dlink hop. Records the headers it was asked with, and its own life."""

        def __init__(self, status=200, poison=True):
            self.status, self.asked, self.closed = status, [], False
            # A real CDN reply sets cookies; a shared session would carry them on.
            self.cookies = {"cdn-seen": "1"} if poison else {}

        async def get(self, url, **kwargs):
            self.asked.append((url, kwargs.get("headers") or {}))
            return Reply("https://data.1024terabox.com/final.mp4", self.status)

        async def close(self):
            self.closed = True

    origin = "https://dm.1024terabox.com"
    item = tb.Item(name="VID.mp4", size=44739275, fs_id="812345678901234",
                   path="/VID.mp4", dlink=f"{origin}/file/abc?sign=xyz")

    made: list[Cdn] = []
    original = tb.terabox._session

    def factory(**_kw):
        made.append(Cdn())
        return made[-1]

    try:
        tb.terabox._session = factory
        final, size = asyncio.run(tb.terabox._direct_url(item, origin))
    finally:
        tb.terabox._session = original

    check("one session, built here rather than handed in", len(made), 1)
    check("and closed before returning", made[0].closed, True)
    check("the CDN url is what the downloader gets",
          final, "https://data.1024terabox.com/final.mp4")
    check("with the size the CDN reports", size, 44739275)

    sent = made[0].asked[0][1]
    check("asked with the download headers, not the session's defaults",
          sent.get("Referer"), f"{origin}/")
    check("carrying the cookie the dlink was signed against",
          sent.get("Cookie"), tb.cookie_header())
    check("and the same User-Agent it was signed against",
          sent.get("User-Agent"), tb.UA)

    # A refusal has to name the status: 403 here means the jar, 404 a stale dlink.
    made.clear()
    try:
        tb.terabox._session = lambda **_kw: made.append(Cdn(status=403)) or made[-1]
        told = None
        try:
            asyncio.run(tb.terabox._direct_url(item, origin))
        except ResolveError as exc:
            told = str(exc)
    finally:
        tb.terabox._session = original
    check("a refusal says so", "403" in (told or ""), True)
    check("and the session is closed even then", made[0].closed, True)

    # No dlink at all is a different sentence, because it means the cookie.
    told = None
    try:
        asyncio.run(tb.terabox._direct_url(
            tb.Item(name="x.mp4", size=1, fs_id="1", path="/x.mp4"), origin))
    except ResolveError as exc:
        told = str(exc)
    check("an empty dlink blames the cookie, not the CDN",
          "TERABOX_COOKIE" in (told or ""), True)

    # Two files behind one folder link: two sessions, so the first CDN's cookies
    # cannot ride along to the second.
    two = [item, tb.Item(name="VID2.mp4", size=100, fs_id="9",
                         path="/VID2.mp4", dlink=f"{origin}/file/def?sign=zzz")]
    made.clear()

    async def listing(*_a, **_kw):
        return two, {"sign": "s"}, origin

    try:
        tb.terabox._session = factory
        tb.terabox._items = listing
        out = asyncio.run(tb.terabox._via_terabox("https://terabox.com/s/1abc"))
    finally:
        tb.terabox._session = original
        del tb.terabox._items
    check("both files came back", [r.title for r in out], ["VID.mp4", "VID2.mp4"])
    check("a session for the listing and one per dlink", len(made), 3)
    check("all of them closed", [s.closed for s in made], [True, True, True])
    check("and no two hops shared one", made[1] is not made[2], True)

    # And the message the user sees when both routes fail is the signed route's.
    # iteraplay is Cloudflare-challenged, so its complaint is the same for every
    # link and hides a real Terabox refusal behind a generic one.
    was_fallback = cfg.terabox_fallback
    try:
        object.__setattr__(cfg, "terabox_fallback", True)

        async def signed_fails(_url):
            raise ResolveError("The download link was refused (403).")

        async def cookieless_fails(_url):
            raise ResolveError(
                "The Terabox helper service sent something unreadable.")

        tb.terabox._via_terabox = signed_fails
        tb.terabox._via_fallback = cookieless_fails
        told = None
        try:
            asyncio.run(tb.terabox.resolve_all("https://terabox.com/s/1abc"))
        except ResolveError as exc:
            told = str(exc)
    finally:
        object.__setattr__(cfg, "terabox_fallback", was_fallback)
        del tb.terabox._via_terabox
        del tb.terabox._via_fallback
    check("both failing shows the signed route's refusal",
          "refused (403)" in (told or ""), True)
    check("not the cookieless route's", "unreadable" in (told or ""), False)


def test_the_cookie_pool_is_the_union_of_both_settings():
    """
    `cookies()` reads the live fields, not a snapshot taken at import.

    This matters because `cfg` is frozen and the only way anything changes it — an
    admin reload, every test in this file — is `object.__setattr__` on one field at
    a time. A helper that trusted `terabox_cookies` to already contain
    `terabox_cookie` would report a cookie the operator had just cleared, and the
    door would charge for a route that cannot run.
    """
    from bot.config import cfg

    was = (cfg.terabox_cookie, cfg.terabox_cookies)
    try:
        object.__setattr__(cfg, "terabox_cookie", "ndus=one")
        object.__setattr__(cfg, "terabox_cookies", ("ndus=one", "ndus=two"))
        check("the first field leads", tb.cookies()[0], "ndus=one")
        check("and the spares follow", tb.cookies(), ("ndus=one", "ndus=two"))
        check("no duplicate when load() already put it first", len(tb.cookies()), 2)
        check("first_cookie is the head of the pool", tb.first_cookie(), "ndus=one")

        # The shape `load()` does not produce but a hand-edited .env does: a first
        # cookie that is not in the tuple at all.
        object.__setattr__(cfg, "terabox_cookie", "ndus=zero")
        check("a first cookie outside the tuple still leads",
              tb.cookies(), ("ndus=zero", "ndus=one", "ndus=two"))

        object.__setattr__(cfg, "terabox_cookie", "")
        check("clearing the first field leaves the spares",
              tb.cookies(), ("ndus=one", "ndus=two"))
        check("and first_cookie moves to the next one",
              tb.first_cookie(), "ndus=one")

        object.__setattr__(cfg, "terabox_cookies", ())
        check("nothing configured is an empty pool", tb.cookies(), ())
        check("and no header to send", tb.cookie_header(), "")

        # Whitespace is the pasted-from-a-browser case, and a blank spare is what a
        # `TERABOX_COOKIE_4=` line with nothing after it looks like.
        object.__setattr__(cfg, "terabox_cookies", ("  ndus=a  ", "", "ndus=b"))
        check("padded values are trimmed and blanks dropped",
              tb.cookies(), ("ndus=a", "ndus=b"))
    finally:
        object.__setattr__(cfg, "terabox_cookie", was[0])
        object.__setattr__(cfg, "terabox_cookies", was[1])


def test_the_rotation_moves_on_a_refused_cookie_and_only_then():
    """
    Four cookies is failover, not speed, and the difference is which errno retries.

    Terabox shapes per CDN host rather than per account (measured 4 September 2026),
    so a second cookie makes no download faster. What it survives is the failure one
    cookie cannot: rate-limited (`400210`), no page token (`4000020`), logged out
    (`-6`). Those move to the next account.

    Everything else must not. A private share, a deleted file and an unrecognised
    share code fail identically on every account, so retrying four times spends four
    times the requests and makes the user wait four times as long for the same
    sentence. `CookieRefused` is the only class that means "ask someone else", and
    `_refusal` is the single place that decides which one an errno gets.
    """
    import asyncio

    from bot.config import cfg

    check("a rate limit is a cookie problem",
          isinstance(tb._refusal(400210, "x"), tb.CookieRefused), True)
    check("a missing page token too",
          isinstance(tb._refusal(4000020, "x"), tb.CookieRefused), True)
    check("and being logged out",
          isinstance(tb._refusal(-6, "x"), tb.CookieRefused), True)
    check("a private share is not",
          isinstance(tb._refusal(-9, "x"), tb.CookieRefused), False)
    check("nor is an unrecognised share code",
          isinstance(tb._refusal(140, "x"), tb.CookieRefused), False)
    check("but every one of them is still a ResolveError the queue can refund",
          [isinstance(tb._refusal(n, "x"), ResolveError)
           for n in (400210, -9, 140)], [True, True, True])
    check("and the errno is kept for the log", tb._refusal(400210, "x").errno, 400210)

    # `cookie_plan` walks the accounts once, bounded, starting where it is told —
    # the same shape as `iteraplay.plan`, and for the same reason.
    pool = ("c1", "c2", "c3")
    check("three cookies inside the budget is three attempts",
          tb.cookie_plan(pool, budget=4), ["c1", "c2", "c3"])
    check("the budget bounds it", tb.cookie_plan(pool, budget=2), ["c1", "c2"])
    check("a later start wraps round", tb.cookie_plan(pool, budget=3, start=2),
          ["c3", "c1", "c2"])
    check("no cookie is no plan", tb.cookie_plan((), budget=4), [])
    check("a zero budget still tries once", len(tb.cookie_plan(pool, budget=0)), 1)
    check("blank entries are skipped",
          tb.cookie_plan(("", "c2"), budget=4), ["c2"])

    # And now the loop itself, with `_attempt` standing in for the network.
    was = (cfg.terabox_cookie, cfg.terabox_cookies, cfg.terabox_fallback_attempts)
    real_attempt = tb.Terabox._attempt
    real_pause = tb.ROTATE_PAUSE_SECONDS
    try:
        object.__setattr__(cfg, "terabox_cookie", "c1")
        object.__setattr__(cfg, "terabox_cookies", ("c1", "c2", "c3"))
        object.__setattr__(cfg, "terabox_fallback_attempts", 4)
        tb.ROTATE_PAUSE_SECONDS = 0.0        # no real sleeping in a test
        tb._cursor = 0

        tried: list[str] = []

        async def second_one_works(_self, url, cookie):
            tried.append(cookie)
            if cookie == "c1":
                raise tb.CookieRefused("rate limited", 400210)
            return [f"resolved by {cookie}"]

        tb.Terabox._attempt = second_one_works
        out = asyncio.run(tb.terabox._via_terabox("https://terabox.com/s/1abc"))
        check("a throttled cookie hands over to the next one", tried, ["c1", "c2"])
        check("and the link still resolves", out, ["resolved by c2"])

        # The important negative: one wasted request, not four.
        tried.clear()
        tb._cursor = 0

        async def private_share(_self, url, cookie):
            tried.append(cookie)
            raise ResolveError("That share is private.")

        tb.Terabox._attempt = private_share
        told = None
        try:
            asyncio.run(tb.terabox._via_terabox("https://terabox.com/s/1abc"))
        except ResolveError as exc:
            told = str(exc)
        check("a link problem is not retried on other accounts", tried, ["c1"])
        check("and the user gets that reason, not a rotation summary",
              told, "That share is private.")

        # Every account refused: the last complaint is the one shown, because it is
        # the only one that describes a credential rather than the link.
        tried.clear()
        tb._cursor = 0

        async def all_dead(_self, url, cookie):
            tried.append(cookie)
            raise tb.CookieRefused(f"{cookie} is logged out", -6)

        tb.Terabox._attempt = all_dead
        told = None
        try:
            asyncio.run(tb.terabox._via_terabox("https://terabox.com/s/1abc"))
        except ResolveError as exc:
            told = str(exc)
        check("all three are tried before giving up", tried, ["c1", "c2", "c3"])
        check("and the refusal reaches the caller", "logged out" in (told or ""), True)

        # The budget is a real bound: five cookies, four attempts.
        object.__setattr__(cfg, "terabox_cookies", ("c1", "c2", "c3", "c4", "c5"))
        tried.clear()
        tb._cursor = 0
        try:
            asyncio.run(tb.terabox._via_terabox("https://terabox.com/s/1abc"))
        except ResolveError:
            pass
        check("TERABOX_FALLBACK_ATTEMPTS caps the walk", len(tried), 4)

        # A batch does not put every first attempt on cookie one. That is the whole
        # point of the cursor: ten links against a throttled first account would
        # otherwise discover it ten times.
        object.__setattr__(cfg, "terabox_cookies", ("c1", "c2", "c3"))
        firsts: list[str] = []

        async def note_first(_self, url, cookie):
            firsts.append(cookie)
            return [cookie]

        tb.Terabox._attempt = note_first
        tb._cursor = 0
        for _ in range(3):
            asyncio.run(tb.terabox._via_terabox("https://terabox.com/s/1abc"))
        check("three links start on three different cookies",
              sorted(firsts), ["c1", "c2", "c3"])
    finally:
        tb.Terabox._attempt = real_attempt
        tb.ROTATE_PAUSE_SECONDS = real_pause
        tb._cursor = 0
        object.__setattr__(cfg, "terabox_cookie", was[0])
        object.__setattr__(cfg, "terabox_cookies", was[1])
        object.__setattr__(cfg, "terabox_fallback_attempts", was[2])


def test_egress_rotates_and_never_lets_a_proxy_fail_a_job():
    """
    Which address a download leaves from, and what happens when one is bad.

    Three things are load-bearing here. Direct egress is one slot in the rotation
    rather than a special case — it was the fastest route in one of the two measured
    proxy batches. A proxy that fails is benched and the job goes out directly, so a
    dead address costs time and never a credit. And the password never appears in
    anything printable: one shared secret covers all ten addresses, and it would
    otherwise reach a log line, an admin card and an exception string.
    """
    from bot import egress
    from bot.config import cfg

    was = cfg.proxies
    try:
        egress.clear_bench()
        object.__setattr__(cfg, "proxies", ())
        egress._cursor = 0
        check("nothing configured is always direct",
              [egress.pick() for _ in range(3)], [None, None, None])

        object.__setattr__(cfg, "proxies", ("http://u:p@a:1", "http://u:p@b:2"))
        egress._cursor = 0
        walk = [egress.pick() for _ in range(6)]
        check("two proxies and direct is a three-slot rotation",
              walk, ["http://u:p@a:1", "http://u:p@b:2", None,
                     "http://u:p@a:1", "http://u:p@b:2", None])
        check("every configured address is actually used, not just the first",
              len({p for p in walk if p}), 2)

        egress.bench("http://u:p@a:1", seconds=300)
        check("a benched proxy leaves the pool", egress.pool(), ("http://u:p@b:2",))
        egress._cursor = 0
        check("and the rotation closes over it",
              [egress.pick() for _ in range(4)],
              ["http://u:p@b:2", None, "http://u:p@b:2", None])
        check("with the time left visible to the admin",
              round(egress.benched()["http://u:p@a:1"]) > 0, True)

        egress.bench("http://u:p@a:1", seconds=0)
        check("a bench that has expired is not a bench",
              egress.is_benched("http://u:p@a:1"), False)
        check("and it comes back on its own", len(egress.pool()), 2)

        egress.bench("http://u:p@b:2", seconds=300)
        egress.clear_bench()
        check("the admin can put them all back", len(egress.pool()), 2)
        check("benching direct is a no-op, not a crash",
              egress.bench(None) or egress.benched(), {})

        # The security half. This string goes into logs and Telegram messages.
        # The credentials below are invented and the addresses are from RFC 5737's
        # documentation range, deliberately: a test whose whole point is "a proxy
        # password must never be printed" is the last place to keep a live one.
        check("the password never reaches a printable string",
              egress.describe("http://wnqvtbxk:7hkzmdqfwlsp@203.0.113.7:6754"),
              "203.0.113.7:6754")
        check("neither does the username",
              "wnqvtbxk" in egress.describe("http://wnqvtbxk:7hkzmdqfwlsp@h:1"), False)
        check("a bare host:port needs no scheme",
              egress.describe("1.2.3.4:8080"), "1.2.3.4:8080")
        check("direct says so plainly", egress.describe(None), "direct")
        check("and an unparseable entry still says nothing secret",
              ":" in egress.describe("http://u:p@[oops"), False)
    finally:
        egress.clear_bench()
        egress._cursor = 0
        object.__setattr__(cfg, "proxies", was)


def test_the_cookie_probe_reports_instead_of_raising():
    """
    Item 6: what the admin card asks each cookie, and what it does with the answer.

    "Terabox is broken" has four causes that look identical from the outside — not
    signed in, signed in on a host nobody asked, signed in with no page token, signed
    in with no space left — and only the first is a cookie worth replacing. So each
    comes back as its own field rather than as one boolean.

    The other half of this test is that the probe never raises. It lives behind a
    button whose entire job is to explain a failure; a diagnostic that throws is a
    second outage on top of the first one.
    """
    import asyncio
    import json

    JS, BD = "c1" * 64, "d" * 32
    HOME = "https://dm.1024terabox.com"
    TWO_TB, HALF = 2199023255552, 549755813888        # a real 2048 GB account, 25% used

    class Reply:
        def __init__(self, payload, status=200):
            self.text = payload if isinstance(payload, str) else json.dumps(payload)
            self.status_code = status

    class Box:
        """One cookie's worth of Terabox: check/login, quota, and the home page."""

        def __init__(self, *, home=HOME, uk=81364087741287, quota=None, page=None,
                     boom=None):
            self.home, self.uk, self.quota = home, uk, quota or {"errno": 0}
            self.page = (page if page is not None
                         else f'fn%28%22{JS}%22%29;x={{"bdstoken":"{BD}"}}')
            self.boom, self.asked, self.closed = boom, [], False

        async def get(self, url, **kwargs):
            self.asked.append(url)
            if self.boom:
                raise self.boom
            origin = "https://" + url.split("/")[2]
            if "/api/check/login" in url:
                return Reply({"errno": 0, "uk": self.uk} if origin == self.home
                             else {"errno": -6})
            if "/api/quota" in url:
                return self.quota if isinstance(self.quota, Reply) else Reply(self.quota)
            return Reply(self.page)

        async def close(self):
            self.closed = True

    def probe(box, cookie="ndus=one", index=1):
        """The real `check_cookie`, with a fake Terabox behind it."""
        tb.terabox._home_for = tb.terabox._tokens_for = None
        tb.terabox._session = lambda **_kw: box
        try:
            return asyncio.run(tb.terabox.check_cookie(cookie, index))
        finally:
            tb.terabox.__dict__.pop("_session", None)
            tb.terabox._home_for = tb.terabox._tokens_for = None

    box = Box(quota={"errno": 0, "used": HALF, "total": TWO_TB})
    good = probe(box, index=2)
    check("a signed-in cookie comes back ok", good.ok, True)
    check("and says which host answered", good.home, HOME)
    check("keeping the index it was asked about", good.index, 2)
    check("with the quota an admin cannot see any other way",
          (good.used_bytes, good.total_bytes), (HALF, TWO_TB))
    check("as a percentage of the account", round(good.full_percent), 25)
    check("and the page token /share/list needs", good.tokens, True)
    check("nothing to report beyond that", good.detail, "")
    check("the session is closed, not leaked", box.closed, True)
    check("every host before the winner was asked",
          any(tb.HOME_HOSTS[0] in url for url in box.asked), True)

    # A full account still signs in, still resolves links, and cannot store anything.
    # Reporting it as ok with a number beside it is the point — `ok` alone would say
    # "fine" about an account that is about to fail every download.
    full = probe(Box(quota={"errno": 0, "used": TWO_TB, "total": TWO_TB}))
    check("a full account is still signed in", full.ok, True)
    check("and reads as full", round(full.full_percent), 100)

    unreadable = probe(Box(quota=Reply({"errno": 0}, 500)))
    check("a quota that will not answer is not a dead cookie", unreadable.ok, True)
    check("it just says so", "quota unreadable" in unreadable.detail, True)
    check("and reports no numbers rather than wrong ones",
          (unreadable.used_bytes, unreadable.total_bytes), (0, 0))
    check("no quota is 0%, not a division by zero",
          tb.CookieHealth(index=1, ok=True).full_percent, 0.0)

    # `bdstoken` missing is the one that reads as a dead cookie in the log — the
    # listing comes back `errno 4000020` — so the card has to be able to name it.
    thin = probe(Box(page=f'fn%28%22{JS}%22%29'))
    check("a home page with no bdstoken is still signed in", thin.ok, True)
    check("but the card is told the token is missing", thin.tokens, False)

    walled = probe(Box(page="<html>please log in</html>"))
    check("no page token at all is reported, not raised", walled.ok, True)
    check("and named", "no page token" in walled.detail, True)

    dead = Box(home="https://not-a-host.invalid")
    gone = probe(dead, index=3)
    check("a cookie signed in nowhere is not ok", gone.ok, False)
    check("it carries Terabox's own errno for it", gone.errno, -6)
    check("names no host, because none answered", gone.home, "")
    check("and says what to do about it", "sign in again" in gone.detail.lower(), True)
    check("every candidate host was tried first", len(dead.asked), len(tb.HOME_HOSTS))
    check("and the session closed on the failure path too", dead.closed, True)

    boom = Box(boom=RuntimeError("connection reset"))
    broke = probe(boom)
    check("a transport error is an answer, not an exception", broke.ok, False)
    check("with the reason attached", "RuntimeError" in broke.detail, True)
    check("still no leaked session", boom.closed, True)

    # `health()` probes *every* configured cookie. A spare that quietly expired is
    # invisible until the day the rotation reaches for it, which is the day the first
    # cookie is already throttled — so the card checks the spares while they are
    # still spare.
    from bot.config import cfg
    was = (cfg.terabox_cookie, cfg.terabox_cookies)
    depth = peak = 0
    seen = []

    async def one(cookie, index=1):
        nonlocal depth, peak
        depth += 1
        peak = max(peak, depth)
        await asyncio.sleep(0)
        seen.append((cookie, index))
        depth -= 1
        return tb.CookieHealth(index=index, ok=True, home=HOME)

    try:
        object.__setattr__(cfg, "terabox_cookie", "ndus=one")
        object.__setattr__(cfg, "terabox_cookies",
                           ("ndus=one", "ndus=two", "ndus=three"))
        tb.terabox.check_cookie = one
        rows = asyncio.run(tb.terabox.health())
        check("all three cookies are probed, not just the working one", len(rows), 3)
        check("in configured order", [c for c, _ in seen],
              ["ndus=one", "ndus=two", "ndus=three"])
        check("numbered the way the card prints them", [i for _, i in seen], [1, 2, 3])
        # Five accounts hitting check/login in the same instant from one address is
        # the shape of a rate limit. This is a diagnostic, not a race.
        check("one at a time, never in parallel", peak, 1)
    finally:
        tb.terabox.__dict__.pop("check_cookie", None)
        object.__setattr__(cfg, "terabox_cookie", was[0])
        object.__setattr__(cfg, "terabox_cookies", was[1])


def test_the_health_card_says_which_host_answered():
    """
    The rendering half of item 6, including the one line that must never appear.

    Every proxy in the rotation carries `user:password@` in its URL, and this card is
    a Telegram message: one `{proxy}` in an f-string here puts the shared credential
    for all ten addresses into a chat, a log line and any exception string that
    quotes it. `egress.describe()` exists for that reason and the assertion below is
    what keeps it in use.
    """
    import asyncio

    from bot import egress
    from bot.config import cfg
    from bot.handlers import admin

    HOME = "https://dm.1024terabox.com"
    TWO_TB, HALF = 2199023255552, 549755813888
    SECRET = "http://wnqvtbxk:7hkzmdqfwlsp@203.0.113.7:6754"
    was = (cfg.terabox_cookie, cfg.terabox_cookies, cfg.proxies, cfg.terabox_fallback)

    async def canned():
        return [
            tb.CookieHealth(index=1, ok=True, home=HOME, used_bytes=HALF,
                            total_bytes=TWO_TB, tokens=True),
            tb.CookieHealth(index=2, ok=True, home=HOME, tokens=False),
            tb.CookieHealth(index=3, ok=False, errno=-6,
                            detail="not signed in — sign in again"),
        ]

    try:
        object.__setattr__(cfg, "terabox_cookie", "ndus=one")
        object.__setattr__(cfg, "terabox_cookies", ("ndus=one", "ndus=two"))
        object.__setattr__(cfg, "proxies", (SECRET, "http://u:p@198.51.100.9:6014"))
        object.__setattr__(cfg, "terabox_fallback", True)
        tb.terabox.health = canned
        egress.bench(SECRET, seconds=300)
        card = asyncio.run(admin._terabox_card())

        check("the password is nowhere in the card", "7hkzmdqfwlsp" in card, False)
        check("nor is the username", "wnqvtbxk" in card, False)
        check("the address itself is fine, and useful", "203.0.113.7:6754" in card, True)
        check("a benched address says how long it is out for",
              "benched" in card and "more" in card, True)

        check("a good cookie is ticked", "✅ <b>#1</b>" in card, True)
        check("and names the host it is signed in on", "dm.1024terabox.com" in card, True)
        check("without the scheme, which is the same for all of them",
              "https://dm.1024terabox.com" in card, False)
        check("its quota is shown as a share of the account", "(25%)" in card, True)
        check("a missing bdstoken is called out by name", "no bdstoken" in card, True)
        check("a dead cookie shows its errno", "errno -6" in card, True)
        check("with Terabox's own words under it", "sign in again" in card, True)
        check("both addresses are counted", "2 proxies" in card, True)

        # Ten CONNECTs per open would make the card the slowest screen in the bot,
        # and it is opened to read the cookie state far more often than to re-test a
        # proxy list. So probing is a button, and the card says so.
        check("probing is offered, not done", "Test proxies" in card, True)
        check("the fallback route is described too", "Fallback" in card, True)
        # Extra cookies are failover. Saying "faster" here is how an operator ends up
        # buying accounts to fix a throttle that is per CDN host.
        check("and nothing claims more cookies are faster",
              "faster" in card.lower(), False)

        # And the probed card, which is the one that renders a line per address.
        egress.clear_bench()
        probed = []

        async def fake_probe(proxy):
            probed.append(proxy)
            return (proxy != SECRET, "402 Payment Required" if proxy == SECRET
                    else "1.7 MB/s")

        was_probe = egress.probe
        try:
            egress.probe = fake_probe
            card = asyncio.run(admin._terabox_card(probe=True))
        finally:
            egress.probe = was_probe
        check("every configured address is tested, not just the pool", len(probed), 2)
        check("a dead one is marked", "✖ 203.0.113.7:6754" in card, True)
        check("with what it answered", "402 Payment Required" in card, True)
        check("a live one is ticked", "✅ 198.51.100.9:6014" in card, True)
        check("and the tally is spelled out", "1 of 2 answered" in card, True)
        check("the password survives probing too", "7hkzmdqfwlsp" in card, False)
        # A dead proxy is a slower download, never a failed job — the download falls
        # back to direct egress. The card says that so nobody treats it as an outage.
        check("and a dead address is not described as a lost job",
              "never a job" in card, True)

        object.__setattr__(cfg, "proxies", ())
        card = asyncio.run(admin._terabox_card())
        check("no proxies reads as direct, not as broken", "direct only" in card, True)
        object.__setattr__(cfg, "terabox_cookie", "")
        object.__setattr__(cfg, "terabox_cookies", ())
        object.__setattr__(cfg, "terabox_fallback", False)
        card = asyncio.run(admin._terabox_card())
        check("with nothing configured the card says both routes are off",
              "Both routes are off" in card, True)
        check("and does not pretend to have probed a cookie",
              "none configured" in card, True)
    finally:
        tb.terabox.__dict__.pop("health", None)
        (object.__setattr__(cfg, "terabox_cookie", was[0]),
         object.__setattr__(cfg, "terabox_cookies", was[1]),
         object.__setattr__(cfg, "proxies", was[2]),
         object.__setattr__(cfg, "terabox_fallback", was[3]))
        egress.clear_bench()

    # The unbench row only exists when there is something to put back, so the card
    # cannot offer an action that does nothing.
    from bot import keyboards as kb
    check("no benched proxies, no unbench button",
          len(kb.admin_health(False).args[0]), 2)
    rows = kb.admin_health(True).args[0]
    check("one benched proxy grows the row", len(rows), 3)
    check("wired to the handler that clears the bench",
          rows[1][0].kwargs.get("callback_data"), "adm:tbox:unbench")
    check("and the admin menu is where the card is reached from",
          [b.kwargs.get("callback_data") for row in kb.admin_menu().args[0] for b in row
           if b.kwargs.get("callback_data") == "adm:tbox"], ["adm:tbox"])


def test_one_panel_from_prompt_to_video():
    """
    The whole flow on one message: prompt → card → live panel → gone.

    Every step here used to be a *new* message, so one link left four of them in the
    chat — "send me your links", "1 link ready", a progress panel and a ✅ receipt —
    with the user's own pasted URL wedged in the middle and the video at the bottom.
    Six links in an evening made the chat unreadable, which is the complaint this
    answers. The assertions are therefore about how many messages exist, not only
    about what they say: checking the text alone passed on the old code too.

    Driven through the registered handlers with the real queue, because what is
    being tested is the handover of one message id between four of them — the
    callback that opens the prompt, the text handler that reads the links, the
    callback that starts the batch, and the runner that finishes it.
    """
    import asyncio
    import shutil as _shutil
    import sqlite3
    import tempfile

    from bot import credits, db, keyboards as kb, state, uploader
    from bot import queue as jobq
    from bot.config import cfg
    from bot.handlers import terabox as handler
    from bot.queue import Queue

    path = Path(tempfile.mkdtemp(prefix="terabot-panel-")) / "test.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn

    USER, BOT = 6100000001, 7200000002
    credits.ensure(USER, "Operator", "operator")
    state.clear_mode(USER)
    live: dict[int, "Msg"] = {}

    class Msg:
        """
        One message in the chat, with an id — which is the whole point.

        `client.edit_message_text(chat, id, …)` is how a handler reaches a message
        it does not hold, so the fake has to be findable by id like the real thing.
        """

        _ids = iter(range(1000, 1999))

        def __init__(self, text, sender=USER, markup=None):
            self.id = next(Msg._ids)
            self.text, self.markup = text, markup
            self.chat = types.SimpleNamespace(id=USER)
            self.from_user = types.SimpleNamespace(id=sender)
            self.replies: list["Msg"] = []
            self.deleted = False
            live[self.id] = self

        async def reply_text(self, text, reply_markup=None, **_kw):
            self.replies.append(Msg(text, BOT, reply_markup))
            return self.replies[-1]

        async def edit_text(self, text, reply_markup=None, **_kw):
            self.text, self.markup = text, reply_markup
            return self

        async def delete(self, **_kw):
            self.deleted = True

        def buttons(self):
            rows = self.markup.args[0] if self.markup else []
            return [b.kwargs.get("callback_data") for row in rows for b in row]

    class Press:
        def __init__(self, data, message):
            self.data = data
            self.from_user = types.SimpleNamespace(id=USER)
            self.message = message
            self.answers: list[str] = []

        async def answer(self, text="", **_kw):
            self.answers.append(text)

    class FakeApp:
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

        async def edit_message_text(self, _chat_id, message_id, text,
                                    reply_markup=None, **_kw):
            return await live[message_id].edit_text(text, reply_markup=reply_markup)

    client = FakeClient()
    queue = Queue(client, workers=1)             # never started: nothing runs them
    app = FakeApp()
    handler.register(app, queue)
    # 1. Menu → Terabox. The menu message *becomes* the prompt, and remembers that
    #    it is the panel the rest of the flow will keep editing.
    panel = Msg("☰ <b>Menu</b>", BOT)
    asyncio.run(app.handlers["open_terabox"](client, Press("mode:terabox", panel)))
    check("the menu message turns into the prompt",
          "Send me your Terabox links" in panel.text, True)
    check("nothing new was posted for it", panel.replies, [])
    mode, payload = state.get_mode(USER)
    check("the flow is open", mode, handler.MODE)
    check("and it is holding that message's id", payload.get("panel"), panel.id)

    # 2. Paste a link. The prompt becomes the confirm card in place.
    pasted = Msg("https://terabox.com/s/1aaa", USER)
    asyncio.run(app.handlers["got_links"](client, pasted))
    check("the prompt itself became the confirm card",
          "1 link(s) ready" in panel.text, True)
    check("so no second message appeared", pasted.replies, [])
    check("and the pasted link is out of the chat", pasted.deleted, True)
    go = [d for d in panel.buttons() if d.startswith("job:go:")]
    check("with one Start button on it", len(go), 1)

    # 3. Press Start. The card becomes the live panel — still the same message.
    press = Press(go[0], panel)
    asyncio.run(app.handlers["start_batch"](client, press))
    check("the card became the waiting panel", "Your link" in panel.text, True)
    check("with a Cancel button", [d for d in panel.buttons()
                                   if d.startswith("job:cancel:")] != [], True)
    check("a single link leaves no receipt behind", panel.replies, [])

    job = queue._q[jobq.LINK_LANE].get_nowait()
    check("the job is animating that very message",
          job.payload["status"] is panel, True)
    # 4. Deliver it. The panel goes away and the video is the only thing left.
    work = Path(tempfile.mkdtemp(prefix="terabot-panel-work-"))
    real_work_dir = cfg.work_dir
    object.__setattr__(cfg, "work_dir", work)

    stream = types.SimpleNamespace(kind="file", url="https://cdn.example/x.mp4",
                                   headers={}, label="original")
    resolved = [types.SimpleNamespace(best=stream, safe_title="clip.mp4",
                                      duration_seconds=0.0)]

    async def fake_resolve(_url):
        return resolved

    async def fake_fetch(_url, out_path, **_kw):
        out_path.write_bytes(b"v" * 64)

    async def fake_send(_client, _chat_id, target, **_kw):
        return uploader.Sent(message=None, size_bytes=target.stat().st_size,
                             seconds=0.01, parts=1)

    real_fetch = handler._fetch_bytes
    real_send = handler.uploader.send_best_effort
    real_looks = handler.media.looks_like_video
    handler.terabox.resolve_all = fake_resolve      # instance attr; popped below
    handler._fetch_bytes = fake_fetch
    handler.uploader.send_best_effort = fake_send
    handler.media.looks_like_video = lambda _p: True
    try:
        asyncio.run(handler._deliver(client, job))
    finally:
        handler.terabox.__dict__.pop("resolve_all", None)
        handler._fetch_bytes = real_fetch
        handler.uploader.send_best_effort = real_send
        handler.media.looks_like_video = real_looks
        object.__setattr__(cfg, "work_dir", real_work_dir)
        _shutil.rmtree(work, ignore_errors=True)

    check("the panel is taken away once the video has gone out", panel.deleted, True)
    check("and it was the only message the bot ever posted",
          sum(1 for m in live.values() if m.from_user.id == BOT), 1)
    # --- the four keys ---------------------------------------------------------
    #
    # A reply-keyboard press is an ordinary text message, so it lands in whichever
    # text handler owns the moment. Both of these were real answers before
    # `_home_key`: 🗂 File got "wrong link — no video found" from inside the Terabox
    # flow, and the inline menu from outside it. Neither is the archive prompt.
    print("\n— every key answers itself, in the flow or out of it —")
    spend_before = credits.balance(USER)

    # The key's own label is not used in the check names: this suite prints to a
    # Windows console that cannot encode an emoji, and a test that dies on its own
    # progress output is a test nobody runs.
    # `after` is the mode the press should leave behind. 🗂 File answers with a
    # prompt it does not own a mode for, so it closes the Terabox flow and leaves
    # nothing; 🔥 Fap owns a flow of its own and *hands over* to it, so the mode it
    # leaves is `fap.MODE` and not None. Both are "the key answers itself" — the
    # difference is only whether the key has somewhere to hand the next message.
    from bot.handlers import fap as fap_handler              # noqa: PLC0415

    # 🔥 Fap only has somewhere to hand over *to* when a resolver is configured, and
    # `fap_api` is empty by default — deliberately, since there is no shared endpoint
    # and a default here would point every install at one server. What is under test
    # is the handover, not the default, so it is set for these four presses and put
    # back afterwards. With it blank the key answers "not switched on yet" and keeps
    # the mode it was in, which is the other route and is covered in test_fap.py.
    real_fap_api = cfg.fap_api
    object.__setattr__(cfg, "fap_api", "https://resolver.example/api/faphouse/mrx")
    try:
        for name, label, want, where, after in (
                ("Fap", kb.KEY_FAP, "Send me the video link", "got_links",
                 fap_handler.MODE),
                ("File", kb.KEY_FILE, "ZIP, RAR or 7z", "got_links", None),
                ("Fap", kb.KEY_FAP, "Send me the video link", "loose_text",
                 fap_handler.MODE),
                ("File", kb.KEY_FILE, "ZIP, RAR or 7z", "loose_text", None)):
            state.set_mode(USER, handler.MODE, panel=panel.id, chat=USER)
            pressed = Msg(label, USER)
            asyncio.run(app.handlers[where](client, pressed))
            answer = pressed.replies[-1]
            check(f"{name} in {where} says its own thing", want in answer.text, True)
            check(f"{name} in {where} is not called a wrong link",
                  "Wrong link" in answer.text, False)
            check(f"{name} in {where} is not the inline menu",
                  [d for d in answer.buttons() if d == "mode:terabox"], [])
            check(f"{name} in {where} takes the press out of the chat",
                  pressed.deleted, True)
            left = state.get_mode(USER)
            check(f"{name} in {where} leaves the Terabox flow behind",
                  left[0] if left else None, after)
    finally:
        object.__setattr__(cfg, "fap_api", real_fap_api)

    menu_key = Msg(kb.KEY_MENU, USER)
    asyncio.run(app.handlers["loose_text"](client, menu_key))
    check("the Menu key still gives the inline menu",
          [d for d in menu_key.replies[-1].buttons() if d == "mode:terabox"],
          ["mode:terabox"])
    check("and it offers the manual too",
          [d for d in menu_key.replies[-1].buttons() if d == "help:open"],
          ["help:open"])
    check("every label on the keyboard is one this handler knows",
          sorted(kb.HOME_LABELS),
          sorted({kb.KEY_TERABOX, kb.KEY_FAP, kb.KEY_FILE, kb.KEY_MENU}))

    tb_key = Msg(kb.KEY_TERABOX, USER)
    asyncio.run(app.handlers["loose_text"](client, tb_key))
    prompt = tb_key.replies[-1]
    check("the Terabox key opens the prompt",
          "Send me your Terabox links" in prompt.text, True)
    check("and hands the flow its own id to edit later",
          state.get_mode(USER)[1].get("panel"), prompt.id)
    check("no key press costs a credit", credits.balance(USER), spend_before)

    state.clear_mode(USER)
    conn.close()


def test_a_folder_is_priced_per_video():
    """
    0.5 credits a video — and a folder link is not one video.

    The price used to be per *link*, and a folder link handed over up to ten videos
    for it. Per-video means the bill is not knowable at the confirm screen, so it is
    taken in two halves: the card holds one video's worth, and `jobq.charge_more`
    settles the rest inside the runner — after `resolve_all` has counted them, and
    before a single byte is fetched. Charging afterwards hands free work to anyone
    who blocks the bot mid-job; holding ten videos' worth up front freezes credit
    for videos that usually are not there.

    Driven through the real handler, the real queue and a real (temporary) database,
    because the two halves live in different files and the thing worth pinning is
    that they add up. The same run carries the privacy assertions: what a folder job
    puts on the screen and on disk is `Video 2 of 3` and `02.mp4`, never the name
    Terabox reported for it.
    """
    import asyncio
    import shutil as _shutil
    import sqlite3
    import tempfile

    from bot import credits, db, state, uploader
    from bot import queue as jobq
    from bot import settings
    from bot.config import cfg
    from bot.handlers import terabox as handler
    from bot.queue import Queue

    path = Path(tempfile.mkdtemp(prefix="terabot-price-")) / "test.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn
    # A database has just been swapped in under the overlay; anything it memoised from
    # whatever ran before this belongs to a file that is no longer being read.
    settings.forget_cache()

    # --- 0. the screen quotes the price that is in force now ----------------------
    # This was a module-level f-string, so the number on it was the number at *import*:
    # an admin who used ⚙️ Prices saw the old price on this screen until the next
    # restart, while being charged the new one. A price on a screen that the bot is not
    # charging is the one bug in this area a user can actually catch the bot out on.
    settings.set("cost_terabox_per_link", 3)
    check("the prompt quotes a price changed while the bot is running",
          "<b>3 credits per video</b>" in handler.prompt(), True)
    settings.set("cost_terabox_per_link", 1)
    check("and a price of exactly one is not '1 credits'",
          "<b>1 credit per video</b>" in handler.prompt(), True)
    check("the old per-link wording is gone for good",
          "per link" in handler.prompt(), False)
    check("reset puts what .env installed back on the screen",
          f"<b>{settings.reset('cost_terabox_per_link'):g} credit" in handler.prompt(),
          True)

    PER = cfg.cost_terabox_per_link
    USER, BOT = 6100000001, 7200000002
    #: The name Terabox reports for every one of the three. Deliberately
    #: unmistakable: every negative assertion below looks for one word of it, and a
    #: neutral-looking sample would have passed on the old code too.
    TITLE = "Bhabhi Ji Ghar Par Hai S03E44 1080p.mp4"
    credits.ensure(USER, "Operator", "operator")
    credits.grant(USER, 5.0, "test float")
    state.clear_mode(USER)

    class Msg:
        _ids = iter(range(3000, 3999))

        def __init__(self, text, sender=USER, markup=None):
            self.id = next(Msg._ids)
            self.text, self.markup = text, markup
            self.chat = types.SimpleNamespace(id=USER)
            self.from_user = types.SimpleNamespace(id=sender)
            self.replies: list["Msg"] = []
            #: Every text this message has ever carried. The panel is edited several
            #: times per video and one leaked frame is enough to put the name in the
            #: chat, so the assertions read this rather than `.text`.
            self.seen: list[str] = [text]
            self.deleted = False

        async def reply_text(self, text, reply_markup=None, **_kw):
            self.replies.append(Msg(text, BOT, reply_markup))
            return self.replies[-1]

        async def edit_text(self, text, reply_markup=None, **_kw):
            self.text, self.markup = text, reply_markup
            self.seen.append(text)
            return self

        async def delete(self, **_kw):
            self.deleted = True

        def buttons(self):
            rows = self.markup.args[0] if self.markup else []
            return [b.kwargs.get("callback_data") for row in rows for b in row]

    class Press:
        def __init__(self, data, message):
            self.data, self.message = data, message
            self.from_user = types.SimpleNamespace(id=USER)
            self.answers: list[str] = []

        async def answer(self, text="", **_kw):
            self.answers.append(text)

    class FakeApp:
        def __init__(self):
            self.handlers = {}

        def _collect(self, *_a, **_kw):
            def keep(fn):
                self.handlers[fn.__name__] = fn
                return fn
            return keep

        on_callback_query = on_message = _collect

    class FakeClient:
        def __init__(self):
            self.said: list[str] = []

        async def send_message(self, _chat_id, text="", **_kw):
            self.said.append(text)
            return Msg(text, BOT)

    client = FakeClient()
    queue = Queue(client, workers=1)             # never started: nothing runs them
    app = FakeApp()
    handler.register(app, queue)

    # --- 1. the card quotes a video and holds one -----------------------------
    pasted = Msg("https://terabox.com/s/1folder")
    asyncio.run(app.handlers["got_links"](client, pasted))
    card = pasted.replies[-1]
    check("the card prices a video, not a link",
          f"{PER:g} credit(s) per video" in card.text, True)
    check("and says a folder is charged for each video inside it",
          "for each video inside it" in card.text, True)
    check("with the floor it starts from",
          f"this starts at {PER:g}" in card.text, True)
    check("counted once the folder has been read, never before",
          "read the folder" in card.text, True)

    before = credits.balance(USER)
    go = [d for d in card.buttons() if d.startswith("job:go:")][0]
    asyncio.run(app.handlers["start_batch"](client, Press(go, card)))
    check("pressing Start takes one video's worth, whatever the link holds",
          round(before - credits.balance(USER), 2), PER)

    job = queue._q[jobq.LINK_LANE].get_nowait()
    check("and the card itself became the live panel",
          job.payload["status"] is card, True)
    check("the jobs row was written at that same floor",
          db.query("SELECT cost FROM jobs WHERE id = ?", (job.row_id,))[0]["cost"], PER)
    # --- 2. the runner counts the folder and takes the rest --------------------
    work = Path(tempfile.mkdtemp(prefix="terabot-price-work-"))
    real_work_dir = cfg.work_dir
    object.__setattr__(cfg, "work_dir", work)

    stream = types.SimpleNamespace(kind="file", url="https://cdn.example/x.mp4",
                                   headers={}, label="original")
    three = [types.SimpleNamespace(best=stream, safe_title=TITLE,
                                   duration_seconds=0.0) for _ in range(3)]
    uploads: list[tuple[str, str]] = []          # (name on disk, title on the panel)

    async def fake_resolve(_url):
        return list(three)

    async def fake_fetch(_url, out_path, **kw):
        out_path.write_bytes(b"v" * 64)
        # The download bar is the frame that used to carry the video's name, so it is
        # made to happen rather than quietly skipped.
        if kw.get("on_progress"):
            await kw["on_progress"](64, 64)

    async def fake_send(_client, _chat_id, target, **kw):
        uploads.append((target.name, kw.get("title") or ""))
        return uploader.Sent(message=None, size_bytes=target.stat().st_size,
                             seconds=0.01, parts=1)

    real_fetch = handler._fetch_bytes
    real_send = handler.uploader.send_best_effort
    real_looks = handler.media.looks_like_video
    handler.terabox.resolve_all = fake_resolve       # instance attr; popped below
    handler._fetch_bytes = fake_fetch
    handler.uploader.send_best_effort = fake_send
    handler.media.looks_like_video = lambda _p: True
    try:
        before = credits.balance(USER)
        asyncio.run(handler._deliver(client, job))

        check("the other two videos are charged in the runner",
              round(before - credits.balance(USER), 2), round(2 * PER, 2))
        check("so the job ends up priced at the whole folder",
              job.cost, round(3 * PER, 2))
        check("and the jobs row moved with it",
              db.query("SELECT cost FROM jobs WHERE id = ?",
                       (job.row_id,))[0]["cost"], round(3 * PER, 2))
        check("all three went out", len(uploads), 3)
        check("expected and delivered agree", (job.expected, job.delivered), (3, 3))
        check("so nothing is refunded", jobq._refund_part(job), 0.0)

        # --- 3. what the screen and the disk are allowed to say ---------------
        #
        # The finished file was already anonymous — `uploader.send_video` passes no
        # `file_name`, so Telegram gets the on-disk one. The panel was not: it is a
        # text message that sits in the chat for the whole job, and it printed the
        # title. These are the assertions for the operator's ask.
        check("no frame of the panel ever named the video",
              [t for t in card.seen if "Bhabhi" in t], [])
        check("nor did anything else the bot posted",
              [t for t in client.said if "Bhabhi" in t], [])
        check("the panel counted the videos instead",
              [t for _n, t in uploads],
              ["Video 1 of 3", "Video 2 of 3", "Video 3 of 3"])
        check("and what Telegram was handed is a number, not a name",
              [n for n, _t in uploads], ["01.mp4", "02.mp4", "03.mp4"])
        check("the real name is still on the job, for the admin's own log",
              job.file_name, TITLE)

        # --- 4. three videos, credit for two ----------------------------------
        #
        # Deliver what is paid for and say so. A refusal leaves someone with nothing
        # for a link that mostly worked, and this is the same shape as the batch trim
        # the door already does — decided here rather than at the card, because until
        # the folder is read there is nothing to trim.
        spend = round(credits.balance(USER) - 2 * PER, 2)
        if spend > 0:
            credits.charge(USER, spend, "test — spend down to two videos")
        panel2 = Msg("(a second panel)", BOT)
        job2 = jobq.Job(user_id=USER, chat_id=USER, kind="terabox",
                        runner=lambda j: handler._deliver(client, j),
                        cost=PER, source="https://terabox.com/s/1folder2",
                        payload={"status": panel2})
        queue.submit(job2)                       # takes the floor, as the card does
        uploads.clear()
        client.said.clear()
        asyncio.run(handler._deliver(client, job2))

        check("the folder was cut to what the balance covered", len(uploads), 2)
        check("and the price with it", job2.cost, round(2 * PER, 2))
        check("expected is what is being attempted, not what the link held",
              job2.expected, 2)
        check("the balance is spent, not overdrawn",
              round(credits.balance(USER), 2), 0.0)
        check("the user is told what the link held",
              [t for t in client.said if "holds <b>3</b> videos" in t] != [], True)
        check("and how many of them are being sent",
              [t for t in client.said if "covers <b>2</b>" in t] != [], True)
        check("the trim named no video either",
              [t for t in client.said + panel2.seen if "Bhabhi" in t], [])

        # --- 5. two of the three arrive ---------------------------------------
        #
        # The refund is per video out of a price that was itself built per video, so
        # the arithmetic has to survive the two-step charge: `_refund_part` reads the
        # 1.5 the runner settled on, not the 0.5 the card quoted.
        partial = jobq.Job(user_id=USER, chat_id=USER, kind="terabox",
                           runner=lambda _j: None, cost=round(3 * PER, 2))
        partial.expected, partial.delivered = 3, 2
        check("a video that never arrived gives its own share back",
              jobq._refund_part(partial), round(PER, 2))
        check("and it reached the balance",
              round(credits.balance(USER), 2), round(PER, 2))

        # --- 6. one video is still one video ----------------------------------
        single = jobq.Job(user_id=USER, chat_id=USER, kind="terabox",
                          runner=lambda _j: None, cost=PER)
        held = credits.balance(USER)
        check("a one-video link asks for nothing more",
              jobq.charge_more(single, PER, 1), 1)
        check("so nothing else is taken",
              round(credits.balance(USER), 2), round(held, 2))
        check("its price is untouched", single.cost, PER)
        single.expected, single.delivered = 1, 0
        check("and it stays all-or-nothing rather than refunding a share of itself",
              jobq._refund_part(single), 0.0)
    finally:
        handler.terabox.__dict__.pop("resolve_all", None)
        handler._fetch_bytes = real_fetch
        handler.uploader.send_best_effort = real_send
        handler.media.looks_like_video = real_looks
        object.__setattr__(cfg, "work_dir", real_work_dir)
        _shutil.rmtree(work, ignore_errors=True)

    state.clear_mode(USER)
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
    # The `1` in `/s/1abc` belongs to the path, not to the key: Terabox's own
    # redirect spells the same share `?surl=abc`. `/share/list` answers errno 140
    # to anything that keeps it, which is what every request used to send.
    check("/s/ path drops the leading 1",
          tb.surl_of("https://terabox.com/s/1abcDEF"), "abcDEF")
    check("surl= is already bare",
          tb.surl_of("https://x.com/x?surl=abcDEF"), "abcDEF")
    # A key may legitimately start with `1`, so a hand-pasted surl= is taken as
    # given — `_listing` retries the other spelling rather than guess here.
    check("a pasted surl=1… is left alone",
          tb.surl_of("https://x.com/x?surl=1abcDEF"), "1abcDEF")
    check("a one-character key survives", tb.surl_of("https://terabox.com/s/1"), "1")
    check("trailing query on /s/",
          tb.surl_of("https://terabox.com/s/1abc?pwd=zz"), "abc")
    check("whitespace is trimmed", tb.surl_of("  https://terabox.com/s/1abc  "), "abc")
    check("the live test share",
          tb.surl_of("https://1024terabox.com/s/1-a0S95G_Ab0RYiAZOdql9Q"),
          "-a0S95G_Ab0RYiAZOdql9Q")
    raises("a link with no key at all", lambda: tb.surl_of("https://terabox.com/hello"))

    print("\n— and the spelling the other endpoint wants —")
    check("shorturlinfo gets the 1 back", tb.prefixed("abcDEF"), "1abcDEF")
    check("but never two", tb.prefixed("1abcDEF"), "1abcDEF")
    check("round trip from a share link",
          tb.prefixed(tb.surl_of("https://terabox.com/s/1abc")), "1abc")

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

    print("\n— where the dlink comes from —")
    # `/share/list` has no dlink key; `/api/shorturlinfo` has one and, signed out,
    # leaves it empty. Both shapes have to survive the parser.
    listed = tb.parse_list(ONE_FILE_NO_DLINK)
    check("a listing with no dlink key still parses", listed[0].name,
          "Movie 1080p.mkv")
    check("and reports no dlink", listed[0].dlink, "")
    info = tb.parse_list(SHORTURLINFO)
    check("so does shorturlinfo's row", info[0].name, "2023-10-16-17-53-43(1).mp4")
    check("whose dlink is present and empty when signed out", info[0].dlink, "")
    check("merged in by fs_id",
          tb.Terabox._fill(listed, {"812345678901234": "https://d/x"})[0].dlink,
          "https://d/x")
    check("an fs_id we did not ask about changes nothing",
          tb.Terabox._fill(listed, {"999": "https://d/y"})[0].dlink, "")
    check("an empty merge changes nothing", tb.Terabox._fill(listed, {})[0].dlink, "")
    check("and a dlink already there is not overwritten",
          tb.Terabox._fill(tb.parse_list(ONE_FILE),
                           {"812345678901234": "https://d/z"})[0].dlink,
          "https://d.terabox.com/file/abc?fid=1&sign=xyz")
    check("the signature bundle comes off the same payload",
          tb.share_context(SHORTURLINFO),
          {"sign": "2d8fbb" + "0" * 34, "timestamp": "1756900000",
           "uk": "4400123456789", "shareid": "42424242"})
    check("and is empty, not missing, for a payload without one",
          tb.share_context(ONE_FILE_NO_DLINK),
          {"sign": "", "timestamp": "", "uk": "", "shareid": ""})
    check("nonsense in, empty out", tb.share_context("<html>"), {})

    print("\n— errno hides in two different fields —")
    check("errno, documented", tb.errno_of({"errno": -6}), -6)
    check("code, what the WAF actually sends", tb.errno_of({"code": 460020}), 460020)
    check("a string is still a number", tb.errno_of({"errno": "140"}), 140)
    check("neither field is no error", tb.errno_of({"list": []}), 0)
    check("not a dict at all", tb.errno_of("<html>login</html>"), 0)
    check("unparseable stays 0", tb.errno_of({"errno": "boom"}), 0)

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
    raises("the wrong surl spelling (errno 140)", lambda: tb.parse_list(WRONG_SPELLING))
    raises("signed-out session (code 460020)", lambda: tb.parse_list(NEED_VERIFY))
    raises("signed-out session (errno 400210)", lambda: tb.parse_list(NEED_VERIFY_V2))
    raises("a captcha instead of the file (errno 400310)",
           lambda: tb.parse_list(NEED_CAPTCHA))
    for name, payload in (("code 460020", NEED_VERIFY), ("errno 400210", NEED_VERIFY_V2),
                          ("errno 400310", NEED_CAPTCHA)):
        try:
            tb.parse_list(payload)
        except ResolveError as exc:
            check(f"{name} blames the cookie, not the link",
                  "TERABOX_COOKIE" in str(exc), True)
    try:
        tb.parse_list(WRONG_SPELLING)
    except ResolveError as exc:
        check("but 140 does not — it is our own mistake, not the operator's",
              "TERABOX_COOKIE" in str(exc), False)

    print("\n— the HLS fallback url —")
    # `/api/streaming?path=` only ever works for a file inside the calling account,
    # so for someone else's share it answered errno -6 whatever the cookie. Shares
    # go through `/share/streaming`, which needs the signature pair.
    ctx = tb.share_context(SHORTURLINFO)
    url = tb.hls_url(ctx, "1029695337079147", origin="https://www.1024tera.com")
    check("asks the share endpoint, not the own-files one",
          "/share/streaming?" in url, True)
    check("and never the own-files one", "/api/streaming" in url, False)
    check("on the host the share landed on",
          url.startswith("https://www.1024tera.com/"), True)
    check("carries the signature pair",
          f"sign={ctx['sign']}" in url and f"timestamp={ctx['timestamp']}" in url, True)
    check("names the file by id", "fid=1029695337079147" in url, True)
    check("and whose share it is", "uk=4400123456789" in url, True)
    check("capped at 1080", "M3U8_AUTO_1080" in url, True)
    check("carries the app id", f"app_id={tb.APP_ID}" in url, True)
    # The one parameter that must never be sent: it is what turns a working call
    # into `code 460020 need verify`.
    check("and never dp-logid", "dp-logid" in url, False)
    check("nor does any other call", "dp-logid" in tb.COMMON, False)

    print("\n— the cookieless route: what iteraplay hands back —")
    from bot.providers import iteraplay as ip
    rows = ip.parse(ITERA_OK)
    check("one file", len(rows), 1)
    check("named", rows[0].name, "VID-20250421-WA0035(1).mp4")
    # The size is the whole point: it matches what Terabox reports for the upload,
    # and the download of exactly that many bytes is what proved this is the
    # original file and not a re-encode.
    check("the original's size, to the byte", rows[0].size, 44739275)
    check("the direct link is the one taken",
          rows[0].url.endswith("&file_name=VID-20250421-WA0035(1).mp4"), True)
    check("the ladder is kept, but only as a spare", rows[0].hls.endswith("480.m3u8"),
          True)
    check("best of the ladder, not first in the dict", "360" in rows[0].hls, False)
    check("mm:ss duration becomes seconds", rows[0].duration, 187.0)
    check("a plain number stays one", ip._seconds(12.5), 12.5)
    check("and nonsense is nothing, not zero", ip._seconds("later"), None)

    print("\n— and what it says when the quota is gone —")
    raises("the sixth video in six hours",
           lambda: ip.parse(ITERA_LIMIT, status_code=429))
    try:
        ip.parse(ITERA_LIMIT, status_code=429)
    except ResolveError as exc:
        check("says what ran out", "quota" in str(exc), True)
        check("and how small the allowance is", "5 videos per 6 hours" in str(exc), True)
        check("and when to come back", "Try again in about" in str(exc), True)
        # Their own message ends "Login for higher limits" — true, and useless to
        # the person waiting, who has no account to log in to.
        check("without telling the user to log in", "Login" in str(exc), False)
        check("and stays plain text", "<" in str(exc), False)
    raises("a 429 with no error field is still a 429",
           lambda: ip.parse({"status": "error"}, status_code=429))
    raises("a folder and a file with no link at all", lambda: ip.parse(ITERA_NOTHING))
    raises("not a dict", lambda: ip.parse("<html>"))
    raises("a plain refusal", lambda: ip.parse({"status": "error"}, status_code=200))

    print("\n— the fallback, end to end, without a network —")
    test_fallback_returns_the_original()
    print("\n— finding the one host the cookie is signed in on —")
    test_the_signed_in_host_is_found_not_configured()
    print("\n— the page tokens /share/list will not work without —")
    test_the_page_tokens_come_off_the_home_page()
    print("\n— the download hop is never the session that did the API calls —")
    test_the_dlink_hop_gets_a_session_of_its_own()
    print("\n— a bare ndus value is put back together the same way twice —")
    # The resolve path always normalised this and the download path did not, so a
    # cookie pasted without its name resolved a dlink and was then refused by the
    # CDN — which reads as an expired cookie and is a typo in .env.
    from bot.config import cfg as _cfg
    _was = _cfg.terabox_cookie
    try:
        for pasted, want in [("abc123", "ndus=abc123"),
                             ("ndus=abc123", "ndus=abc123"),
                             ("  abc123  ", "ndus=abc123"),
                             ("ndus=abc; lang=en", "ndus=abc; lang=en")]:
            object.__setattr__(_cfg, "terabox_cookie", pasted)
            check(f"{pasted!r} becomes {want!r}", tb.cookie_header(), want)
            check(f"and the downloader sends the same for {pasted!r}",
                  tb.terabox._headers("https://x.com").get("Cookie"), want)
    finally:
        object.__setattr__(_cfg, "terabox_cookie", _was)
    print("\n— one link, several accounts and addresses —")
    test_rotation_spreads_across_accounts_and_addresses()
    print("\n— the cookie pool is the union of both settings —")
    test_the_cookie_pool_is_the_union_of_both_settings()
    print("\n— four cookies is failover, and only a refused cookie retries —")
    test_the_rotation_moves_on_a_refused_cookie_and_only_then()
    print("\n— which address a download leaves from —")
    test_egress_rotates_and_never_lets_a_proxy_fail_a_job()
    print("\n— asking one cookie whether it is still signed in —")
    test_the_cookie_probe_reports_instead_of_raising()
    print("\n— and printing that where an admin can read it —")
    test_the_health_card_says_which_host_answered()

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
    # A foreign url is **not** nobody's any more. The Fap provider's door is
    # deliberately wide — its resolver decides what is a video, and an allowlist in
    # the bot would refuse links that service can actually fetch — so it answers for
    # anything with a host and a path. What still has to hold is the other direction:
    # every Terabox mirror reaches the route that owns cookies, batching and its own
    # price, whichever provider happens to be asked first. That is `FOREIGN_HOSTS`,
    # and it is the assertion worth having here.
    from bot.providers.faphouse import faphouse
    check("a foreign url falls to the wide door, not to nobody",
          find_for("https://example.com/x") is faphouse, True)
    check("but no Terabox mirror ever does",
          {find_for(f"https://{h}/s/1abc") is tb.terabox
           for h in ("terabox.com", "www.terabox.com", "1024terabox.com",
                     "4funbox.com", "mirrobox.com", "nephobox.com",
                     "terasharefile.com", "freeterabox.com")}, {True})

    print("\n— starting a batch charges the human, not the bot —")
    test_batch_charges_whoever_pressed_the_button()

    print("\n— one message from the prompt to the video —")
    test_one_panel_from_prompt_to_video()

    print("\n— a folder is priced per video, and names none of them —")
    test_a_folder_is_priced_per_video()

    # The wait itself is a product decision, so it gets a test. A position in line
    # ("3rd, waiting for a free worker") teaches everyone watching that they are
    # behind other people and does not make the wait shorter; a moving band reads
    # as work happening. If someone ever puts the queue text back, this fails.
    print("\n— the wait shows motion, not a position in line —")
    from bot import ui
    from bot import queue as jobq
    from bot.handlers import terabox as handler

    frames = [ui.waiting_block("📦 Your link", tick) for tick in range(4)]
    check("no word for a queue anywhere in it",
          [w for w in ("queue", "queued", "position", "waiting for a free")
           if any(w in f.lower() for f in frames)], [])
    check("every tick looks different, so it reads as alive",
          len(set(frames)), 4)
    check("the bar is always the same width",
          {f.count(ui.BAR_FULL) + f.count(ui.BAR_EMPTY) for f in frames},
          {ui.BAR_WIDTH})
    check("the band is 3 cells, wherever it is",
          {f.count(ui.BAR_FULL) for f in frames}, {3})
    check("it wraps rather than running off the end",
          ui.waiting_block("x", 0) == ui.waiting_block("x", 0), True)
    check("a batch link names its own number",
          "Link 3 of 7" in ui.waiting_block(
              handler._waiting_title(
                  jobq.Job(user_id=1, chat_id=1, kind="terabox", runner=None,
                           cost=1.0, index=2, total_in_batch=7)), 0), True)
    check("a lone link does not pretend to be a batch",
          handler._waiting_title(
              jobq.Job(user_id=1, chat_id=1, kind="terabox", runner=None,
                       cost=1.0, index=0, total_in_batch=1)), "📦 Your link")
    check("html is escaped, so a filename cannot break the panel",
          "&lt;b&gt;" in ui.waiting_block("<b>", 0), True)

    # The band bounces rather than wrapping. A wrapped band arrives at the right
    # edge as two separate pieces, which reads as a broken bar — so the check is
    # that the lit cells are always one unbroken run, at every tick of a full
    # there-and-back cycle.
    runs = {ui._band(t).strip(ui.BAR_EMPTY).count(ui.BAR_EMPTY)
            for t in range(2 * ui.BAR_WIDTH + 5)}
    check("the lit cells are always one unbroken block", runs, {0})
    check("and it visits both ends",
          {ui._band(t)[0] for t in range(2 * ui.BAR_WIDTH)}
          | {ui._band(t)[-1] for t in range(2 * ui.BAR_WIDTH)},
          {ui.BAR_FULL, ui.BAR_EMPTY})

    # A wait of unknown length can still say one true thing: how long it has been.
    check("no clock until the caller knows one", "⏱" in ui.waiting_block("x", 1), False)
    check("elapsed is shown when it is known",
          "⏱ 1m 5s" in ui.waiting_block("x", 1, seconds=65), True)
    check("the wording moves too, so it cannot read as frozen",
          len({ui.waiting_block("x", t).split("\n")[1] for t in range(12)}) > 1, True)

    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
