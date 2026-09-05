"""
The parts of the wizard that talk to a person.

Three things live here and nothing else does: printing, one prompt, and the
validators that decide whether an answer may be accepted.

The prompt has one rule worth stating out loud — **an existing value is shown as
the default, so pressing Enter keeps it.** That is what makes `python -m setup`
re-runnable: somebody who only wants to change the price walks through twelve
questions pressing Enter and changes one. It is also why a secret's default is
shown masked rather than not shown at all: `[8840…li4]` tells the operator which
token is already in there without putting it on a screen that may be shared.

Validators raise `Invalid` with a sentence the person answering can act on, and
are called in a loop until they stop raising. They *return* the cleaned value, so
"was it valid" and "what should be stored" are one answer and cannot drift —
`bot_token` accepts `123 : ABC` and returns `123:ABC`, and the caller never sees
the spaces.

Nothing in here reaches the network. Every real check — is this cookie signed in,
does this token exist, does this proxy answer — is in `checks.py`, because a shape
being right and a credential working are different questions and the wizard asks
them at different times.
"""

from __future__ import annotations

import getpass
import re
import sys
from typing import Callable, Iterable


class Cancelled(Exception):
    """
    Ctrl-C or end of input.

    Raised instead of letting `KeyboardInterrupt` escape, so `__main__` can print
    one line and leave — a traceback here reads as a crash in the installer, and
    the installer is the first thing anybody sees of this project.
    """


class Invalid(ValueError):
    """A validator's complaint, phrased for the person who typed the answer."""


_TTY = sys.stdout.isatty()

# ₹ and ✓ are in almost every line this wizard prints, and a Windows console
# defaults to cp1252, where the rupee sign raises UnicodeEncodeError mid-sentence.
# The bot's own home is Linux and UTF-8, so this only matters for a dry run on a
# laptop — but a wizard that crashes while explaining prices is a bad first
# impression. `errors="replace"` is the backstop for a console that still cannot
# draw them: a missing glyph is not a reason to stop.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")       # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):                    # pragma: no cover
        pass


def _c(code: str) -> str:
    """A colour, or nothing at all when this is a pipe or a log file."""
    return code if _TTY else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
OFF = _c("\033[0m")
CYAN = _c("\033[1;36m")
GREEN = _c("\033[1;32m")
RED = _c("\033[1;31m")
YELLOW = _c("\033[1;33m")


def say(text: str = "") -> None:
    print(text)


def title(text: str) -> None:
    print(f"\n{CYAN}{text}{OFF}")
    print(f"{DIM}{'─' * min(len(text), 72)}{OFF}")


def step(n: int, total: int, text: str) -> None:
    print(f"\n{CYAN}[{n}/{total}]{OFF}  {BOLD}{text}{OFF}")


def ok(text: str) -> None:
    print(f"  {GREEN}✓{OFF}  {text}")


def bad(text: str) -> None:
    print(f"  {RED}✖{OFF}  {text}")


def warn(text: str) -> None:
    print(f"  {YELLOW}!{OFF}  {text}")


def note(text: str) -> None:
    """Explanation, not result. Dim and indented so it reads as background."""
    for line in text.splitlines():
        print(f"     {DIM}{line}{OFF}")


def hr() -> None:
    print(f"{DIM}{'─' * 72}{OFF}")


def mask(value: str, keep: int = 4) -> str:
    """
    A secret in a form that proves *which* secret it is without giving it away.

    Short values are hidden whole: four characters with the first and last shown
    is not masking, it is a hint. Anything long enough keeps its ends, which is
    what lets an operator tell "the cookie I pasted" from "the old one".
    """
    if not value:
        return "(not set)"
    if len(value) <= keep * 2:
        return "•" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def mask_token(token: str) -> str:
    """
    A bot token with its id left readable.

    The digits before the colon are the bot's own user id — public, printed by
    Telegram in every message the bot sends, and the one part that identifies
    which bot this is. Only the half after the colon is a credential.
    """
    if ":" not in token:
        return mask(token)
    head, _, tail = token.partition(":")
    return f"{head}:{mask(tail, 3)}"


