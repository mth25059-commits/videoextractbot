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

# The provider module itself needs no pyrogram; base.py needs none either.
_pyrogram = types.ModuleType("pyrogram")
_pyrogram.Client = type("Client", (), {})
sys.modules.setdefault("pyrogram", _pyrogram)

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

    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
