"""
The nightly health report.

One message to every admin, once a day, so the state of the box is something that
arrives rather than something somebody remembers to go and look at. The operator asked
for midnight IST; the box runs on UTC, so the default is 18:30 UTC and the
conversion lives in `cfg.daily_report_utc` with the arithmetic written down next
to it.

The report itself is `handlers.admin.daily_report` — it composes the same three
cards the admin panel shows, so there is one renderer for the server card and not
a second one that quietly goes stale. This module is only the clock.
"""

from __future__ import annotations

import asyncio
import logging
import time

from pyrogram import Client

from .config import cfg
from .queue import Queue

log = logging.getLogger(__name__)

#: IST is UTC+5:30, with no daylight saving — the one timezone fact this needs.
IST_OFFSET = 5 * 3600 + 30 * 60


def parse_hhmm(value: str) -> tuple[int, int] | None:
    """`"18:30"` → `(18, 30)`. Anything unparseable is None, which turns it off."""
    parts = (value or "").strip().split(":")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour, minute


def seconds_until(value: str, now: float | None = None) -> float:
    """
    How long until the next `HH:MM` UTC, always strictly in the future.

    `now % 86400` is midnight UTC of the current day, exactly: Unix time counts no
    leap seconds, so every UTC midnight is a whole multiple of 86400. That is what
    lets this be arithmetic rather than a calendar library, and it is why the same
    trick would be wrong for a local-time target.
    """
    parsed = parse_hhmm(value)
    if parsed is None:
        raise ValueError(f"not an HH:MM time: {value!r}")
    hour, minute = parsed
    now = time.time() if now is None else now
    target = now - (now % 86400) + hour * 3600 + minute * 60
    while target <= now:
        target += 86400
    return target - now


def ist_stamp(when: float | None = None) -> str:
    """The report's own date line, in the timezone its reader lives in."""
    when = time.time() if when is None else when
    return time.strftime("%d %b %Y, %H:%M IST", time.gmtime(when + IST_OFFSET))


async def send_report(client: Client, jobs: Queue) -> int:
    """Render once, deliver to every admin. Returns how many were reached."""
    from .handlers import admin

    text = await admin.daily_report(jobs)
    reached = 0
    for admin_id in cfg.admin_ids:
        try:
            await client.send_message(admin_id, text, disable_web_page_preview=True)
            reached += 1
        except Exception as exc:
            log.warning("nightly report could not reach admin %s: %s", admin_id, exc)
    return reached


async def run(client: Client, jobs: Queue) -> None:
    """
    Sleep until the hour, send, repeat. Cancelled from `main.run` at shutdown.

    Every failure inside the loop is swallowed and logged on purpose. This task
    outlives days of uptime and a report is a convenience: an unreachable admin, a
    Terabox probe that times out or a `MESSAGE_TOO_LONG` must cost tomorrow's
    report, not the loop that would have sent it.
    """
    if parse_hhmm(cfg.daily_report_utc) is None:
        log.info("nightly report is off (DAILY_REPORT_UTC=%r)", cfg.daily_report_utc)
        return

    # One `time.time()`, not two: sampling it again for the stamp lands a fraction
    # of a second short of the target, and 18:30:00 UTC truncates to "23:59 IST" —
    # a log line that says the report fires a minute before it does.
    now = time.time()
    log.info("nightly report at %s UTC — next one %s", cfg.daily_report_utc,
             ist_stamp(now + seconds_until(cfg.daily_report_utc, now)))
    while True:
        await asyncio.sleep(seconds_until(cfg.daily_report_utc))
        try:
            reached = await send_report(client, jobs)
            log.info("nightly report sent to %d admin(s)", reached)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("nightly report failed", exc_info=True)
        # Past the target by a second or two now, so the next wait is a full day.
        await asyncio.sleep(1.0)
