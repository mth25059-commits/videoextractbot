"""
ffmpeg / ffprobe wrapper.

The one performance rule in here: **never re-encode.** An HLS stream is already
H.264 in MPEG-TS segments; turning it into a playable MP4 is a container change,
not a conversion. With `-c copy` a 1 GB 1080p video is repackaged in well under a
minute on one core. Re-encoding the same file would take 20+ minutes and pin
every core on the box — which is why this module has no quality/bitrate options
at all. There is nothing to tune because nothing is being encoded.

`-movflags +faststart` matters: it moves the MP4 index to the front of the file
so Telegram can stream it without downloading the whole thing first. Without it
the video shows up as a file that must be fully downloaded before it plays.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, float], Awaitable[None]]  # (done_seconds, total_seconds)
CancelCheck = Callable[[], bool]

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

#: What counts as a video inside an archive. Anything missing from here is filed as
#: "other files (skipped)" and never sent, so the list being short is the same bug as
#: refusing the file outright — ffmpeg reads all of these, and `remux_to_mp4` turns
#: whatever Telegram will not play inline into an MP4 with `-c copy`.
VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp",
    ".m2ts", ".mts", ".vob", ".ogv", ".rm", ".rmvb", ".asf", ".divx", ".mpe", ".m1v", ".m2v",
    ".mp4v", ".f4v", ".3g2", ".mxf", ".dav",
}

# First bytes of the common containers, for when an extension lies.
MAGIC = (
    (b"\x1a\x45\xdf\xa3", "mkv/webm"),
    (b"RIFF", "avi"),
    (b"FLV", "flv"),
    (b"\x00\x00\x01\xba", "mpeg-ps"),
    (b"\x00\x00\x01\xb3", "mpeg-vid"),
)


class MediaError(Exception):
    """A user-facing failure from ffmpeg. Message is shown as-is."""


@dataclass
class MediaInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    size_bytes: int = 0
    video_codec: str = ""

    @property
    def ok(self) -> bool:
        return self.duration > 0 and self.width > 0


def tools_available() -> tuple[bool, str]:
    """Checked once at boot so a missing ffmpeg is a startup error, not a job failure."""
    missing = [n for n, p in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)) if not shutil.which(p)]
    if missing:
        return False, (
            f"{' and '.join(missing)} not found on PATH. Install with: "
            "apt-get install -y ffmpeg"
        )
    return True, ""


# --- pure helpers (unit-testable without ffmpeg present) ---------------------

_EXTINF = re.compile(r"^#EXTINF:\s*([0-9.]+)", re.MULTILINE)


def hls_duration(playlist_text: str) -> float:
    """Total seconds in a media playlist, by summing its #EXTINF tags.

    Returns 0.0 for a master playlist (it lists renditions, not segments), which
    is the caller's signal to fall back to ffprobe.
    """
    return round(sum(float(m) for m in _EXTINF.findall(playlist_text or "")), 3)


def parse_progress(chunk: str) -> dict[str, str]:
    """Parse a `-progress pipe:1` block into a dict of its key=value lines."""
    out = {}
    for line in chunk.splitlines():
        key, _, value = line.strip().partition("=")
        if key and value:
            out[key] = value
    return out


def progress_seconds(fields: dict[str, str]) -> float | None:
    """Pull elapsed output time out of a progress block, in seconds."""
    for key, divisor in (("out_time_us", 1_000_000), ("out_time_ms", 1_000_000)):
        raw = fields.get(key)
        if raw and raw.lstrip("-").isdigit():
            return max(0.0, int(raw) / divisor)
    stamp = fields.get("out_time")
    if stamp and ":" in stamp:
        try:
            hours, minutes, seconds = stamp.split(":")
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except ValueError:
            return None
    return None


def looks_like_video(path: Path) -> bool:
    """Extension first, then magic bytes — archives routinely hold mislabelled files."""
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True
    return any(head.startswith(sig) for sig, _ in MAGIC)


# --- ffprobe ----------------------------------------------------------------

async def _run(cmd: list[str], timeout: float = 120) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MediaError("The file took too long to inspect and was given up on.")
    return proc.returncode or 0, stdout, stderr


async def probe(target: str | Path, headers: dict[str, str] | None = None) -> MediaInfo:
    """Read duration/resolution/codec. Works on a local path or a remote URL."""
    cmd = [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd.append(str(target))

    code, stdout, stderr = await _run(cmd, timeout=90)
    if code != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        hint = detail[-1] if detail else "unknown error"
        raise MediaError(f"Could not read that video. ({hint[:160]})")

    try:
        payload = json.loads(stdout or b"{}")
    except json.JSONDecodeError:
        raise MediaError("Could not read that video — ffprobe returned nothing usable.")

    info = MediaInfo()
    fmt = payload.get("format") or {}
    try:
        info.duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        info.duration = 0.0
    try:
        info.size_bytes = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        info.size_bytes = 0

    for stream in payload.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and not info.width:
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.video_codec = stream.get("codec_name") or ""
            if not info.duration:
                try:
                    info.duration = float(stream.get("duration") or 0)
                except (TypeError, ValueError):
                    pass
        elif kind == "audio":
            info.has_audio = True

    return info


# --- fetching ---------------------------------------------------------------

async def _pump_progress(proc, total: float, on_progress: ProgressCb | None,
                         cancelled: CancelCheck | None) -> None:
    """Read ffmpeg's -progress stream and report. Kills the process if cancelled."""
    assert proc.stdout is not None
    block: list[str] = []
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        block.append(line)
        if not line.startswith("progress="):
            continue

        fields = parse_progress("\n".join(block))
        block.clear()
        if cancelled and cancelled():
            proc.kill()
            return
        if on_progress:
            done = progress_seconds(fields)
            if done is not None:
                try:
                    await on_progress(done, total)
                except Exception:  # a failing progress edit must not kill the job
                    log.debug("progress callback raised", exc_info=True)


