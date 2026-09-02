"""
The provider contract — the plug-in slot.

Everything downstream of this file (queue, remux, upload, credits, progress bar)
is source-agnostic. A provider's only job is: take whatever the user pasted, and
come back with a `Resolved` describing what can be fetched and at which
qualities. It never downloads, never touches Telegram, never touches credits.

Adding a source means writing one subclass and registering it in REGISTRY. Nothing
else in the bot changes.
"""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field


class ResolveError(Exception):
    """Raised when a link cannot be turned into something fetchable.

    The message is shown to the user verbatim, so write it for them: say what
    went wrong and what they can do, not which exception fired.
    """


@dataclass(frozen=True)
class Stream:
    """One fetchable rendition of one file."""

    url: str
    label: str                      # what the user sees: "1080p", "original"
    kind: str = "hls"               # "hls" (m3u8 playlist) or "file" (direct bytes)
    height: int | None = None
    width: int | None = None
    bandwidth: int | None = None    # bits/sec, from the HLS manifest when known
    size_bytes: int | None = None   # only ever known for kind="file"
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def sort_key(self) -> tuple[int, int]:
        """Highest resolution first, bandwidth breaking ties."""
        return (self.height or 0, self.bandwidth or 0)


@dataclass
class Resolved:
    """What a provider found behind one link."""

    title: str
    streams: list[Stream]
    duration_seconds: float | None = None
    thumbnail_url: str | None = None
    source_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.streams:
            raise ResolveError("Nothing playable was found behind that link.")
        self.streams.sort(key=lambda s: s.sort_key, reverse=True)

    @property
    def best(self) -> Stream:
        return self.streams[0]

    def by_label(self, label: str) -> Stream | None:
        for stream in self.streams:
            if stream.label.lower() == label.lower():
                return stream
        return None

    @property
    def labels(self) -> list[str]:
        return [s.label for s in self.streams]

    @property
    def safe_title(self) -> str:
        """A filename that survives every filesystem and Telegram's own limits."""
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", self.title or "video").strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
        return (cleaned or "video")[:120]


class Provider(abc.ABC):
    """One source of videos."""

    name: str = "provider"
    label: str = "Provider"
    #: How many links this source accepts in one batch message.
    max_batch: int = 10

    @abc.abstractmethod
    def matches(self, text: str) -> bool:
        """True if `text` looks like a link this provider owns."""

    @abc.abstractmethod
    async def resolve(self, url: str) -> Resolved:
        """Turn one link into a `Resolved`. Raise `ResolveError` with a user-facing message."""

    def extract_links(self, text: str) -> list[str]:
        """Pull every link this provider owns out of a pasted blob, de-duplicated."""
        found, seen = [], set()
        for candidate in re.findall(r"https?://\S+", text or ""):
            candidate = candidate.rstrip(").,;\"'>]")
            if candidate in seen or not self.matches(candidate):
                continue
            seen.add(candidate)
            found.append(candidate)
        return found


REGISTRY: dict[str, Provider] = {}


def register(provider: Provider) -> Provider:
    REGISTRY[provider.name] = provider
    return provider


def get(name: str) -> Provider:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ResolveError(f"That service is not available right now.") from None


def find_for(url: str) -> Provider | None:
    for provider in REGISTRY.values():
        if provider.matches(url):
            return provider
    return None
