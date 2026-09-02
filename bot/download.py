"""
Direct-file downloader.

Used when a provider returns `kind="file"` — a plain URL whose bytes must be
pulled one chunk at a time. HLS never comes through here; an m3u8 goes to
`media.fetch_to_mp4` and lets ffmpeg assemble the segments.

Why curl_cffi and not aiohttp: several of these hosts fingerprint the TLS
handshake (JA3) and answer anything that is not a real browser with 403, no
matter how correct the headers look. `impersonate` makes the handshake itself
match Chrome's.

curl_cffi is synchronous, so the transfer runs in a worker thread while a small
async ticker publishes progress and watches for cancellation. The thread writes
to a `.part` file and only renames on success, so an interrupted download can
never be mistaken for a complete one — and a retry resumes from the bytes
already on disk with a Range request instead of starting over.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

ProgressCb = Callable[[int, int], Awaitable[None]]  # (done_bytes, total_bytes)
CancelCheck = Callable[[], bool]

IMPERSONATE = "chrome124"
CHUNK = 1 << 20          # 1 MiB — big enough that the GIL is not the bottleneck
TICK = 1.0               # how often progress is published to the caller

class DownloadError(Exception):
    """User-facing download failure. The message is shown as written."""


@dataclass
class _Shared:
    """The only state crossing the thread boundary. Ints and bools only."""
    done: int = 0
    total: int = 0
    stop: bool = False
    finished: bool = False
    error: str = ""


def _session(proxy: str | None):
    from curl_cffi import requests as curl  # imported late so tests can skip it
    return curl.Session(impersonate=IMPERSONATE, proxy=proxy, timeout=60,
                        allow_redirects=True, verify=True)


def _browser_headers(extra: dict[str, str] | None) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    headers.update(extra or {})
    return headers


async def probe_size(url: str, headers: dict[str, str] | None = None,
                     proxy: str | None = None) -> int:
    """
    Content-Length without downloading. 0 means "the server would not say".

    Worth one request: it is what lets the bot refuse a 4 GB file before spending
    an hour fetching it, and what makes the progress bar show a percentage.
    """
    def _run() -> int:
        try:
            with _session(proxy) as session:
                resp = session.head(url, headers=_browser_headers(headers))
                if resp.status_code >= 400 or not resp.headers.get("content-length"):
                    # Plenty of CDNs refuse HEAD; ask for one byte instead.
                    resp = session.get(url, headers={**_browser_headers(headers),
                                                     "Range": "bytes=0-0"}, stream=True)
                    rng = resp.headers.get("content-range", "")
                    resp.close()
                    if "/" in rng:
                        tail = rng.rsplit("/", 1)[1].strip()
                        return int(tail) if tail.isdigit() else 0
                    return 0
                return int(resp.headers["content-length"])
        except Exception:
            return 0

    return await asyncio.to_thread(_run)

def _transfer(url: str, partial: Path, headers: dict[str, str] | None,
              proxy: str | None, shared: _Shared) -> None:
    """Blocking body of one attempt. Runs in a thread; touches only `shared`."""
    resume_from = partial.stat().st_size if partial.exists() else 0
    request_headers = _browser_headers(headers)
    if resume_from:
        request_headers["Range"] = f"bytes={resume_from}-"

    with _session(proxy) as session:
        resp = session.get(url, headers=request_headers, stream=True)
        try:
            if resp.status_code in (401, 403):
                raise DownloadError("The host refused the download (link may have expired).")
            if resp.status_code == 404:
                raise DownloadError("That file is no longer on the server.")
            if resp.status_code >= 400:
                raise DownloadError(f"The host answered {resp.status_code}.")

            # 206 honours the resume; a 200 means it ignored Range and is sending
            # the whole file, so the bytes on disk must be thrown away.
            if resume_from and resp.status_code != 206:
                resume_from = 0
            mode = "ab" if resume_from else "wb"
            shared.done = resume_from

            length = resp.headers.get("content-length")
            if length and str(length).isdigit():
                shared.total = resume_from + int(length)

            with partial.open(mode) as handle:
                for chunk in resp.iter_content(chunk_size=CHUNK):
                    if shared.stop:
                        return
                    if not chunk:
                        continue
                    handle.write(chunk)
                    shared.done += len(chunk)
        finally:
            resp.close()

async def _pump(shared: _Shared, on_progress: ProgressCb | None,
                cancelled: CancelCheck | None) -> None:
    """Publish progress once a second and translate a cancel flag for the thread."""
    while not shared.finished:
        await asyncio.sleep(TICK)
        if cancelled and cancelled():
            shared.stop = True
            return
        if on_progress and shared.done:
            try:
                await on_progress(shared.done, shared.total)
            except Exception:
                log.debug("progress callback raised", exc_info=True)


async def to_file(
    url: str,
    out_path: Path,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
    on_progress: ProgressCb | None = None,
    cancelled: CancelCheck | None = None,
    max_bytes: int = 0,
    attempts: int = 3,
) -> Path:
    """
    Download `url` to `out_path`, resuming across attempts. Returns the path.

    Raises `DownloadError` with something a user can act on, or CancelledError if
    `cancelled()` went true. A dropped connection at 90% resumes from 90%; only
    an outright refusal by the host is fatal.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".part")

    if max_bytes:
        expected = await probe_size(url, headers, proxy)
        if expected and expected > max_bytes:
            partial.unlink(missing_ok=True)
            raise DownloadError(
                f"That file is {expected / (1 << 20):.0f} MB, over the "
                f"{max_bytes / (1 << 20):.0f} MB limit."
            )

    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        shared = _Shared()
        pump = asyncio.create_task(_pump(shared, on_progress, cancelled))
        try:
            await asyncio.to_thread(_transfer, url, partial, headers, proxy, shared)
        except DownloadError:
            shared.finished = True
            pump.cancel()
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("download attempt %d/%d failed: %s", attempt, attempts, last_error)
        finally:
            shared.finished = True
            pump.cancel()

        if shared.stop or (cancelled and cancelled()):
            partial.unlink(missing_ok=True)
            raise asyncio.CancelledError

        got = partial.stat().st_size if partial.exists() else 0
        complete = got > 0 and (not shared.total or got >= shared.total)
        if complete:
            if on_progress:
                try:
                    await on_progress(got, shared.total or got)
                except Exception:
                    pass
            partial.replace(out_path)
            return out_path

        if attempt < attempts:
            await asyncio.sleep(min(10.0, 2.0 * attempt))  # resumes from `got`

    partial.unlink(missing_ok=True)
    raise DownloadError(f"Download kept failing. ({last_error or 'connection dropped'})")
