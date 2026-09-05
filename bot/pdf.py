"""
A PDF, written by hand.

One export button is not worth a dependency. `reportlab` was the obvious choice
and it is 4 MB of wheel plus a `pip install` on a box that has to be restarted to
pick it up; what this file needs is text on paginated A4, which is about eighty
lines of PDF once you accept two limits:

* **Monospace only.** Courier is one of the fourteen fonts every reader has
  built in, so nothing is embedded, and its glyphs are all exactly 0.6 em wide —
  which is what makes `_wrap` able to break a 180-character Terabox URL at the
  right column without carrying a width table for every character.
* **Latin-1 only.** The base-14 fonts are byte-encoded; a Devanagari filename has
  no code point in WinAnsi at all, and embedding a Unicode font would be the 4 MB
  back again. Anything unrepresentable becomes `?` — visible, rather than a reader
  that refuses to open the file.

Neither limit costs anything for what this is used for: a dated table of links,
sizes and outcomes that an operator prints or forwards.
"""

from __future__ import annotations

from pathlib import Path

#: A4 in PostScript points, and the frame we draw inside it.
PAGE_W, PAGE_H = 595.0, 842.0
MARGIN = 40.0
TOP = PAGE_H - MARGIN
BOTTOM = MARGIN + 22.0          # the footer lives in the gap below this

BODY = 8.5
LEAD = 11.5                     # baseline-to-baseline for BODY
CHAR_EM = 0.6                   # every Courier glyph, exactly


def columns(size: float = BODY) -> int:
    """How many characters fit across the frame at `size`."""
    return max(8, int((PAGE_W - 2 * MARGIN) / (size * CHAR_EM)))


def _latin1(text: object) -> bytes:
    return str(text).encode("cp1252", "replace")


def _escaped(text: object) -> bytes:
    """A PDF string literal: backslash, both parens, and nothing else, must be quoted."""
    out = _latin1(text)
    for char in (b"\\", b"(", b")"):
        out = out.replace(char, b"\\" + char)
    return out.replace(b"\r", b" ").replace(b"\n", b" ").replace(b"\t", b"  ")


def wrap(text: str, width: int, indent: str = "") -> list[str]:
    """
    Break `text` to `width` characters, continuation lines prefixed with `indent`.

    Words are kept whole where they fit and cut where they cannot fit on a line of
    their own, because the long strings here are URLs and filenames: a 180-character
    Terabox URL has no space to break at, and refusing to cut it would run it off
    the right edge of the page instead.
    """
    room = max(4, width)
    lines: list[str] = []
    prefix = ""
    parts: list[str] = []

    def flush() -> None:
        nonlocal parts, prefix
        if parts:
            lines.append(prefix + " ".join(parts))
            parts = []
            prefix = indent

    for word in str(text).split():
        while len(prefix) + len(word) > room:
            flush()
            cut = max(1, room - len(prefix))
            lines.append(prefix + word[:cut])
            word = word[cut:]
            prefix = indent
        used = len(prefix) + sum(len(part) + 1 for part in parts) + len(word)
        if parts and used > room:
            flush()
        parts.append(word)
    flush()
    return lines or [""]


class Writer:
    """
    Text down the page, a new one started whenever the last line runs out of room.

    Deliberately not a layout engine: `line`, `rule` and `gap` in the order they
    are called, which is all a report is. Page numbers are stamped in `save`
    rather than as each page is finished, because "of 4" is not known until then.
    """

    def __init__(self, title: str = "", subtitle: str = "") -> None:
        self._pages: list[list[bytes]] = []
        self._ops: list[bytes] = []
        self._y = TOP
        self.title = title
        if title:
            self.line(title, size=14.0, bold=True, lead=19.0)
            if subtitle:
                self.line(subtitle, size=8.0, lead=13.0)
            self.rule()

    # --- page plumbing ------------------------------------------------------

    def _new_page(self) -> None:
        if self._ops:
            self._pages.append(self._ops)
        self._ops = []
        self._y = TOP

    def _room_for(self, height: float) -> None:
        if self._y - height < BOTTOM:
            self._new_page()

    # --- drawing -----------------------------------------------------------

    def line(self, text: str = "", *, size: float = BODY, bold: bool = False,
             lead: float | None = None, x: float = MARGIN) -> None:
        step = lead if lead is not None else (LEAD if size <= BODY else size * 1.35)
        self._room_for(step)
        self._y -= step
        if str(text).strip():
            font = b"/F2" if bold else b"/F1"
            self._ops.append(b"BT %s %.1f Tf %.1f %.1f Td (%s) Tj ET"
                             % (font, size, x, self._y, _escaped(text)))

    def wrapped(self, text: str, *, indent: str = "", size: float = BODY,
                bold: bool = False) -> None:
        for piece in wrap(text, columns(size), indent):
            self.line(piece, size=size, bold=bold)

    def rule(self, gap: float = 6.0) -> None:
        self._room_for(gap + 2)
        self._y -= gap
        self._ops.append(b"0.75 w 0.6 G %.1f %.1f m %.1f %.1f l S"
                         % (MARGIN, self._y, PAGE_W - MARGIN, self._y))

    def gap(self, height: float = LEAD) -> None:
        self._room_for(height)
        self._y -= height

    def page_break(self) -> None:
        self._new_page()

    # --- output ------------------------------------------------------------

    def _footer(self, page: int, total: int) -> bytes:
        note = f"{self.title} — page {page} of {total}" if self.title else \
               f"page {page} of {total}"
        return (b"BT /F1 7.0 Tf %.1f %.1f Td (%s) Tj ET"
                % (MARGIN, MARGIN - 4, _escaped(note)))

    def save(self, path: Path) -> Path:
        pages = list(self._pages)
        if self._ops or not pages:
            pages.append(self._ops)

        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)           # object numbers are 1-based

        catalog, pages_obj = add(b""), add(b"")     # patched once the kids exist
        courier = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier "
                      b"/Encoding /WinAnsiEncoding >>")
        courier_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold "
                           b"/Encoding /WinAnsiEncoding >>")
        resources = (b"<< /Font << /F1 %d 0 R /F2 %d 0 R >> >>"
                     % (courier, courier_bold))

        kids: list[int] = []
        for index, ops in enumerate(pages, start=1):
            stream = b"\n".join(ops + [self._footer(index, len(pages))])
            content = add(b"<< /Length %d >>\nstream\n%s\nendstream"
                          % (len(stream), stream))
            kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.0f %.0f] "
                            b"/Resources %s /Contents %d 0 R >>"
                            % (pages_obj, PAGE_W, PAGE_H, resources, content)))

        objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
        objects[pages_obj - 1] = (b"<< /Type /Pages /Kids [%s] /Count %d >>"
                                  % (b" ".join(b"%d 0 R" % k for k in kids), len(kids)))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (number, body)

        start_xref = len(out)
        out += b"xref\n0 %d\n" % (len(objects) + 1)
        out += b"0000000000 65535 f \n"        # every entry is exactly 20 bytes
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objects) + 1, catalog, start_xref))

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(out))
        return path
