"""
The wizard: thirteen questions, one review page, one verification pass, then start.

Order is the operator's, not the code's — token, admin id, UPI, payee, the bank
alert, cookies, proxies, api_id/api_hash, the rate, the Terabox price, the other
prices, the database, the channels to force-join. Each answer is checked before the
next question is asked, because finding out on question twelve that the cookie from
question six was a guest session means answering twelve questions again.

Four rules hold everywhere in here:

  * **Re-runnable.** Every question shows what is already installed as its default,
    so `python -m setup` a month later is Enter thirteen times and one changed price.
  * **Nothing is written until the review is accepted.** Not `.env`, not the
    systemd unit. The one exception is `supabase.txt`, which has to exist before
    the question about it can be answered, and which contains no secret.
  * **A failing answer is re-asked, never silently kept.** A dead proxy, a
    signed-out cookie or a channel the bot is not admin in is offered for retype or
    dropped — writing one into `.env` to be discovered later is how a bot ends up
    "randomly" failing.
  * **Secrets are masked on screen, in the review, and in every recap.** The only
    place a token exists in full is `.env`, mode 600.

What the wizard deliberately does *not* ask: worker counts, the upload ceiling,
scratch directory, report time, fallback settings. `.env.example` explains all of
them and ships sane values, and a twenty-question wizard is one nobody finishes.
`⚙️ Prices` in the admin menu edits the seven prices later, from Telegram, without
a restart — the wizard only sets what they start at.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import ask, checks, envfile, service
from .ask import bad, hr, note, ok, say, step, title, warn

ROOT = checks.ROOT
TOTAL = 13

#: The tree being set up. Normally the same as `ROOT`, and different exactly when
#: `python -m setup --dir /srv/bot` is used — which is why `supabase.txt` cannot
#: just be written next to this file: it would land in one tree while the operator
#: was told to look in the other. `run()` sets it before the first question.
APP_DIR = ROOT

#: The seven prices the admin panel can also edit, as `.env` key -> attribute.
#: Kept in this shape because `bot/settings.py:EDITABLE` is the other half of the
#: same list, and the wizard is only setting what those start at.
PRICES = {
    "CREDITS_PER_RUPEE": "credits_per_rupee",
    "COST_TERABOX_PER_LINK": "cost_terabox_per_link",
    "COST_ZIP_UPTO_1GB": "cost_zip_upto_1gb",
    "COST_ZIP_UPTO_2GB": "cost_zip_upto_2gb",
    "COST_FAP_480": "cost_fap_480",
    "COST_FAP_720": "cost_fap_720",
    "COST_FAP_1080": "cost_fap_1080",
}


@dataclass
class Answers:
    """
    Everything the wizard collects, with the shipped defaults as its defaults.

    Prices are floats and not strings because half-credits are real (1080p costs
    1.5) and because the review has to print them the way a person wrote them —
    `%g`, so 2.0 reads as 2.
    """

    bot_token: str = ""
    admin_ids: str = ""
    api_id: str = ""
    api_hash: str = ""

    upi_id: str = ""
    upi_payee_name: str = ""
    imap_user: str = ""
    imap_app_password: str = ""
    imap_sender: str = "no-reply@famapp.in"
    fampay_checked: bool = False
    paysvc_secret: str = ""

    cookies: list[str] = field(default_factory=list)
    proxies: list[str] = field(default_factory=list)
    #: Channels a user must be in before the bot answers. Empty is the default and
    #: means the gate is off entirely — see `bot/joingate.py`.
    force_join: list[str] = field(default_factory=list)

    credits_per_rupee: float = 1.5
    cost_terabox_per_link: float = 0.5
    cost_zip_upto_1gb: float = 2.0
    cost_zip_upto_2gb: float = 4.0
    cost_fap_480: float = 1.0
    cost_fap_720: float = 1.5
    cost_fap_1080: float = 2.0

    database_url: str = ""

    #: Carried through a re-run rather than asked. `FAP_API` is the operator's own
    #: resolver and is blank on a fresh install, which switches the 🔥 route off
    #: politely instead of pointing it at somebody else's server.
    fap_api: str = ""

    @property
    def payments_on(self) -> bool:
        """Top-ups need somewhere for the money to go. Nothing else is required."""
        return bool(self.upi_id and self.upi_payee_name)

    @property
    def auto_confirm_on(self) -> bool:
        """Credits appearing on their own needs an inbox to watch as well."""
        return bool(self.payments_on and self.imap_user and self.imap_app_password)

    def env(self) -> dict[str, str]:
        """Every key this wizard owns. Anything absent here keeps `.env.example`'s value."""
        values = {
            "API_ID": self.api_id,
            "API_HASH": self.api_hash,
            "BOT_TOKEN": self.bot_token,
            "ADMIN_IDS": self.admin_ids,
            "UPI_ID": self.upi_id,
            "UPI_PAYEE_NAME": self.upi_payee_name,
            "IMAP_USER": self.imap_user,
            "IMAP_APP_PASSWORD": self.imap_app_password,
            "IMAP_SENDER": self.imap_sender,
            "PAYSVC_SECRET": self.paysvc_secret,
            "PROXIES": ",".join(self.proxies),
            "FORCE_JOIN": ",".join(self.force_join),
            "DATABASE_URL": self.database_url,
            "FAP_API": self.fap_api,
        }
        values.update(envfile.cookie_keys(self.cookies))
        for key, attr in PRICES.items():
            values[key] = f"{getattr(self, attr):g}"
        return values


