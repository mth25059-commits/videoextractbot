"""
Live check: does a real link come back as a real, fetchable video?

Not part of the automated suite — this one needs the network and a cookie, so it is
run by hand:

    .venv/Scripts/python tests/live_check.py                 # the three test links
    .venv/Scripts/python tests/live_check.py <url> [<url> …]

It asks the questions in the order they fail, because they fail for different reasons
and only the last one costs anything:

1. **Does the bot recognise the host at all?** A domain missing from `HOSTS` is
   ignored in silence, which looks exactly like a broken bot. `terasharefile.com`
   was missing until two test links turned up on it.
2. **Which host is the cookie signed in on?** A Terabox session is valid on exactly
   one domain and answers `errno -6` on the rest, so asking the wrong one is
   indistinguishable from having no cookie. This prints the answer instead of
   guessing at it.
3. **Does `resolve_all` return a stream?** Asked through the provider — the same
   call the queue makes — so what prints here is what a user would get, refusals
   included, and the cookieless fallback is exercised only if the cookie path fails,
   exactly as in production.
4. **Do the bytes arrive?** One ranged request per video, first mebibyte only, with
   the headers the provider says to send. A `dlink` that resolves and then 403s is
   the failure this catches, and nothing else does.

Step 4 reads about 1 MiB per link off Terabox's CDN and nothing else is spent. If the
run falls through to iteraplay, that route's guest allowance is five videos per six
hours, so a fourth run in a day prints the quota refusal — a real result, not a bug.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")

from bot.config import cfg                                     # noqa: E402
from bot.providers import terabox as tb                         # noqa: E402
from bot.providers.base import ResolveError                     # noqa: E402

LINKS = [
    "https://1024terabox.com/s/1-a0S95G_Ab0RYiAZOdql9Q",
    "https://terasharefile.com/s/1rXZvcmBwMQlbMbZ7EDQwJg",
    "https://terasharefile.com/s/1vAQMs-3Zus6RX36DkPnmLA",
]

#: What the first bytes of a working MP4 look like. Checked because a CDN that is
#: refusing us still answers 200 with an HTML error body, and a size alone cannot
#: tell those apart.
MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free")


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{n:,} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


async def fetch_head(stream, want: int = 1_048_576) -> str:
    """One ranged GET of the resolved stream. A line describing what came back."""
    from curl_cffi.requests import AsyncSession
    headers = dict(stream.headers or {})
    headers["Range"] = f"bytes=0-{want - 1}"
    async with AsyncSession(impersonate="chrome124", timeout=90) as session:
        try:
            response = await session.get(stream.url, headers=headers,
                                         allow_redirects=True, stream=True)
        except Exception as exc:                                # noqa: BLE001
            return f"UNREACHABLE — {type(exc).__name__}: {exc}"
        body = b""
        try:
            if response.status_code >= 400:
                return f"REFUSED {response.status_code}"
            async for chunk in response.aiter_content():
                body += chunk
                if len(body) >= want:
                    break
        finally:
            close = getattr(response, "aclose", None)
            if callable(close):
                await close()
    total = (response.headers.get("content-range") or "").rpartition("/")[2]
    boxes = sorted({body[i + 4:i + 8].decode("latin-1")
                    for i in range(0, max(0, min(len(body), 8192) - 8))
                    if body[i + 4:i + 8] in MP4_BOXES})
    return (f"{response.status_code} {response.headers.get('content-type') or '?'}"
            f"  got {human(len(body))}"
            f"  of {human(int(total)) if total.isdigit() else '?'}"
            f"  boxes={boxes or 'NONE — not a video body'}")


async def one(url: str) -> bool:
    """Every question for one link. True if bytes arrived."""
    print(f"\n=== {url}")
    if not tb.terabox.matches(url):
        print("  host      NOT RECOGNISED — add it to HOSTS, the bot ignores this")
        return False
    print(f"  host      recognised, surl={tb.surl_of(url)!r}")
    try:
        found = await tb.terabox.resolve_all(url)
    except ResolveError as exc:
        # Verbatim what the user would be shown, refund line aside.
        print(f"  resolve   REFUSED — {exc}")
        return False
    except Exception as exc:                                    # noqa: BLE001
        print(f"  resolve   BROKE — {type(exc).__name__}: {exc}")
        return False

    print(f"  resolve   {len(found)} video(s)")
    ok = True
    for item in found:
        best = item.best
        print(f"            {best.label:<8} {best.kind:<4} "
              f"{human(best.size_bytes or 0):>12}  {item.title}")
        print(f"            via {best.url.split('/')[2]}")
        line = await fetch_head(best)
        print(f"  bytes     {line}")
        ok = ok and line.startswith("2") and "NONE" not in line
    return ok


async def where_signed_in() -> None:
    """Question 2, printed once: which Terabox domain accepts each cookie.

    Every configured cookie is probed, not just the first. A spare that has quietly
    expired is invisible until the day the rotation needs it, which is exactly the
    day the first one is already throttled.
    """
    pool = tb.cookies()
    if not pool:
        print("home      no cookie — the signed route is off, fallback only")
        return
    for index, cookie in enumerate(pool, start=1):
        tag = f"home {index}/{len(pool)}"
        session = tb.terabox._session(cookie=cookie)
        try:
            home = await tb.terabox._home(session, cookie)
            tokens = await tb.terabox._tokens(session, home, cookie)
            print(f"{tag:<9} {home}")
            print(f"tokens    {len(tokens)} chars"
                  f"{' with bdstoken' if 'bdstoken' in tokens else ' (no bdstoken)'}")
        except ResolveError as exc:
            print(f"{tag:<9} NOT SIGNED IN — {exc}")
        finally:
            await tb.terabox._close(session)


async def main(urls: list[str]) -> int:
    print(f"\ncookies: {len(tb.cookies()) or 'EMPTY'}   "
          f"fallback: {'on' if cfg.terabox_fallback else 'off'}"
          f"   accounts: {len(cfg.terabox_fallback_tokens) or 'guest'}"
          f"   proxies: {len(cfg.proxies) or 'none'}"
          f"   up to {cfg.terabox_fallback_attempts} attempt(s)/link")
    gate = tb.terabox.unavailable()
    if gate:
        print("the bot would refuse every link at the door — both routes are off")
    await where_signed_in()

    good = 0
    for url in urls:
        good += await one(url)
    print(f"\n{good}/{len(urls)} link(s) delivered playable bytes\n")
    return 0 if good == len(urls) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:] or LINKS)))
