"""
Scratch space: claim it, release it, and sweep up whatever a crash left behind.

The promise is that **nothing a user sends stays on the VPS**. Every job already
deletes its own files as it goes — one video on disk at a time, a part sent and
removed before the next is cut — but "the job deletes its own files" only holds
while the job is alive to run its `finally`. A `SIGKILL`, an OOM kill, a power
loss or an unanswered password prompt all skip it, and until now the leftovers
stayed until someone noticed. On a box with 24 GB free, two abandoned 2 GB
archives are a real outage.

So the rule is not "delete at the end", it is **two independent guarantees**:

1. A live job's directory is *claimed*, and only its own code deletes it.
2. Anything under `work_dir` that is **not** claimed is somebody's leftover, and
   the janitor takes it — immediately at boot, where by definition nothing is
   running, and after `STALE_SECONDS` while the bot is up.

That second guarantee is what makes the first one safe to get wrong.

**Why not stream the archives instead?** Because it cannot be done: ZIP, RAR and
7z all put their directory of contents at the *end* of the file, so reading one
means seeking backwards through it, and a Telegram download is forwards-only.
An archive has to land on disk to be opened at all. What is negotiable is *how
long* it stays there, and that is what `release_when_done` shortens.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

from . import state
from .config import cfg

log = logging.getLogger(__name__)

#: The only names the janitor will ever delete. Every handler builds its directory
#: name from one of these (`tb-<user>-<row>`, `zip-<user>-<message>`,
#: `fap-<user>-<row>`), so anything else under `work_dir` was put there by a human
#: and is left alone. A handler added without its prefix here still works and still
#: cleans up in its own `finally` — but what a `kill -9` leaves behind is then never
#: swept, so the prefix is not optional.
PREFIXES = ("tb-", "zip-", "fap-")

#: How long an unclaimed directory must have sat untouched before it is taken.
#: Tied to the state TTL on purpose: an archive waiting on a password is
#: unreachable once its prompt expires at `state.TTL_SECONDS`, so a quarter of an
#: hour past that is certainly dead rather than merely quiet.
STALE_SECONDS = state.TTL_SECONDS + 15 * 60

#: How often the janitor looks. Nothing is urgent — the disk guard in
#: `Queue._wait_for_disk` already makes a full disk wait rather than fail — so
#: this is deliberately lazy.
SWEEP_EVERY_SECONDS = 10 * 60

#: Directories a running job owns. Resolved paths, because `release()` is called
#: with whatever the handler happens to be holding.
_live: set[Path] = set()


def _key(path: Path) -> Path:
    """One canonical form, so claim and release agree on the same directory."""
    try:
        return path.resolve()
    except OSError:                                     # pragma: no cover
        return path.absolute()


def claim(path: Path) -> Path:
    """Create a job directory and mark it as in use. Returns the same path."""
    path.mkdir(parents=True, exist_ok=True)
    _live.add(_key(path))
    return path


def unclaim(path: Path) -> None:
    """
    Stop protecting a directory without deleting it.

    One caller: a locked archive whose password has been asked for. The file has
    to survive the round trip to the user, but if they never answer, nothing will
    ever come back for it — so it stops being live and the janitor inherits it.
    """
    _live.discard(_key(path))


def release(path: Path) -> None:
    """Delete a job directory and everything in it. Safe to call twice."""
    _live.discard(_key(path))
    shutil.rmtree(path, ignore_errors=True)


def live() -> tuple[Path, ...]:
    """The claimed directories, for the admin card."""
    return tuple(sorted(_live))


def _ours(path: Path) -> bool:
    """
    True for a job directory this module is allowed to delete.

    Deliberately narrow. Only directories, only directly under `work_dir`, only
    with a name a handler would have generated. A loose file at the top of
    `work_dir` is left where it is: nothing here creates one, so its existence
    means a person put it there, and deleting a stranger's file to reclaim a few
    megabytes is not a trade this should make on its own.
    """
    return path.is_dir() and path.name.startswith(PREFIXES)


def size_of(path: Path) -> int:
    """Bytes on disk under `path`. Best effort — a file being written may vanish."""
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def idle_seconds(path: Path) -> float:
    """
    How long since anything under `path` was last written.

    The newest mtime in the tree, not the directory's own: a job that is halfway
    through a 20-minute upload has not touched its parent directory since it
    created `out/`, and judging that directory by its own mtime alone would call
    a working job stale. This is the belt to `_live`'s braces — neither is
    trusted alone.
    """
    newest = 0.0
    for item in (path, *path.rglob("*")):
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            continue
    return max(0.0, time.time() - newest) if newest else 0.0


def sweep(*, min_idle_seconds: float = STALE_SECONDS) -> tuple[int, int]:
    """
    Delete unclaimed job directories. Returns `(directories, bytes)` removed.

    Two conditions, both required: not claimed by a running job, and untouched
    for `min_idle_seconds`. Boot passes 0 for the second one because nothing is
    running yet, which is the only moment a leftover can be recognised for
    certain rather than inferred.
    """
    root = cfg.work_dir
    if not root.is_dir():
        return (0, 0)

    dirs = freed = 0
    for path in sorted(root.iterdir()):
        if not _ours(path) or _key(path) in _live:
            continue
        idle = idle_seconds(path)
        if idle < min_idle_seconds:
            continue
        size = size_of(path)
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():                               # pragma: no cover
            log.warning("scratch: could not remove %s", path)
            continue
        dirs += 1
        freed += size
        log.info("scratch: removed %s (%.1f MB, idle %.0f min)",
                 path.name, size / 1048576, idle / 60)
    return (dirs, freed)


def sweep_at_boot() -> tuple[int, int]:
    """
    Clear `work_dir` completely, before the first job starts.

    Anything here now is from a previous life of the process, and no job survives
    a restart: the queue refunds what was in flight on the way down (`run.sh
    stop`), and a crash that skipped even that left a row the queue will never
    pick up again. So there is nothing to preserve, and the alternative is paying
    for the same gigabytes every restart until someone SSHes in.
    """
    dirs, freed = sweep(min_idle_seconds=0)
    if dirs:
        log.info("scratch: boot sweep freed %.1f MB from %d leftover job(s)",
                 freed / 1048576, dirs)
    return (dirs, freed)


async def janitor(interval: float = SWEEP_EVERY_SECONDS) -> None:
    """
    Sweep every `interval` seconds until cancelled.

    Started and cancelled by `main.run`. A crash in here must never take the bot
    down with it — the worst case of a failed sweep is disk that stays used,
    while an unhandled exception in a bare task is a silent death — so the loop
    logs and carries on.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            dirs, freed = sweep()
            if dirs:
                log.info("scratch: janitor freed %.1f MB from %d stale job(s)",
                         freed / 1048576, dirs)
        except asyncio.CancelledError:
            raise
        except Exception:                               # noqa: BLE001
            log.warning("scratch: sweep failed", exc_info=True)


def report() -> dict[str, int]:
    """What is on disk right now, for the admin card and the nightly report."""
    root = cfg.work_dir
    held = [p for p in root.iterdir() if _ours(p)] if root.is_dir() else []
    return {
        "dirs": len(held),
        "bytes": sum(size_of(p) for p in held),
        "live": len(_live),
        "orphans": sum(1 for p in held if _key(p) not in _live),
    }