def from_env(values: dict[str, str]) -> Answers:
    """
    What is already installed, as the starting point for a re-run.

    A price that cannot be read as a number falls back to the shipped default
    rather than raising: an `.env` somebody hand-edited to `COST_FAP_720=one and a
    half` should be a question with a sensible default, not a crash on line one.
    """
    a = Answers()
    a.bot_token = values.get("BOT_TOKEN", "").strip()
    a.admin_ids = values.get("ADMIN_IDS", "").strip()
    a.api_id = values.get("API_ID", "").strip()
    a.api_hash = values.get("API_HASH", "").strip()
    a.upi_id = values.get("UPI_ID", "").strip()
    a.upi_payee_name = values.get("UPI_PAYEE_NAME", "").strip()
    a.imap_user = values.get("IMAP_USER", "").strip()
    a.imap_app_password = values.get("IMAP_APP_PASSWORD", "").strip()
    a.imap_sender = values.get("IMAP_SENDER", "").strip() or a.imap_sender
    a.paysvc_secret = values.get("PAYSVC_SECRET", "").strip()
    a.database_url = values.get("DATABASE_URL", "").strip()
    a.fap_api = values.get("FAP_API", "").strip()
    a.cookies = envfile.cookies_from(values)
    a.proxies = [p.strip() for p in values.get("PROXIES", "").split(",") if p.strip()]
    a.force_join = [c.strip() for c in values.get("FORCE_JOIN", "").split(",")
                    if c.strip()]
    for key, attr in PRICES.items():
        raw = values.get(key, "").strip()
        if raw:
            try:
                setattr(a, attr, float(raw))
            except ValueError:
                pass
    # The old spelling of the same fact, upside down. Read only when the new key is
    # absent, which is the same precedence `bot/config.py` applies.
    if not values.get("CREDITS_PER_RUPEE", "").strip():
        old = values.get("RUPEES_PER_CREDIT", "").strip()
        try:
            if old and float(old) > 0:
                a.credits_per_rupee = 1.0 / float(old)
        except ValueError:
            pass
    return a


# --------------------------------------------------------------------------- #
# The twelve questions
# --------------------------------------------------------------------------- #

def q_token(a: Answers) -> None:
    a.bot_token = ask.ask(
        "Bot token", default=a.bot_token, validate=ask.bot_token, secret=True,
        hint="From @BotFather on Telegram: /mybots → your bot → API Token.\n"
             "Looks like 1234567890:AAF… — paste the whole thing.",
    )
    ok(f"bot id {ask.bot_id_of(a.bot_token)} — checked against Telegram at the end")


def q_admin(a: Answers) -> None:
    a.admin_ids = ask.ask(
        "Your Telegram id", default=a.admin_ids, validate=ask.telegram_ids,
        hint="The numeric one, not @username. Send /start to @userinfobot and it\n"
             "replies with yours. Comma-separate if more than one person is admin.",
    )
    ok(f"admin: {a.admin_ids}")


def q_upi(a: Answers) -> None:
    note("Where top-up money goes. This is printed on every QR, so it is not a\n"
         "secret — but a typo here silently pays a stranger, so scan your own QR\n"
         "once before telling anybody the bot takes payments.\n"
         "Leave it blank and top-ups are simply off; everything else works.")
    a.upi_id = ask.ask("UPI id", default=a.upi_id, validate=ask.upi_id,
                       allow_blank=True)
    if not a.upi_id:
        warn("top-ups will be off — the 💳 button will say so instead of drawing a QR")
    else:
        ok(a.upi_id)