def ask(
    label: str,
    *,
    default: str = "",
    validate: Callable[[str], str] | None = None,
    secret: bool = False,
    allow_blank: bool = False,
    hint: str = "",
) -> str:
    """
    One question, asked until the answer is usable.

    `allow_blank` is how a feature gets switched off: an empty UPI id means
    top-ups are simply not offered, which is a real answer and not a refusal to
    answer. Without it the only way past the payment block would be to invent a
    UPI id, and a QR that pays a stranger is worse than no QR.

    Pressing Enter takes the default and then puts it through `validate` like any
    typed answer, which matters twice. `ask_choice` shows `1` and means `local`, so
    an unvalidated default would return the digit and land the operator in the wrong
    branch of the biggest question in the wizard. And a value already in `.env` that
    no longer passes — a proxy whose port was hand-edited to 99999 — gets asked
    about now rather than written back out unread.
    """
    if hint:
        note(hint)
    shown = (mask(default) if secret else default) if default else ""
    suffix = f" {DIM}[{shown}]{OFF}" if shown else ""
    if allow_blank and not shown:
        suffix = f" {DIM}[leave blank to skip]{OFF}"
    reader = getpass.getpass if secret else input
    while True:
        try:
            raw = reader(f"  {BOLD}{label}{OFF}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        if not raw and default:
            raw = default                       # Enter keeps what is already there
        if not raw:
            if allow_blank:
                return ""
            bad("this one cannot be left blank")
            continue
        if validate is None:
            return raw
        try:
            return validate(raw)
        except Invalid as exc:
            bad(str(exc))
            if raw == default:
                # The complaint is about something already installed, and repeating
                # the question with the same default would loop forever on Enter.
                default = ""
                suffix = f" {DIM}[leave blank to skip]{OFF}" if allow_blank else ""


def ask_yes_no(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"  {BOLD}{label}{OFF} {DIM}[{hint}]{OFF}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise Cancelled from None
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        bad("answer y or n")


def ask_number(label: str, *, default: float, minimum: float = 0.0,
               maximum: float = 1000.0, hint: str = "") -> float:
    """
    A price or a rate.

    `maximum` is a typo guard and not a policy — the same one `settings.EDITABLE`
    carries, for the same reason: `50` typed where `0.5` was meant is a hundredfold
    price rise nobody notices until a user complains.
    """
    def check(raw: str) -> str:
        value = number(raw)
        if not minimum < float(value) <= maximum:
            raise Invalid(f"has to be more than {minimum:g} and at most {maximum:g}")
        return value

    return float(ask(label, default=f"{default:g}", validate=check, hint=hint))


def ask_choice(label: str, options: Iterable[tuple[str, str]], default: str = "") -> str:
    """
    Pick one of a few named things. Returns the key, not the number typed.

    Used for the database question, where "1" and "2" mean nothing a month later
    but `local` and `supabase` still do.
    """
    items = list(options)
    for n, (_, text) in enumerate(items, 1):
        print(f"     {BOLD}{n}{OFF}  {text}")
    keys = [key for key, _ in items]
    shown = str(keys.index(default) + 1) if default in keys else ""

    def check(raw: str) -> str:
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return keys[int(raw) - 1]
        if raw.lower() in keys:
            return raw.lower()
        raise Invalid(f"type a number from 1 to {len(items)}")

    return ask(label, default=shown, validate=check)


# --------------------------------------------------------------------------- #
# Validators. Shape only — see the module docstring.
# --------------------------------------------------------------------------- #

def number(raw: str) -> str:
    """A number, with the rupee sign and stray commas people type forgiven."""
    cleaned = raw.replace("₹", "").replace(",", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        raise Invalid(f"'{raw}' is not a number") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise Invalid("that is not a number I can store")
    return f"{value:g}"


def bot_token(raw: str) -> str:
    """
    `<digits>:<35 or so characters>`, which is what @BotFather hands over.

    Spaces are stripped rather than refused because the token is pasted, and a
    paste out of a chat bubble very often brings one with it.
    """
    match = re.fullmatch(r"(\d{5,})\s*:\s*([A-Za-z0-9_-]{30,})", raw.strip())
    if not match:
        raise Invalid(
            "that does not look like a bot token. It comes from @BotFather and "
            "looks like 1234567890:AAF… — digits, a colon, then about 35 more "
            "characters."
        )
    return f"{match.group(1)}:{match.group(2)}"


def bot_id_of(token: str) -> str:
    """The bot's own numeric id, which is the half of the token that is public."""
    return token.partition(":")[0]


def telegram_ids(raw: str) -> str:
    """
    One or more numeric Telegram ids, comma separated on the way out.

    A username is the commonest wrong answer here and gets its own sentence,
    because "invalid" does not tell anybody that @BotFather is the wrong place to
    look and @userinfobot is the right one.
    """
    parts = [p for p in re.split(r"[,\s]+", raw.strip()) if p]
    if any(p.startswith("@") or not p.lstrip("-").isdigit() for p in parts):
        raise Invalid(
            "this needs the numeric id, not a @username. Send /start to "
            "@userinfobot on Telegram and it replies with yours."
        )
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ",".join(seen)


def api_id(raw: str) -> str:
    if not raw.strip().isdigit():
        raise Invalid("API_ID is only digits — it is the number next to App api_id "
                      "on my.telegram.org")
    return raw.strip()


def api_hash(raw: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw.strip()):
        raise Invalid("API_HASH is 32 hex characters, from the same page as API_ID")
    return raw.strip().lower()


def upi_id(raw: str) -> str:
    """
    `name@handle`. Shape is all that can be checked here, and it is not enough —
    which is why the wizard prints the warning about scanning your own QR once.
    """
    cleaned = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-_]{2,256}@[A-Za-z][A-Za-z0-9.\-]{1,63}", cleaned):
        raise Invalid("a UPI id looks like yourname@fam or 9876543210@ybl")
    return cleaned


def email(raw: str) -> str:
    cleaned = raw.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", cleaned):
        raise Invalid("that is not an email address")
    return cleaned


def app_password(raw: str) -> str:
    """
    A Gmail app password: sixteen letters, shown in groups of four.

    The spaces are stripped for us because Google displays them and people paste
    what they see. A 16-character answer that is not a Google app password will
    pass this and fail at the IMAP login instead, which is the right place for it.
    """
    cleaned = raw.replace(" ", "").strip()
    if len(cleaned) < 12:
        raise Invalid(
            "that is too short for an app password. It is not your Google "
            "password — make one at myaccount.google.com → Security → 2-Step "
            "Verification → App passwords."
        )
    return cleaned


def proxy(raw: str) -> str:
    """
    `http://user:pass@host:port`, with a missing scheme filled in.

    A bare `host:port` is accepted and normalised, because that is how proxy
    sellers print them and `egress.describe` already handles both.
    """
    cleaned = raw.strip()
    if "://" not in cleaned:
        cleaned = f"http://{cleaned}"
    match = re.fullmatch(r"(https?|socks5h?)://(?:([^@/\s]+)@)?([^@/:\s]+):(\d{1,5})",
                         cleaned)
    if not match or not 1 <= int(match.group(4)) <= 65535:
        raise Invalid("expected host:port, or http://user:pass@host:port")
    return cleaned


def postgres_url(raw: str) -> str:
    """
    A Postgres connection string, with Supabase's own trap named.

    Port 5432 is Supabase's *direct* connection: it needs a long-lived socket that
    most VPS networks behind NAT will not hold open, and it is not what this bot is
    tuned for (prepared statements are turned off for the pooler). Refusing is
    wrong — somebody may be running their own Postgres on 5432 — so this warns and
    accepts.
    """
    cleaned = raw.strip()
    if not re.match(r"^postgres(ql)?://", cleaned):
        raise Invalid("it starts with postgresql:// — copy the whole line from "
                      "Supabase → Project Settings → Database → Connection string")
    if "@" not in cleaned or ":" not in cleaned.rsplit("@", 1)[-1]:
        raise Invalid("that string has no host in it — copy it again, whole")
    if ":5432/" in cleaned or cleaned.endswith(":5432"):
        warn("that is port 5432, the direct connection. Use the POOLER string on "
             "port 6543 — the direct one often will not connect from a VPS.")
    return cleaned


def hhmm(raw: str) -> str:
    cleaned = raw.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", cleaned):
        raise Invalid("write it as HH:MM in 24-hour UTC, like 18:30")
    return cleaned
