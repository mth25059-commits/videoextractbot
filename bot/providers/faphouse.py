"""
The link route — one link, a quality menu, one video.

**The site is not scraped, and this module does not know which site it is.** the operator
runs a resolver of his own and this is only its client: the link the user pasted is
appended to a fixed endpoint, and what comes back already names every rendition the
video has. Which sites that resolver accepts is *its* business and changes without
this file changing — so the door here is deliberately wide (see `matches`), and the
resolver's own error message is what a rejected link gets.

    <FAP_API>?url=<the pasted link>&title_lang=en
        │
        └─► {"errno": 0, "data": {"file": {"file_name": …, "stream_url": …,
                                           "quality_streams": [{…}, …]}}}

So the whole provider is: build the URL, read the JSON, map each entry onto a
`Stream`. No login, no cookie, no signing, and nothing here knows how the resolver
does its job.

**Every rendition is an HLS playlist, not a file.** `stream_url` ends `.m3u8` at
every quality and the JSON's `size`/`size_readable` are `null`, so the byte count of
a variant cannot be known until ffmpeg has assembled it. That is why `kind` is
`"hls"` here, and why nothing downstream may rely on `size_bytes` for this source.

**Height comes from the second number of `resolution`, never the first.** The 1080p
variant of the measured sample is `1400x1080`, not `1920x1080` — this site crops
rather than pillarboxes. Reading the width would have filed it as 720p and charged
1.5 credits for a 1080p video. The `quality` string is the fallback, and a rung with
neither is dropped rather than guessed at.

**The pasted link is percent-encoded whole.** A URL may legitimately carry `&` and
`=`; unencoded, a `&title_lang=hi` inside one would become a second parameter of the
*outer* request. `quote(url, safe="")` makes it a single opaque value.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote, urlparse

from .. import settings
from ..config import cfg
from .base import Provider, Resolved, ResolveError, Stream, register

log = logging.getLogger(__name__)

#: Hosts that belong to a *different* provider and must never be taken here. Terabox
#: links have their own route with its own price, cookie pool and batching, so a
#: pasted Terabox link has to fall through to it rather than be handed to a resolver
#: that knows nothing about it.
#:
#: Everything else is accepted. **This is the whole point:** the resolver takes more
#: than one site and the list is not this module's to keep — a host allowlist here
#: would silently refuse a link the service can actually fetch, and the refusal would
#: look like a bug in the bot rather than a limit of the resolver. So the door is
#: wide, the resolver is the authority on what is a video, and a link it does not
#: know comes back as *its* sentence (`errno != 0`), which is more use to the user
#: than a guess made here from the shape of the URL. Nothing is charged for a link
#: that turns out not to resolve.
FOREIGN_HOSTS = ("terabox", "1024tera", "teraboxapp", "4funbox", "mirrobox",
                 "nephobox", "momerybox", "tibibox", "terasharefile", "freeterabox")

#: Hosts that are never a video page, so they are refused at the door rather than
#: spent on a round trip. Only the obvious ones — this is a courtesy, not a filter.
NOT_VIDEOS = ("t.me", "telegram.me", "telegram.org", "google.com", "youtube.com",
              "youtu.be", "wa.me", "whatsapp.com", "facebook.com", "instagram.com")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

#: The resolver has a page of its own to fetch and parse, so it is not instant — but
#: a user is watching a spinner while this runs, so it is not allowed to be slow.
TIMEOUT = 60


def rungs() -> tuple[tuple[int, str, float], ...]:
    """
    The priced ladder — (exact height, button label, credits).

    Read live on every call rather than frozen at import time: the three prices are
    editable from the admin panel and the setup wizard, so a menu built after a
    change has to show the new number without the process being restarted.
    """
    return ((480, "480p", settings.get("cost_fap_480")),
            (720, "720p", settings.get("cost_fap_720")),
            (1080, "1080p", settings.get("cost_fap_1080")))


def price_of(height: int | None) -> float:
    """
    What one download of this height costs.

    Bands rather than exact matches, because this also has to price the odd
    rendition that is not on the ladder at all: 240p is charged as the cheapest rung
    and 1440p as the dearest, so an unusual video can never cost more than the best
    copy of an ordinary one.
    """
    tall = height or 0
    for ceiling, _label, cost in rungs():
        if tall <= ceiling:
            return cost
    return rungs()[-1][2]


def _stream_at(resolved: Resolved, height: int, label: str) -> Stream | None:
    """
    The best stream that *is* this rung, or None.

    Matched on the height the resolver reported, with the label as a fallback for an
    entry whose `resolution` could not be read. `resolved.streams` is already sorted
    tallest-then-fattest, so the first hit is the best copy of the rung.
    """
    for stream in resolved.streams:
        if stream.height == height or stream.label.lower() == label.lower():
            return stream
    return None

def menu(resolved: Resolved) -> list[tuple[Stream, float]]:
    """
    The buttons to show: one per priced rung the video actually has, cheapest first.

    the operator's rule, in his words — *"if video 1080 m nhi hai available toh opt hi do
    dega"*: a rung the video does not have is not offered at all, rather than offered
    and then quietly served something else. Renditions below the cheapest rung (the
    measured sample carries a 240p) are hidden for the same reason in reverse — a
    240p button sitting next to a 480p one at the same price is a worse menu than no
    button.

    Unless hiding them would leave nothing. A video with only low renditions still
    has to be downloadable, so the best stream there is gets one button under its own
    honest label, priced by `price_of` — the cheapest rung for a small one, and never
    more than the dearest for an unusually tall one.
    """
    offered: list[tuple[Stream, float]] = []
    for height, label, cost in rungs():
        stream = _stream_at(resolved, height, label)
        if stream is not None:
            offered.append((stream, cost))
    if offered:
        return offered
    best = resolved.best
    return [(best, price_of(best.height))]


def refusal_for(status: int) -> str:
    """
    What to tell the user about an HTTP status the resolver answered with.

    The two cases are genuinely different advice and the same sentence for both is
    actively misleading. **A 5xx is the service, not the link** — as of 5 September
    2026 the live resolver answers 502 for *every* video, because the session it
    harvests upstream has gone; telling that user to "send a different link" sends
    them off to try five more and conclude the bot is broken. A 4xx really can be the
    link, so there the suggestion is worth making.

    The endpoint is never named: it is not the user's to fix, and it is the operator's
    private address.
    """
    if status >= 500:
        return ("The video service is down right now — this is on its side, not "
                f"your link (HTTP {status}). Try again in a few minutes.")
    return (f"The video service refused that request (HTTP {status}). Try again, or "
            "send a different link.")


def wire_url(url: str) -> str:
    """
    The pasted link as a *server* would see it — its `#fragment` cut off.

    Every link off this site carries one, and it is only referral tracking: the two
    the operator sent both end `#dmVwPU1haW4gcGFnZSZ2ZWI9Rmlyc3QgNjAgb24gbWFpbg==`, which
    is `vep=Main page&veb=First 60 on main`. **A browser never transmits the fragment
    at all** — it is the one part of a URL that stays on the client — so a resolver
    fetching the page has never received it from a real visitor, and percent-encoding
    it into `url=…%23dmVwPU1haW4…` hands it something no browser would send. Whether
    that 404s or is quietly dropped is the resolver's business, and not worth finding
    out one broken link at a time.

    It also makes the same video the same request. Two people opening one video from
    two places on the site paste two different strings; the resolver now sees one URL
    for both, which is the only place that matters — the pasted text is still logged
    and shown back exactly as the user sent it.

    Only the fragment goes. The query is left exactly as pasted, because a video id
    legitimately lives in one.
    """
    return (url or "").strip().split("#", 1)[0]


def endpoint_for(url: str) -> str:
    """The resolver call for one pasted link. See the module docstring on encoding."""
    base = cfg.fap_api.strip().rstrip("&?")
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}url={quote(wire_url(url), safe='')}&title_lang=en"


def _dimensions(entry: dict) -> tuple[int | None, int | None]:
    """(width, height) for one `quality_streams` entry."""
    found = re.match(r"\s*(\d{2,5})\s*[x×]\s*(\d{2,5})",
                     str(entry.get("resolution") or ""))
    if found:
        return int(found.group(1)), int(found.group(2))
    found = re.search(r"(\d{3,4})\s*p", str(entry.get("quality") or ""), re.I)
    if found:
        return None, int(found.group(1))
    return None, None


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None

def _safe_label(value: object) -> str:
    """
    A label that survives a callback, since `q:<token>:<label>` is split on colons.

    Only reached for an entry whose `resolution` was unreadable — everything else is
    labelled from its height. Anything outside `[A-Za-z0-9._-]` goes, including the
    colon that would otherwise let a crafted `quality` field forge a token.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", str(value or ""))[:12]
    return cleaned or "original"