def q_payee(a: Answers) -> None:
    if not a.upi_id:
        note("skipped — no UPI id, so there is no QR to put a name on")
        a.upi_payee_name = ""
        return
    a.upi_payee_name = ask.ask(
        "Name to show on the QR", default=a.upi_payee_name,
        hint="What the payer sees in their UPI app. Your own name is fine.",
    )
    ok(a.upi_payee_name)


def q_fampay(a: Answers) -> None:
    """
    The inbox that makes credits appear on their own, and the proof that it will.

    Two separate things, asked together because they are one feature: the Gmail
    login the poller uses, and one saved alert mail run through the gateway's own
    parser. The second is the only way to know settlement works **before a rupee
    moves** — and it is checked here rather than at the end because a mail that
    does not parse is a thing to fix now, with the file still in front of you.
    """
    if not a.payments_on:
        note("skipped — no UPI id, so nothing needs confirming")
        a.imap_user = a.imap_app_password = ""
        return
    note("Leave both blank and top-ups still work: every 'I have paid' goes to you\n"
         "for a one-tap confirm instead of settling by itself.")
    a.imap_user = ask.ask("Gmail address that gets the bank alerts",
                          default=a.imap_user, validate=ask.email, allow_blank=True)
    if not a.imap_user:
        warn("automatic confirmation off — you will confirm each payment by tapping")
        a.imap_app_password = ""
        return
    a.imap_app_password = ask.ask(
        "Gmail app password", default=a.imap_app_password,
        validate=ask.app_password, secret=True, allow_blank=True,
        hint="NOT your Google password. Make one at myaccount.google.com →\n"
             "Security → 2-Step Verification → App passwords. Spaces are fine.",
    )
    if not a.imap_app_password:
        warn("automatic confirmation off until an app password is set")
        return
    a.imap_sender = ask.ask("Address the alerts arrive from", default=a.imap_sender,
                            validate=ask.email)
    _fampay_proof(a)


def _fampay_proof(a: Answers) -> None:
    """
    One real alert mail, parsed by the code that will do it in production.

    The file has to be the mail *itself*, not its text: the DKIM signature lives in
    the headers, and a copy-paste of the body loses them. In Gmail that is
    ⋮ → Download message.
    """
    path = APP_DIR / "fampay.txt"
    say()
    note(f"Now one real 'you received ₹X' alert, to prove settlement will work.\n"
         f"In Gmail: open the alert → ⋮ (top right) → Download message.\n"
         f"Then put that whole file here:\n\n"
         f"    nano {path}\n\n"
         f"Paste it, Ctrl+O, Enter, Ctrl+X. Any amount will do — ₹1 to yourself is\n"
         f"enough. This file is gitignored and is only read, never sent anywhere.")
    while True:
        if not ask.ask_yes_no("saved it? (n skips this check)", True):
            warn("skipped — settlement is unproven until a real payment lands")
            a.fampay_checked = False
            return
        result = checks.fampay(path, a.imap_sender)
        if result.ok:
            ok(result.detail)
            a.fampay_checked = True
            return
        bad(result.detail)
        if result.hint:
            note(result.hint)


def q_cookies(a: Answers) -> None:
    """
    Terabox cookies, one at a time, each one checked before the next is offered.

    Checked one at a time on purpose: a cookie that turns out to be a guest session
    is worth knowing about while the browser it came from is still open. Ceiling of
    six, and they buy **failover, not speed** — Terabox shapes per CDN host, not per
    account, so a second cookie does not make any download faster. What it does is
    keep the bot working when the first account is rate-limited or logged out.
    """
    note("Sign in at terabox.com, then DevTools (F12) → Application → Cookies →\n"
         "copy the value of `ndus`. Either `ndus=Y1abc…` or just `Y1abc…` works.\n"
         "Blank is allowed: without a cookie the bot uses the third-party resolver,\n"
         "which works but discloses every link to it and is rate-limited.")
    kept: list[str] = []
    existing = list(a.cookies)
    while len(kept) < 6:
        n = len(kept) + 1
        default = existing[n - 1] if len(existing) >= n else ""
        label = "Terabox cookie" if n == 1 else f"Terabox cookie {n}"
        value = ask.ask(label, default=default, secret=True, allow_blank=True)
        if not value:
            break
        say("     checking…")
        result = checks.cookie(value, n)
        if result.ok:
            ok(result.detail)
            kept.append(value)
        else:
            bad(result.detail)
            if result.hint:
                note(result.hint)
            if ask.ask_yes_no("paste a different one?", True):
                existing = existing[:n - 1]        # do not offer the bad one again
                continue
        if len(kept) >= 6:
            note("that is the ceiling of six")
            break
        if not ask.ask_yes_no("add another cookie?", False):
            break
    a.cookies = kept
    if not kept:
        warn("no cookie — links will go through the third-party resolver only")
    else:
        ok(f"{len(kept)} cookie{'s' if len(kept) > 1 else ''} working")


