"""
Which address a download leaves the box from.

Measured from the live VM on 4 September 2026, and every rule here comes out of
those numbers rather than out of taste:

    1 stream, direct                        1.38 - 1.81 MB/s
    4 ranged chunks of one file, direct     1.87 MB/s      1.03x   <- dead end
    1 stream, a lapsed proxy                0.39 MB/s      0.22x
    1 stream, a live proxy (best six)       1.71 - 1.76 MB/s  1.24 - 1.28x
    3 different links at once, direct       3.11 MB/s aggregate
    3 different links, a proxy each         3.92 MB/s aggregate  1.26x

**A second address does not make one download faster.** Terabox shapes per CDN
host, so splitting one file across four routes buys 3%. What a second address does
buy is ~26% on *aggregate* throughput when several links run at once, and it
spreads ban and quota risk instead of betting the whole bot on one IP.

**Round-robin, not `proxies[0]`.** The code this replaced read the first entry and
never touched the other nine, so ten configured addresses were one address with
nine decorations.

**Direct egress is always in the rotation, and it is where a failure lands.** A
proxy whose plan has lapsed answers `CONNECT` with `HTTP 402 Payment Required` and
carries nothing at all — a whole batch of ten did exactly that — and two of the ten
live ones measured 0.25-0.36 MB/s against a 1.38 MB/s direct baseline. So a proxy
is never allowed to fail a job: it is benched and the job goes out directly.

**Resolve direct, download through the proxy.** Resolving *through* one is flakier
(1 of 3 attempts died with an SSL timeout), and Terabox does not bind a `dlink` to
the address that asked for it — measured — so there is nothing to gain by paying
that risk twice.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from .config import cfg

log = logging.getLogger(__name__)

#: How long a proxy stays out of the rotation after it fails a job. Long enough
#: that a batch does not keep rediscovering the same dead address, short enough
#: that a proxy which was merely having a bad minute comes back on its own.
BENCH_SECONDS = 600.0

#: Where a probe asks "which address did this leave from". Chosen because the
#: answer is the useful half of the check: the admin card can then show that a
#: route really does present a different IP, not just that it answered.
PROBE_URL = "https://api.ipify.org"
PROBE_TIMEOUT = 15.0

_cursor = 0
_benched: dict[str, float] = {}


def describe(proxy: str | None) -> str:
    """`host:port` with the credentials stripped — safe for a log or an admin card.

    Proxy URLs are `http://user:pass@host:port`, and the password is a live secret
    shared across every address in the list. It must never reach a log file, a
    Telegram message or an exception string.
    """
    if not proxy:
        return "direct"
    try:
        parts = urlsplit(proxy if "//" in proxy else f"http://{proxy}")
    except ValueError:
        return "proxy"
    host = parts.hostname or "proxy"
    return f"{host}:{parts.port}" if parts.port else host


def bench(proxy: str | None, seconds: float = BENCH_SECONDS) -> None:
    """Take a proxy out of the rotation for a while. `None` (direct) is ignored."""
    if not proxy:
        return
    _benched[proxy] = time.monotonic() + max(0.0, seconds)
    log.info("egress: benched %s for %.0fs", describe(proxy), seconds)


def is_benched(proxy: str) -> bool:
    until = _benched.get(proxy)
    if until is None:
        return False
    if until <= time.monotonic():
        del _benched[proxy]
        return False
    return True


def benched() -> dict[str, float]:
    """Seconds left on each benched proxy, keyed by the address itself."""
    now = time.monotonic()
    return {p: until - now for p, until in _benched.items() if until > now}


def clear_bench() -> None:
    """Put every benched proxy straight back. The admin card offers this."""
    _benched.clear()


def pool() -> tuple[str, ...]:
    """The proxies currently in the rotation, benched ones removed."""
    return tuple(p for p in cfg.proxies if p and not is_benched(p))


def pick() -> str | None:
    """
    The next address for one job, or `None` for direct egress.

    Direct is one slot in the rotation rather than a special case: it was the
    fastest route in one of the two measured proxy batches, so a list of three
    proxies means four jobs leave from four different addresses.

    Round-robin per *job*, not per chunk. Concurrency across links is what scales
    (3.11 MB/s aggregate against 0.45 for one), so what matters is that two jobs
    running at once do not share one route.
    """
    global _cursor
    live = pool()
    if not live:
        return None
    slot = _cursor % (len(live) + 1)
    _cursor = (_cursor + 1) % 9973          # prime: no lockstep with a batch size
    return None if slot == len(live) else live[slot]


async def probe(proxy: str, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """
    One request through `proxy`, so a dead list is found by the admin, not by a user.

    Returns `(alive, detail)` where detail is the egress IP it presented, or the
    reason it did not answer. `HTTP 402` is called out by name: that is what a
    lapsed proxy plan says, and a whole batch of ten answering it that way is the
    tell for a list that needs replacing rather than debugging.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:                     # pragma: no cover - dependency check
        return False, "curl_cffi is not installed"

    session = AsyncSession(impersonate="chrome124", timeout=timeout, proxy=proxy)
    try:
        response = await session.get(PROBE_URL)
        if response.status_code == 402:
            return False, "402 Payment Required — the proxy plan has lapsed"
        if response.status_code >= 400:
            return False, f"answered {response.status_code}"
        return True, (response.text or "").strip()[:45] or "no address returned"
    except Exception as exc:                # noqa: BLE001 - any transport error
        return False, f"{type(exc).__name__}"
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result
