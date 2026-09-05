"""
The Fap route: the resolver's JSON in, a priced menu out.

Run: python tests/test_fap.py

Everything here is driven through `faphouse.parse()` on the one payload that was
actually measured, so no network and no ffmpeg is needed. Two things are worth saying
about what is being protected:

**The 1080p rendition of the real sample is `1400x1080`.** Reading the *first* number
would file it as 720p and charge 1.5 credits for a 1080p video — a silent money bug
that no amount of manual testing on a 1920x1080 video would ever show.

**A quality button that cannot be clicked is indistinguishable from a slow bot.** The
label goes into `q:<token>:<label>` callback data, so the pattern the handler listens on
and the buttons `kb.quality_choice` renders have to agree exactly; the test asserts they
do, rather than trusting that they do.
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

import json                                  # noqa: E402
import re                                    # noqa: E402
import time                                  # noqa: E402

from bot import keyboards as kb, queue as jobq, scratch, ui   # noqa: E402
from bot.config import cfg                   # noqa: E402
from bot.handlers import fap                 # noqa: E402
from bot.providers import ResolveError       # noqa: E402
from bot.providers import faphouse as fapmod  # noqa: E402
from bot.providers.faphouse import faphouse  # noqa: E402

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


#: The resolver's answer for one real video, kept verbatim apart from shortened URLs.
#: `size` and `size_readable` really are null at every quality, and the 1080p entry
#: really is 1400 wide — this site crops rather than pillarboxes.
SAMPLE = {
    "errno": 0,
    "data": {"file": {
        "file_name": "Japanese Girl Loves Hard Sex",
        "size": None,
        "size_readable": None,
        "duration": 1523,
        "stream_url": "https://video-am.flixcdn.com/x/multi=426x240:240,1400x1080:1080/J6/format/_TPL_.mp4.m3u8",
        "quality_streams": [
            {"quality": "240p", "resolution": "426x240", "bandwidth": 460392,
             "stream_url": "https://video-am.flixcdn.com/x/J6/format/240.mp4.m3u8?t=a%3D%3D"},
            {"quality": "480p", "resolution": "854x480", "bandwidth": 1334647,
             "stream_url": "https://video-am.flixcdn.com/x/J6/format/480.mp4.m3u8?t=b%3D%3D"},
            {"quality": "720p", "resolution": "1280x720", "bandwidth": 2932580,
             "stream_url": "https://video-am.flixcdn.com/x/J6/format/720.mp4.m3u8?t=c%3D%3D"},
            {"quality": "1080p", "resolution": "1400x1080", "bandwidth": 4715762,
             "stream_url": "https://video-am.flixcdn.com/x/J6/format/1080.mp4.m3u8?t=d%3D%3D"},
        ],
        "thumb": "https://ic-nss.fhcdngroup.online/x/7.jpg",
    }},
}

LINK = "https://faphouse.com/videos/japanese-girl-loves-hard-sex-1234"

print("\n-- reading the resolver's answer --")

got = fapmod.parse(json.loads(json.dumps(SAMPLE)), LINK)
check("the title is the file name", got.title, "Japanese Girl Loves Hard Sex")
check("every rendition is kept", len(got.streams), 4)
check("tallest first", [s.height for s in got.streams], [1080, 720, 480, 240])
check("all HLS, none a file", {s.kind for s in got.streams}, {"hls"})
check("no size is invented", {s.size_bytes for s in got.streams}, {None})
check("the duration comes through", got.duration_seconds, 1523)
check("the thumbnail comes through", got.thumbnail_url,
      "https://ic-nss.fhcdngroup.online/x/7.jpg")
check("the pasted link is remembered", got.source_url, LINK)

tallest = got.best
check("1400x1080 is 1080p, not 720p", tallest.height, 1080)
check("and its width is read too", tallest.width, 1400)
check("and it is labelled by height", tallest.label, "1080p")
check("the escaped tail is passed through byte for byte",
      tallest.url.endswith("1080.mp4.m3u8?t=d%3D%3D"), True)
check("bandwidth is kept for the sort", tallest.bandwidth, 4715762)
check("labels are what a button will show", list(got.labels),
      ["1080p", "720p", "480p", "240p"])
check("by_label finds one", got.by_label("480p").url.endswith("480.mp4.m3u8?t=b%3D%3D"),
      True)


print("\n-- the priced menu --")

options = fapmod.menu(got)
check("three buttons, not four", [s.label for s, _c in options],
      ["480p", "720p", "1080p"])
check("cheapest first", [c for _s, c in options], [1, 1.5, 2])
check("the 240p rung is hidden", any(s.label == "240p" for s, _c in options), False)
check("each button carries its own stream",
      [s.url.rsplit("/", 1)[-1].split("?")[0] for s, _c in options],
      ["480.mp4.m3u8", "720.mp4.m3u8", "1080.mp4.m3u8"])

no1080 = json.loads(json.dumps(SAMPLE))
no1080["data"]["file"]["quality_streams"] = \
    no1080["data"]["file"]["quality_streams"][:3]
short = fapmod.menu(fapmod.parse(no1080, LINK))
check("a video without 1080p is offered two buttons",
      [s.label for s, _c in short], ["480p", "720p"])
check("and is never charged 2 credits", max(c for _s, c in short), 1.5)

only240 = json.loads(json.dumps(SAMPLE))
only240["data"]["file"]["quality_streams"] = \
    only240["data"]["file"]["quality_streams"][:1]
low = fapmod.menu(fapmod.parse(only240, LINK))
check("a video with only a low copy still gets one button", len(low), 1)
check("and under its own honest label", low[0][0].label, "240p")
check("and at the cheapest rung", low[0][1], cfg.cost_fap_480)

print("\n-- what an off-ladder height costs --")

check("240p is charged as the cheapest rung", fapmod.price_of(240), cfg.cost_fap_480)
check("480p exactly", fapmod.price_of(480), cfg.cost_fap_480)
check("just over 480 is the next rung up", fapmod.price_of(481), cfg.cost_fap_720)
check("720p exactly", fapmod.price_of(720), cfg.cost_fap_720)
check("1080p exactly", fapmod.price_of(1080), cfg.cost_fap_1080)
check("1440p can never cost more than the dearest rung",
      fapmod.price_of(1440), cfg.cost_fap_1080)
check("an unknown height is charged as the cheapest",
      fapmod.price_of(None), cfg.cost_fap_480)

# The prices are settings, so the ladder has to be read at call time. Frozen at
# import, an operator's .env would be quietly ignored and the menu would charge
# something the guide does not say.
object.__setattr__(cfg, "cost_fap_720", 3.0)
check("the menu follows cfg, not a constant",
      [c for _s, c in fapmod.menu(got)], [1, 3.0, 2])
object.__setattr__(cfg, "cost_fap_720", 1.5)
check("and back again", [c for _s, c in fapmod.menu(got)], [1, 1.5, 2])


print("\n-- the endpoint --")

endpoint = fapmod.endpoint_for(LINK)
check("the pasted link goes after url=",
      endpoint.startswith(cfg.fap_api + "?url="), True)
check("and the language is asked for", endpoint.endswith("&title_lang=en"), True)

# A link may legitimately carry & and = of its own. Unencoded, a &title_lang=hi
# inside one would become a second parameter of the OUTER request.
sneaky = fapmod.endpoint_for("https://faphouse.com/v/x?a=1&title_lang=hi")
check("an embedded & cannot become a parameter of the outer call",
      sneaky.count("title_lang"), 2)
check("because the whole link is one opaque value",
      "%3Ftitle_lang" not in sneaky and "a%3D1%26title_lang%3Dhi" in sneaky, True)

object.__setattr__(cfg, "fap_api", "https://x.example/api?k=1")
check("a base that already has a query is joined with &",
      fapmod.endpoint_for("https://faphouse.com/v/x").startswith(
          "https://x.example/api?k=1&url="), True)
object.__setattr__(cfg, "fap_api", "https://x.example/api?")
check("a trailing ? is not doubled",
      fapmod.endpoint_for("https://faphouse.com/v/x").count("?"), 1)
object.__setattr__(cfg, "fap_api", "https://resolver.example/api/faphouse/mrx")

# Every link off this site carries a #fragment, and a browser never transmits one.
# the operator's two samples both end #dmVwPU1haW4gcGFnZSZ2ZWI9Rmlyc3QgNjAgb24gbWFpbg==,
# which decodes to "vep=Main page&veb=First 60 on main" — referral tracking, nothing
# the resolver can use.
TRACKED = LINK + "#dmVwPU1haW4gcGFnZSZ2ZWI9Rmlyc3QgNjAgb24gbWFpbg=="
check("the tracking fragment is cut before the wire", fapmod.wire_url(TRACKED), LINK)
check("a link without one is untouched", fapmod.wire_url(LINK), LINK)
check("a bare # goes too", fapmod.wire_url(LINK + "#"), LINK)
check("only the first # counts", fapmod.wire_url("https://h.example/v#a#b"),
      "https://h.example/v")
check("surrounding space goes as well",
      fapmod.wire_url("  " + TRACKED + "  "), LINK)
check("and nothing at all is not a crash", fapmod.wire_url(""), "")
check("a query is NOT cut, because a video id lives in one",
      fapmod.wire_url("https://h.example/watch?v=abc#x"), "https://h.example/watch?v=abc")
check("so the resolver is never sent an encoded #",
      "%23" in fapmod.endpoint_for(TRACKED), False)
check("and both spellings of the same video are one request",
      fapmod.endpoint_for(TRACKED), fapmod.endpoint_for(LINK))
check("while the user's own link is what is remembered",
      fapmod.parse(SAMPLE, TRACKED).source_url, TRACKED)

print("\n-- what a refused request is called --")

# The live resolver answers 502 for every single video, because the session it
# harvests upstream has gone. "Send a different link" is the wrong advice for that
# user — they will try five more and conclude the bot is broken.
down = fapmod.refusal_for(502)
check("a 5xx is named as the service's problem", "is down right now" in down, True)
check("and says so about the link too", "not your link" in down, True)
check("with the status, for a screenshot", "HTTP 502" in down, True)
check("and does not send them hunting for another link",
      "different link" in down, False)
check("503 reads the same way", "is down right now" in fapmod.refusal_for(503), True)
bad = fapmod.refusal_for(404)
check("a 4xx can be the link, so there it is worth suggesting",
      "different link" in bad, True)
check("and it is not called an outage", "is down right now" in bad, False)
check("403 too", "different link" in fapmod.refusal_for(403), True)
check("neither one ever names the endpoint",
      [s for s in (down, bad) if "resolver.example" in s or "shadowlink" in s], [])

print("\n-- every button has to be clickable --")

pattern = re.compile(fap.PICK_PATTERN)
markup = kb.quality_choice("tok-123", [(s.label, c) for s, c in fapmod.menu(got)])
buttons = [b for row in markup.inline_keyboard for b in row]
data = [b.callback_data for b in buttons]
check("three quality buttons and a cancel", len(data), 4)
check("the handler's pattern routes every one of them",
      [bool(pattern.match(d)) for d in data[:3]], [True, True, True])
check("the token comes back out whole",
      [pattern.match(d).group("token") for d in data[:3]], ["tok-123"] * 3)
check("and so does the label",
      [pattern.match(d).group("label") for d in data[:3]], ["480p", "720p", "1080p"])
check("cancel is a different prefix, so a different handler owns it",
      data[3], "job:cancel:tok-123")

# The label is split out of the callback on colons, so a colon inside one would let
# a crafted `quality` field forge a token. Only reached for an entry whose
# `resolution` could not be read at all.
check("a colon is stripped out of a label", fapmod._safe_label("a:b:c"), "abc")
check("so is a space and a slash", fapmod._safe_label("hd stream/1"), "hdstream1")
check("a long one is cut", fapmod._safe_label("x" * 40), "x" * 12)
check("an empty one still has a name", fapmod._safe_label(""), "original")
check("and it still matches the pattern",
      bool(pattern.match(f"q:t:{fapmod._safe_label('720p HD:')}")), True)

odd = json.loads(json.dumps(SAMPLE))
odd["data"]["file"]["quality_streams"] = [
    {"quality": "Full HD: best", "resolution": "", "stream_url": "https://x/y.m3u8"}]
label = fapmod.parse(odd, LINK).streams[0].label
check("an unreadable resolution falls back to a safe label", label, "FullHDbest")
check("which is also routable", bool(pattern.match(f"q:tok:{label}")), True)


print("\n-- reading dimensions --")

check("width x height", fapmod._dimensions({"resolution": "1400x1080"}), (1400, 1080))
check("a unicode times sign too",
      fapmod._dimensions({"resolution": "854×480"}), (854, 480))
check("spaces are allowed", fapmod._dimensions({"resolution": " 1280 x 720 "}),
      (1280, 720))
check("no resolution falls back to the quality string",
      fapmod._dimensions({"quality": "720p"}), (None, 720))
check("case does not matter", fapmod._dimensions({"quality": "1080P"}), (None, 1080))
check("neither one is a guess", fapmod._dimensions({"quality": "best"}), (None, None))

print("\n-- somebody else's service, so nothing is assumed --")

raises("a non-dict payload is a sentence, not a TypeError", ResolveError,
       lambda: fapmod.parse("<html>502 Bad Gateway</html>", LINK))
raises("so is a list", ResolveError, lambda: fapmod.parse([1, 2], LINK))
raises("an errno is reported", ResolveError,
       lambda: fapmod.parse({"errno": 2, "errmsg": "private share"}, LINK))
raises("even without an errmsg", ResolveError, lambda: fapmod.parse({"errno": 404}))
raises("no data at all", ResolveError, lambda: fapmod.parse({"errno": 0}))
raises("data with nothing playable in it", ResolveError,
       lambda: fapmod.parse({"errno": 0, "data": {"file": {"file_name": "x"}}}))

try:
    fapmod.parse({"errno": 2, "errmsg": "video is private"}, LINK)
except ResolveError as exc:
    check("and the service's own wording is passed on",
          "video is private" in str(exc), True)

check("errno 0 is not an error", fapmod.parse(SAMPLE, LINK).title[:8], "Japanese")
check("a missing errno is not either",
      len(fapmod.parse({"data": SAMPLE["data"]}, LINK).streams), 4)

# The file object one level up. Never measured, but a resolver that changes shape
# should degrade to a readable sentence rather than a KeyError on a worker.
flat = {"errno": 0, "data": dict(SAMPLE["data"]["file"])}
check("a file object at the top level still reads",
      len(fapmod.parse(flat, LINK).streams), 4)

# No quality_streams at all: the multi-variant master is the only thing left, and
# its height cannot be known from here.
master_only = json.loads(json.dumps(SAMPLE))
master_only["data"]["file"]["quality_streams"] = []
lone = fapmod.parse(master_only, LINK)
check("the master playlist is the fallback", len(lone.streams), 1)
check("labelled honestly", lone.streams[0].label, "original")
check("and charged as the cheapest rung, not the dearest",
      fapmod.menu(lone)[0][1], cfg.cost_fap_480)

dupes = json.loads(json.dumps(SAMPLE))
one = dupes["data"]["file"]["quality_streams"][1]
dupes["data"]["file"]["quality_streams"] = [one, dict(one), one]
check("the same stream_url twice is one stream",
      len(fapmod.parse(dupes, LINK).streams), 1)

junk = json.loads(json.dumps(SAMPLE))
junk["data"]["file"]["quality_streams"] = ["nonsense", None, {"quality": "480p"}]
raises("entries with no stream_url are not streams", ResolveError,
       lambda: fapmod.parse({"errno": 0, "data": {"file": {
           "file_name": "x", "quality_streams": junk["data"]["file"]["quality_streams"],
           "stream_url": ""}}}, LINK))

print("\n-- which links this provider owns --")

check("the plain host", faphouse.matches("https://faphouse.com/videos/x-1"), True)
check("www", faphouse.matches("https://www.faphouse.com/videos/x-1"), True)
check("mobile", faphouse.matches("https://m.faphouse.com/videos/x-1"), True)
check("http as well as https", faphouse.matches("http://faphouse.com/v/1"), True)
check("a port is ignored", faphouse.matches("https://faphouse.com:443/v/1"), True)
check("the bare host is not a video",
      faphouse.matches("https://faphouse.com/"), False)
check("a Terabox link belongs to the other provider",
      faphouse.matches("https://terabox.com/s/1abc"), False)
check("nor is prose", faphouse.matches("send me the video please"), False)
check("nor an empty string", faphouse.matches(""), False)

# The door is deliberately wide, and this is the assertion that says so out loud.
# The resolver takes whatever sites *it* takes, and that list is not this module's to
# keep: an allowlist here would refuse a link the service can actually fetch, and the
# refusal would read as a bug in the bot rather than a limit of the resolver. So an
# unknown video host is accepted, the resolver is the authority on what is a video,
# and a link it does not know comes back as its own sentence. Nothing is charged for
# a link that turns out not to resolve.
check("an unknown video host is accepted, not guessed at",
      faphouse.matches("https://some-tube.example/videos/x-1"), True)
check("a lookalike host is accepted too, because the resolver decides",
      faphouse.matches("https://faphouse.com.other.example/v/1"), True)
check("every Terabox mirror family still belongs to the other route",
      [h for h in ("terabox.com", "1024terabox.com", "www.4funbox.com",
                   "nephobox.com", "terasharefile.com")
       if faphouse.matches(f"https://{h}/s/1abc")], [])
check("and the obvious non-videos are refused at the door",
      [h for h in ("t.me", "telegram.me", "youtube.com", "youtu.be", "wa.me",
                   "instagram.com")
       if faphouse.matches(f"https://{h}/something")], [])
check("a bare domain is someone typing a site name, not sending a video",
      faphouse.matches("https://some-tube.example"), False)
check("and neither ftp nor a file path is a link",
      [t for t in ("ftp://host.example/v.mp4", "/videos/x-1", "faphouse.com/v/1")
       if faphouse.matches(t)], [])
check("trailing punctuation is trimmed off a pasted link",
      faphouse.extract_links("watch (https://faphouse.com/videos/a-1)."),
      ["https://faphouse.com/videos/a-1"])
check("one link, and only the ones this provider owns",
      faphouse.extract_links("https://terabox.com/s/1 https://faphouse.com/v/2"),
      ["https://faphouse.com/v/2"])
check("this provider takes one link at a time", faphouse.max_batch, 1)
check("it is registered under its own name", faphouse.name, "fap")

check("configured, nothing blocks the door", faphouse.unavailable(), "")
object.__setattr__(cfg, "fap_api", "")
blocked = faphouse.unavailable()
check("with no endpoint the door says so", blocked.startswith("🔧"), True)
check("and says nothing was charged", "Nothing was charged" in blocked, True)
object.__setattr__(cfg, "fap_api", "https://resolver.example/api/faphouse/mrx")


print("\n-- how the handler is wired in --")

check("the mode has its own name", fap.MODE, "await_fap_link")
check("which the state module knows about",
      "await_fap_link" in str(__import__("bot.state", fromlist=["Mode"]).Mode), True)
check("the janitor is allowed to clean up after it", "fap-" in scratch.PREFIXES, True)
check("a fap job drains down the link lane, not the archive one",
      jobq.lane_of("fap"), jobq.lane_of("terabox"))
check("the prompt quotes the real ladder",
      all(f"{c:g} credit" in fap.prompt() for _h, _l, c in fapmod.rungs()), True)
check("and says one link, because the menu belongs to one video",
      "One link" in fap.prompt(), True)

job = jobq.Job(user_id=7, chat_id=7, kind="fap", runner=lambda j: None,
               cost=2.0, title="A Very Long Title " * 6, quality="1080p")
job.row_id = 41
title = fap._panel_title(job)
check("the live panel does not name the video", "Very Long Title" in title, False)
check("it says Video instead", "Video" in title, True)
check("and the quality is on the panel", title.endswith("1080p"), True)
check("the scratch directory carries the prefix and the row",
      fap._work_dir(job).name, "fap-7-41")

card = fap._menu_card(got, fapmod.menu(got), have=5)
check("the quality card does not name the video either",
      "Japanese Girl" in card, False)
check("and the balance", "5 credit" in card, True)
check("and says nothing is charged yet", "Nothing is charged" in card, True)
check("with three rungs offered it complains about none",
      "does not have" in card, False)

thin = fapmod.parse(no1080, LINK)
check("a missing rung is named, so an absent 1080p reads as the video's limit",
      "does not have 1080p" in fap._menu_card(thin, fapmod.menu(thin), have=5), True)
check("a second link is mentioned rather than silently dropped",
      "you sent 2 links" in fap._menu_card(thin, fapmod.menu(thin), 5, sent=2).lower(),
      True)


print("\n-- seconds are not bytes --")

# `media.fetch_to_mp4` reports progress in seconds of video. Run through
# `ui.progress_block` that reads as "45 B / 300 B" at "12 B/s", which is why this
# route has a renderer of its own.
panel = ui.assembling_block("🔥 clip · 1080p", 45.0, 300.0, time.monotonic() - 30)
check("no byte counts anywhere in it", " B" in panel or "MB" in panel, False)
check("it says how much video exists", "45s / 5m 0s of video" in panel, True)
check("as a percentage too", "15.0%" in panel, True)
check("and a multiple of real time, which is the only honest speed here",
      "×1.5 speed" in panel, True)
check("an unknown length still renders",
      "of video ready" in ui.assembling_block("t", 20.0, 0.0, time.monotonic() - 10),
      True)

def test_the_whole_route_from_key_to_video():
    """
    Link in, credit out, video out — driven through the registered handlers.

    Everything above this line tests a piece. This tests the *route*: the callback
    that opens the prompt, the text handler that reads the link, the callback that
    charges, the real `Queue` that runs it, and `_deliver` that sends the file. Only
    the two things that leave the box are faked — the resolver's HTTP call and
    ffmpeg/Telegram — so the money path, the token handover and the panel reuse are
    the real code.

    It exists because the resolver is down. When `resolver.example` answers again
    the only thing that changes is where `SAMPLE` comes from, and this proves the
    rest of the chain was already right rather than finding out one paid credit at a
    time.
    """
    import asyncio                                       # noqa: PLC0415
    import shutil as _shutil                             # noqa: PLC0415
    import sqlite3                                       # noqa: PLC0415
    import tempfile                                      # noqa: PLC0415
    import types                                         # noqa: PLC0415

    from bot import credits, db, media, state, uploader   # noqa: PLC0415
    from bot.queue import Queue                          # noqa: PLC0415

    home = Path(tempfile.mkdtemp(prefix="terabot-fap-"))
    conn = sqlite3.connect(home / "test.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn

    USER, BOT = 6100000001, 7200000002
    credits.ensure(USER, "the operator", "operator")
    credits.grant(USER, 10, "test float")
    state.clear_mode(USER)
    live: dict[int, object] = {}

    class Msg:
        """One message in the chat, findable by id — see `client.edit_message_text`."""

        _ids = iter(range(2000, 2999))

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
            """
            Every `callback_data` on the message, flattened.

            Real pyrogram objects here, unlike `test_terabox.py` which stubs the
            library out — so the rows come off `inline_keyboard`. A reply keyboard
            (`kb.home_keys()`) carries no callback data at all and answers `[]`.
            """
            rows = getattr(self.markup, "inline_keyboard", None) or []
            return [b.callback_data for row in rows for b in row
                    if getattr(b, "callback_data", None)]

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
        def __init__(self):
            self.sent: list[str] = []

        async def send_message(self, _chat_id, text, **_kw):
            self.sent.append(text)

        async def edit_message_text(self, _chat_id, message_id, text,
                                    reply_markup=None, **_kw):
            return await live[message_id].edit_text(text, reply_markup=reply_markup)

    client = FakeClient()
    queue = Queue(client, workers=1)          # never started: nothing runs them
    app = FakeApp()
    fap.register(app, queue)

    work = home / "work"
    work.mkdir()
    real_work_dir = cfg.work_dir
    object.__setattr__(cfg, "work_dir", work)

    # The two things that would leave the box. `resolve` stands in for the HTTP call
    # to the operator's resolver and answers with the payload measured off the real one;
    # `fetch_to_mp4` stands in for ffmpeg walking an HLS playlist.
    asked: list[str] = []

    async def fake_resolve(url):
        asked.append(url)
        return fapmod.parse(SAMPLE, url)

    async def fake_fetch(_url, out_path, **_kw):
        out_path.write_bytes(b"v" * 4096)

    async def fake_send(_client, _chat_id, target, **_kw):
        return uploader.Sent(message=None, size_bytes=target.stat().st_size,
                             seconds=0.01, parts=1)

    faphouse.resolve = fake_resolve            # instance attr; popped in the finally
    real_fetch, real_send = media.fetch_to_mp4, uploader.send_best_effort
    media.fetch_to_mp4 = fake_fetch
    uploader.send_best_effort = fake_send
    try:
        # 1. The menu's 🔥 button. That very message becomes the prompt — the flow
        #    owns one message on screen from here to the video.
        panel = Msg("☰ <b>Menu</b>", BOT)
        asyncio.run(app.handlers["open_fap"](client, Press("mode:soon", panel)))
        check("the menu message turns into the Fap prompt",
              "Send me the video link" in panel.text, True)
        check("nothing new was posted for it", panel.replies, [])
        mode, payload = state.get_mode(USER)
        check("the flow is open", mode, fap.MODE)
        check("and it is holding that message's id", payload.get("panel"), panel.id)

        # 2. Paste the link the operator actually sends — tracking fragment and all.
        start_balance = credits.balance(USER)
        pasted = Msg(TRACKED, USER)
        asyncio.run(app.handlers["got_link"](client, pasted))
        check("the prompt itself became the quality menu",
              "Pick a quality" in panel.text, True)
        check("and it names no video, on screen or anywhere near it",
              "Japanese Girl" in panel.text, False)
        check("so no second message appeared", pasted.replies, [])
        check("and the pasted link is out of the chat", pasted.deleted, True)
        check("the resolver was asked once", len(asked), 1)
        check("with the link as the user sent it - the fragment is cut at the wire",
              asked[0], TRACKED)
        picks = [d for d in panel.buttons() if d.startswith("q:")]
        check("three quality buttons are offered", len(picks), 3)
        check("and they are the rungs the video has",
              [d.rsplit(":", 1)[1] for d in picks], ["480p", "720p", "1080p"])
        check("with a Cancel beside them",
              [d for d in panel.buttons() if d.startswith("job:cancel:")] != [], True)
        check("reading the link costs nothing", credits.balance(USER), start_balance)

        # 3. Tap 720p. **This is the tap that spends.**
        chosen = next(d for d in picks if d.endswith(":720p"))
        press = Press(chosen, panel)
        asyncio.run(app.handlers["pick_quality"](client, press))
        check("the tap is acknowledged so the button stops spinning",
              press.answers, ["Starting…"])
        check("exactly the quoted price is taken, once",
              credits.balance(USER), start_balance - cfg.cost_fap_720)
        check("the menu became the waiting panel", "720p" in panel.text, True)
        check("which can still be cancelled",
              [d for d in panel.buttons() if d.startswith("job:cancel:")] != [], True)
        check("and no quality button is left to tap twice",
              [d for d in panel.buttons() if d.startswith("q:")], [])

        job = queue._q[jobq.LINK_LANE].get_nowait()
        check("the job went down the link lane, not the archive one",
              queue._q[jobq.ZIP_LANE].qsize(), 0)
        check("it is animating that very message", job.payload["status"] is panel, True)
        check("it carries the price that was quoted", job.cost, cfg.cost_fap_720)
        check("and the quality that was tapped", job.quality, "720p")
        check("and the stream that quality was priced for",
              job.payload["stream"].height, 720)
        check("the pasted link is what gets logged", job.source, TRACKED)
        check("and the video's own length rides along, so the bar has a total",
              job.payload["seconds"], 1523.0)

        # 4. A second tap on the same menu. `state.take` already spent the token, so
        #    this is where a double tap dies instead of charging twice.
        again = Press(chosen, panel)
        asyncio.run(app.handlers["pick_quality"](client, again))
        check("a second tap is refused", "no longer live" in again.answers[0], True)
        check("and cannot charge twice",
              credits.balance(USER), start_balance - cfg.cost_fap_720)

        # 5. Run it through the real `_run_one`, so the marking and the money are the
        #    queue's own code and not a copy of it here.
        asyncio.run(queue._run_one(job))
        check("the panel is taken away once the video has gone out",
              panel.deleted, True)
        check("and the bot never posted a second message for it",
              sum(1 for m in live.values() if m.from_user.id == BOT), 1)
        check("the file was named for the user, not for the worker",
              job.file_name, "Japanese Girl Loves Hard Sex [720p]")
        check("its size is what actually went out", job.size_bytes, 4096)
        check("a delivered job stays charged",
              credits.balance(USER), start_balance - cfg.cost_fap_720)
        row = conn.execute("SELECT status, charged, cost, quality FROM jobs "
                           "WHERE id = ?", (job.row_id,)).fetchone()
        check("the row says done", row["status"], "done")
        check("and that it was paid for", row["charged"], 1)
        check("at the price on the button", row["cost"], cfg.cost_fap_720)
        check("for the quality that was picked", row["quality"], "720p")
        check("and nothing of it is left on disk", list(work.iterdir()), [])
        # `_run_one` owns the money and the row; the *worker loop* owns the slot, in a
        # `finally` so a crash cannot leak one. This test drives `_run_one` directly,
        # so it has to hand the slot back the way `_worker` does — asserted rather
        # than quietly done, because a leaked slot would lock the user out for good.
        check("the worker loop, not _run_one, is what frees the slot",
              queue.busy(USER), 1)
        queue._release(job)
        check("and once it does, the user may send another link", queue.busy(USER), 0)

        # 6. The same route again, but ffmpeg fails. Nobody pays for a video that did
        #    not arrive, and the refund is the queue's, not this route's.
        #
        # A fresh panel: the first one was *deleted* when its video went out, and a
        # flow that kept editing a deleted message would be testing a fake that is
        # kinder than Telegram. `answered` reads the reply wherever the flow put it —
        # into the panel it was handed, or as a new message when it has none.
        panel = Msg("☰ <b>Menu</b>", BOT)

        def answered(msg):
            return msg.replies[-1] if msg.replies else panel

        async def angry_fetch(_url, _out, **_kw):
            raise RuntimeError("ffmpeg fell over")

        media.fetch_to_mp4 = angry_fetch
        state.set_mode(USER, fap.MODE, panel=panel.id, chat=USER)
        second = Msg(TRACKED, USER)
        asyncio.run(app.handlers["got_link"](client, second))
        card = answered(second)
        doomed = next(d for d in card.buttons() if d.endswith(":480p"))
        before_fail = credits.balance(USER)
        asyncio.run(app.handlers["pick_quality"](client, Press(doomed, card)))
        check("the cheap rung was charged first",
              credits.balance(USER), before_fail - cfg.cost_fap_480)
        bad = queue._q[jobq.LINK_LANE].get_nowait()
        asyncio.run(queue._run_one(bad))
        check("a failed job gives the credit back",
              credits.balance(USER), before_fail)
        check("and the user is told, with the refund in writing",
              "refunded" in client.sent[-1], True)
        check("without a stack trace in it", "Traceback" in client.sent[-1], False)
        failed_row = conn.execute("SELECT status, charged FROM jobs WHERE id = ?",
                                  (bad.row_id,)).fetchone()
        check("the row says failed", failed_row["status"], "failed")
        check("and no longer says paid", failed_row["charged"], 0)
        check("and the scratch directory went with it", list(work.iterdir()), [])
        queue._release(bad)
        media.fetch_to_mp4 = fake_fetch

        # 7. Cancel a job that is already running. **The Cancel button on a live Fap
        #    panel is answered by `terabox.cancel_job`** — pyrogram runs only the first
        #    handler that matches `job:cancel:`, so one handler knows every flow's
        #    kinds. A running fap job is parked as `"fap"`, which is deliberately *not*
        #    in that handler's "nothing was charged yet" list: the flag is set and the
        #    refund is the queue's to make.
        from bot.handlers import terabox as tbh            # noqa: PLC0415

        tbh.register(app, queue)
        state.set_mode(USER, fap.MODE, panel=panel.id, chat=USER)
        quitter = Msg(TRACKED, USER)
        asyncio.run(app.handlers["got_link"](client, quitter))
        quit_card = answered(quitter)
        take = next(d for d in quit_card.buttons() if d.endswith(":1080p"))
        before_cancel = credits.balance(USER)
        asyncio.run(app.handlers["pick_quality"](client, Press(take, quit_card)))
        live_job = queue._q[jobq.LINK_LANE].get_nowait()
        check("the dearest rung was charged",
              credits.balance(USER), before_cancel - cfg.cost_fap_1080)
        check("the running job is parked as fap, not fap_pick",
              state.peek(live_job.token, USER).kind, "fap")

        stop = Press(f"job:cancel:{live_job.token}", quit_card)
        asyncio.run(app.handlers["cancel_job"](client, stop))
        check("Cancel does not claim nothing was charged, because it was",
              "nothing was charged" in stop.answers[0].lower(), False)
        check("it promises the credits back instead",
              "credits come back" in stop.answers[0], True)
        check("and the job knows it is cancelled", live_job.cancelled, True)

        sent_before = len(client.sent)
        asyncio.run(queue._run_one(live_job))
        queue._release(live_job)
        check("a cancelled job is refunded in full",
              credits.balance(USER), before_cancel)
        cancelled_row = conn.execute("SELECT status, charged FROM jobs WHERE id = ?",
                                     (live_job.row_id,)).fetchone()
        check("the row says cancelled", cancelled_row["status"], "cancelled")
        check("and not charged", cancelled_row["charged"], 0)
        check("no video was uploaded for it", len(client.sent), sent_before)
        check("and the disk is clean again", list(work.iterdir()), [])


        #    the resolver is troubled at all — "one credit gone, an error, credit
        #    back" is a rotten answer to something that was never going to work.
        spent = credits.balance(USER)
        credits.charge(USER, spent, "test: empty the wallet")
        calls_before = len(asked)
        state.set_mode(USER, fap.MODE, panel=panel.id, chat=USER)
        broke = Msg(TRACKED, USER)
        asyncio.run(app.handlers["got_link"](client, broke))
        answer = answered(broke)
        check("an empty wallet is told so", "credit" in answer.text.lower(), True)
        check("and the resolver was never called for it", len(asked), calls_before)
        check("no menu is offered", [d for d in answer.buttons()
                                     if d.startswith("q:")], [])
        check("and nothing was charged for the refusal", credits.balance(USER), 0)
        credits.grant(USER, 10, "test: refill")

        # 8. One job per person, checked at the door as well as at the tap.
        state.set_mode(USER, fap.MODE, panel=panel.id, chat=USER)
        parked_job = Msg(TRACKED, USER)
        asyncio.run(app.handlers["got_link"](client, parked_job))
        held_card = answered(parked_job)
        first = next(d for d in held_card.buttons() if d.endswith(":480p"))
        asyncio.run(app.handlers["pick_quality"](client, Press(first, held_card)))
        running = credits.balance(USER)
        state.set_mode(USER, fap.MODE, panel=panel.id, chat=USER)
        while_busy = Msg(TRACKED, USER)
        asyncio.run(app.handlers["got_link"](client, while_busy))
        check("a second link while one is running is refused",
              "already" in answered(while_busy).text.lower(), True)
        check("and costs nothing", credits.balance(USER), running)

        # Leave nothing running behind this test: the held job is still queued and
        # `stop()` is the code that owes its owner the credit back.
        stranded = credits.balance(USER)
        asyncio.run(queue.stop())
        check("a restart refunds whatever was still queued",
              credits.balance(USER), stranded + cfg.cost_fap_480)

    finally:
        faphouse.__dict__.pop("resolve", None)
        media.fetch_to_mp4 = real_fetch
        uploader.send_best_effort = real_send
        object.__setattr__(cfg, "work_dir", real_work_dir)
        state.clear_mode(USER)
        conn.close()
        _shutil.rmtree(home, ignore_errors=True)


test_the_whole_route_from_key_to_video()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
