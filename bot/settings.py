"""
Prices that can be changed while the bot is running.

`cfg` cannot do this. `Config` is a frozen dataclass built once in
`config.py`'s module body, so a price read from it is the price that was in
`.env` when the process started — and this bot is meant to have its rates edited
from two places: the setup wizard at install time, and the admin's ⚙️ Prices
screen at any time after. A restart between "change the price" and "the price
changed" is the difference between a control and a config file.

So the live truth is a row in the `settings` table and `.env` is the
*install-time default*:

    settings.get("cost_terabox_per_link")   -> the DB row if there is one,
                                               otherwise cfg.cost_terabox_per_link

Only the keys in `EDITABLE` work that way. Everything else in `cfg` — tokens,
cookies, worker counts, paths — stays exactly where it is: those are properties of
the box, not decisions the operator makes twice a week, and half of them cannot
be applied without a restart anyway.

Reads go through a tiny cache because the price is read on nearly every message
(the Terabox prompt alone reads two). The cache is invalidated by `set()`, which
is the only writer, so the value can never be stale in this process. It is also
per-process: one bot, one paysvc, and paysvc never reads a price.
"""

from __future__ import annotations

import logging
import threading

from . import db
from .config import cfg

log = logging.getLogger(__name__)

#: name -> (label for the admin screen, unit, sane ceiling)
#:
#: The ceiling is not a policy, it is a typo guard: `50` typed where `0.50` was
#: meant is a 100x price rise that the admin will only find out about from a user
#: complaint. Anything genuinely above these is an `.env` edit plus a restart,
#: which is a deliberate enough act to not need a guard.
EDITABLE: dict[str, tuple[str, str, float]] = {
    "credits_per_rupee":     ("₹1 buys",              "credits", 100.0),
    "cost_terabox_per_link": ("Terabox, per video",    "credits", 50.0),
    "cost_zip_upto_1gb":     ("Archive up to 1 GB",    "credits", 50.0),
    "cost_zip_upto_2gb":     ("Archive up to 2 GB",    "credits", 50.0),
    "cost_fap_480":          ("Fap 480p",              "credits", 50.0),
    "cost_fap_720":          ("Fap 720p",              "credits", 50.0),
    "cost_fap_1080":         ("Fap 1080p",             "credits", 50.0),
}

_lock = threading.RLock()
_cache: dict[str, float] = {}


class BadValue(ValueError):
    """A price the admin typed that cannot be stored. The message is user-facing."""


def _default(name: str) -> float:
    return float(getattr(cfg, name))


def get(name: str) -> float:
    """
    The live value of one editable price.

    Falls back to `cfg` — which is `.env`, or the dataclass default — whenever
    the row is absent, which is the normal state of a fresh install. A database
    that cannot be read falls back too rather than raising: a bot that refuses to
    quote a price is worse than one quoting the installed default.
    """
    if name not in EDITABLE:
        raise KeyError(f"{name} is not an editable setting")
    with _lock:
        if name in _cache:
            return _cache[name]
        value = _default(name)
        try:
            row = db.one("SELECT value FROM settings WHERE key = ?", (name,))
        except Exception:
            # Before db.connect(), or a database that has gone away. Neither is a
            # reason to fail the message being answered.
            log.debug("settings.get(%s) fell back to cfg", name, exc_info=True)
            return value
        if row is not None:
            try:
                value = float(row["value"])
            except (TypeError, ValueError):
                # Someone edited the table by hand. The default is still right.
                log.warning("settings row %s is not a number: %r", name, row["value"])
        _cache[name] = value
        return value


def set(name: str, value: float) -> float:
    """
    Store one price and make it live immediately. Returns what was stored.

    Raises `BadValue` with a sentence fit to show the admin, because every caller
    is a chat handler and the alternative is a stack trace in a Telegram message.
    """
    if name not in EDITABLE:
        raise KeyError(f"{name} is not an editable setting")
    label, unit, ceiling = EDITABLE[name]
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BadValue(f"“{value}” is not a number.") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise BadValue("That is not a number I can price anything at.")
    if number <= 0:
        raise BadValue(f"{label} has to be more than 0 — free is not a price.")
    if number > ceiling:
        raise BadValue(f"{label} caps at {ceiling:g} {unit}. Was a decimal point missed?")
    # Two decimals, because credits are REAL in the database for exactly the
    # half-credit prices this holds, and a rate of 1.4999999999999998 is a rate
    # that prints wrongly somewhere eventually.
    number = round(number, 2)
    with _lock:
        db.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (name, repr(number), db.now()),
        )
        _cache[name] = number
    log.info("price changed: %s = %g", name, number)
    return number


def reset(name: str) -> float:
    """Drop the row and go back to what `.env` said. Returns the value now in force."""
    if name not in EDITABLE:
        raise KeyError(f"{name} is not an editable setting")
    with _lock:
        db.execute("DELETE FROM settings WHERE key = ?", (name,))
        _cache.pop(name, None)
    return get(name)


def all_prices() -> dict[str, float]:
    """Every editable price as it stands now — for the admin card and the wizard."""
    return {name: get(name) for name in EDITABLE}


def is_default(name: str) -> bool:
    """True while nobody has changed this one from what `.env` installed."""
    with _lock:
        try:
            return db.one("SELECT 1 FROM settings WHERE key = ?", (name,)) is None
        except Exception:
            return True


def forget_cache() -> None:
    """
    Drop the read cache. For tests, and for anything that swaps the database
    underneath a running process — the row is the truth, this only holds a copy.
    """
    with _lock:
        _cache.clear()


#: `rupees_per_credit` is the same fact as `credits_per_rupee` upside down, and it
#: is derived rather than stored so the two can never disagree. `payments.py`
#: divides by it; a human reads the other one.
def rupees_per_credit() -> float:
    per_rupee = get("credits_per_rupee")
    return 1 / per_rupee if per_rupee > 0 else cfg.rupees_per_credit
