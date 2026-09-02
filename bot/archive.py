"""
ZIP handling.

The user's phone is the reason this module exists: a 1.5 GB archive of eight
videos is unusable on a phone — it must be downloaded whole, then unpacked into
1.5 GB of *more* storage before a single video can be watched. Here the archive is
opened on the server and each video is sent to the chat as its own playable
video, so nothing is ever stored on the phone.

`pyzipper` instead of the standard library's `zipfile`: real-world archives are
routinely AES-encrypted, which `zipfile` cannot read at all (it only handles the
ancient ZipCrypto). pyzipper is a drop-in that handles both.

Nothing here touches Telegram or credits — that is `handlers/zipfiles.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import media
from .config import cfg

log = logging.getLogger(__name__)

GB = 1024 ** 3

#: Entries that are never worth extracting, whatever their extension says.
JUNK_PREFIXES = ("__MACOSX/", ".DS_Store", "._")


class ArchiveError(Exception):
    """User-facing archive failure. Shown as written."""


class NeedsPassword(ArchiveError):
    def __init__(self) -> None:
        super().__init__("🔒 That archive is password-protected. Send me the password.")


@dataclass
class Entry:
    """One file inside the archive."""
    name: str
    size: int
    encrypted: bool = False

    @property
    def path(self) -> Path:
        return Path(self.name)

    @property
    def is_video(self) -> bool:
        return self.path.suffix.lower() in media.VIDEO_SUFFIXES

    @property
    def is_junk(self) -> bool:
        n = self.name.replace("\\", "/")
        base = n.rsplit("/", 1)[-1]
        return (n.endswith("/") or not base
                or any(n.startswith(p) or base.startswith(p) for p in JUNK_PREFIXES))

def price_for(size_bytes: int) -> float:
    """
    Credits for one archive, by its size.

    The published price list is 0–1 GB = 2 credits, 1–2 GB = 4 credits. Beyond
    2 GB the same per-GB rate is continued rather than refused, because Telegram
    can still deliver the individual videos inside it.
    """
    if size_bytes <= GB:
        return float(cfg.cost_zip_upto_1gb)
    if size_bytes <= 2 * GB:
        return float(cfg.cost_zip_upto_2gb)
    extra_gb = -(-(size_bytes - 2 * GB) // GB)  # ceil
    per_gb = float(cfg.cost_zip_upto_2gb) / 2.0
    return round(float(cfg.cost_zip_upto_2gb) + extra_gb * per_gb, 2)


def _open(path: Path, password: str | None):
    try:
        import pyzipper
    except ImportError:  # pragma: no cover - pyzipper is in requirements.txt
        raise ArchiveError("ZIP support is not installed on the server.")

    try:
        handle = pyzipper.AESZipFile(path)
    except Exception as exc:
        raise ArchiveError(f"That does not open as a ZIP file. ({exc})") from exc
    if password:
        handle.setpassword(password.encode("utf-8"))
    return handle


def inspect(path: Path, password: str | None = None) -> list[Entry]:
    """List the archive's contents. Raises `NeedsPassword` if it is locked."""
    with _open(path, password) as zf:
        entries = [
            Entry(name=i.filename, size=i.file_size, encrypted=bool(i.flag_bits & 0x1))
            for i in zf.infolist()
        ]
        if not entries:
            raise ArchiveError("That archive is empty.")
        if any(e.encrypted for e in entries) and not password:
            raise NeedsPassword()
        if password:
            # Fail now, not eight videos in: read one byte from the first real entry.
            first = next((e for e in entries if not e.is_junk), None)
            if first:
                try:
                    with zf.open(first.name) as member:
                        member.read(1)
                except RuntimeError as exc:
                    raise ArchiveError("That password did not work.") from exc
                except Exception as exc:
                    raise ArchiveError(f"Could not read the archive. ({exc})") from exc
    return entries


def videos_in(entries: list[Entry]) -> list[Entry]:
    """The entries worth sending, biggest first — the feature film before the trailer."""
    return sorted((e for e in entries if e.is_video and not e.is_junk and e.size > 0),
                  key=lambda e: e.size, reverse=True)

def safe_name(name: str) -> str:
    """
    Flatten an entry name to a bare filename.

    A ZIP is allowed to contain `../../etc/passwd`, and extracting it as given
    would write outside the work directory. Only the last path segment is ever
    used, with the characters no filesystem accepts stripped out.
    """
    import re
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "", base).strip(". ")
    return base[:150] or "video.mp4"


def extract_one(path: Path, entry: Entry, out_dir: Path,
                password: str | None = None) -> Path:
    """
    Unpack a single entry to `out_dir`, streaming rather than loading it into RAM.

    One video at a time is the point: a 1.5 GB archive of eight videos never needs
    1.5 GB of free disk, only enough for the largest single file, because the
    caller deletes each one after it has been sent.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / safe_name(entry.name)
    with _open(path, password) as zf:
        try:
            with zf.open(entry.name) as member, target.open("wb") as handle:
                while True:
                    chunk = member.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
        except RuntimeError as exc:
            target.unlink(missing_ok=True)
            raise ArchiveError("That password did not work.") from exc
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise ArchiveError(f"Could not extract {safe_name(entry.name)}. ({exc})") from exc
    return target


def summary(entries: list[Entry], videos: list[Entry], size_bytes: int,
            cost: float) -> str:
    from . import ui
    others = len([e for e in entries if not e.is_junk]) - len(videos)
    lines = [
        "🗂 <b>Archive opened</b>",
        "──────────────────",
        f"📦 Size          {ui.human_bytes(size_bytes)}",
        f"🎬 Videos found  <b>{len(videos)}</b>",
    ]
    if others > 0:
        lines.append(f"📄 Other files   {others} <i>(skipped)</i>")
    lines += ["", f"💰 Cost: <b>{cost:g} credits</b> for the whole archive", ""]
    for entry in videos[:10]:
        lines.append(f"  • {ui.esc(safe_name(entry.name))}  —  {ui.human_bytes(entry.size)}")
    if len(videos) > 10:
        lines.append(f"  • …and {len(videos) - 10} more")
    return "\n".join(lines)
