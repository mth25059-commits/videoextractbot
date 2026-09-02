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
check("txt is not", archive.Entry("readme.txt", 10).is_video, False)
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

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