def q_proxies(a: Answers) -> None:
    """
    Optional outbound proxies, each probed for whether it answers and from where.

    Measured, so it is worth being plain about: three links at once totalled
    3.11 MB/s direct and 3.92 MB/s with one address each — a 1.26x gain on
    *aggregate* throughput, and nothing at all for a single download. A slow proxy
    is worse than none; two of ten addresses measured carried 0.25-0.36 MB/s
    against a 1.38 MB/s direct baseline.
    """
    if not ask.ask_yes_no("Do you have proxies to use?", bool(a.proxies)):
        if a.proxies:
            warn(f"dropping the {len(a.proxies)} already configured")
        a.proxies = []
        return
    note("Your own proxies only. One per prompt, http://user:pass@host:port —\n"
         "a bare host:port is fine too. Blank line when done.")
    kept: list[str] = []
    existing = list(a.proxies)
    while True:
        n = len(kept) + 1
        default = existing[n - 1] if len(existing) >= n else ""
        value = ask.ask(f"Proxy {n}", default=default, validate=ask.proxy,
                        allow_blank=True)
        if not value:
            break
        say("     probing…")
        result = checks.proxy(value)
        if result.ok:
            ok(result.detail)
            kept.append(value)
            continue
        bad(result.detail)
        note("A dead entry costs time on every job that picks it, so it is better\n"
             "left out than kept. Order matters — the list is walked in order.")
        if not ask.ask_yes_no("retype this one?", True):
            existing = existing[:n - 1]
            if not ask.ask_yes_no("add another proxy?", False):
                break
    a.proxies = kept
    if kept:
        ok(f"{len(kept)} proxy address{'es' if len(kept) > 1 else ''} answering")
    else:
        note("connecting directly, which is a perfectly normal setup")


def q_api(a: Answers) -> None:
    """
    `api_id` and `api_hash` — the pair, and the one credential that cannot be
    rotated. There is one per phone number, and publishing it earns a permanent
    `API_ID_PUBLISHED_FLOOD`. It goes in `.env`, mode 600, and nowhere else.
    """
    note("From my.telegram.org → API development tools. These belong to YOUR\n"
         "Telegram account, not to the bot, and they are read as a pair.\n"
         "There is one per phone number and it cannot be replaced — keep it out of\n"
         "screenshots and out of any file you push.")
    a.api_id = ask.ask("API_ID", default=a.api_id, validate=ask.api_id)
    a.api_hash = ask.ask("API_HASH", default=a.api_hash, validate=ask.api_hash,
                         secret=True)
    ok("saved — the pair is tried together with the bot token at the end")


def q_rate(a: Answers) -> None:
    a.credits_per_rupee = ask.ask_number(
        "₹1 buys how many credits?", default=a.credits_per_rupee, maximum=100.0,
        hint="1.5 means ₹20 tops up 30 credits. This is the number the bot shows\n"
             "people, and you can change it later from Telegram without a restart.",
    )
    ok(f"₹1 = {a.credits_per_rupee:g} credits  →  ₹20 gives "
       f"{20 * a.credits_per_rupee:g}")


def q_terabox_price(a: Answers) -> None:
    a.cost_terabox_per_link = ask.ask_number(
        "Credits per Terabox video", default=a.cost_terabox_per_link, maximum=50.0,
        hint="Per VIDEO, not per link. A folder link holding ten videos is charged\n"
             "ten times this, and nothing is charged for videos that are not there.",
    )
    rupees = a.cost_terabox_per_link / a.credits_per_rupee
    ok(f"{a.cost_terabox_per_link:g} credits a video — about ₹{rupees:.2f} each")


