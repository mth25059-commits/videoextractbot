"""
ZIP pricing and entry-safety checks. No pyzipper, no Telegram needed.

Run: python tests/test_archive.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")

from bot import archive  # noqa: E402

GB = archive.GB
passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


print("\nprice_for (0-1 GB = 2 cr, 1-2 GB = 4 cr)")
check("empty file", archive.price_for(0), 2.0)
check("500 MB", archive.price_for(500 * 1024 * 1024), 2.0)
check("exactly 1 GB", archive.price_for(GB), 2.0)
check("1 GB + 1 byte", archive.price_for(GB + 1), 4.0)
check("exactly 2 GB", archive.price_for(2 * GB), 4.0)
check("3 GB continues the rate", archive.price_for(2 * GB + 1), 6.0)
check("5 GB", archive.price_for(5 * GB), 10.0)

print("\nsafe_name (a ZIP may contain ../../etc/passwd)")
check("path traversal flattened",
      archive.safe_name("../../etc/passwd"), "passwd")
check("windows separators flattened",
      archive.safe_name(r"folder\sub\clip.mp4"), "clip.mp4")
check("nested path flattened",
      archive.safe_name("Season 1/Ep 2/show.mkv"), "show.mkv")
check("illegal characters stripped",
      archive.safe_name('bad<>:"|?*name.mp4'), "badname.mp4")
check("absolute path flattened",
      archive.safe_name("/etc/shadow"), "shadow")
check("empty falls back", archive.safe_name(""), "video.mp4")
check("dot-only falls back", archive.safe_name("..."), "video.mp4")
check("long name capped", len(archive.safe_name("x" * 400 + ".mp4")), 150)

print("\nEntry")
check("mp4 is a video", archive.Entry("a/b/clip.mp4", 10).is_video, True)
check("mkv is a video", archive.Entry("clip.MKV", 10).is_video, True)
# The suffix list decides what gets sent at all: a format missing from it is filed
# as "other files (skipped)" and silently never delivered.
for _suffix in (".m2ts", ".mts", ".vob", ".rmvb", ".ogv", ".divx", ".f4v", ".dav", ".asf"):
    check(f"{_suffix} is a video too",
          archive.Entry(f"episode{_suffix}", 10).is_video, True)
check("txt is not", archive.Entry("readme.txt", 10).is_video, False)
check("a subtitle is not", archive.Entry("episode.srt", 10).is_video, False)
check("a nfo is not", archive.Entry("episode.nfo", 10).is_video, False)
check("directory entry is junk", archive.Entry("folder/", 0).is_junk, True)
check("macos metadata is junk", archive.Entry("__MACOSX/clip.mp4", 10).is_junk, True)
check("apple double is junk", archive.Entry("._clip.mp4", 10).is_junk, True)
check("real file is not junk", archive.Entry("clip.mp4", 10).is_junk, False)

print("\nvideos_in (biggest first, junk and empties dropped)")
entries = [
    archive.Entry("trailer.mp4", 5_000_000),
    archive.Entry("__MACOSX/movie.mp4", 300),
    archive.Entry("movie.mkv", 900_000_000),
    archive.Entry("notes.txt", 1_000),
    archive.Entry("empty.mp4", 0),
    archive.Entry("extras/short.mp4", 20_000_000),
]
check("ordered and filtered",
      [e.name for e in archive.videos_in(entries)],
      ["movie.mkv", "extras/short.mp4", "trailer.mp4"])
check("nothing sendable", archive.videos_in([archive.Entry("a.txt", 5)]), [])

print("\nsummary")
videos = archive.videos_in(entries)
text = archive.summary(entries, videos, 950 * 1024 * 1024, 2.0)
check("counts the videos", "<b>3</b>" in text, True)
check("counts the skipped others", "Other files   2" in text, True)
check("states the price", "2 credits" in text, True)
check("names are escaped and flattened", "short.mp4" in text, True)
check("no format known, stays generic", "Archive opened" in text, True)
check("a RAR says so", "RAR opened" in archive.summary(entries, videos, 1, 2.0, "rar"), True)
check("a 7z says so", "7z opened" in archive.summary(entries, videos, 1, 2.0, "7z"), True)
check("a ZIP says so", "ZIP opened" in archive.summary(entries, videos, 1, 2.0, "zip"), True)

# --- the format comes off the first bytes, never the name --------------------
#
# A RAR named `movie.zip` is routine: WhatsApp and Terabox both rename freely.
# The old extension-only check answered that with "does not open as a ZIP file",
# which reads as the bot being broken rather than the name being wrong.
print("\nkind_of reads magic bytes, not the extension")
import tempfile                                          # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="terabot-magic-"))


def _write(name: str, head: bytes) -> Path:
    p = _tmp / name
    p.write_bytes(head + b"\0" * 64)
    return p


check("a real ZIP", archive.kind_of(_write("a.zip", b"PK\x03\x04")), "zip")
check("an empty ZIP still opens", archive.kind_of(_write("b.zip", b"PK\x05\x06")), "zip")
check("a spanned ZIP still opens", archive.kind_of(_write("c.zip", b"PK\x07\x08")), "zip")
check("RAR4", archive.kind_of(_write("d.rar", b"Rar!\x1a\x07\x00")), "rar")
check("RAR5", archive.kind_of(_write("e.rar", b"Rar!\x1a\x07\x01\x00")), "rar")
check("7z", archive.kind_of(_write("f.7z", b"7z\xbc\xaf\x27\x1c")), "7z")
check("a RAR named .zip is still a RAR",
      archive.kind_of(_write("movie.zip", b"Rar!\x1a\x07\x00")), "rar")
check("a ZIP named .rar is still a ZIP",
      archive.kind_of(_write("movie.rar", b"PK\x03\x04")), "zip")

try:
    archive.kind_of(_write("clip.mp4", b"\x00\x00\x00\x18ftyp"))
    check("a plain video is refused", "no error", "ArchiveError")
except archive.ArchiveError as exc:
    check("a plain video is refused", "not a ZIP, RAR or 7z" in str(exc), True)
    check("and is not blamed on the name", "It is named" in str(exc), False)

try:
    archive.kind_of(_write("g.zip", b"not an archive at all"))
    check("a mislabelled file names the mismatch", "no error", "ArchiveError")
except archive.ArchiveError as exc:
    check("a mislabelled file names the mismatch", "It is named .zip" in str(exc), True)

check("kind_or_blank swallows the refusal",
      archive.kind_or_blank(_tmp / "clip.mp4"), "")
check("kind_or_blank still answers for a real one",
      archive.kind_or_blank(_tmp / "d.rar"), "rar")
check("a missing file does not raise either",
      archive.kind_or_blank(_tmp / "nothing-here.zip"), "")

print("\nevery format kind_of returns has a reader")
check("readers cover the magic table",
      sorted(set(archive.READERS)), sorted({n for _, n in archive.MAGIC}))
check("and every reader is named for the user",
      sorted(archive.FORMAT_NAMES), sorted(archive.READERS))
check(".cbz is offered before download", ".cbz" in archive.ARCHIVE_SUFFIXES, True)
check(".rar is offered before download", ".rar" in archive.ARCHIVE_SUFFIXES, True)
check(".7z is offered before download", ".7z" in archive.ARCHIVE_SUFFIXES, True)
check("a video suffix is not", ".mp4" in archive.ARCHIVE_SUFFIXES, False)

import shutil as _shutil                                 # noqa: E402
_shutil.rmtree(_tmp, ignore_errors=True)

# --- a real archive, listed and unpacked -------------------------------------
#
# Everything above is about names and bytes. This part builds actual archives and
# runs them through `inspect` / `extract_one`, because the reader layer is where a
# format change actually breaks: a wrong member name or a handle closed too early
# only shows up against a real file.
print("\na real archive, opened and unpacked")
_ar = Path(tempfile.mkdtemp(prefix="terabot-real-"))
import pyzipper                                          # noqa: E402

_zp = _ar / "clips.zip"
with pyzipper.AESZipFile(_zp, "w") as _zf:
    _zf.writestr("Season 1/big.mp4", b"A" * 5000)
    _zf.writestr("Season 1/small.mkv", b"B" * 100)
    _zf.writestr("__MACOSX/._big.mp4", b"junk")
    _zf.writestr("readme.txt", b"hello")

_found = archive.inspect(_zp)
_vids = archive.videos_in(_found)
check("both videos listed", [archive.safe_name(e.name) for e in _vids],
      ["big.mp4", "small.mkv"])
check("the junk and the text file are not videos", len(_vids), 2)
_out = archive.extract_one(_zp, _vids[0], _ar / "out")
check("the bytes came out whole", _out.read_bytes(), b"A" * 5000)
check("and the nested path was flattened away", _out.name, "big.mp4")

# `as_name` is the privacy hook. The on-disk name is the name Telegram receives —
# `uploader.send_video` passes no `file_name` — so the archive route, the one place a
# real name reached the disk, extracts to a number instead.
_named = archive.extract_one(_zp, _vids[0], _ar / "out3", as_name="01")
check("as_name replaces the entry's name", _named.name, "01.mp4")
check("and the bytes are still the same file", _named.read_bytes(), b"A" * 5000)
check("a non-mp4 keeps its own suffix, which ffmpeg needs to pick a demuxer",
      archive.extract_one(_zp, _vids[1], _ar / "out3", as_name="02").name, "02.mkv")
check("a caller that gives its own suffix is obeyed",
      archive.extract_one(_zp, _vids[0], _ar / "out3", as_name="03.dat").name, "03.dat")
check("and an as_name is not trusted any more than an entry name is",
      archive.extract_one(_zp, _vids[0], _ar / "out3",
                          as_name="../../evil").parent, _ar / "out3")

_locked = _ar / "locked.zip"
with pyzipper.AESZipFile(_locked, "w", compression=pyzipper.ZIP_DEFLATED,
                         encryption=pyzipper.WZ_AES) as _zf:
    _zf.setpassword(b"hunter2")
    _zf.writestr("secret.mp4", b"D" * 2000)

try:
    archive.inspect(_locked)
    check("a locked archive asks for the password", "no error", "NeedsPassword")
except archive.NeedsPassword:
    check("a locked archive asks for the password", True, True)
except archive.ArchiveError as exc:
    check("a locked archive asks for the password", f"wrong error: {exc}", "NeedsPassword")

try:
    archive.inspect(_locked, "wrong-one")
    check("a wrong password is refused up front", "no error", "ArchiveError")
except archive.ArchiveError as exc:
    check("a wrong password is refused up front", isinstance(exc, archive.ArchiveError), True)
    check("and is not reported as needing one again",
          isinstance(exc, archive.NeedsPassword), False)

check("the right password lists it",
      [e.name for e in archive.inspect(_locked, "hunter2")], ["secret.mp4"])
check("and unpacks it",
      archive.extract_one(_locked, archive.Entry("secret.mp4", 2000),
                          _ar / "out2", "hunter2").read_bytes(), b"D" * 2000)

try:
    import py7zr                                         # noqa: E402
except ImportError:
    print("  skip  7z round trip — py7zr is not installed here (it is on the box)")
else:
    _sp = _ar / "clips.7z"
    with py7zr.SevenZipFile(_sp, "w") as _z7:
        _z7.writestr(b"C" * 3000, "folder/movie.mp4")
        _z7.writestr(b"notes", "folder/read.txt")
    check("7z is detected by its magic", archive.kind_of(_sp), "7z")
    _s7 = archive.videos_in(archive.inspect(_sp))
    check("7z lists its one video", [archive.safe_name(e.name) for e in _s7],
          ["movie.mp4"])
    check("7z unpacks to the flattened name",
          archive.extract_one(_sp, _s7[0], _ar / "out7").read_bytes(), b"C" * 3000)
    check("and the scratch directory it used is gone",
          [p.name for p in (_ar / "out7").iterdir()], ["movie.mp4"])

try:
    import rarfile                                       # noqa: E402
except ImportError:
    print("  skip  RAR backend check — rarfile is not installed here (it is on the box)")
else:
    check("rarfile picked a backend that exists",
          bool(rarfile.tool_setup(sevenzip=False, sevenzip2=False)), True)

_shutil.rmtree(_ar, ignore_errors=True)

# --- the price the user is SHOWN is the price price_for() charges ------------
#
# This is the bug that shipped: the ladder above has charged 6 credits for a
# 2-3 GB archive from the beginning, and the menu's price list stopped at 2 GB —
# so the feature was real and invisible. `zipfiles.prompt()` is now rendered from
# `price_for`, and this section is what holds the two together.
#
# It is a function, not the module constant it used to be, because `price_for` reads
# the two ZIP rungs out of the settings table — a constant built at import would have
# quoted the price the process started with for ever, and the point of that table is
# that the admin can change a price without a restart. The last block below is what
# proves the screen actually follows a change.
#
# Importing the handler needs pyrogram, which nothing else in this file wants, so
# it is stubbed here rather than at the top.
print("\nthe quoted price list matches price_for")


def _stub_pyrogram() -> None:
    import types

    class _F:
        """Enough of a filter for `private & text & ~command([...]) & in_mode(x)`."""

        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def __and__(self, _other):
            return self

        __rand__ = __or__ = __ror__ = __and__

        def __invert__(self):
            return self

    def create(func, name="CustomFilter", **kwargs):
        obj = _F(**kwargs)
        obj.check = lambda client, update: func(obj, client, update)
        obj.name = name
        return obj

    filters = types.ModuleType("pyrogram.filters")
    filters.create = create
    filters.regex = filters.command = lambda *_a, **_kw: _F()
    filters.private = filters.text = filters.document = _F()
    errors = types.ModuleType("pyrogram.errors")
    errors.FloodWait = type("FloodWait", (Exception,), {"value": 0})
    kinds = types.ModuleType("pyrogram.types")
    for name in ("Message", "CallbackQuery",
                 "InlineKeyboardButton", "InlineKeyboardMarkup",
                 "KeyboardButton", "ReplyKeyboardMarkup"):
        # Constructible, and forgiving about how: `keyboards.py` really does build
        # these, so a class that refuses arguments turns any test that touches a
        # keyboard into a TypeError about `object()` rather than a real result.
        setattr(kinds, name, type(name, (), {
            "__init__": lambda self, *a, **kw: setattr(self, "args", (a, kw)),
        }))
    root = types.ModuleType("pyrogram")
    root.Client = type("Client", (), {})
    root.filters, root.errors, root.types = filters, errors, kinds
    sys.modules.update({"pyrogram": root, "pyrogram.filters": filters,
                        "pyrogram.errors": errors, "pyrogram.types": kinds})


_stub_pyrogram()
from bot.handlers import zipfiles                     # noqa: E402

quoted = zipfiles.prompt()
for label, size in (("up to 1 GB", GB), ("1 – 2 GB", 2 * GB),
                    ("2 – 3 GB", 3 * GB), ("3 – 4 GB", 4 * GB)):
    row = f"• {label} — <b>{archive.price_for(size):g} credits</b>"
    check(f"{label} is quoted at what it costs", row in quoted, True)

check("and the 2-3 GB row really does say 6 credits",
      "• 2 – 3 GB — <b>6 credits</b>" in quoted, True)

print("\nthe prompt offers every format the readers handle")
for word in ("ZIP", "RAR", "7z"):
    check(f"{word} is offered", word in quoted, True)
check("the per-extra-GB rate is quoted, and matches the ladder",
      f"+{archive.price_for(5 * GB) - archive.price_for(4 * GB):g} credits</b> per extra GB"
      in quoted, True)
check("and splitting is promised rather than a skip",
      "in parts" in quoted, True)

# --- a price changed mid-run reaches the screen and the charge ----------------
#
# The reason `price_for` and `prompt()` read the settings table instead of `cfg`: the
# admin's ⚙️ Prices screen and the setup wizard both write it, and neither restarts the
# bot. Without this, the change would be stored, reported as saved, and then quoted
# wrongly to every user until the next deploy.
print("\na rung changed while the bot runs")
import sqlite3                                          # noqa: E402

from bot import db as _db, settings as _settings        # noqa: E402


def _pricefail(name, value) -> bool:
    """True when `settings.set` refused this, with a sentence fit to show an admin."""
    try:
        _settings.set(name, value)
    except _settings.BadValue as exc:
        return bool(str(exc).strip())
    return False


_pricedir = tempfile.mkdtemp(prefix="terabot-price-")
_conn = sqlite3.connect(Path(_pricedir) / "test.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(_db.SCHEMA)
_conn.commit()
_db._conn = _conn
_settings.forget_cache()

check("the installed default is in force to begin with",
      _settings.is_default("cost_zip_upto_1gb"), True)
_settings.set("cost_zip_upto_1gb", 3)
check("price_for follows the new rung", archive.price_for(GB), 3.0)
check("and the quoted list is rebuilt from it",
      "• up to 1 GB — <b>3 credits</b>" in zipfiles.prompt(), True)
check("the rungs above it are untouched", archive.price_for(2 * GB), 4.0)
_settings.set("cost_zip_upto_2gb", 5)
check("the 2 GB rung moves the per-GB rate with it",
      archive.price_for(3 * GB), 7.5)
check("which the screen quotes too",
      "• 2 – 3 GB — <b>7.5 credits</b>" in zipfiles.prompt(), True)
check("a price is refused above its ceiling",
      _pricefail("cost_zip_upto_1gb", 5000), True)
check("and refused at zero — free is not a price",
      _pricefail("cost_zip_upto_1gb", 0), True)
_settings.reset("cost_zip_upto_1gb")
_settings.reset("cost_zip_upto_2gb")
check("reset puts the installed ladder back", archive.price_for(GB), 2.0)
_conn.close()
_shutil.rmtree(_pricedir, ignore_errors=True)
_db._conn = None
_settings.forget_cache()

# --- over the ceiling is parts, not a refusal --------------------------------
#
# The operator's words: "koi video file break kre … max jitte file ho wo sab break krdo".
# A video Telegram will not take whole is still the video that was paid for.
print("\nsend_in_parts cuts instead of refusing")
import asyncio                                           # noqa: E402

from bot import uploader                                 # noqa: E402

_real_limit = uploader.limit_bytes
uploader.limit_bytes = lambda: 100                       # a 100-byte "2 GB"

check("one part when it fits", uploader.part_count(100), 1)
check("two parts one byte over", uploader.part_count(101), 2)
check("three parts", uploader.part_count(250), 3)
check("an exact multiple does not gain a part", uploader.part_count(200), 2)

note = uploader.rejoin_note("My Show S01E02.mkv", 3)
check("the note says how many", "3 parts" in note, True)
check("the note names the file", "My Show S01E02.mkv" in note, True)
check("the note gives the linux command", "cat &quot;My Show" in note or "cat \"My Show" in note, True)
check("the note gives the windows command", "copy /b" in note, True)
check("the note warns about missing pieces", "every part" in note, True)


class _FakeClient:
    """Records what would have gone to Telegram."""

    def __init__(self):
        self.docs: list[tuple[str, int]] = []
        self.texts: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.texts.append(text)
        return object()

    async def send_document(self, chat_id, document, **kw):
        path = Path(document)
        self.docs.append((kw.get("file_name") or path.name, path.stat().st_size))
        return object()


_tmp2 = Path(tempfile.mkdtemp(prefix="terabot-split-"))
big = _tmp2 / "movie.mkv"
big.write_bytes(bytes(range(256)) * 1)                   # 256 bytes -> 3 parts
fake = _FakeClient()
sent = asyncio.run(uploader.send_in_parts(fake, 1, big, caption="cap"))

check("every part was sent", len(fake.docs), 3)
check("named .001 .002 .003", [n for n, _ in fake.docs],
      ["movie.mkv.001", "movie.mkv.002", "movie.mkv.003"])
check("cut at the ceiling", [s for _, s in fake.docs], [100, 100, 56])
check("the pieces add up to the whole file",
      sum(s for _, s in fake.docs), big.stat().st_size)
check("the join instructions were sent once",
      sum("parts" in t for t in fake.texts), 1)
check("Sent reports the part count", sent.parts, 3)
check("Sent reports the whole size", sent.size_bytes, 256)
check("no part files were left on disk",
      sorted(p.name for p in _tmp2.iterdir()), ["movie.mkv"])

exact = _tmp2 / "exact.mkv"
exact.write_bytes(b"x" * 200)
fake2 = _FakeClient()
check("an exact multiple sends exactly that many",
      asyncio.run(uploader.send_in_parts(fake2, 1, exact)).parts, 2)
check("and leaves no empty trailing part", len(fake2.docs), 2)

uploader.limit_bytes = _real_limit
_shutil.rmtree(_tmp2, ignore_errors=True)

# --- nothing stays on the VPS ------------------------------------------------
#
# The operator's item 9: "vps pe kuch save na ho". Two halves, tested separately,
# because they fail separately. The job deletes its own files as it goes — that is
# `scratch.claim`/`release` and the early archive unlink — and the janitor deletes
# what a job never got the chance to, which is the only half that survives a
# `kill -9`.
print("\nscratch: a claimed directory is protected, an orphan is not")
import time                                              # noqa: E402
import types                                             # noqa: E402

from bot import scratch                                  # noqa: E402
from bot.config import cfg                               # noqa: E402

_scratch_root = Path(tempfile.mkdtemp(prefix="terabot-scratch-"))
_real_work_dir = cfg.work_dir
object.__setattr__(cfg, "work_dir", _scratch_root)


def _make(name: str, *, size: int = 32, age: float = 0.0) -> Path:
    """A job directory with a file in it, optionally backdated."""
    path = _scratch_root / name
    (path / "out").mkdir(parents=True, exist_ok=True)
    (path / "out" / "clip.mp4").write_bytes(b"v" * size)
    if age:
        when = time.time() - age
        for item in (path / "out" / "clip.mp4", path / "out", path):
            os.utime(item, (when, when))
    return path


live_dir = scratch.claim(_make("zip-1-100"))
orphan = _make("tb-2-200", age=scratch.STALE_SECONDS + 60)
fresh = _make("tb-3-300")
stranger = _scratch_root / "notes"
stranger.mkdir()
(stranger / "keep.txt").write_bytes(b"mine")

check("a claimed directory is live", live_dir in scratch.live(), True)
dirs, freed = scratch.sweep()
check("only the stale orphan went", dirs, 1)
check("and its bytes were counted", freed, 32)
check("the claimed job survived", live_dir.exists(), True)
check("a fresh unclaimed one survived too", fresh.exists(), True)
check("the orphan is gone", orphan.exists(), False)
check("somebody else's directory is never touched", stranger.exists(), True)

check("a live job is not idle even with an old mtime",
      scratch.idle_seconds(live_dir) < 5, True)
check("size_of counts the tree", scratch.size_of(live_dir), 32)

# `unclaim` is the locked-archive case: keep the file, drop the protection.
scratch.unclaim(live_dir)
check("unclaim leaves the file where it is", live_dir.exists(), True)
check("but stops protecting it", scratch.live(), ())
check("still too young to sweep", scratch.sweep()[0], 0)

report = scratch.report()
check("the report counts what is on disk", report["dirs"], 2)
check("and how much of it nobody owns", report["orphans"], 2)

# Boot: nothing can be running, so everything unclaimed goes however new it is.
scratch.claim(_make("zip-9-900"))
dirs, _ = scratch.sweep_at_boot()
check("boot takes every unclaimed leftover", dirs, 2)
check("a claim still protects even then — and at real boot there are none",
      sorted(p.name for p in _scratch_root.iterdir()), ["notes", "zip-9-900"])
scratch.release(_scratch_root / "zip-9-900")
check("release deletes and deregisters",
      ((_scratch_root / "zip-9-900").exists(), scratch.live()), (False, ()))
check("release twice is not an error", scratch.release(_scratch_root / "zip-9-900"), None)

print("\nthe archive is deleted before the last upload, not after")


class _StubStatus:
    """The panel — which records every frame, because that is where the name leaked."""

    def __init__(self):
        self.seen: list[str] = []

    async def edit_text(self, text="", *_a, **_kw):
        self.seen.append(text)
        return None


class _SeenClient(_FakeClient):
    """Notes whether the archive was still on disk while each video went out."""

    def __init__(self, watched: Path):
        super().__init__()
        self.watched = watched
        self.archive_present: list[bool] = []

    async def send_video(self, chat_id, video, **kw):
        self.archive_present.append(self.watched.exists())
        return types.SimpleNamespace(id=1)


_job_dir = scratch.claim(_scratch_root / "zip-7-700")
_zip_path = _job_dir / "two.zip"
with pyzipper.AESZipFile(_zip_path, "w") as _zf:
    _zf.writestr("a.mp4", b"a" * 64)
    _zf.writestr("b.mp4", b"b" * 64)
_videos = archive.videos_in(archive.inspect(_zip_path))
check("both videos are in there", len(_videos), 2)

_seen = _SeenClient(_zip_path)
_sent_names: list[str] = []
_sent_titles: list[str] = []
_panel = _StubStatus()


async def _fake_send_best_effort(client, chat_id, path, **kw):
    _sent_names.append(path.name)
    _sent_titles.append(kw.get("title") or "")
    await client.send_video(chat_id, path)
    return uploader.Sent(message=None, size_bytes=path.stat().st_size,
                         seconds=0.01, parts=1)


_real_best_effort = uploader.send_best_effort
uploader.send_best_effort = _fake_send_best_effort
_zip_job = zipfiles.jobq.Job(
    user_id=1, chat_id=1, kind="zip", runner=lambda j: None, cost=2.0,
    payload={"zip_path": _zip_path, "password": None, "videos": _videos,
             "status": _panel},
)
try:
    asyncio.run(zipfiles._deliver(_seen, _zip_job))
finally:
    uploader.send_best_effort = _real_best_effort

check("both videos were sent", len(_sent_names), 2)
check("what Telegram was handed is a number, not the archive's own name",
      _sent_names, ["01.mp4", "02.mp4"])
check("and the panel counted them rather than naming them",
      _sent_titles, ["Video 1 of 2", "Video 2 of 2"])
check("no frame of the panel carried an entry name",
      [t for t in _panel.seen if "a.mp4" in t or "b.mp4" in t], [])
check("the receipt says how many, and nothing about what",
      "2 of 2 videos sent" in _panel.seen[-1], True)
check("the archive was still there for the first upload",
      _seen.archive_present[0], True)
check("and gone by the last one — no doubled disk at the worst moment",
      _seen.archive_present[-1], False)
check("the whole job directory went with it", _job_dir.exists(), False)
check("and it is not claimed any more", scratch.live(), ())

object.__setattr__(cfg, "work_dir", _real_work_dir)
_shutil.rmtree(_scratch_root, ignore_errors=True)

# --- the user's own upload does not stay in the chat -------------------------
#
# The operator's item 7: "user bhje to cht se delete ho". The pasted link and the pressed
# key were already taken back out; the uploaded archive was the one input still
# sitting there for ever, and a 2 GB RAR in a chat is the whole of the ask. The
# password typed for a locked one was worse — it stayed too, and it is the most
# sensitive thing anyone types into this bot.
#
# Driven through the registered handlers with a real queue and a real (temporary)
# database, because what is being tested is *when* the delete happens: after the
# bytes are on the box, and never on a path where the user's copy is all there is.
print("\nthe uploaded archive and the typed password are taken out of the chat")

import sqlite3                                            # noqa: E402

from bot import credits, db, state                        # noqa: E402
from bot.queue import LINK_LANE, ZIP_LANE, Job, Queue      # noqa: E402


async def _never_runs_here(_job):                          # pragma: no cover
    raise AssertionError("this job is only ever held, never run")


_chat_root = Path(tempfile.mkdtemp(prefix="terabot-chat-"))
(_chat_root / "work").mkdir(parents=True, exist_ok=True)
object.__setattr__(cfg, "work_dir", _chat_root / "work")

_conn = sqlite3.connect(_chat_root / "test.db", check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(db.SCHEMA)
_conn.commit()
db._conn = _conn

_USER = 6100000001
credits.ensure(_USER, "Operator", "operator")
credits.grant(_USER, 50.0, "test float")
state.clear_mode(_USER)

_plain = _chat_root / "two.zip"
with pyzipper.AESZipFile(_plain, "w") as _zf:
    _zf.writestr("a.mp4", b"a" * 64)
    _zf.writestr("b.mp4", b"b" * 64)

_locked2 = _chat_root / "locked.zip"
with pyzipper.AESZipFile(_locked2, "w", compression=pyzipper.ZIP_DEFLATED,
                         encryption=pyzipper.WZ_AES) as _zf:
    _zf.setpassword(b"hunter2")
    _zf.writestr("secret.mp4", b"D" * 2000)


class _Doc:
    def __init__(self, name, size):
        self.file_name, self.file_size = name, size
        self.mime_type = "application/zip"


class _ChatMsg:
    _ids = iter(range(500, 599))

    def __init__(self, text="", document=None):
        self.id = next(_ChatMsg._ids)
        self.text, self.document = text, document
        self.chat = types.SimpleNamespace(id=_USER)
        self.from_user = types.SimpleNamespace(id=_USER)
        self.replies: list["_ChatMsg"] = []
        self.deleted = False

    async def reply_text(self, text, reply_markup=None, **_kw):
        self.replies.append(_ChatMsg(text))
        return self.replies[-1]

    async def edit_text(self, text, reply_markup=None, **_kw):
        self.text = text
        return self

    async def delete(self, **_kw):
        self.deleted = True


class _ZipClient:
    """Hands over a real archive when asked to download one, and records its posts."""

    def __init__(self, source: Path):
        self.source = source
        self.said: list[str] = []

    async def download_media(self, _message, file_name=None, progress=None, **_kw):
        _shutil.copyfile(self.source, file_name)
        if progress:
            await progress(64, 64)
        return file_name

    async def send_message(self, _chat_id, text="", reply_markup=None, **_kw):
        self.said.append(text)
        return _ChatMsg(text)


class _CollectApp:
    def __init__(self):
        self.handlers = {}

    def _collect(self, *_a, **_kw):
        def keep(fn):
            self.handlers[fn.__name__] = fn
            return fn
        return keep

    on_callback_query = on_message = _collect

# PLACEHOLDER_CHAT_CLEARANCE_2

_zclient = _ZipClient(_plain)
_queue = Queue(_zclient, workers=1)          # never started: nothing runs the job
_app = _CollectApp()
zipfiles.register(_app, _queue)

_upload = _ChatMsg(document=_Doc("Holiday 2024 Goa.zip", _plain.stat().st_size))
asyncio.run(_app.handlers["got_document"](_zclient, _upload))
check("the archive was accepted and queued", _queue.depth(), 1)
check("and the upload is out of the chat", _upload.deleted, True)
check("the reply that stays behind is the panel, not the file",
      "video" in _upload.replies[-1].text.lower(), True)

# The other half of the `_accept_archive -> bool` decision: a refused upload is the
# only copy the user has, and deleting it would leave them with nothing to resend.
_second = _ChatMsg(document=_Doc("Another.zip", _plain.stat().st_size))
asyncio.run(_app.handlers["got_document"](_zclient, _second))
check("a second archive while one is running is turned away",
      "One archive at a time" in _second.replies[-1].text, True)
check("and it says a link is still free, so nobody waits for no reason",
      "Links are not blocked" in _second.replies[-1].text, True)
check("and that one stays put — it is the only copy they can send again",
      _second.deleted, False)

# The lane rule from the archive side. The user above has a ZIP in flight and is
# refused a second one; a *link* of theirs in flight must not refuse them an archive,
# because it is Terabox's CDN on one end and Telegram's on the other.
_link_job = Job(user_id=_USER + 1, chat_id=_USER + 1, kind="terabox",
                source="https://terabox.com/s/1x", cost=1.0,
                runner=_never_runs_here)
_queue._hold(_link_job)
check("a link in flight is not counted against the archive lane",
      _queue.busy(_USER + 1, ZIP_LANE), 0)
check("though it is counted against its own",
      _queue.busy(_USER + 1, LINK_LANE), 1)
check("and the total still sees both", _queue.busy(_USER + 1), 1)
_queue._release(_link_job)
check("releasing a link leaves no row behind", _queue.busy(_USER + 1), 0)

_wrong_kind = _ChatMsg(document=_Doc("notes.txt", 12))
_wrong_kind.document.mime_type = "text/plain"
asyncio.run(_app.handlers["got_document"](_zclient, _wrong_kind))
check("something that is not an archive is answered, not deleted",
      (("not an archive" in _wrong_kind.replies[-1].text), _wrong_kind.deleted),
      (True, False))

# --- the password ------------------------------------------------------------
_pw_dir = cfg.work_dir / f"zip-{_USER}-777"
_pw_dir.mkdir(parents=True, exist_ok=True)
_pw_zip = _pw_dir / "locked.zip"
_shutil.copyfile(_locked2, _pw_zip)

state.set_mode(_USER, "await_zip_password", zip_path=str(_pw_zip), message_id=1)
_wrong_pw = _ChatMsg(text="not-it")
asyncio.run(_app.handlers["maybe_password"](_zclient, _wrong_pw))
check("a wrong password is taken out of the chat too", _wrong_pw.deleted, True)
check("and is never quoted back in the reply",
      [t for t in _zclient.said if "not-it" in t], [])
check("the flow is still waiting for one",
      (state.get_mode(_USER) or ("",))[0], "await_zip_password")
check("nothing was queued for it", _queue.depth(), 1)

_typed = _ChatMsg(text="hunter2")
asyncio.run(_app.handlers["maybe_password"](_zclient, _typed))
check("the password goes out of the chat before the archive is opened",
      _typed.deleted, True)
check("it is not echoed anywhere either",
      [t for t in _zclient.said if "hunter2" in t], [])
check("and the archive was queued once it opened", _queue.depth(), 2)
check("the follow-up is a new message, since the one to reply to is gone",
      len(_typed.replies), 0)
check("the flow is closed", state.get_mode(_USER), None)

state.clear_mode(_USER)
object.__setattr__(cfg, "work_dir", _real_work_dir)
_conn.close()
_shutil.rmtree(_chat_root, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
