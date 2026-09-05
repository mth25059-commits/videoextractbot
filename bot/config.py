"""
Configuration — every tunable lives in .env, nothing is hardcoded here.

Import `cfg` and read attributes off it. Anything missing that the bot cannot
run without raises at import time with a message naming the variable, so a bad
deploy fails on the first second instead of on the first user.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """
    Minimal .env reader. No dependency, and it will not clobber real env vars.

    A ` #` after a value ends it. Without that, `IMAP_APP_PASSWORD=   # not yet`
    reads as the *string* "# not yet" — which looks set, so the bot would promise
    credits that confirm themselves while no inbox is actually being watched.
    A `#` with no space before it is kept, because passwords contain them.

    Quotes: exactly one matched surrounding pair comes off, which is what
    `paysvc/server.js:loadEnv` does. That matters beyond tidiness — `PAYSVC_SECRET`
    and `IMAP_APP_PASSWORD` are read by both this process and the Node one, and a
    secret the two disagree about is a callback the payment service rejects for a
    bad signature, with nothing in either log to say why.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        bare = value.strip()
        if len(bare) >= 2 and bare[0] == bare[-1] and bare[0] in ('"', "'"):
            value = bare[1:-1]
        else:
            # Before trimming: once the leading spaces are gone, `KEY=   # note`
            # looks like a value that simply begins with '#'.
            for i in range(len(value)):
                if value[i] == "#" and (i == 0 or value[i - 1].isspace()):
                    value = value[:i]
                    break
            value = value.strip()
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _req_int(name: str) -> int:
    """
    Required, and a number. `int(_req(name))` would do the job right up until
    someone pastes `API_ID=1234 5678` out of my.telegram.org, and then the bot
    dies on a bare ValueError that names neither the variable nor the file it
    came from. This is the likeliest mistake anyone makes on a fresh box.
    """
    raw = _req(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a whole number, got {raw!r} — check .env for a "
            f"stray space, quote or letter."
        ) from exc


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _num(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _ids(name: str) -> tuple[int, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    out = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"{name} contains a non-numeric id: {chunk!r}") from exc
    return tuple(out)


def _list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _tokens() -> tuple[str, ...]:
    """iteraplay `login_token`s, plural or singular, de-duplicated in order.

    TERABOX_FALLBACK_TOKEN came first and may still be set on a running box, so it
    is read too rather than silently ignored — one account is a perfectly good
    configuration, it is just no longer the only one.
    """
    found = list(_list("TERABOX_FALLBACK_TOKENS"))
    single = os.environ.get("TERABOX_FALLBACK_TOKEN", "").strip()
    if single:
        found.append(single)
    seen, out = set(), []
    for token in found:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return tuple(out)


def _cookies() -> tuple[str, ...]:
    """Every Terabox cookie, `TERABOX_COOKIE` first then `TERABOX_COOKIE_2..6`.

    **Numbered keys, not one comma-separated list.** A cookie value is a header
    fragment: it legitimately contains `=` and `;`, and a real `ndus` can contain a
    comma, so `_list` would shred it into halves that are each a broken cookie.
    Numbering also keeps the order explicit, which matters — element zero is the one
    rotation reaches for first.

    Spares buy **failover, not speed**: Terabox shapes per CDN host, not per
    account (measured 4 September 2026), so a second cookie makes nothing faster.
    What it does is keep the bot working when one account is rate-limited or
    logged out.
    """
    found = [os.environ.get("TERABOX_COOKIE", "").strip()]
    found += [os.environ.get(f"TERABOX_COOKIE_{n}", "").strip() for n in range(2, 7)]
    seen, out = set(), []
    for cookie in found:
        if cookie and cookie not in seen:
            seen.add(cookie)
            out.append(cookie)
    return tuple(out)


def _rate() -> tuple[float, float]:
    """
    The exchange rate, as both of the numbers people ask for.

    Returns `(credits_per_rupee, rupees_per_credit)` — the same fact twice, because
    the bot needs each one in a different place and deriving it at the point of use
    is how a display ends up reading **"1 credit = ₹0.6667"**. `credits_for()` in
    `payments.py` divides by `rupees_per_credit`, so that number has to exist; a
    human reads "₹1 = 1.5 credits", so that one has to exist too, exactly, rather
    than as `1/0.6667`.

    `CREDITS_PER_RUPEE` is the setting to reach for: it is the sentence the operator
    actually says. `RUPEES_PER_CREDIT` came first and is still honoured for a box
    that has it set, but if both are present the readable one wins — the fallback
    exists so an old `.env` keeps working, not so two settings can disagree.
    """
    per_rupee = _num("CREDITS_PER_RUPEE", 0)
    per_credit = _num("RUPEES_PER_CREDIT", 0)
    if per_rupee > 0:
        return per_rupee, 1 / per_rupee
    if per_credit > 0:
        return 1 / per_credit, per_credit
    return 1.5, 1 / 1.5


@dataclass(frozen=True)
class Config:
    # telegram
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: tuple[int, ...]
    log_chat_id: int | None
    #: Channels a user has to be in before the bot answers them. Blank switches the
    #: whole gate off, which is what a fresh clone wants — see `bot/joingate.py` for
    #: the accepted spellings.
    force_join: tuple[str, ...]

    # credits
    free_credits_on_join: float
    min_topup_rupees: int
    #: Two views of one rate — see `_rate()`. Never set one without the other.
    credits_per_rupee: float
    rupees_per_credit: float
    #: The price of **one video**, not one link: a folder link that holds ten of them
    #: is charged ten times this, settled once the folder has been read.
    cost_terabox_per_link: float
    cost_zip_upto_1gb: float
    cost_zip_upto_2gb: float
    max_links_per_batch: int

    # workers
    #
    # Two independent lanes, because the two services do not compete for the same
    # resource. Terabox jobs are limited by Terabox's own per-CDN-host shaping
    # (~1.5 MB/s a stream, measured 4 September 2026); ZIP jobs pull from Telegram,
    # unpack locally and upload, and never touch Terabox at all. Sharing one pool
    # made four zips block three link jobs for no reason.
    #
    # Raising these does not make any single download faster — nothing here is
    # CPU-bound and the box has ~13x headroom on its own pipe. It is so that six
    # people all *start* at once instead of watching a queue, which is the part
    # that actually feels slow.
    max_concurrent_jobs: int
    max_concurrent_zip_jobs: int
    max_upload_mb: int
    work_dir: Path

    # payments
    paysvc_url: str
    paysvc_secret: str
    paid_callback_port: int
    payment_window_minutes: int

    # a menu button that only says "not available yet" — no download, no charge
    show_soon_button: bool = False
    soon_button_label: str = "🔥  Fap"

    # terabox: the operator's own logged-in cookie. Anonymous listing works, but
    # only a signed-in session is given a download link, so without either this or
    # the fallback below the service says so instead of charging.
    #
    # `terabox_cookies` is the whole pool — this one first, then TERABOX_COOKIE_2..6.
    # It exists for **failover, not speed**: the throttle is on the CDN host serving
    # the bytes, not on the account asking, so a second cookie makes no download
    # faster. It takes over when the first is rate-limited (`errno 400210`) or has
    # been logged out, which one cookie cannot survive at all.
    terabox_cookie: str = ""
    terabox_cookies: tuple[str, ...] = field(default=())
    terabox_max_files_per_link: int = 10

    # The cookieless route, through the third-party resolver iteraplay.com. It
    # returns the original file at full quality, which is why it is on by default —
    # with it off and no cookie the bot cannot fetch anything at all. Two things to
    # know before leaving it on: every link handled this way is disclosed to
    # someone else's server, and a caller with no account of their own gets five
    # videos per six hours.
    #
    # That ceiling is the reason for the plural. Each `login_token` in
    # TERABOX_FALLBACK_TOKENS is an iteraplay account the operator registered
    # themselves, and each entry in PROXIES is a separate egress IP; the limit is
    # counted per account and per address, so N tokens and M proxies raise it
    # together. Both lists may be empty, which is simply the guest allowance.
    terabox_fallback: bool = True
    terabox_fallback_tokens: tuple[str, ...] = field(default=())

    # How many (token, proxy) pairs one link may be tried through before giving up.
    # Without a bound, ten links against ten proxies is a hundred requests to
    # someone else's API for a single batch.
    terabox_fallback_attempts: int = 4

    # misc
    proxies: tuple[str, ...] = field(default=())

    # When the nightly health report goes to the admins, as HH:MM in **UTC**. The
    # box runs on UTC and the operator reads IST, so the conversion is done here
    # once rather than in the head of whoever edits it later: IST is UTC+5:30, so
    # midnight IST is 18:30 UTC. Set it to an empty string to send no report.
    daily_report_utc: str = "18:30"

    # fap: a resolver of your own, which turns one video page into a list of HLS
    # renditions. Blank by default and deliberately so — there is no shared endpoint
    # to point at, and a default here would send every install's links to whichever
    # server happened to be written down. Empty turns the 🔥 key into a polite "not
    # switched on" that charges nobody, which is the right state until you run one.
    fap_api: str = ""

    # The priced ladder, one entry per rung the menu offers. Only the rungs a video
    # actually has are shown, so these are prices and not promises. Halves are the
    # reason `credits` is REAL in the database rather than INTEGER.
    cost_fap_480: float = 1.0
    cost_fap_720: float = 1.5
    cost_fap_1080: float = 2.0

    # Where the five tables live. Empty is the normal case: one SQLite file under
    # `data/`, on this box, gone when this box is. A Postgres URL here — in practice
    # Supabase's *pooler* string, port 6543, because a VPS behind NAT cannot hold a
    # direct 5432 session open — puts users, credits, the ledger and the job history
    # somewhere that outlives the rental. Nothing else changes: `bot/db.py` keeps the
    # same four helpers and no call site knows which one answered.
    #
    # What does *not* move with it, said plainly because it is money: `paysvc`'s own
    # `data/orders.json` is a local file, so a box lost mid-payment loses the orders
    # that had not settled yet — the FamApp mailbox is still the record of truth for
    # those. `data/terabot.session` stays local too, and should: it is an MTProto
    # credential tied to one bot token.
    database_url: str = ""

    @property
    def db_path(self) -> Path:
        return ROOT / "data" / "bot.db"

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    @property
    def payments_enabled(self) -> bool:
        return bool(self.paysvc_secret)


def load() -> Config:
    work_dir = Path(os.environ.get("WORK_DIR", "./downloads").strip() or "./downloads")
    if not work_dir.is_absolute():
        work_dir = ROOT / work_dir

    per_rupee, per_credit = _rate()

    cfg = Config(
        api_id=_req_int("API_ID"),
        api_hash=_req("API_HASH"),
        bot_token=_req("BOT_TOKEN"),
        admin_ids=_ids("ADMIN_IDS"),
        log_chat_id=(_ids("LOG_CHAT_ID") or (None,))[0],
        force_join=_list("FORCE_JOIN"),
        free_credits_on_join=_num("FREE_CREDITS_ON_JOIN", 2),
        min_topup_rupees=_int("MIN_TOPUP_RUPEES", 20),
        credits_per_rupee=per_rupee,
        rupees_per_credit=per_credit,
        cost_terabox_per_link=_num("COST_TERABOX_PER_LINK", 0.5),
        cost_zip_upto_1gb=_num("COST_ZIP_UPTO_1GB", 2),
        cost_zip_upto_2gb=_num("COST_ZIP_UPTO_2GB", 4),
        max_links_per_batch=_int("MAX_LINKS_PER_BATCH", 10),
        max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 6),
        max_concurrent_zip_jobs=_int("MAX_CONCURRENT_ZIP_JOBS", 4),
        max_upload_mb=_int("MAX_UPLOAD_MB", 2000),
        work_dir=work_dir,
        paysvc_url=os.environ.get("PAYSVC_URL", "http://127.0.0.1:4400").rstrip("/"),
        paysvc_secret=os.environ.get("PAYSVC_SECRET", "").strip(),
        paid_callback_port=_int("PAID_CALLBACK_PORT", 8081),
        payment_window_minutes=_int("PAYMENT_WINDOW_MINUTES", 10),
        show_soon_button=_bool("SHOW_SOON_BUTTON", False),
        soon_button_label=os.environ.get("SOON_BUTTON_LABEL", "🔥  Fap").strip() or "🔥  Fap",
        terabox_cookie=os.environ.get("TERABOX_COOKIE", "").strip(),
        terabox_cookies=_cookies(),
        terabox_max_files_per_link=_int("TERABOX_MAX_FILES_PER_LINK", 10),
        terabox_fallback=_bool("TERABOX_FALLBACK", True),
        terabox_fallback_tokens=_tokens(),
        terabox_fallback_attempts=_int("TERABOX_FALLBACK_ATTEMPTS", 4),
        proxies=_list("PROXIES"),
        daily_report_utc=os.environ.get("DAILY_REPORT_UTC", "18:30").strip(),
        fap_api=os.environ.get("FAP_API", "").strip(),
        cost_fap_480=_num("COST_FAP_480", 1),
        cost_fap_720=_num("COST_FAP_720", 1.5),
        cost_fap_1080=_num("COST_FAP_1080", 2),
        database_url=os.environ.get("DATABASE_URL", "").strip(),
    )

    if not cfg.admin_ids:
        raise ConfigError("ADMIN_IDS is empty — set at least your own user id.")

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg


cfg = load()