def q_other_prices(a: Answers) -> None:
    note("Archives, by size. A ZIP/RAR/7z is unpacked and each video inside is\n"
         "sent back; this is the price of the archive, once.")
    a.cost_zip_upto_1gb = ask.ask_number("Archive up to 1 GB",
                                         default=a.cost_zip_upto_1gb, maximum=50.0)
    a.cost_zip_upto_2gb = ask.ask_number("Archive up to 2 GB",
                                         default=a.cost_zip_upto_2gb, maximum=50.0)
    say()
    note("The 🔥 Fap ladder — one price per quality. Only the rungs a video\n"
         "actually has are offered, so a video with no 1080p copy is never charged\n"
         "for one. Halves are fine.")
    a.cost_fap_480 = ask.ask_number("Fap 480p", default=a.cost_fap_480, maximum=50.0)
    a.cost_fap_720 = ask.ask_number("Fap 720p", default=a.cost_fap_720, maximum=50.0)
    a.cost_fap_1080 = ask.ask_number("Fap 1080p", default=a.cost_fap_1080,
                                     maximum=50.0)
    if not a.fap_api:
        note("FAP_API is not set, so the 🔥 route answers 'not switched on yet' and\n"
             "charges nobody. These prices wait until it is.")


def q_database(a: Answers) -> None:
    """
    Where the credits live — the question with the most consequence in the wizard.

    A local file is right for one VPS you intend to keep. Postgres is right when the
    box is rented: users, credits, the ledger and the job history then live
    somewhere the box ending cannot touch. Nothing else in the bot changes, and no
    price or feature depends on which is picked.
    """
    from bot import db

    note("Credits are keyed on the Telegram user id, so they already survive the\n"
         "bot token changing. This question is about the box itself ending.")
    choice = ask.ask_choice(
        "Where should credits live?",
        [("local", "This box — one file, data/bot.db.  Simple, and gone if the "
                   "box is."),
         ("supabase", "Supabase / any Postgres.  Credits outlive the VPS being "
                      "resized, moved or shut down.")],
        default="supabase" if a.database_url else "local",
    )
    if choice == "local":
        if a.database_url and not ask.ask_yes_no(
                "there is a Postgres configured — really move back to a local file?",
                False):
            ok("keeping the Postgres already configured")
            return
        a.database_url = ""
        ok(f"local file — {db.cfg.db_path}")
        note("Worth knowing: paysvc/data/orders.json and data/terabot.session are\n"
             "local either way. Losing the box mid-payment loses only orders that\n"
             "had not settled; your bank inbox is still the record for those.")
        return

    target = APP_DIR / "supabase.txt"
    target.write_text(db.supabase_sql(), encoding="utf-8")
    say()
    note(f"Written: {target}\n\n"
         f"  1.  Open your project on supabase.com\n"
         f"  2.  Left sidebar → SQL Editor → New query\n"
         f"  3.  Paste everything in that file, press Run\n"
         f"  4.  Come back here\n\n"
         f"Running it twice is safe — every line says IF NOT EXISTS, so no credit\n"
         f"anybody already has is touched.")
    while True:
        a.database_url = ask.ask(
            "Connection string", default=a.database_url, validate=ask.postgres_url,
            secret=True,
            hint="Supabase → Project Settings → Database → Connection string.\n"
                 "Use the POOLER one, port 6543 — not 5432. The direct port needs a\n"
                 "connection most VPS networks behind NAT will not hold open.",
        )
        say("     connecting…")
        result = checks.database(a.database_url)
        if result.ok:
            ok(result.detail)
            return
        bad(result.detail)
        if result.hint:
            note(result.hint)
        if not ask.ask_yes_no("try again?", True):
            if ask.ask_yes_no("use a local file instead, for now?", True):
                a.database_url = ""
                ok(f"local file — {db.cfg.db_path}")
            return


