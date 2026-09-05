"""
Terabox — turning a share link into something fetchable.

A Terabox share page is a JavaScript app; the video is not in the HTML. What the
page does behind the scenes is what this module does directly:

    /s/1abc…  ──►  surl=abc…  ──►  /share/list?shorturl=abc…       names, sizes,
                   (redirect)      /api/shorturlinfo?shorturl=1abc…  fs_ids, dlink
                                                     │
                                                     └──►  dlink  ──►  the bytes
                                                           (302 to a CDN host)

**The leading `1` is not part of the key, and the two endpoints disagree about
it.** A share path is `/s/1<key>`, and Terabox's own redirect spells that same
share `?surl=<key>`. `/share/list` wants the bare key and answers `errno 140` to
the `1`-prefixed one — which was every request this module had ever sent, so the
listing failed for a reason that had nothing to do with credentials.
`/api/shorturlinfo` wants it the other way round, *with* the `1`, and answers
`errno 2` without. Both measured; a wrong-spelling retry covers the ambiguity of a
hand-pasted `?surl=`.

**A logged-in cookie — and it is only logged in on one host.** Anonymous listing
works: names, sizes, `fs_id`, `path`, `md5` and thumbnails all come back to a
signed-out caller. `dlink` does not; the field is there and empty. What fills it,
measured end to end on 4 September 2026, is three things at once:

    1. the *home* host — the one the session is bound to. `TERABOX_COOKIE` is
       accepted by exactly one of Terabox's domains and answers `errno -6 user not
       login` on every other, and it is not `www.terabox.com`: Terabox redirects
       the browser to whichever host it gave the account (`dm.1024terabox.com` for
       the first live cookie). So it is discovered, per cookie, by `_home`.
    2. `jsToken` and `bdstoken`, scraped from `<home>/main` — a page the home host
       renders *as the operator*. Scraping them from the share page instead is the
       trap: `/s/…` redirects to `www.terabox.app`, which is a guest there, so its
       `jsToken` is a guest token and `/share/list` answers `errno 4000020`.
    3. `/share/list` with the bare surl. **This is where `dlink` comes from now** —
       308 characters of it, pointing at `dm-d.1024terabox.com`, which answers
       `HTTP 206 video/mp4` and supports Range.

`/api/shorturlinfo` never carries a `dlink` even signed in, so it is asked only for
the `sign`/`timestamp` pair the HLS fallback needs, and only when something is
missing a link. `/share/tplconfig` does not exist on the home host (404 HTML), and
`/share/download` is not needed at all.

**And a second route for when there is no cookie.** `TERABOX_FALLBACK` sends the
link to iteraplay.com instead, which has a signed-in account of its own and hands
back the original file — full quality, verified byte-for-byte, no Terabox login
anywhere. It is second choice rather than first because it discloses every link it
handles to a third party and comes with a quota of five videos per six hours for a
caller with no account there; `bot/providers/iteraplay.py` documents both. With a
cookie present Terabox is asked first and this only catches the failure.

**The API host moves.** The same share redirected to `www.terabox.app` and to
`www.1024tera.com` twenty minutes apart, and set a `shareRedirectDomain` cookie
saying so. That redirect decides where an *anonymous* listing is asked. It does not
decide where a signed call goes — those are asked of the home host above, because
the session is only valid there.

**A browser TLS fingerprint.** Terabox fronts its API with a WAF that rejects
Python's handshake before it ever looks at the headers, so every request goes
through `curl_cffi` with `impersonate` set. Plain `requests`/`httpx` get a 403
that looks like a bad cookie and is not.

**`dlink` is not the file.** It is a signed, short-lived redirect that only works
with the same cookie and User-Agent that asked for it, and it expires in minutes
— so it is resolved as late as possible, not cached.

Quality: there is no quality menu here. The original upload *is* the highest
quality, so a link resolves to exactly one stream — the file itself. The HLS
transcode is only a fallback for when `dlink` is refused, and it is capped at
1080p by Terabox, which is why it is second choice and not first.

Everything above was measured against live shares — the anonymous half on
3 September 2026, the signed half on 4 September 2026 with the operator's own cookie,
which resolved both trial links and served their first mebibyte as
`video/mp4` at `44,739,275` and `40,642,362` bytes, matching the sizes Terabox
reports exactly. `tests/test_terabox.py` covers the parsing against recorded
response shapes.

One warning worth keeping: a soft rate limit reads as a dead cookie. The same call
that answered `errno 0` answered `errno 400210` a few dozen requests later from the
same address, then recovered on its own. So `400210`/`4000020` after a burst means
back off, not "replace the cookie".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, quote, urlparse

from .base import Provider, ResolveError, Resolved, Stream, register
from . import iteraplay
from ..config import cfg

log = logging.getLogger(__name__)

#: Every domain Terabox has shipped the same product under. Adding one is the
#: cheapest fix there is: a host missing from here does not read as a Terabox link
#: at all, so the bot stays silent and the user thinks it ignored them.
#: `terasharefile.com` was missing until the operator pasted two links on it.
HOSTS = frozenset({
    "terabox.com", "www.terabox.com", "terabox.app", "www.terabox.app",
    "1024terabox.com", "www.1024terabox.com", "1024tera.com", "www.1024tera.com",
    "teraboxapp.com", "www.teraboxapp.com",
    "teraboxlink.com", "www.teraboxlink.com", "terasharelink.com", "www.terasharelink.com",
    "terasharefile.com", "www.terasharefile.com",
    "4funbox.com", "www.4funbox.com", "mirrobox.com", "www.mirrobox.com",
    "nephobox.com", "www.nephobox.com", "momerybox.com", "www.momerybox.com",
    "tibibox.com", "www.tibibox.com", "freeterabox.com", "www.freeterabox.com",
    "terafileshare.com", "www.terafileshare.com",
})

API = "https://www.terabox.com"
APP_ID = "250528"

#: Where to look for the host that accepts `TERABOX_COOKIE`, best guess first.
#: One of these answers `/api/check/login` with `errno 0`; the rest answer `errno -6`
#: for the same cookie, because a Terabox session is bound to the host that issued
#: it. `dm.` leads because that is the one a live account was actually found on —
#: Terabox redirects `www.terabox.com` to it in the browser — and `www.terabox.com`
#: is kept last rather than dropped, since an account created there would sit there.
HOME_HOSTS = (
    "https://dm.1024terabox.com",
    "https://www.1024terabox.com",
    "https://www.terabox.app",
    "https://www.1024tera.com",
    "https://www.terabox.com",
)

#: How long a scraped `jsToken`/`bdstoken` pair is reused before `<home>/main` is
#: read again. A ten-link batch would otherwise fetch that page ten times; the pair
#: is a session token and outlives a batch comfortably, and a stale one fails loudly
#: (`errno 4000020`) rather than quietly, so a short window costs nothing.
TOKEN_TTL_SECONDS = 300

#: The UA has to match the one the dlink was issued to, so it is fixed here
#: rather than left to curl_cffi's default, which varies by impersonation target.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv",
             ".wmv", ".mpg", ".mpeg", ".m2ts", ".3gp"}

#: What Terabox answers when the link is fine but we are not.
ERRNO_HELP = {
    -6: "Terabox refused the session. The TERABOX_COOKIE has expired — sign in "
        "again and replace it.",
    -7: "That link has been removed or made private.",
    -9: "That link does not exist any more.",
    -12: "Terabox is rate-limiting this account. Wait a few minutes.",
    2: "Terabox could not read that link.",
    105: "That is not a share link Terabox recognises.",
    117: "That link needs a password, which is not supported yet.",
    # 140 means the shorturl was spelled the way the *other* endpoint wants it —
    # see `surl_of`. `_listing` retries the other spelling before this can surface,
    # so reaching the user means both spellings were refused.
    130: "Terabox will not transcode that file for this account.",
    140: "Terabox did not recognise that share code.",
    # Measured against a live share on 3 September 2026 with no cookie at all:
    # `/share/download` answers 400310 and the page bundle ships
    # `/api/vcode/v2/get` + `/verify`, so "verify_v2" is a captcha — one a
    # signed-in session is not asked to solve.
    400310: "Terabox asked for a captcha instead of the file, which only happens "
            "to a signed-out session. The TERABOX_COOKIE is missing or has "
            "expired — sign in again and replace it.",
    # A signed-out session with a `dp-logid` gets these two. We no longer send
    # that parameter, so they should not appear; the text stays in case they do.
    460020: "Terabox will not serve this link to a signed-out session. The "
            "TERABOX_COOKIE is missing or has expired — sign in again and "
            "replace it.",
    400210: "Terabox will not serve this link to a signed-out session. The "
            "TERABOX_COOKIE is missing or has expired — sign in again and "
            "replace it.",
}

#: The refusals another cookie might not get, and the only ones worth rotating for.
#: `-6` is a session Terabox no longer recognises, `4000020` a page token that went
#: stale mid-batch, `-12`/`400210`/`460020` a soft rate limit the same account
#: recovers from on its own (measured: `errno 0`, then `400210` a few dozen requests
#: later from the same address, then fine again).
#:
#: Everything else is a property of the *link* — deleted, private, password-locked,
#: not a share at all — and every account on earth is refused it identically. Trying
#: five cookies for one of those spends five accounts' goodwill and makes the user
#: wait five times as long for the same answer.
COOKIE_ERRNOS = frozenset({-6, -12, 400210, 460020, 4000020})


class CookieRefused(ResolveError):
    """A refusal that belongs to the credential rather than to the link.

    A `ResolveError` subclass on purpose: every existing `except ResolveError` — the
    fallback route, the queue's refund path, the handler's user message — keeps
    working unchanged, and only the code that wants to rotate has to know about it.
    """

    def __init__(self, message: str, errno: int = 0) -> None:
        super().__init__(message)
        self.errno = errno


def _refusal(errno: int, message: str) -> ResolveError:
    """The right exception class for one errno, so callers can tell them apart."""
    if errno in COOKIE_ERRNOS:
        return CookieRefused(message, errno)
    return ResolveError(message)


@dataclass(frozen=True)
class Item:
    """One file inside a share link."""

    name: str
    size: int
    fs_id: str
    path: str
    dlink: str = ""
    is_dir: bool = False

    @property
    def is_video(self) -> bool:
        dot = self.name.lower().rfind(".")
        return dot != -1 and self.name.lower()[dot:] in VIDEO_EXT


@dataclass(frozen=True)
class CookieHealth:
    """One cookie's answer to "are you still signed in, and where?".

    `home` is the whole point of reporting this at all: a session is bound to one
    `HOME_HOSTS` entry and answers `-6` on the rest, so "not signed in" and "asked
    the wrong host" look identical from the outside. Printing which host answered
    turns that into a fact instead of a guess.
    """

    index: int
    ok: bool
    errno: int = 0
    home: str = ""
    used_bytes: int = 0
    total_bytes: int = 0
    tokens: bool = False
    detail: str = ""

    @property
    def full_percent(self) -> float:
        return (self.used_bytes / self.total_bytes * 100) if self.total_bytes else 0.0


def surl_of(url: str) -> str:
    """
    The share key, spelled the way Terabox's own `?surl=` spells it — no leading `1`.

        /s/1abcdef            -> abcdef    (the `1` belongs to the path, not the key)
        ?surl=abcdef          -> abcdef
        /sharing/link?surl=…  -> …

    `/share/list` wants this form and answers `errno 140` to any other. The one
    endpoint that wants the `1` back gets it from `prefixed()`.

    A hand-pasted `?surl=1abcdef` is ambiguous — a key may legitimately begin with
    `1` — so the `1` is only stripped from `/s/` paths, where it is always the
    prefix. `_listing` retries the other spelling rather than guessing here.
    """
    parsed = urlparse(url.strip())
    query = parse_qs(parsed.query)
    if "surl" in query and query["surl"][0]:
        return query["surl"][0]
    match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise ResolveError(
            "That does not look like a Terabox share link. It should look "
            "like https://terabox.com/s/1abc…")
    key = match.group(1)
    return key[1:] if key.startswith("1") and len(key) > 1 else key


def prefixed(surl: str) -> str:
    """The same key as `/api/shorturlinfo` wants it: with the leading `1`."""
    return surl if surl.startswith("1") else "1" + surl


def cookies() -> tuple[str, ...]:
    """Every configured Terabox cookie, best first, de-duplicated.

    `cfg.terabox_cookie` is folded in live rather than trusted to already be element
    zero of `cfg.terabox_cookies`. `load()` does put it there, but that one field is
    what an admin reload or a test changes on its own, and the rotation has to
    follow it rather than a snapshot taken at import time.
    """
    out: list[str] = []
    for candidate in (cfg.terabox_cookie, *cfg.terabox_cookies):
        value = (candidate or "").strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def first_cookie() -> str:
    """The cookie to use when a caller did not name one, or `""` when none is set."""
    pool = cookies()
    return pool[0] if pool else ""


def cookie_plan(pool: tuple[str, ...], *, budget: int, start: int = 0) -> list[str]:
    """The cookies to try for one link, in order, starting at `start`.

    Same shape as `iteraplay.plan` and for the same reason: the starting offset
    rotates per link, so a ten-link batch does not hammer cookie one ten times and
    then discover it is throttled ten times. `budget` bounds it — five cookies
    against a link that is simply deleted is five pointless round-trips.
    """
    accounts = tuple(c for c in pool if c)
    if not accounts:
        return []
    total = max(1, min(int(budget), len(accounts)))
    return [accounts[(start + step) % len(accounts)] for step in range(total)]


def cookie_header(cookie: str | None = None) -> str:
    """One Terabox cookie as a `Cookie:` header value.

    Pasting the bare `ndus` value without its name is the common mistake, and
    `.env.example` promises it works, so the name is put back. It has to happen in
    one place: the resolve path used to normalise and the download path did not, so
    a bare value resolved a `dlink` and was then refused by the CDN — the one
    failure that looks like an expired cookie but is a config typo.

    The cookie is an argument rather than a global read so that one attempt of a
    rotating resolve cannot pick up a different account's credential halfway
    through. `None` means "whichever is configured first", which is what every
    caller outside the rotation wants.
    """
    value = (cookie if cookie is not None else first_cookie()).strip()
    if not value:
        return ""
    return value if "=" in value else f"ndus={value}"


#: Where the next link starts its walk through the cookie pool. Advanced once per
#: resolve so a ten-link batch spreads its first attempts across the accounts
#: instead of putting all ten on cookie one.
_cursor = 0

#: Breathing room between two attempts on the same link. `400210` is a *rate*
#: limit that recovers on its own — the same call answered `errno 0` a few dozen
#: requests earlier — so rolling straight into the next account at full speed is
#: how one throttled cookie becomes four. Short enough that a user waiting on a
#: real failover does not notice it.
ROTATE_PAUSE_SECONDS = 1.5


def share_context(payload: dict) -> dict[str, str]:
    """
    The signature bundle `/api/shorturlinfo` hands back, as flat strings.

    `sign` and `timestamp` are a pair — `/share/streaming` rejects one without the
    other — and `uk`/`shareid` identify whose share it is. All four are needed to
    ask for the HLS fallback, and all four come back to an anonymous caller.
    """
    if not isinstance(payload, dict):
        return {}
    return {
        "sign": str(payload.get("sign") or ""),
        "timestamp": str(payload.get("timestamp") or ""),
        "uk": str(payload.get("uk") or payload.get("uk_str") or ""),
        "shareid": str(payload.get("shareid") or payload.get("share_id") or ""),
    }


def errno_of(payload: dict) -> int:
    """
    The error code, out of whichever field this particular response used.

    `errno` is the documented one, but the WAF replies with `code` and no `errno`
    at all — so reading only `errno` turned "need verify" into an empty file list
    and a puzzling "nothing to download" for the user.
    """
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("errno") or payload.get("code") or 0)
    except (TypeError, ValueError):
        return 0


def parse_list(payload: dict, *, only_videos: bool = True) -> list[Item]:
    """
    Turn a `/share/list` response into items.

    Kept as a plain function over already-decoded JSON so the shape handling is
    testable without a network or a cookie — which is most of what breaks here.
    """
    if not isinstance(payload, dict):
        raise ResolveError("Terabox sent something unreadable.")

    errno = errno_of(payload)
    if errno:
        raise _refusal(errno, ERRNO_HELP.get(
            errno, f"Terabox refused that link (error {errno})."))

    items: list[Item] = []
    for row in payload.get("list") or []:
        try:
            name = str(row.get("server_filename") or row.get("filename") or "").strip()
            is_dir = str(row.get("isdir", "0")) in ("1", "true", "True")
            item = Item(
                name=name,
                size=int(row.get("size") or 0),
                fs_id=str(row.get("fs_id") or ""),
                path=str(row.get("path") or ""),
                dlink=str(row.get("dlink") or ""),
                is_dir=is_dir,
            )
        except (TypeError, ValueError):
            continue
        if not item.name:
            continue
        if only_videos and not item.is_dir and not item.is_video:
            continue
        items.append(item)

    items.sort(key=lambda i: (i.is_dir, -i.size))
    return items


#: The query every call carries. Copied from the bundle's own `commonParamMaker`,
#: minus one field: `dp-logid`. That parameter is what makes Terabox answer
#: `code 460020 need verify` — measured both ways, with and without a cookie, and
#: it is the difference between a file list and a refusal. Do not add it back.
COMMON = f"app_id={APP_ID}&web=1&channel=dubox&clienttype=0"


def hls_url(context: dict[str, str], fs_id: str, *, origin: str = API,
            quality: str = "M3U8_AUTO_1080") -> str:
    """
    The transcoded fallback, for a *share* rather than for a file we own.

    `/api/streaming?path=` — what this used to build — only ever works for a file
    inside the calling account, so for someone else's share it answers `errno -6`
    however good the cookie is. Shares are asked for through `/share/streaming`,
    which needs `uk`, `shareid`, `fid` and the `sign`/`timestamp` pair from
    `share_context()`; without the pair it answers `errno 2 invalid timestamp`.

    Terabox caps this at 1080p even for a 4K upload, which is why it is the
    fallback and not the first choice.
    """
    return (f"{origin}/share/streaming?uk={quote(context.get('uk', ''), safe='')}"
            f"&shareid={quote(context.get('shareid', ''), safe='')}"
            f"&fid={quote(fs_id, safe='')}&type={quality}"
            f"&sign={quote(context.get('sign', ''), safe='')}"
            f"&timestamp={quote(context.get('timestamp', ''), safe='')}"
            f"&esl=1&isplayer=1&{COMMON}")


class Terabox(Provider):
    name = "terabox"
    label = "Terabox"

    #: Both keyed on the cookie they were found for, so replacing `TERABOX_COOKIE`
    #: re-probes instead of inheriting the previous account's host and tokens.
    _home_for: tuple[str, str] | None = None
    _tokens_for: tuple[tuple[str, str], float, str] | None = None

    @property
    def max_batch(self) -> int:
        return cfg.max_links_per_batch

    def matches(self, text: str) -> bool:
        try:
            parsed = urlparse(text.strip())
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.netloc or "").lower().split(":")[0]
        if host not in HOSTS:
            return False
        return bool(re.search(r"/s/[A-Za-z0-9_-]+", parsed.path)
                    or "surl=" in (parsed.query or ""))

    def unavailable(self) -> str:
        """
        Why this provider cannot resolve anything at all right now, or "".

        `_session()` refuses the same case, but it runs on a worker — after the
        credit has been taken. It is refunded, so no money is lost, yet "one credit
        gone, an error, credit back" is a rotten answer to something that was never
        going to work. Cheap enough to ask at the door, so the door asks.

        A signed-out session can read a share — names, sizes, `fs_id`, thumbnails —
        but not download one: `dlink` comes back empty and `/share/download` asks
        for a captcha (`errno 400310`, measured 3 September 2026). So bytes need
        either the cookie or `TERABOX_FALLBACK`, and with neither there is nothing
        here worth charging for.
        """
        if not cookies() and not cfg.terabox_fallback:
            return ("🔧 <b>Terabox is not switched on yet</b>\n\n"
                    "The bot still needs its Terabox sign-in "
                    "(<code>TERABOX_COOKIE</code>) before it can fetch anything. "
                    "<b>Nothing was charged.</b>")
        return ""

    # --- the network half ----------------------------------------------------

    def _session(self, *, cookie: bool | str = True, proxy: str | None = None):
        """One browser-shaped session. Imported late so the module loads without curl_cffi.

        `cookie` is three-valued. `False` is the cookieless route, which talks to
        iteraplay rather than to Terabox and must not carry the operator's
        credentials to a third party. A string is one specific cookie, which is what
        the rotation passes. `True` means whichever is configured first.

        `proxy` is honoured exactly as given, **and nothing is chosen here.** This
        used to default to `cfg.proxies[0]`, which sent every signed API call through
        one address for no gain: resolving through a proxy measured flakier (1 of 3
        attempts died with an SSL timeout) and Terabox does not bind a `dlink` to the
        address that asked for it. So the resolve leaves directly and only the
        download rotates — see `bot/egress.py`. The cookieless route still passes one
        explicitly, because iteraplay's quota *is* counted per address.

        Every message raised from here is shown to the user through `ui.esc`, so
        these strings stay plain text — no tags, they would arrive as literal
        angle brackets. The refund line is added by the queue, not here.
        """
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError as exc:      # pragma: no cover - dependency check
            raise ResolveError(
                "The Terabox client is not installed on the server "
                "(pip install curl_cffi).") from exc

        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cookie is not False:
            value = cookie_header(cookie if isinstance(cookie, str) else None)
            if not value:
                raise ResolveError(
                    "Terabox is not set up on this bot yet — the operator has to "
                    "add TERABOX_COOKIE.")
            headers["Cookie"] = value
            headers["Referer"] = f"{API}/"
        return AsyncSession(
            impersonate="chrome124",
            timeout=45,
            headers=headers,
            proxy=proxy,
        )

    async def _get_json(self, session, url: str, *, referer: str = "") -> dict:
        headers = {"Referer": referer} if referer else None
        response = await session.get(url, allow_redirects=True, headers=headers)
        if response.status_code == 403:
            # Raised as a credential refusal so the rotation moves on: a 403 here is
            # about who asked, not about the link, and the next account may well be
            # fine. `-6` is Terabox's own "user not login", the closest thing it has.
            raise _refusal(
                -6,
                "Terabox blocked the request. Usually the cookie has expired.")
        if response.status_code >= 400:
            raise ResolveError(f"Terabox answered {response.status_code}.")
        try:
            return json.loads(response.text)
        except ValueError:
            # An HTML body here means a login wall, not a JSON API.
            raise _refusal(
                -6,
                "Terabox returned a login page instead of the file list — the "
                "cookie is no longer valid.") from None

    async def _home(self, session, cookie: str | None = None) -> str:
        """
        The one host this cookie is actually signed in on.

        A Terabox session is bound to the host that issued it. The same cookie that
        answers `/api/check/login` with `errno 0` and a real `uk` on its own home
        host answers `errno -6 user not login` on every other domain — the cookie is
        scoped `.1024terabox.com` so the browser sends it everywhere, and the
        rejection is server-side. Asking the wrong host is indistinguishable from
        having no cookie at all, which is the shape of every earlier failure here.

        So it is discovered rather than configured: whichever candidate says `errno
        0` wins, and the answer is cached against the cookie it was found for, so a
        replaced cookie — or the next one in the rotation — re-probes instead of
        inheriting a stale host.
        """
        active = cookie if cookie is not None else first_cookie()
        cached = getattr(self, "_home_for", None)
        if cached and cached[0] == active:
            return cached[1]
        for origin in HOME_HOSTS:
            try:
                payload = await self._get_json(
                    session, f"{origin}/api/check/login?{COMMON}",
                    referer=f"{origin}/main")
            except ResolveError as exc:
                log.info("terabox: %s did not answer check/login (%s)", origin, exc)
                continue
            code = errno_of(payload)
            if code == 0 and str(payload.get("uk") or "0") != "0":
                log.info("terabox: signed in on %s", origin)
                self._home_for = (active, origin)
                return origin
        raise _refusal(
            -6,
            "The TERABOX_COOKIE is not signed in to any Terabox domain — sign in "
            "again in a browser and replace it.")

    async def _tokens(self, session, home: str, cookie: str | None = None) -> str:
        """
        `&jsToken=…&bdstoken=…`, ready to append, scraped from `<home>/main`.

        `/share/list` needs these on top of the cookie; without them a perfectly
        good session gets `errno 4000020`. They have to come from a page the *home*
        host renders as the operator. The obvious place to look — the share page —
        is the wrong one: `/s/…` redirects to `www.terabox.app`, which is signed out,
        so its token is a guest token and changes nothing.

        `jsToken` sits in the bundle URL-encoded as `fn%28"…"%29`, which is
        `fn("…")`; the plain JSON spelling is tried too because the page has shipped
        both.
        """
        cached = getattr(self, "_tokens_for", None)
        active = cookie if cookie is not None else first_cookie()
        if cached and cached[0] == (active, home) and cached[1] > time.monotonic():
            return cached[2]
        page = await session.get(f"{home}/main", allow_redirects=True,
                                 headers={"Accept": "text/html,application/xhtml+xml,*/*"})
        html = page.text or ""
        js = (re.search(r"fn%28%22([0-9A-Fa-f]{60,})%22%29", html)
              or re.search(r'"jsToken"\s*:\s*"([0-9A-Fa-f]{60,})"', html))
        if not js:
            raise _refusal(
                4000020,
                "Terabox did not hand over the page token needed to read shares — "
                "the TERABOX_COOKIE may have just expired.")
        bd = re.search(r'bdstoken"\s*:\s*"([0-9a-f]{32})"', html)
        query = f"&jsToken={quote(js.group(1), safe='')}"
        if bd:
            query += f"&bdstoken={bd.group(1)}"
        self._tokens_for = ((active, home),
                            time.monotonic() + TOKEN_TTL_SECONDS, query)
        return query

    async def _origin(self, session, url: str) -> str:
        """
        The host this particular share lives on.

        Terabox redirects the same share to different domains from one hour to the
        next — `www.terabox.app` and `www.1024tera.com` both turned up for the test
        link — and sets a `shareRedirectDomain` cookie to say which one it picked.
        Following the redirect asks the signed calls of whichever host answered,
        instead of a domain hardcoded here, and warms the cookie jar on the way.
        """
        try:
            page = await session.get(url, allow_redirects=True)
        except Exception as exc:                # noqa: BLE001 - any transport error
            # The host, not the link. Which domain refused to answer is the whole
            # content of this line; the share id only made `bot.log` a record of what
            # people fetched, which is the trail this bot is not supposed to leave.
            log.info("terabox: could not follow %s (%s), using %s",
                     urlparse(url).netloc or "the share host", exc, API)
            return API
        landed = urlparse(str(page.url) or "")
        if landed.scheme in ("http", "https") and landed.netloc:
            return f"{landed.scheme}://{landed.netloc}"
        return API

    async def _listing(self, session, origin: str, surl: str, scope: str,
                       tokens: str = "", referer: str = "") -> tuple[list[Item], str]:
        """
        `/share/list` for one scope, and the surl spelling that worked.

        With `tokens` this is the endpoint that carries `dlink`; without them it is
        the anonymous listing, which carries everything except that.

        `errno 140` means only that the key was spelled the way the *other*
        endpoint wants it, so the other spelling is worth one retry — cheaper than
        deciding up front whether a pasted `?surl=1…` had a real `1` in it. The
        spelling that answered is handed back so the folder calls skip the retry.
        """
        first = surl
        second = surl[1:] if surl.startswith("1") and len(surl) > 1 else prefixed(surl)
        for candidate in (first, second):
            payload = await self._get_json(
                session, f"{origin}/share/list?{COMMON}"
                         f"&shorturl={quote(candidate, safe='')}{scope}{tokens}",
                referer=referer)
            if errno_of(payload) == 140 and candidate != second:
                # Which *spelling* was refused, never the key itself: a surl in the log
                # is the content, and this line only ever needed to say that the retry
                # happened and in which direction.
                log.info("terabox: shorturl refused (errno 140), retrying with the "
                         "%s spelling",
                         "un-prefixed" if candidate.startswith("1") else "1-prefixed")
                continue
            return parse_list(payload), candidate
        raise ResolveError("Terabox did not recognise that share code.")

    async def _dlinks(self, session, origin: str, surl: str, scope: str,
                      tokens: str = "",
                      referer: str = "") -> tuple[dict[str, str], dict[str, str]]:
        """
        The signature bundle from `/api/shorturlinfo`, and any `dlink` it happens to carry.

        It carries none — the field is absent signed out *and* signed in, on the home
        host as much as anywhere else. What it is asked for is the `sign`/`timestamp`
        pair the HLS fallback needs, which no other endpoint hands over. It wants the
        *prefixed* spelling and `root=1`; bare gets `errno 2`, prefixed-without-root
        gets `errno -3`.

        A failure here is not fatal: the listing still stands, so this logs and
        returns nothing rather than losing the file names too.
        """
        try:
            payload = await self._get_json(
                session, f"{origin}/api/shorturlinfo?{COMMON}"
                         f"&shorturl={quote(prefixed(surl), safe='')}{scope}{tokens}",
                referer=referer)
        except ResolveError as exc:
            log.info("terabox: shorturlinfo unavailable (%s)", exc)
            return {}, {}
        code = errno_of(payload)
        if code:
            log.info("terabox: shorturlinfo refused (errno %s)", code)
            return {}, {}
        links = {}
        for row in payload.get("list") or []:
            fs_id, dlink = str(row.get("fs_id") or ""), str(row.get("dlink") or "")
            if fs_id and dlink:
                links[fs_id] = dlink
        return links, share_context(payload)

    @staticmethod
    def _fill(items: list[Item], links: dict[str, str]) -> list[Item]:
        """Copy in the dlinks the listing itself did not carry. `Item` is frozen."""
        return [item if item.dlink or not links.get(item.fs_id)
                else replace(item, dlink=links[item.fs_id])
                for item in items]

    async def _items(self, session, url: str,
                     cookie: str | None = None) -> tuple[list[Item], dict[str, str], str]:
        """
        Top level of the share, one directory deep if that is where the videos are.

        Two different hosts, depending on whether there is a cookie. Signed in, every
        call goes to the home host with the page tokens attached, because that is the
        only place the session is valid and the only way `/share/list` returns a
        `dlink`. Signed out, the share's own redirect target is used — it works for
        the listing, which is all an anonymous caller can have.

        `/api/shorturlinfo` is asked only when something still has no link, since it
        never supplies one and is really being asked for the HLS `sign`/`timestamp`.
        """
        surl = surl_of(url)
        active = cookie if cookie is not None else first_cookie()
        if active:
            origin = await self._home(session, active)
            tokens = await self._tokens(session, origin, active)
        else:
            origin = await self._origin(session, url)
            tokens = ""
        referer = f"{origin}/s/{prefixed(surl)}"

        items, spelling = await self._listing(session, origin, surl, "&root=1",
                                              tokens, referer)
        videos = [i for i in items if not i.is_dir]
        folders = [i for i in items if i.is_dir]
        context: dict[str, str] = {}
        if any(not i.dlink for i in videos):
            links, context = await self._dlinks(session, origin, surl, "&root=1",
                                                tokens, referer)
            videos = self._fill(videos, links)

        for folder in folders:
            if len(videos) >= cfg.terabox_max_files_per_link:
                break
            scope = f"&dir={quote(folder.path, safe='')}"
            try:
                inner, _ = await self._listing(session, origin, spelling, scope,
                                               tokens, referer)
            except ResolveError as exc:
                # No folder name: the depth is what makes an unreadable sub-folder
                # debuggable, and the name is the one part that describes the content.
                log.info("terabox: a sub-folder was unreadable (%s)", exc)
                continue
            inner = [i for i in inner if not i.is_dir]
            if any(not i.dlink for i in inner):
                inner_links, _ = await self._dlinks(session, origin, surl, scope,
                                                    tokens, referer)
                inner = self._fill(inner, inner_links)
            videos += inner

        if not videos:
            raise ResolveError(
                "No video was found behind that link — it may be photos, "
                "documents, or an empty folder.")
        videos.sort(key=lambda i: -i.size)
        return videos[:cfg.terabox_max_files_per_link], context, origin

    def _headers(self, origin: str, cookie: str | None = None) -> dict[str, str]:
        """What the downloader has to send to be the same caller that got the link.

        A `dlink` is signed against the cookie *and* the User-Agent that asked for
        it, and the CDN checks the Referer against the host that issued it — which
        is why the origin is threaded through rather than assumed. The cookie is
        threaded for the same reason: with rotation on, the link may have been issued
        to the second or third account, and sending the first one's `ndus` beside
        another account's signature is a 403.
        """
        headers = {"User-Agent": UA, "Referer": f"{origin}/"}
        value = cookie_header(cookie)
        if value:
            headers["Cookie"] = value
        return headers

    async def _direct_url(self, item: Item, origin: str = API,
                          cookie: str | None = None) -> tuple[str, int]:
        """
        Follow `dlink` to the CDN URL the bytes actually come from.

        Terabox answers the first hop with a 302; some mirrors answer 200 with the
        file. Either way the URL we end up with is what gets handed to the
        downloader, together with whatever size the CDN reports.

        **It gets its own session, and that is the whole point of this method.**
        Reusing the one that just called `check/login`, `/main` and `/share/list`
        earns a flat `403 text/plain` from the CDN, every time, for a `dlink` that a
        clean session fetches at 200. Measured on the live box, same link, seconds
        apart:

            reused API session, no Range   403      fresh session, no Range   200
            reused API session, ranged     403      fresh session, ranged     206

        What the reused session has that a new one does not is three cookies the API
        replies leave in the jar — `browserid`, `csrfToken`, `lang` — which then
        travel to the CDN beside `ndus`. It is not the `Accept` header: adding the
        API's `application/json` Accept to a clean session still returns 200. So the
        session is built here rather than passed in, because a caller that shares one
        cannot be told apart from a caller that does not until the download 403s, and
        that reads as an expired cookie.
        """
        if not item.dlink:
            # Measured signed-out: the field comes back present and empty, and
            # `/share/download` asks for a captcha instead. An expired cookie
            # looks exactly like no cookie here.
            raise _refusal(
                -6,
                "Terabox did not return a download link for that file — the "
                "TERABOX_COOKIE is signed out.")
        session = self._session(cookie=cookie if cookie is not None else True)
        try:
            response = await session.get(item.dlink, allow_redirects=True,
                                         stream=True,
                                         headers=self._headers(origin, cookie))
            try:
                if response.status_code >= 400:
                    raise ResolveError(
                        f"The download link was refused ({response.status_code}).")
                final = str(response.url) or item.dlink
                size = int(response.headers.get("content-length") or 0) or item.size
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        finally:
            await self._close(session)
        return final, size

    async def resolve(self, url: str) -> Resolved:
        """The biggest video behind the link. See `resolve_all` for folders."""
        every = await self.resolve_all(url)
        return every[0]

    async def check_cookie(self, cookie: str, index: int = 1) -> CookieHealth:
        """
        Is this one cookie signed in, and how much room does its account have?

        Asked exactly the way a real job asks, which is the point: `_home` walks
        `HOME_HOSTS` looking for `errno 0`, so a cookie that answers here will
        answer for a link too, and one that does not has already failed for the same
        reason it would have failed a user. The extra `/api/quota` call is the part
        an admin cannot get any other way — a signed-in account with no space left
        still resolves links and then cannot be used for anything.

        Never raises. This runs behind a button that exists to diagnose failures, so
        it reports them instead of becoming one.
        """
        session = self._session(cookie=cookie)
        try:
            try:
                home = await self._home(session, cookie)
            except ResolveError as exc:
                return CookieHealth(index=index, ok=False,
                                    errno=getattr(exc, "errno", -6), home="",
                                    detail=str(exc))
            used = total = 0
            detail = ""
            try:
                quota = await self._get_json(
                    session, f"{home}/api/quota?checkfree=1&{COMMON}",
                    referer=f"{home}/main")
                used = int(quota.get("used") or 0)
                total = int(quota.get("total") or 0)
            except (ResolveError, TypeError, ValueError) as exc:
                detail = f"quota unreadable ({exc})"

            tokens = False
            try:
                tokens = "bdstoken" in await self._tokens(session, home, cookie)
            except ResolveError as exc:
                detail = detail or f"no page token ({exc})"
            return CookieHealth(index=index, ok=True, errno=0, home=home,
                                used_bytes=used, total_bytes=total,
                                tokens=tokens, detail=detail)
        except Exception as exc:                    # noqa: BLE001 - diagnostics only
            return CookieHealth(index=index, ok=False, errno=0, home="",
                                detail=f"{type(exc).__name__}: {exc}")
        finally:
            await self._close(session)

    async def health(self) -> list[CookieHealth]:
        """
        Every configured cookie, probed one at a time.

        All of them, not just the first — a spare that has quietly expired is
        invisible until the day the rotation needs it, which is exactly the day the
        first one is already throttled. Sequential on purpose: five accounts hitting
        `check/login` in the same instant from one address is the shape of a rate
        limit, and this is a diagnostic, not a race.
        """
        return [await self.check_cookie(cookie, index)
                for index, cookie in enumerate(cookies(), start=1)]

    @staticmethod
    async def _close(session) -> None:
        """`AsyncSession.close()` returns a coroutine; `Session.close()` does not."""
        close = getattr(session, "close", None)
        if not callable(close):
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

    async def resolve_all(self, url: str) -> list[Resolved]:
        """
        Every video behind one link, biggest first.

        Two routes, and the cookie decides which is tried first. Terabox itself is
        preferred whenever there is a cookie to ask with: no third party sees the
        link, no shared quota, and the bytes come from Terabox's own CDN. The
        cookieless route is what is left when the cookie is missing or has expired
        — which is not a rare case, it is the state the bot ships in.

        Whichever runs, one link is one charge however many files come back, capped
        by TERABOX_MAX_FILES_PER_LINK so a shared folder of two hundred clips cannot
        be fetched for a single credit.

        When the signed route was tried and *both* fail, the user is told what the
        signed route said. Its complaint is the diagnostic one — it is the route with
        the credentials, and it got further. The fallback's is misleading here:
        iteraplay has been behind a Cloudflare challenge since 4 September 2026, so
        it answers "sent something unreadable" for every link, working or not, and
        showing that instead hides a real Terabox refusal behind a generic one.
        """
        if cookies():
            try:
                return await self._via_terabox(url)
            except ResolveError as exc:
                if not cfg.terabox_fallback:
                    raise
                log.info("terabox: %s — trying the cookieless route", exc)
                try:
                    return await self._via_fallback(url)
                except ResolveError as second:
                    log.info("terabox: the cookieless route failed too (%s)", second)
                    raise exc from second
        elif not cfg.terabox_fallback:
            raise ResolveError(
                "Terabox is not set up on this bot yet — the operator has to add "
                "TERABOX_COOKIE.")
        return await self._via_fallback(url)

    async def _via_fallback(self, url: str) -> list[Resolved]:
        """
        The same files, resolved by iteraplay instead, with no Terabox login.

        `normal_dlink` is the original upload — measured byte-for-byte against the
        size Terabox reports — so this is not a downgrade the way the HLS transcode
        is. What it costs is a third party seeing the link and a small shared quota;
        see `iteraplay` for both. Their reply already carries names, sizes and
        durations, so Terabox is not asked anything at all on this path.

        The quota is why the call is `resolve_rotating`: it walks the operator's own
        accounts and proxies until one of them is not yet spent. With neither
        configured that is a single attempt, exactly as before.
        """
        rows = await iteraplay.resolve_rotating(
            lambda *, proxy=None: self._session(cookie=False, proxy=proxy),
            url, ua=UA,
            tokens=cfg.terabox_fallback_tokens,
            proxies=cfg.proxies,
            budget=cfg.terabox_fallback_attempts,
        )

        headers = {"User-Agent": UA, "Referer": iteraplay.HOME}
        out: list[Resolved] = []
        for row in rows[:cfg.terabox_max_files_per_link]:
            # The original and the 360p/480p ladder are never offered side by side:
            # `Resolved` sorts by height, and "original" has none to sort by, so a
            # 480p rendition would outrank the full-quality file and win `best`.
            stream = (Stream(url=row.url, label="original", kind="file",
                             size_bytes=row.size or None, headers=headers)
                      if row.url else
                      Stream(url=row.hls, label=row.quality or "480p", kind="hls",
                             headers=headers))
            out.append(Resolved(title=row.name, streams=[stream],
                                duration_seconds=row.duration,
                                thumbnail_url=row.thumbnail or None,
                                source_url=url, headers=headers))
        if not out:
            raise ResolveError(
                "Nothing behind that link could be fetched. Try again in a few "
                "minutes.")
        return out

    async def _via_terabox(self, url: str) -> list[Resolved]:
        """
        Terabox's own CDN, walking the configured cookies until one answers.

        **The rotation is for failover, not for speed.** Terabox shapes per CDN host,
        not per account — measured 4 September 2026 — so a second cookie makes no
        download faster. What it survives is the one failure a single cookie cannot:
        an account that is rate-limited (`400210`), missing its page token
        (`4000020`) or logged out (`-6`) takes the whole bot down with it.

        Only those refusals move to the next account. A private share, a deleted
        file, a share code Terabox does not recognise — those fail identically
        through every account, so retrying spends four times the requests and makes
        the user wait four times as long for the same answer. `CookieRefused` is the
        one class that means "ask someone else", and `_refusal` is the only place
        that decides.

        The starting offset advances per link so a ten-link batch does not put every
        first attempt on cookie one, and there is a real sleep between attempts:
        `400210` is a *rate* limit, and rolling straight into the next account at
        full speed is how one throttled cookie becomes four.
        """
        global _cursor

        pool = cookies()
        plan = cookie_plan(pool, budget=cfg.terabox_fallback_attempts, start=_cursor)
        _cursor = (_cursor + 1) % 9973      # prime: no lockstep with a batch size
        if not plan:
            raise ResolveError(
                "Terabox is not set up on this bot yet — the operator has to add "
                "TERABOX_COOKIE.")

        last: ResolveError | None = None
        for attempt, cookie in enumerate(plan):
            if attempt:
                await asyncio.sleep(ROTATE_PAUSE_SECONDS)
            try:
                return await self._attempt(url, cookie)
            except CookieRefused as exc:
                last = exc
                log.info("terabox: cookie %d/%d refused (errno %s) — %s",
                         attempt + 1, len(plan), exc.errno, exc)
                continue
        raise last or ResolveError(
            "Terabox would not hand over that link on any configured account.")

    async def _attempt(self, url: str, cookie: str) -> list[Resolved]:
        """One pass at the signed route on exactly one cookie.

        Split out of `_via_terabox` so the rotation loop above has one thing to
        retry, and so nothing on this path can quietly reach for the global cookie:
        every call takes `cookie` explicitly.
        """
        session = self._session(cookie=cookie)
        try:
            items, context, origin = await self._items(session, url, cookie)
            headers = self._headers(origin, cookie)
            out: list[Resolved] = []
            refused: CookieRefused | None = None
            for item in items:
                try:
                    direct, size = await self._direct_url(item, origin, cookie)
                    stream = Stream(url=direct, label="original", kind="file",
                                    size_bytes=size, headers=headers)
                except ResolveError as exc:
                    # The transcode is worse and capped at 1080p, but it is a video,
                    # so a refused `dlink` is not the end of this item. The refusal is
                    # kept, though: if HLS does not save a single file either, it is
                    # the credential that failed and the next account deserves a turn.
                    if isinstance(exc, CookieRefused):
                        refused = exc
                    # `fs_id`, not the file name — it is what the next call uses and it
                    # says nothing about what the video is.
                    log.info("terabox: dlink failed for fs_id %s (%s), falling back to HLS",
                             item.fs_id or "?", exc)
                    if not item.fs_id or not context.get("sign"):
                        continue
                    stream = Stream(
                        url=hls_url(context, item.fs_id, origin=origin),
                        label="1080p", kind="hls", height=1080, headers=headers)
                out.append(Resolved(title=item.name, streams=[stream],
                                    source_url=url, headers=headers))
            if not out:
                raise refused or ResolveError(
                    "Terabox would not hand over any of the files behind that link.")
            return out
        finally:
            await self._close(session)


terabox = register(Terabox())
