"""
Archive handling — ZIP, RAR and 7z.

The user's phone is the reason this module exists: a 1.5 GB archive of eight
videos is unusable on a phone — it must be downloaded whole, then unpacked into
1.5 GB of *more* storage before a single video can be watched. Here the archive is
opened on the server and each video is sent to the chat as its own playable
video, so nothing is ever stored on the phone.

Three formats, three libraries, one shape:

* **ZIP** — `pyzipper` instead of the standard library's `zipfile`: real-world
  archives are routinely AES-encrypted, which `zipfile` cannot read at all (it only
  handles the ancient ZipCrypto). pyzipper is a drop-in that handles both.
* **RAR** — `rarfile`, driven by the `unar` binary. Not `unrar`: that one is
  non-free and not in Ubuntu's default repositories, and `unar` reads RAR5 as well.
* **7z** — `py7zr`, pure Python, so nothing outside pip has to be present.

**The format is decided by the file's first bytes, never by its name.** A RAR sent
as `movie.zip` is routine — WhatsApp and Terabox both rename freely — and the old
extension-only check answered that with "does not open as a ZIP file", which reads
as the bot being broken rather than the name being wrong.

Nothing here touches Telegram or credits — that is `handlers/zipfiles.py`.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import media
from .config import cfg

log = logging.getLogger(__name__)

GB = 1024 ** 3

#: Entries that are never worth extracting, whatever their extension says.
JUNK_PREFIXES = ("__MACOSX/", ".DS_Store", "._")

#: First bytes → format. `Rar!\x1a\x07` covers both RAR4 (`…\x00`) and RAR5 (`…\x01\x00`).
MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),          # an empty archive still has to open
    (b"PK\x07\x08", "zip"),          # spanned/split, written by some phone apps
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
)

#: Names the document handler will accept before the file has been downloaded, when
#: the bytes cannot be sniffed yet. Deliberately wider than `MAGIC` — `.cbz`/`.cbr`
#: are ZIP and RAR under another name, and being wrong here only costs one download.
ARCHIVE_SUFFIXES = frozenset({".zip", ".rar", ".7z", ".cbz", ".cbr", ".cb7"})

#: What each format is called when the bot has to say it out loud.
FORMAT_NAMES = {"zip": "ZIP", "rar": "RAR", "7z": "7z"}



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

    The price list the bot shows is 0–1 GB = 2, 1–2 GB = 4, 2–3 GB = 6, 3–4 GB = 8,
    and it is rendered *from this function* rather than written out beside it, so
    the two cannot drift. Beyond 2 GB the same per-GB rate continues rather than
    the archive being refused, because Telegram can still deliver the individual
    videos inside it.
    """
    if size_bytes <= GB:
        return float(cfg.cost_zip_upto_1gb)
    if size_bytes <= 2 * GB:
        return float(cfg.cost_zip_upto_2gb)
    extra_gb = -(-(size_bytes - 2 * GB) // GB)  # ceil
    per_gb = float(cfg.cost_zip_upto_2gb) / 2.0
    return round(float(cfg.cost_zip_upto_2gb) + extra_gb * per_gb, 2)


def kind_of(path: Path) -> str:
    """
    Which format this archive actually is, read off its first bytes.

    The name is used only as a tiebreaker for the error message, never to choose the
    reader — see the module docstring for why.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError as exc:
        raise ArchiveError(f"Could not read that file. ({exc})") from exc
    for magic, name in MAGIC:
        if head.startswith(magic):
            return name
    named = path.suffix.lower()
    guess = (" It is named " + named + ", but the file inside is something else."
             if named in ARCHIVE_SUFFIXES else "")
    raise ArchiveError(
        "That is not a ZIP, RAR or 7z archive." + guess
        + "\n\nSend the archive itself, not a shortcut or a renamed video.")


class _Reader:
    """
    One archive, opened. Subclasses differ only in how they list and unpack.

    Deliberately not a context manager over the whole job: an archive is opened
    once to list it and again to pull each video out, because holding a handle open
    across a 20-minute upload is what makes a stale NFS mount or a swept temp file
    fail eight videos in rather than at the start.
    """

    fmt = "archive"

    def close(self) -> None:                                  # pragma: no cover
        pass

    def entries(self) -> list[Entry]:                         # pragma: no cover
        raise NotImplementedError

    def _stream(self, entry: Entry):                          # pragma: no cover
        raise NotImplementedError

    def extract_to(self, entry: Entry, target: Path) -> None:
        """Stream one member to `target` — never into memory, these are videos."""
        with self._stream(entry) as member, target.open("wb") as out:
            while True:
                chunk = member.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)

    def read_one_byte(self, entry: Entry) -> None:
        """Cheapest possible proof that the password works."""
        with self._stream(entry) as member:
            member.read(1)


class _Zip(_Reader):
    fmt = "ZIP"

    def __init__(self, path: Path, password: str | None):
        try:
            import pyzipper
        except ImportError:  # pragma: no cover - pyzipper is in requirements.txt
            raise ArchiveError("ZIP support is not installed on the server.")
        try:
            self._zf = pyzipper.AESZipFile(path)
        except Exception as exc:
            raise ArchiveError(f"That does not open as a ZIP file. ({exc})") from exc
        if password:
            self._zf.setpassword(password.encode("utf-8"))

    def close(self) -> None:
        self._zf.close()

    def entries(self) -> list[Entry]:
        return [Entry(name=i.filename, size=i.file_size,
                      encrypted=bool(i.flag_bits & 0x1))
                for i in self._zf.infolist()]

    def _stream(self, entry: Entry):
        return self._zf.open(entry.name)


class _Rar(_Reader):
    fmt = "RAR"

    def __init__(self, path: Path, password: str | None):
        try:
            import rarfile
        except ImportError:
            raise ArchiveError("RAR support is not installed on the server.")
        try:
            self._rf = rarfile.RarFile(path)
        except Exception as exc:
            raise ArchiveError(f"That does not open as a RAR file. ({exc})") from exc
        if password:
            self._rf.setpassword(password)

    def close(self) -> None:
        self._rf.close()

    def entries(self) -> list[Entry]:
        out = []
        for info in self._rf.infolist():
            locked = False
            try:
                locked = bool(info.needs_password())
            except Exception:               # older rarfile, or a directory entry
                locked = False
            name = info.filename + ("/" if info.is_dir() else "")
            out.append(Entry(name=name, size=info.file_size or 0, encrypted=locked))
        return out

    def _stream(self, entry: Entry):
        return self._rf.open(entry.name.rstrip("/"))


class _Seven(_Reader):
    fmt = "7z"

    def __init__(self, path: Path, password: str | None):
        try:
            import py7zr
        except ImportError:
            raise ArchiveError("7z support is not installed on the server.")
        self._path, self._password = path, password
        self._py7zr = py7zr
        try:
            with self._open() as handle:
                self._locked = bool(handle.needs_password())
        except Exception as exc:
            raise ArchiveError(f"That does not open as a 7z file. ({exc})") from exc

    def _open(self):
        return self._py7zr.SevenZipFile(self._path, "r", password=self._password)

    def entries(self) -> list[Entry]:
        with self._open() as handle:
            return [Entry(name=i.filename + ("/" if i.is_directory else ""),
                          size=int(i.uncompressed or 0), encrypted=self._locked)
                    for i in handle.list()]

    def extract_to(self, entry: Entry, target: Path) -> None:
        """
        py7zr has no per-member file object, so it writes to a scratch directory.

        It keeps the archive's own folder structure on the way out, so the result is
        found at its internal path and then moved to `target` — one file at a time,
        so the disk still only ever holds the largest single video.
        """
        inner = entry.name.rstrip("/")
        with tempfile.TemporaryDirectory(dir=str(target.parent)) as tmp:
            with self._open() as handle:
                handle.extract(path=tmp, targets=[inner])
            produced = Path(tmp) / inner
            if not produced.is_file():
                raise ArchiveError(
                    f"7z did not produce {safe_name(entry.name)}. It may be locked.")
            shutil.move(str(produced), str(target))

    def read_one_byte(self, entry: Entry) -> None:
        with self._open() as handle:
            data = handle.read(targets=[entry.name.rstrip("/")]) or {}
        if not data:
            raise ArchiveError("That password did not work.")


READERS = {"zip": _Zip, "rar": _Rar, "7z": _Seven}


def _open(path: Path, password: str | None) -> _Reader:
    """Open `path` with whichever reader its first bytes call for."""
    return READERS[kind_of(path)](path, password)



def inspect(path: Path, password: str | None = None) -> list[Entry]:
    """List the archive's contents. Raises `NeedsPassword` if it is locked."""
    reader = _open(path, password)
    try:
        entries = reader.entries()
        if not entries:
            raise ArchiveError("That archive is empty.")
        if any(e.encrypted for e in entries) and not password:
            raise NeedsPassword()
        if password:
            # Fail now, not eight videos in: read one byte from the first real entry.
            first = next((e for e in entries if not e.is_junk), None)
            if first:
                try:
                    reader.read_one_byte(first)
                except ArchiveError:
                    raise
                except RuntimeError as exc:
                    raise ArchiveError("That password did not work.") from exc
                except Exception as exc:
                    raise ArchiveError(f"Could not read the archive. ({exc})") from exc
    finally:
        reader.close()
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
                password: str | None = None, as_name: str = "") -> Path:
    """
    Unpack a single entry to `out_dir`, streaming rather than loading it into RAM.

    One video at a time is the point: a 1.5 GB archive of eight videos never needs
    1.5 GB of free disk, only enough for the largest single file, because the
    caller deletes each one after it has been sent.

    `as_name` overrides what the file is called on disk, keeping only the entry's
    suffix if the caller did not give one. It exists because the on-disk name is the
    name Telegram receives — `uploader.send_video` passes no `file_name`, so pyrogram
    uses the basename — and this is the one route where that would be the archive's
    own name. The Terabox and link routes have always written `01.mp4`; the handler
    passes `as_name` so this one does too. The real name is still what the "Skipped:"
    list reports, because there it is the only way to know *which* file failed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if as_name:
        target = out_dir / (safe_name(as_name) or safe_name(entry.name))
        if not target.suffix:
            target = target.with_suffix(Path(safe_name(entry.name)).suffix)
    else:
        target = out_dir / safe_name(entry.name)
    reader = _open(path, password)
    try:
        reader.extract_to(entry, target)
    except ArchiveError:
        target.unlink(missing_ok=True)
        raise
    except RuntimeError as exc:
        target.unlink(missing_ok=True)
        raise ArchiveError("That password did not work.") from exc
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise ArchiveError(
            f"Could not extract {safe_name(entry.name)}. ({exc})") from exc
    finally:
        reader.close()
    return target


def summary(entries: list[Entry], videos: list[Entry], size_bytes: int,
            cost: float, fmt: str = "") -> str:
    from . import ui
    others = len([e for e in entries if not e.is_junk]) - len(videos)
    name = FORMAT_NAMES.get(fmt, "Archive")
    lines = [
        f"🗂 <b>{name} opened</b>",
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


def kind_or_blank(path: Path) -> str:
    """
    `kind_of` for display: the format name, or "" when it cannot be read.

    Used where the answer only decorates a message, so a broken file must not turn
    a summary into an exception.
    """
    try:
        return kind_of(path)
    except ArchiveError:
        return ""