def q_force_join(a: Answers) -> None:
    """
    Channels a user has to be in before the bot answers them at all.

    Asked last because it is the easiest question to skip — blank means the bot
    answers everybody, which is what a fresh install wants — and because it is the
    only one that needs something done on Telegram first.

    Each channel is checked live, one at a time, and the check is stricter than it
    looks: the bot has to be an **admin** there, not a subscriber. Telegram refuses
    `get_chat_member` to a plain member, and the gate is deliberately fail-open so
    that mistake cannot lock anybody out — which means it would silently let
    everybody through while looking like it works. Here is the only place that can
    be caught.
    """
    note("Force-join: the bot answers nobody until they are in your channels.\n"
         "Blank turns it off, which is the default — the bot then answers everybody.\n\n"
         "First, on Telegram: add this bot as an ADMIN in each channel — channel →\n"
         "Manage → Administrators → Add Admin → your bot. Admin, not subscriber:\n"
         "Telegram will not tell a plain member who else is in a channel.")
    say()
    note("Public channel:   @myupdates      (pasting https://t.me/myupdates is fine)\n"
         "Private channel:  -1001234567890|https://t.me/+AbCdEf\n"
         "                  Both halves. The id is what membership is checked\n"
         "                  against; the link is what goes on the join button.\n"
         "                  Forward any post from it to @userinfobot for the id.")
    kept: list[str] = []
    existing = list(a.force_join)
    while True:
        n = len(kept) + 1
        default = existing[n - 1] if len(existing) >= n else ""
        label = "Channel to force-join" if n == 1 else f"Channel {n}"
        value = ask.ask(label, default=default, validate=ask.channel, allow_blank=True)
        if not value:
            break
        say("     asking Telegram…")
        result = checks.channel(value, a.api_id, a.api_hash, a.bot_token)
        if result.ok:
            ok(result.detail)
            kept.append(value)
        else:
            bad(result.detail)
            if result.hint:
                note(result.hint)
            note("Nothing is written yet. Fix it on Telegram in another window, then\n"
                 "type the same thing again — it is asked afresh every time.")
            existing = existing[:n - 1]        # do not offer the broken one again
            if ask.ask_yes_no("try this channel again?", True):
                continue
        if not ask.ask_yes_no("add another channel?", False):
            break
    a.force_join = kept
    if not kept:
        note("no force-join — the bot answers everybody, which is a normal setup")
        return
    ok(f"{len(kept)} channel{'s' if len(kept) > 1 else ''} to join before use")
    note("You are never gated yourself: everybody in ADMIN_IDS goes straight\n"
         "through, so you cannot lock yourself out of your own admin panel.")


# --------------------------------------------------------------------------- #
# What each answer looks like on the review page
# --------------------------------------------------------------------------- #

def _s_token(a: Answers) -> str:
    return ask.mask_token(a.bot_token) if a.bot_token else "(not set)"


def _s_upi(a: Answers) -> str:
    return a.upi_id or "(off — no top-ups)"


def _s_fampay(a: Answers) -> str:
    if not a.auto_confirm_on:
        return "(off — you confirm each payment by tapping)"
    proof = "alert mail verified" if a.fampay_checked else "alert mail NOT verified"
    return f"{a.imap_user} · {ask.mask(a.imap_app_password)} · {proof}"


def _s_cookies(a: Answers) -> str:
    if not a.cookies:
        return "(none — third-party resolver only)"
    return f"{len(a.cookies)} working · " + ", ".join(ask.mask(c, 3) for c in a.cookies)


def _s_proxies(a: Answers) -> str:
    from bot import egress
    if not a.proxies:
        return "(none — connecting directly)"
    return f"{len(a.proxies)} answering · " + ", ".join(
        egress.describe(p) for p in a.proxies)


def _s_api(a: Answers) -> str:
    return f"{a.api_id} · {ask.mask(a.api_hash)}"


def _s_zip_fap(a: Answers) -> str:
    return (f"ZIP {a.cost_zip_upto_1gb:g}/{a.cost_zip_upto_2gb:g} · "
            f"Fap {a.cost_fap_480:g}/{a.cost_fap_720:g}/{a.cost_fap_1080:g}"
            + ("" if a.fap_api else "  (Fap route off)"))


def _s_database(a: Answers) -> str:
    if not a.database_url:
        return "this box — data/bot.db"
    host = a.database_url.rsplit("@", 1)[-1].split("/")[0].split("?")[0]
    return f"postgres — {host}  (credits outlive this box)"


def _s_force_join(a: Answers) -> str:
    if not a.force_join:
        return "(off — the bot answers everybody)"
    # The id half only, for a private channel: the invite link is long, and what
    # matters on a review page is which channels, not how to join them.
    shown = [c.partition("|")[0] for c in a.force_join]
    return f"{len(shown)} to join first · " + ", ".join(shown)


@dataclass
class Question:
    label: str
    put: Callable[[Answers], None]
    show: Callable[[Answers], str]


QUESTIONS: list[Question] = [
    Question("Bot token",            q_token,         _s_token),
    Question("Admin id",             q_admin,         lambda a: a.admin_ids),
    Question("UPI id",               q_upi,           _s_upi),
    Question("Payee name",           q_payee,         lambda a: a.upi_payee_name or "—"),
    Question("Bank alert inbox",     q_fampay,        _s_fampay),
    Question("Terabox cookies",      q_cookies,       _s_cookies),
    Question("Proxies",              q_proxies,       _s_proxies),
    Question("api_id / api_hash",    q_api,           _s_api),
    Question("₹1 buys",              q_rate,          lambda a: f"{a.credits_per_rupee:g} credits"),
    Question("Terabox per video",    q_terabox_price, lambda a: f"{a.cost_terabox_per_link:g} credits"),
    Question("ZIP and Fap prices",   q_other_prices,  _s_zip_fap),
    Question("Where credits live",   q_database,      _s_database),
    Question("Join these channels",  q_force_join,    _s_force_join),
]


