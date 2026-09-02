"""
Phase 2 checks — the logic that can be verified without ffmpeg, network or Telegram.

Run: python tests/test_phase2.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os
os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")

from bot import media                      # noqa: E402
from bot.providers.base import Resolved, Stream, ResolveError, Provider  # noqa: E402

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def raises(name, exc, fn):
    global passed, failed
    try:
        fn()
    except exc:
        passed += 1
        print(f"  ok   {name}")
        return
    except Exception as other:
        failed += 1
        print(f"  FAIL {name} — raised {type(other).__name__}, wanted {exc.__name__}")
        return
    failed += 1
    print(f"  FAIL {name} — nothing raised")


# --- HLS playlist parsing ----------------------------------------------------
print("\nhls_duration")
MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXTINF:10.000,
seg0.ts
#EXTINF:10.000,
seg1.ts
#EXTINF:4.560,
seg2.ts
#EXT-X-ENDLIST
"""
MASTER_PLAYLIST = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1920x1080
1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720
720/index.m3u8
"""
check("sums EXTINF", media.hls_duration(MEDIA_PLAYLIST), 24.56)
check("master returns 0", media.hls_duration(MASTER_PLAYLIST), 0.0)
check("empty input", media.hls_duration(""), 0.0)
check("None input", media.hls_duration(None), 0.0)

# --- ffmpeg -progress parsing ------------------------------------------------
print("\nparse_progress / progress_seconds")
BLOCK = """frame=1234
fps=250.5
out_time_us=41000000
speed=8.35x
progress=continue"""
fields = media.parse_progress(BLOCK)
check("key count", len(fields), 5)
check("speed field", fields.get("speed"), "8.35x")
check("out_time_us -> 41s", media.progress_seconds(fields), 41.0)
check("out_time_ms fallback",
      media.progress_seconds({"out_time_ms": "2500000"}), 2.5)
check("HH:MM:SS fallback",
      media.progress_seconds({"out_time": "01:02:03.500"}), 3723.5)
check("negative clamped to 0",
      media.progress_seconds({"out_time_us": "-5000000"}), 0.0)
check("nothing usable", media.progress_seconds({"frame": "1"}), None)
check("garbage timestamp", media.progress_seconds({"out_time": "N/A:N/A"}), None)

# --- video sniffing ----------------------------------------------------------
print("\nlooks_like_video")
tmp = ROOT / "tests" / "_tmp"
tmp.mkdir(parents=True, exist_ok=True)

by_ext = tmp / "clip.mkv"
by_ext.write_bytes(b"not really a video")
check("known extension wins", media.looks_like_video(by_ext), True)

mp4_magic = tmp / "noext"
mp4_magic.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8)
check("ftyp at offset 4", media.looks_like_video(mp4_magic), True)

mkv_magic = tmp / "mislabelled.dat"
mkv_magic.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 12)
check("matroska magic", media.looks_like_video(mkv_magic), True)

junk = tmp / "readme.txt"
junk.write_bytes(b"just some notes about the videos")
check("plain text rejected", media.looks_like_video(junk), False)
check("missing file rejected", media.looks_like_video(tmp / "nope.bin"), False)

# --- the provider contract ---------------------------------------------------
print("\nResolved / Stream")
res = Resolved(
    title="  My  Video: Part/2  ",
    streams=[
        Stream(url="u480", label="480p", height=480, bandwidth=800_000),
        Stream(url="u1080", label="1080p", height=1080, bandwidth=4_000_000),
        Stream(url="u720", label="720p", height=720, bandwidth=1_500_000),
    ],
)
check("sorted highest first", res.labels, ["1080p", "720p", "480p"])
check("best is 1080p", res.best.url, "u1080")
check("by_label is case-insensitive", res.by_label("720P").url, "u720")
check("by_label miss", res.by_label("4k"), None)
check("safe_title cleaned", res.safe_title, "My Video Part2")
check("bandwidth breaks ties",
      Resolved(title="t", streams=[
          Stream(url="a", label="a", height=720, bandwidth=1),
          Stream(url="b", label="b", height=720, bandwidth=9),
      ]).best.url, "b")
raises("empty streams rejected", ResolveError,
       lambda: Resolved(title="t", streams=[]))
check("safe_title falls back", Resolved(title='<>:"/\\|?*', streams=[
    Stream(url="a", label="a")]).safe_title, "video")
check("safe_title capped at 120", len(Resolved(title="x" * 400, streams=[
    Stream(url="a", label="a")]).safe_title), 120)


class Fake(Provider):
    name, label = "fake", "Fake"

    def matches(self, text):
        return "fake.test/" in (text or "")

    async def resolve(self, url):
        raise NotImplementedError


print("\nextract_links")
fake = Fake()
blob = """check these out:
https://fake.test/a1 and (https://fake.test/a2), plus
https://other.test/nope and https://fake.test/a1 again."""
check("owned links only, de-duplicated",
      fake.extract_links(blob), ["https://fake.test/a1", "https://fake.test/a2"])
check("trailing punctuation stripped",
      fake.extract_links("see https://fake.test/x."), ["https://fake.test/x"])
check("no links", fake.extract_links("nothing here"), [])
check("empty input", fake.extract_links(""), [])

for leftover in tmp.iterdir():
    leftover.unlink()
tmp.rmdir()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