async def fetch_to_mp4(
    url: str,
    out_path: Path,
    *,
    total_seconds: float = 0.0,
    headers: dict[str, str] | None = None,
    on_progress: ProgressCb | None = None,
    cancelled: CancelCheck | None = None,
    timeout: float = 6 * 3600,
) -> Path:
    """
    Pull an HLS playlist (or any remote stream ffmpeg can open) into a single
    faststart MP4, copying the streams rather than re-encoding.

    `aac_adtstoasc` is required, not optional: HLS carries AAC in ADTS frames and
    MP4 needs it in ASC form. Skip it and the file lands with silent audio.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".part")
    partial.unlink(missing_ok=True)

    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd += [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        "-nostats",
        str(partial),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    pump = asyncio.create_task(_pump_progress(proc, total_seconds, on_progress, cancelled))
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        partial.unlink(missing_ok=True)
        raise MediaError("Download ran past the time limit and was stopped.")
    finally:
        pump.cancel()

    if cancelled and cancelled():
        partial.unlink(missing_ok=True)
        raise asyncio.CancelledError

    if proc.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        hint = tail[-1][:200] if tail else "the source refused the connection"
        raise MediaError(f"Download failed. ({hint})")

    partial.replace(out_path)
    return out_path


async def remux_to_mp4(src: Path, out_path: Path,
                       cancelled: CancelCheck | None = None) -> Path:
    """
    Repackage an already-downloaded file as faststart MP4 without re-encoding.

    This is what makes a .mkv from a ZIP play inline in Telegram instead of
    arriving as a document. If the streams are not MP4-compatible the copy fails,
    and the caller is expected to fall back to sending the original as-is —
    re-encoding on the box is never worth it.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    partial = out_path.with_suffix(out_path.suffix + ".part")
    partial.unlink(missing_ok=True)

    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        "-c", "copy",
        "-movflags", "+faststart",
        str(partial),
    ]
    code, _, stderr = await _run(cmd, timeout=3600)
    if cancelled and cancelled():
        partial.unlink(missing_ok=True)
        raise asyncio.CancelledError
    if code != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        tail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise MediaError(f"Could not repackage as MP4. ({tail[-1][:160] if tail else 'copy refused'})")

    partial.replace(out_path)
    return out_path


async def thumbnail(src: Path, out_path: Path, at_seconds: float | None = None,
                    duration: float = 0.0) -> Path | None:
    """
    Grab one frame as a JPEG for the video's Telegram preview.

    Seeks ~12% in by default: frame zero on most videos is a black fade or a
    studio bumper, which makes every upload look identical in the chat list.
    Returns None on failure — a missing thumbnail is cosmetic, never fatal.
    """
    if at_seconds is None:
        at_seconds = max(1.0, duration * 0.12) if duration else 3.0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{at_seconds:.2f}",
        "-i", str(src),
        "-frames:v", "1",
        # Telegram caps thumbnails at 320px on the long edge and ~200 KB.
        "-vf", "scale=320:-2",
        "-q:v", "4",
        str(out_path),
    ]
    try:
        code, _, _ = await _run(cmd, timeout=90)
    except MediaError:
        return None
    if code != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        return None
    return out_path