# --------------------------------------------------------------------------- #
# Review, verify, start
# --------------------------------------------------------------------------- #

def review(a: Answers) -> int | None:
    """
    Everything on one page. Returns the question to re-answer, or None to accept.

    Numbered so a change is one keystroke and not another twelve questions. The
    numbers match the order they were asked in, which is the order they are printed
    in, which is the order they are stored in — there is only one list.
    """
    say()
    hr()
    title("Everything, before anything is written")
    for n, q in enumerate(QUESTIONS, 1):
        dots = "." * max(2, 22 - len(q.label))
        say(f"  {ask.BOLD}{n:>2}{ask.OFF}  {q.label} {ask.DIM}{dots}{ask.OFF} "
            f"{q.show(a)}")
    say()
    if not a.payments_on:
        note("Top-ups are OFF. The 💳 button will say so; nothing else changes.")
    elif not a.auto_confirm_on:
        note("Top-ups are ON but confirm by hand — each 'I have paid' comes to you.")
    if not a.fap_api:
        note("The 🔥 Fap route is OFF (FAP_API is not set).")
    if a.force_join:
        note("Force-join is ON: nobody but you gets an answer until they are in "
             "those channels.")
    hr()
    say()

    def pick(raw: str) -> str:
        low = raw.strip().lower()
        if low in ("", "s", "start", "y", "yes", "go"):
            return "start"
        if low in ("q", "quit", "exit"):
            return "quit"
        if low.isdigit() and 1 <= int(low) <= len(QUESTIONS):
            return low
        raise ask.Invalid(f"type a number from 1 to {len(QUESTIONS)}, or "
                          f"Enter to start")

    answer = ask.ask("Number to change, or Enter to start", default="start",
                     validate=pick)
    if answer == "quit":
        raise ask.Cancelled
    return None if answer == "start" else int(answer) - 1


def verify(a: Answers) -> bool:
    """
    Run every check for real, and say which failures actually block a start.

    Three are fatal: no ffmpeg means every job fails, a refused credential means
    the bot never connects, and an unreachable database means it cannot even read a
    balance. Everything else is reported and does not block — `paysvc` is not
    running yet on a first install, and that is the normal answer, not a fault.
    """
    title("Checking it all for real")
    fatal = False

    def line(label: str, result: checks.Result, blocking: bool = False) -> None:
        nonlocal fatal
        (ok if result.ok else bad)(f"{label} — {result.detail}")
        if not result.ok:
            if result.hint:
                note(result.hint)
            if blocking:
                fatal = True

    line("ffmpeg", checks.ffmpeg(), blocking=True)
    say("     asking Telegram about the token…")
    line("Telegram", checks.telegram(a.api_id, a.api_hash, a.bot_token), blocking=True)
    line("database", checks.database(a.database_url), blocking=True)

    for n, value in enumerate(a.cookies, 1):
        line(f"Terabox cookie {n}", checks.cookie(value, n))
    for value in a.proxies:
        line("proxy", checks.proxy(value))
    # Not blocking, and deliberately: the gate fails open, so a channel the bot has
    # been thrown out of costs nobody access to the bot. It does silently stop
    # gating, though, which is why it is re-checked here rather than trusted from
    # the answer twenty minutes ago.
    for value in a.force_join:
        line(f"channel {value.partition('|')[0]}",
             checks.channel(value, a.api_id, a.api_hash, a.bot_token))
    if a.payments_on:
        line("node / paysvc deps", checks.node())
        line("paysvc", checks.paysvc("http://127.0.0.1:4400"))
        if a.auto_confirm_on:
            line("bank alert", checks.fampay(APP_DIR / "fampay.txt", a.imap_sender))

    say()
    if fatal:
        bad("something the bot cannot start without is not right yet.")
        note("Nothing has been started. Fix it by number on the page above, or\n"
             "quit, sort it out, and run `python -m setup` again — every answer is\n"
             "already saved and Enter keeps it.")
        return False
    ok("everything the bot needs is in place")
    return True


