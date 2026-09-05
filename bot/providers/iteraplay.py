"""
iteraplay — the cookieless way to the same file, and what it costs.

Terabox will describe a share to anyone but only hand the bytes to a signed-in
session: `dlink` comes back present and empty and `/share/download` answers
`errno 400310 need verify_v2`. `iteraplay.com` is a third-party resolver with a
signed-in Terabox account of its own, so it can answer what we cannot.

**It returns the original file, not a transcode.** Measured 3 September 2026
against `terasharefile.com/s/1vAQMs-3Zus6RX36DkPnmLA`: `normal_dlink` delivered
44,739,275 bytes — byte-for-byte the size Terabox reports for the upload — as
`video/mp4`, with `ftyp`/`moov`/`mdat` present and the boxes covering the file
exactly. The `fast_stream_url` ladder beside it is a downgrade (360p and 480p
only), so `normal_dlink` is the one worth having and the ladder is a last resort.

**The catch is the quota, and it is small.** A caller with no iteraplay account is
a `guest`, and a guest gets **5 videos per 6 hours**; the sixth is HTTP 429,
`error: usage_limit`, with a `resetTime`. The bot takes ten links in one batch, so
a single full batch is twice a guest's entire allowance. `TERABOX_FALLBACK_TOKEN`
sends an iteraplay `login_token` instead, which their own refusal says raises the
limit — it has to be an account the operator registered themselves.

**Two costs beyond the quota.** Every link handled here is disclosed to someone
else's server; and the bytes come from their host too — a Cloudflare Worker whose
name *changes between responses* (`wild-king-480f.qymonyny` and
`frosty-boat-ef16.waxipylo`, twenty minutes apart), so it is read out of each
reply and never hardcoded. It was also slow: 42 MB in 178 s, about 0.2 MB/s.

Nothing here borrows anyone's credentials. The quota is counted per account and
per calling address, so `resolve_rotating` walks whatever the operator configured
themselves — `TERABOX_FALLBACK_TOKENS`, `PROXIES` — and stops at the first answer.
With neither set it is one address, one request per link, and a plain refusal when
the guest allowance is spent, which is exactly what it did before.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .base import ResolveError

log = logging.getLogger(__name__)


class QuotaExceeded(ResolveError):
    """The allowance for this account and this address is spent.

    Its own class because it is the one refusal worth retrying: the same link
    through a different account or a different egress IP may well succeed, whereas
    a private or deleted share fails identically everywhere and retrying it only
    spends someone else's API for nothing. The message is unchanged, so anything
    catching `ResolveError` still shows the user exactly what it showed before.
    """


class Unreachable(ResolveError):
    """Nothing answered. Retryable for the same reason, and more often than not it
    is the proxy that is dead rather than iteraplay."""


HOME = "https://iteraplay.com/"
ENDPOINT = "https://iteraplay.com/api/download"

#: Qualities `fast_stream_url` has actually offered, best first. Their ladder stops
#: at 480p, so it is only reached if `normal_dlink` is missing from the reply.
LADDER = ("1080p", "720p", "480p", "360p", "240p")


def headers(ua: str) -> dict[str, str]:
    """Their API answers only to its own page's Origin and Referer."""
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": HOME.rstrip("/"),
        "referer": HOME,
        "user-agent": ua,
    }


@dataclass(frozen=True)
class Row:
    """One file iteraplay resolved, and where its bytes are."""

    name: str
    size: int
    url: str                    # normal_dlink: the original upload, full quality
    quality: str = ""
    duration: float | None = None
    thumbnail: str = ""
    hls: str = ""               # best fast_stream_url — a downgrade, used only if
                                # `url` is empty