def _errmsg(payload: dict) -> str:
    """Whatever the resolver called the problem, if it said."""
    for key in ("errmsg", "message", "msg", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    return ""


def parse(payload: object, source_url: str = "") -> Resolved:
    """
    The resolver's JSON → a `Resolved`. Pure, so the tests need no network.

    Deliberately defensive about the envelope: this is somebody else's service, and
    `data.file` is the only shape ever measured. A resolver that one day answers with
    the file object at the top level, or stops sending `errno`, should degrade to a
    readable sentence rather than a `KeyError` on a worker.
    """
    if not isinstance(payload, dict):
        raise ResolveError("The video service sent something unreadable. Try again.")

    errno = _int_or_none(payload.get("errno"))
    if errno:
        detail = _errmsg(payload) or f"error {errno}"
        raise ResolveError(
            f"That link could not be read ({detail}). Check that it opens in a "
            "browser and that the video is not private.")

    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    file = data.get("file")
    if not isinstance(file, dict):
        file = data if (data.get("stream_url") or data.get("quality_streams")) else {}
    if not file:
        raise ResolveError("Nothing playable came back for that link. Try another.")

    title = str(file.get("file_name") or file.get("title") or "video").strip() or "video"

    streams: list[Stream] = []
    seen: set[str] = set()
    for entry in file.get("quality_streams") or []:
        if not isinstance(entry, dict):
            continue
        # Passed through byte for byte. These carry `%3D%3D`, and re-encoding or
        # unquoting one is what turns a working variant into a 403.
        url = str(entry.get("stream_url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        width, height = _dimensions(entry)
        streams.append(Stream(
            url=url,
            label=f"{height}p" if height else _safe_label(entry.get("quality")),
            kind="hls",
            height=height,
            width=width,
            bandwidth=_int_or_none(entry.get("bandwidth")),
        ))

    if not streams:
        master = str(file.get("stream_url") or "").strip()
        if master:
            # The multi-variant master playlist. Charged as the cheapest rung
            # because its height cannot be known from here, and quietly charging 2
            # credits for what may be a 480p copy is the worse of the two mistakes.
            streams.append(Stream(url=master, label="original", kind="hls"))

    if not streams:
        raise ResolveError("That video has no playable stream — it may be private, "
                           "deleted, or blocked in this region.")

    thumb = str(file.get("thumb") or file.get("thumbnail") or "").strip() or None
    return Resolved(title=title, streams=streams,
                    duration_seconds=_int_or_none(file.get("duration")) or None,
                    thumbnail_url=thumb, source_url=source_url)


class Faphouse(Provider):
    """The resolver's client. One link at a time — the menu belongs to one video."""

    name = "fap"
    label = "Fap"
    max_batch = 1

    def matches(self, text: str) -> bool:
        """
        Any http(s) link with a host and a path, except another provider's.

        The inverse of Terabox's allowlist, and deliberately so — see `FOREIGN_HOSTS`.
        A path is still required, because a bare domain is someone typing a site name
        rather than sending a video, and there is nothing for the resolver to read.
        """
        try:
            parsed = urlparse((text or "").strip())
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.netloc or "").lower().split(":")[0].strip(".")
        if not host or "." not in host or not parsed.path.strip("/"):
            return False
        # Substring, not equality: these families run dozens of mirror domains and the
        # point is only to leave them to the route that owns them.
        if any(mark in host for mark in FOREIGN_HOSTS):
            return False
        return not any(host == bad or host.endswith("." + bad) for bad in NOT_VIDEOS)

    def unavailable(self) -> str:
        """
        Why nothing can be resolved right now, or "". Asked at the door.

        The endpoint has a default, so this only fires where someone has emptied
        `FAP_API` on purpose. It is still asked before a credit moves, for the same
        reason Terabox asks: "one credit gone, an error, credit back" is a rotten
        answer to something that was never going to work.
        """
        if not cfg.fap_api.strip():
            return ("🔧 <b>This one is not switched on yet</b>\n\n"
                    "The server still needs its <code>FAP_API</code> address before "
                    "it can fetch anything here. <b>Nothing was charged.</b>")
        return ""

    async def resolve(self, url: str) -> Resolved:
        blocked = self.unavailable()
        if blocked:
            raise ResolveError("This service is not switched on on the server yet.")
        body = await self._get(endpoint_for(url))
        try:
            payload = json.loads(body)
        except ValueError:
            # HTML where JSON was expected: the resolver is down, or has put a page
            # in front of itself. Either way the user's link is not what is wrong.
            log.warning("fap: non-JSON from the resolver (%d bytes)", len(body))
            raise ResolveError("The video service is not answering properly right "
                               "now. Try again in a few minutes.") from None
        return parse(payload, url)

    async def _get(self, endpoint: str) -> str:
        """
        One GET, through a browser-shaped handshake.

        `curl_cffi` rather than `requests`, for the same reason as everywhere else in
        this package: hosts on this route fingerprint the TLS handshake and answer a
        plain Python one with 403 before reading a single header.
        """
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError as exc:      # pragma: no cover - dependency check
            raise ResolveError("The video client is not installed on the server "
                               "(pip install curl_cffi).") from exc

        session = AsyncSession(
            impersonate="chrome124", timeout=TIMEOUT,
            headers={"User-Agent": UA,
                     "Accept": "application/json, text/plain, */*",
                     "Accept-Language": "en-US,en;q=0.9"})
        try:
            response = await session.get(endpoint)
        except Exception as exc:
            raise ResolveError("Could not reach the video service — it may be down. "
                               "Try again in a minute.") from exc
        finally:
            close = getattr(session, "close", None)
            result = close() if callable(close) else None
            if hasattr(result, "__await__"):
                await result

        status = getattr(response, "status_code", 0)
        if status >= 400:
            log.warning("fap: the resolver answered %s", status)
            raise ResolveError(refusal_for(status))
        return response.text or ""

    async def playlist_seconds(self, stream: Stream) -> float:
        """
        How long the chosen variant runs, for the progress bar. 0.0 when unknown.

        Best effort in the strongest sense: the bar is nicer with a total and works
        without one, so every failure here — a refused fetch, a master playlist with
        no `#EXTINF` of its own — comes back as zero rather than raising into a job
        the user has already paid for.
        """
        # Imported here rather than at the top: a provider is meant to know nothing
        # about ffmpeg, and this is only borrowing its playlist arithmetic.
        from .. import media

        try:
            return media.hls_duration(await self._get(stream.url))
        except Exception as exc:
            log.debug("fap: no duration for %s (%s)", stream.label, exc)
            return 0.0


faphouse = register(Faphouse())