def commit(a: Answers, app_dir: Path, backed_up: list[Path]) -> Path:
    """
    Write `.env` — by filling in `.env.example`, so every comment survives.

    The old file is copied to the next `env-before-N.bak` the first time this runs,
    and not again on a second pass through the review: ten passes should not leave
    ten backups, and the one worth keeping is the `.env` that was there before the
    wizard started.
    """
    env_path = app_dir / ".env"
    if not backed_up:
        saved = envfile.backup(env_path)
        if saved:
            backed_up.append(saved)
            note(f"the previous .env is saved as {saved.name}")
    template = (app_dir / ".env.example").read_text(encoding="utf-8")
    envfile.write(env_path, envfile.render(template, a.env()))
    ok(f"{env_path} written, readable only by its owner (mode 600)")
    return env_path


BANNER = r"""
        _     _                   _                  _   _           _
 _   __(_) __| | ___  ___  ___ __| |___ ____ _ __ __| |_| |__  ___ _| |_
 \ \/ /| |/ _` |/ -_)/ _ \/ -_)\ \ /  _/ _` | _/ _|  _| '_ \/ _ \  _|  _|
  \  / |_|\__,_|\___|\___/\___|/_\_\\__\__,_|\__\__|\__|_.__/\___/\__|\__|
   \/
"""


def _greet(app_dir: Path, existing: bool) -> None:
    say(f"{ask.CYAN}{BANNER}{ask.OFF}")
    say(f"  Setting up in {ask.BOLD}{app_dir}{ask.OFF}")
    if existing:
        note("There is already a .env here, so every question below shows what is\n"
             "installed now. Press Enter to keep it and change only what you want.")
    else:
        note("Thirteen questions. Each answer is checked before the next one is asked,\n"
             "and nothing is written until you have seen it all on one page.\n"
             "Ctrl-C at any point leaves everything exactly as it is.")


def _finish(a: Answers, app_dir: Path, user: str, do_start: bool) -> int:
    """
    Enable it for the next reboot, start it now, and say what to type next.

    Ownership before start: a supervisor started as root leaves root-owned pidfiles
    and logs, and the operator's own next `run.sh` then cannot write them — which
    looks like a broken script rather than a wrong owner.
    """
    title("Starting")
    service.own(app_dir, user)
    installed, detail = service.install_unit(app_dir, user)
    (ok if installed else note)(detail)

    if not do_start:
        say()
        note("Not starting, as asked. When you want it up:\n"
             f"    bash {app_dir}/deploy/run.sh start")
        return 0

    started, output = service.run_sh(app_dir, "start", as_user=user)
    for row in output.splitlines():
        say(f"     {row}")
    if not started:
        bad("the supervisor reported a problem starting up")
        note(f"Look at the logs, they say why:\n    tail -n 40 {app_dir}/logs/bot.log")
        return 1

    say()
    _, status = service.run_sh(app_dir, "status", as_user=user)
    for row in status.splitlines():
        say(f"     {row}")

    say()
    hr()
    ok("Running. Open Telegram and send /start to your bot.")
    say()
    note(f"Watch it:      tail -f {app_dir}/logs/bot.log\n"
         f"Stop it:       bash {app_dir}/deploy/run.sh stop\n"
         f"Change prices: ⚙️ Prices in the bot's admin menu — no restart needed\n"
         f"Run this again: python -m setup   (Enter keeps every answer)")
    if not a.payments_on:
        say()
        note("Top-ups are off. Run this again and answer the UPI question to turn\n"
             "them on; nothing else has to change.")
    hr()
    return 0


def run(app_dir: Path, *, do_start: bool = True) -> int:
    """
    The whole wizard. Returns the process exit code.

    `checks.prepare()` comes first and has to: `bot.config` raises at import when
    `API_ID`/`API_HASH`/`BOT_TOKEN` are missing, and every question after this line
    imports something from `bot` to do its checking.
    """
    checks.prepare()
    global APP_DIR
    APP_DIR = app_dir
    env_path = app_dir / ".env"
    _greet(app_dir, env_path.exists())
    a = from_env(envfile.read(env_path))
    if not a.paysvc_secret:
        # The shared secret between bot and paysvc. Generated, never asked: it is a
        # random string whose only requirement is that both sides have the same one,
        # and asking a person to invent it is asking for "password123".
        a.paysvc_secret = secrets.token_urlsafe(32)

    for n, question in enumerate(QUESTIONS, 1):
        step(n, TOTAL, question.label)
        question.put(a)

    backed_up: list[Path] = []
    while True:
        choice = review(a)
        if choice is not None:
            step(choice + 1, TOTAL, QUESTIONS[choice].label)
            QUESTIONS[choice].put(a)
            continue
        commit(a, app_dir, backed_up)
        if verify(a):
            break

    return _finish(a, app_dir, service.default_user(), do_start)
