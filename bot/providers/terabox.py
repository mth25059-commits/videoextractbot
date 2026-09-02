"""
Terabox — turning a share link into something fetchable.

A Terabox share page is a JavaScript app; the video is not in the HTML. What the
page does behind the scenes is what this module does directly:

    /s/1abc…  ──►  surl=1abc…  ──►  /share/list?shorturl=…  ──►  dlink  ──►  file
                   (redirect)       (JSON: names, sizes,        (302 to a
                                     fs_ids, dlinks)            CDN host)

Three things make or break it:

**A logged-in cookie.** Anonymous `share/list` answers `errno: -6` for most
links. `TERABOX_COOKIE` holds the `ndus=…` value from a browser session — it is
the account whose quota is used, so it must be the operator's own account.

**A browser TLS fingerprint.** Terabox fronts its API with a WAF that rejects
Python's handshake before it ever looks at the headers, so every request goes
through `curl_cffi` with `impersonate` set. Plain `requests`/`httpx` get a 403
that looks like a bad cookie and is not.

**`dlink` is not the file.** It is a signed, short-lived redirect that only works
with the same cookie and User-Agent that asked for it, and it expires in minutes
— so it is resolved as late as possible, not cached.

Quality: there is no quality menu here. The original upload *is* the highest
quality, so a link resolves to exactly one stream — the file itself. The HLS
transcode (`/api/streaming`) is only a fallback for when `dlink` is refused, and
it is capped at 1080p by Terabox, which is why it is second choice and not first.

Nothing in this file has been run against the live API from this machine: there is
no cookie and no network here. The parsing is covered by `tests/test_terabox.py`
against recorded response shapes; the request layer needs one real link to shake
out, and the first thing to check if it fails is `errno` in the log.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlparse

from .base import Provider, ResolveError, Resolved, Stream, register
from ..config import cfg

log = logging.getLogger(__name__)

#: Every domain Terabox has shipped the same product under.
HOSTS = frozenset({
    "terabox.com", "www.terabox.com", "terabox.app", "www.terabox.app",
    "1024terabox.com", "www.1024terabox.com", "teraboxapp.com", "www.teraboxapp.com",
    "teraboxlink.com", "www.teraboxlink.com", "terasharelink.com", "www.terasharelink.com",
    "4funbox.com", "www.4funbox.com", "mirrobox.com", "www.mirrobox.com",
    "nephobox.com", "www.nephobox.com", "momerybox.com", "www.momerybox.com",
    "tibibox.com", "www.tibibox.com", "freeterabox.com", "www.freeterabox.com",
    "terafileshare.com", "www.terafileshare.com",
})

API = "https://www.terabox.com"
APP_ID = "250528"

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
    # Measured against a live share on 3 September 2026 with no cookie at all:
    # `/share/list` answers {"code": 460020, "errmsg": "need verify"} and
    # `/api/shorturlinfo` answers {"errno": 400210, "errmsg": "need verify_v2"}.
    # A signed-out session used to get a real file list; it no longer does, which
    # is also why every public "terabox downloader" worker returns 500 now. Same
    # meaning as -6 for our purposes: the cookie is missing or no longer signed in.
    460020: "Terabox will not serve this link to a signed-out session. The "
            "TERABOX_COOKIE is missing or has expired — sign in again and "
            "replace it.",
    400210: "Terabox will not serve this link to a signed-out session. The "
            "TERABOX_COOKIE is missing or has expired — sign in again and "
            "replace it.",
}


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


def surl_of(url: str) -> str:
    """
    Pull the short-url key out of any of the shapes Terabox hands out.

        /s/1abcdef            -> 1abcdef
        ?surl=abcdef          -> 1abcdef   (the API always wants the leading 1)
        /sharing/link?surl=…  -> …
    """
    parsed = urlparse(url.strip())
    query = parse_qs(parsed.query)
    if "surl" in query and query["surl"][0]:
        key = query["surl"][0]
    else:
        match = re.search(r"/s/([A-Za-z0-9_-]+)", parsed.path)
        if not match:
            raise ResolveError(
                "That does not look like a Terabox share link. It should look "
                "like https://terabox.com/s/1abc…")
        key = match.group(1)
    return key if key.startswith("1") else "1" + key


def parse_list(payload: dict, *, only_videos: bool = True) -> list[Item]:
    """
    Turn a `/share/list` response into items.

    Kept as a plain function over already-decoded JSON so the shape handling is
    testable without a network or a cookie — which is most of what breaks here.
    """
    if not isinstance(payload, dict):
        raise ResolveError("Terabox sent something unreadable.")

    # `errno` is the documented field, but the WAF replies with `code` instead and
    # no `errno` at all — so reading only `errno` turned "need verify" into an
    # empty file list and a puzzling "nothing to download" for the user.
    errno = payload.get("errno") or payload.get("code") or 0
    if errno:
        raise ResolveError(ERRNO_HELP.get(
            int(errno), f"Terabox refused that link (error {errno})."))

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


def hls_url(path: str, *, quality: str = "M3U8_AUTO_1080") -> str:
    """The transcoded fallback. Terabox caps this at 1080p even for a 4K upload."""
    return (f"{API}/api/streaming?path={quote(path, safe='')}"
            f"&app_id={APP_ID}&clienttype=0&type={quality}")


class Terabox(Provider):
    name = "terabox"
    label = "Terabox"

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

        There is no cookieless path to fall back to. Terabox now answers a
        signed-out `/share/list` with `code 460020 need verify` (measured
        3 September 2026), which is why every public terabox-downloader worker
        returns 500 — the cookie is the only way in.
        """
        if not cfg.terabox_cookie:
            return ("🔧 <b>Terabox is not switched on yet</b>\n\n"
                    "The bot still needs its Terabox sign-in "
                    "(<code>TERABOX_COOKIE</code>) before it can fetch anything. "
                    "<b>Nothing was charged.</b>")
        return ""

    # --- the network half ----------------------------------------------------

    def _session(self):
        """One browser-shaped session. Imported late so the module loads without curl_cffi.

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

        if not cfg.terabox_cookie:
            raise ResolveError(
                "Terabox is not set up on this bot yet — the operator has to add "
                "TERABOX_COOKIE.")

        cookie = cfg.terabox_cookie
        if "=" not in cookie:           # a bare ndus value is the common mistake
            cookie = f"ndus={cookie}"
        return AsyncSession(
            impersonate="chrome124",
            timeout=45,
            headers={
                "User-Agent": UA,
                "Cookie": cookie,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"{API}/",
            },
            proxy=(cfg.proxies[0] if cfg.proxies else None),
        )

    async def _get_json(self, session, url: str) -> dict:
        response = await session.get(url, allow_redirects=True)
        if response.status_code == 403:
            raise ResolveError(
                "Terabox blocked the request. Usually the cookie has expired.")
        if response.status_code >= 400:
            raise ResolveError(f"Terabox answered {response.status_code}.")
        try:
            return json.loads(response.text)
        except ValueError:
            # An HTML body here means a login wall, not a JSON API.
            raise ResolveError(
                "Terabox returned a login page instead of the file list — the "
                "cookie is no longer valid.") from None

    async def _items(self, session, url: str) -> list[Item]:
        """Top level of the share, one directory deep if that is where the videos are."""
        surl = surl_of(url)
        listing = await self._get_json(
            session, f"{API}/share/list?app_id={APP_ID}&shorturl={surl}"
                     f"&root=1&web=1&clienttype=0")
        items = parse_list(listing)

        videos = [i for i in items if not i.is_dir]
        folders = [i for i in items if i.is_dir]
        for folder in folders:
            if len(videos) >= cfg.terabox_max_files_per_link:
                break
            inner = await self._get_json(
                session, f"{API}/share/list?app_id={APP_ID}&shorturl={surl}"
                         f"&dir={quote(folder.path, safe='')}&web=1&clienttype=0")
            try:
                videos += [i for i in parse_list(inner) if not i.is_dir]
            except ResolveError as exc:
                log.info("terabox: folder %s unreadable (%s)", folder.name, exc)

        if not videos:
            raise ResolveError(
                "No video was found behind that link — it may be photos, "
                "documents, or an empty folder.")
        videos.sort(key=lambda i: -i.size)
        return videos[:cfg.terabox_max_files_per_link]

    async def _direct_url(self, session, item: Item) -> tuple[str, int]:
        """
        Follow `dlink` to the CDN URL the bytes actually come from.

        Terabox answers the first hop with a 302; some mirrors answer 200 with the
        file. Either way the URL we end up with is what gets handed to the
        downloader, together with whatever size the CDN reports.
        """
        if not item.dlink:
            raise ResolveError("Terabox did not return a download link for that file.")
        response = await session.get(item.dlink, allow_redirects=True, stream=True)
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
        return final, size

    async def resolve(self, url: str) -> Resolved:
        """The biggest video behind the link. See `resolve_all` for folders."""
        every = await self.resolve_all(url)
        return every[0]

    async def resolve_all(self, url: str) -> list[Resolved]:
        """
        Every video behind one link, biggest first.

        One link is one charge however many files come back, capped by
        TERABOX_MAX_FILES_PER_LINK so a shared folder of two hundred clips cannot
        be fetched for a single credit.
        """
        session = self._session()
        try:
            items = await self._items(session, url)
            out: list[Resolved] = []
            for item in items:
                try:
                    direct, size = await self._direct_url(session, item)
                    stream = Stream(url=direct, label="original", kind="file",
                                    size_bytes=size,
                                    headers={"User-Agent": UA, "Referer": f"{API}/",
                                             "Cookie": cfg.terabox_cookie})
                except ResolveError as exc:
                    # The transcode is worse and capped at 1080p, but it is a video.
                    log.info("terabox: dlink failed for %s (%s), falling back to HLS",
                             item.name, exc)
                    if not item.path:
                        continue
                    stream = Stream(url=hls_url(item.path), label="1080p", kind="hls",
                                    height=1080,
                                    headers={"User-Agent": UA, "Referer": f"{API}/",
                                             "Cookie": cfg.terabox_cookie})
                out.append(Resolved(title=item.name, streams=[stream],
                                    source_url=url, headers=stream.headers))
            if not out:
                raise ResolveError(
                    "Terabox would not hand over any of the files behind that link.")
            return out
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                result = close()
                if hasattr(result, "__await__"):
                    await result


terabox = register(Terabox())