def _wait_hint(usage: dict) -> str:
    """A "try again in …" clause built from their `resetTime`, or "" if absent."""
    stamp = str((usage or {}).get("resetTime") or "").strip()
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    minutes = int((when - datetime.now(timezone.utc)).total_seconds() // 60)
    if minutes <= 0:
        return " Try again now."
    if minutes < 60:
        return f" Try again in {minutes} min."
    return f" Try again in about {minutes // 60}h {minutes % 60}m."


def parse(payload: dict, *, status_code: int = 200) -> list[Row]:
    """
    Their reply, as rows — or a `ResolveError` written for the person waiting.

    The quota refusal is the one that matters, because it is the one that will
    actually happen: five videos and a guest is done for six hours. It arrives as
    HTTP 429 with `error: "usage_limit"`, and their own `message` ends in "Login
    for higher limits", which is advice for the operator and noise for the user —
    so the wait is rebuilt from `usage.resetTime` instead of echoing their text.
    """
    if not isinstance(payload, dict):
        raise ResolveError(
            "The Terabox helper service sent something unreadable. Try again in a "
            "few minutes.")

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    error = str(payload.get("error") or "")
    if error == "usage_limit" or status_code == 429:
        limit = usage.get("limit")
        allowance = f" ({limit} videos per 6 hours)" if limit else ""
        raise QuotaExceeded(
            f"The free Terabox route has used up its quota{allowance}."
            f"{_wait_hint(usage)}")
    if status_code >= 400 or payload.get("status") != "success":
        note = str(payload.get("message") or payload.get("status") or status_code)
        log.info("iteraplay: refused (%s) %s", status_code, note[:200])
        raise ResolveError(
            "The Terabox helper service could not open that link. It may be "
            "private, removed, or password-protected.")

    rows: list[Row] = []
    for entry in payload.get("list") or []:
        if not isinstance(entry, dict) or entry.get("is_dir"):
            continue
        ladder = entry.get("fast_stream_url")
        ladder = ladder if isinstance(ladder, dict) else {}
        best = next((str(ladder[q]) for q in LADDER if ladder.get(q)), "")
        try:
            size = int(entry.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("normal_dlink") or "").strip()
        if not name or not (url or best):
            continue
        rows.append(Row(name=name, size=size, url=url,
                        quality=str(entry.get("quality") or ""),
                        duration=_seconds(entry.get("duration")),
                        thumbnail=str(entry.get("thumbnail") or ""), hls=best))
    if not rows:
        raise ResolveError(
            "No video was found behind that link — it may be photos, documents, "
            "or an empty folder.")
    rows.sort(key=lambda r: -r.size)
    return rows


def _seconds(value) -> float | None:
    """Their `duration`, which has come back both as a number and as `mm:ss`."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    parts = str(value).split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for number in numbers:                  # h:m:s, m:s, or s
        total = total * 60 + number
    return total


async def resolve(session, url: str, *, ua: str, token: str = "") -> list[Row]:
    """
    Hand one Terabox share to iteraplay and take back links to the real files.

    The warm-up GET is not decoration: their API answers the POST only for a caller
    that has been given a session cookie by the page first. `token` is the
    operator's own `login_token`, sent only when configured — a guest works, just
    five videos per six hours of it.
    """
    jar = {"login_token": token, "remember_me": "true"} if token else None
    try:
        await session.get(HOME, allow_redirects=True, cookies=jar)
        response = await session.post(ENDPOINT, json={"url": url},
                                      headers=headers(ua), cookies=jar, timeout=30)
    except ResolveError:
        raise
    except Exception as exc:                # noqa: BLE001 - any transport error
        log.info("iteraplay: unreachable (%s: %s)", type(exc).__name__, exc)
        raise Unreachable(
            "The Terabox helper service is not responding. Try again in a few "
            "minutes.") from exc

    body = (response.text or "").strip()
    try:
        payload = json.loads(body)
    except ValueError:
        log.info("iteraplay: %s, not JSON: %.200s", response.status_code, body)
        raise ResolveError(
            "The Terabox helper service sent something unreadable. Try again in a "
            "few minutes.") from None
    rows = parse(payload, status_code=response.status_code)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if usage:
        log.info("iteraplay: %s/%s used (%s)", usage.get("current"),
                 usage.get("limit"), usage.get("userType"))
    return rows


#: Where the next link starts walking the plan. Ten links in a batch would otherwise
#: all spend the first account first and leave the rest of them idle.
_cursor = 0


def plan(tokens, proxies, *, budget: int, start: int = 0) -> list[tuple[str, str]]:
    """The (token, proxy) pairs to try for one link, in order.

    The allowance is counted per account *and* per calling address, so the two
    lists are walked together rather than nested: with one account and ten proxies
    that is ten fresh allowances, and nesting would only ever have used the first.
    An empty list means "as configured" — no token, or this machine's own IP —
    which is why a box with nothing set still gets exactly one plain attempt.
    """
    accounts = tuple(t for t in tokens if t) or ("",)
    routes = tuple(p for p in proxies if p) or ("",)
    total = max(1, min(int(budget), max(len(accounts), len(routes))))
    return [(accounts[(start + step) % len(accounts)],
             routes[(start + step) % len(routes)]) for step in range(total)]


async def _close(session) -> None:
    closer = getattr(session, "close", None)
    if closer is None:
        return
    result = closer()
    if hasattr(result, "__await__"):
        await result


async def resolve_rotating(make_session, url: str, *, ua: str, tokens=(),
                           proxies=(), budget: int = 4) -> list[Row]:
    """
    `resolve`, but through each configured account and address until one answers.

    `make_session(proxy=…)` is called fresh for every attempt on purpose: a session
    that has already been given a guest cookie through one address would carry it
    to the next, and the second attempt would be the first one again.

    Only a spent quota or a dead route is retried. A private, deleted or
    password-protected share is refused identically everywhere, so it is raised the
    moment it is seen instead of being asked ten more times.
    """
    global _cursor
    steps = plan(tokens, proxies, budget=budget, start=_cursor)
    _cursor = (_cursor + 1) % 997                 # prime: no accidental lockstep
    refusal: ResolveError | None = None
    for index, (token, proxy) in enumerate(steps, 1):
        session = make_session(proxy=proxy or None)
        try:
            return await resolve(session, url, ua=ua, token=token)
        except (QuotaExceeded, Unreachable) as exc:
            refusal = exc
            log.info("iteraplay: attempt %s/%s no good (%s)", index, len(steps),
                     type(exc).__name__)
        finally:
            await _close(session)
    # Every route was spent or dead. The last refusal is the honest one to show:
    # it is the quota wording when quota was the problem, and the unreachable
    # wording when nothing answered.
    raise refusal or Unreachable(
        "The Terabox helper service is not responding. Try again in a few minutes.")
