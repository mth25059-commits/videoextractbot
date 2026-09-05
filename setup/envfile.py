"""
Reading and writing `.env`, and never losing what was in it.

The one design decision here: **`.env` is written by filling in `.env.example`,
not by generating a fresh file from a dict.** `.env.example` is 260 lines of
comments explaining every key — why the pooler port is 6543, why cookies are
numbered instead of comma-separated, what a wrong `UPI_ID` costs. Generating
`.env` from scratch would throw all of that away, and the next person to open the
file with `nano` is the same person who needed it.

So `render()` walks the template, replaces the value on any `KEY=` line it has an
answer for, and leaves every comment, blank line and untouched key exactly where
it was. Keys with no line in the template are appended at the end under one
heading. The result reads like the example, filled in.

Two smaller things that are easy to get wrong and are handled here:

  * **Quoting.** Both readers of this file — `bot/config.py:_load_dotenv` and
    `paysvc/server.js:loadEnv` — end a value at a ` #`, so a password containing
    " #" would be silently cut in half. Both also honour double quotes and skip
    the comment cut inside them. `quote()` therefore wraps a value in `"` exactly
    when it needs it, and no more often than that.
  * **Backups.** A re-run that writes a bad token must not be the end of the good
    one. The old file is copied to the next free `env-before-N.bak` before
    anything is written, which is the convention the live box already used.

`.env` is written mode 600 through `os.open`, not written and then chmod'ed: the
gap between those two is a window where the bot token is world-readable, and on a
VPS with more than one login that window is the whole problem.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

#: Written at the top of the appended block, so a key that was not in the template
#: is obviously not an accident.
_APPENDED = "\n# --- ADDED BY THE SETUP WIZARD ----------------------------------------------\n"


def read(path: Path) -> dict[str, str]:
    """
    An existing `.env` as a dict, read exactly the way the bot reads it.

    Deliberately a copy of `config._load_dotenv`'s parsing rather than a call into
    it: that function writes into `os.environ`, and the wizard must be able to look
    at a file without adopting it. The two must agree, and the shared cases are
    covered in `tests/test_setup.py`.

    One matched pair of surrounding quotes is removed and nothing else — which is
    what `paysvc/server.js:loadEnv` has always done, and what `_load_dotenv` was
    corrected to do. Stripping *every* outer quote instead would cut a value ending
    in one in half, and the two readers would then disagree about a secret both of
    them read.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        bare = value.strip()
        if len(bare) >= 2 and bare[0] == bare[-1] and bare[0] in ('"', "'"):
            value = bare[1:-1]
        else:
            # Before trimming: once the leading spaces are gone, `KEY=   # note`
            # looks like a value that simply begins with '#'.
            for i, ch in enumerate(value):
                if ch == "#" and (i == 0 or value[i - 1].isspace()):
                    value = value[:i]
                    break
            value = value.strip()
        values[key.strip()] = value
    return values


def quote(value: str) -> str:
    """
    A value as it must appear after the `=`, quoted only when it has to be.

    Needed when the value contains a `#` after whitespace (an unquoted value ends
    there), when it has leading or trailing space (that is trimmed), or when it
    begins or ends with a quote (which would be read as the wrapper).

    Nothing is escaped, and nothing needs to be: all three readers take off one
    matched pair and stop, so `"` inside is already safe and even a value that is
    itself `"quoted"` comes back whole from `""quoted""`. A `\\"` would arrive at the
    bot as a literal backslash, because no reader here unescapes anything.
    """
    if value == "":
        return ""
    needs = (
        re.search(r"(^|\s)#", value) is not None
        or value != value.strip()
        or value[:1] in ('"', "'")
        or value[-1:] in ('"', "'")
    )
    return f'"{value}"' if needs else value


def render(template: str, values: dict[str, str]) -> str:
    """
    `.env.example` with the answers filled in, comments intact.

    A key present in `values` but absent from the template is appended rather than
    dropped — `TERABOX_COOKIE_2..6` are in the example, but a future key added by
    the wizard before somebody updates the example would otherwise vanish silently.
    """
    remaining = dict(values)
    out: list[str] = []
    for raw in template.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={quote(remaining.pop(key))}")
                continue
        out.append(raw)
    text = "\n".join(out).rstrip() + "\n"
    if remaining:
        text += _APPENDED
        for key, value in remaining.items():
            text += f"{key}={quote(value)}\n"
    return text


def backup(path: Path) -> Path | None:
    """
    Copy `.env` aside before it is replaced. Returns where it went, or None.

    Numbered rather than timestamped so the generations sort in the order they
    happened, and so `env-before-1.bak` is always the first `.env` that ever
    worked on this box — which is the one somebody wants back.
    """
    if not path.exists():
        return None
    n = 1
    while (dest := path.parent / f"env-before-{n}.bak").exists():
        n += 1
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    return dest


def write(path: Path, text: str) -> None:
    """
    Write `.env` so that it is never, for any instant, readable by anyone else.

    `O_CREAT` with mode 600 does that; an existing file keeps whatever mode it
    already had, so it is chmod'ed too. `O_TRUNC` rather than unlink-and-create,
    because unlinking a file somebody has open loses it.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:                                    # pragma: no cover
            pass                                           # Windows, and harmless


def cookie_keys(cookies: list[str]) -> dict[str, str]:
    """
    Cookies as `TERABOX_COOKIE`, `TERABOX_COOKIE_2` … `TERABOX_COOKIE_6`.

    **Numbered, never one comma-separated list.** An `ndus` value legitimately
    contains `=` and `;` and sometimes a `,`; splitting on commas would shred one
    cookie into two broken halves, and the failure would look like an expired
    account rather than a mangled string.

    Unused slots are written blank on purpose, so removing a cookie on a re-run
    actually removes it instead of leaving the old one behind.
    """
    keys = {"TERABOX_COOKIE": cookies[0] if cookies else ""}
    for n in range(2, 7):
        keys[f"TERABOX_COOKIE_{n}"] = cookies[n - 1] if len(cookies) >= n else ""
    return keys


def cookies_from(values: dict[str, str]) -> list[str]:
    """The other direction, so a re-run starts with what is already installed."""
    found = [values.get("TERABOX_COOKIE", "").strip()]
    found += [values.get(f"TERABOX_COOKIE_{n}", "").strip() for n in range(2, 7)]
    return [c for c in found if c]
