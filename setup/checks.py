"""
Every check the wizard runs, and the reason none of them needs a subprocess.

`bot/config.py` builds a frozen `cfg` in its module body and raises `ConfigError`
if `API_ID`, `API_HASH` or `BOT_TOKEN` is missing — so importing anything from
`bot` before the questions are answered fails, and importing it after does not
help either, because `cfg` is built once and every module holds its own reference
to it. The obvious workaround is to shell out: write `.env`, run a subprocess,
read what it says. That would mean writing `.env` before the answers are
verified, which is exactly the thing this wizard exists not to do.

It is not needed, because **every verification in this bot already takes its input
as an argument rather than reading `cfg`**:

    Terabox.check_cookie(cookie, index)     -> CookieHealth   (never raises)
    egress.probe(proxy, timeout)            -> (alive, detail)
    media.tools_available()                 -> (ok, why)
    db.missing_tables(conn)                 -> [table, …]      (takes a connection)
    pyrogram.Client(api_id=…, api_hash=…)   -> constructed here, from the answers

So `prepare()` puts *placeholders* in `os.environ` — enough for `bot.config` to
import and not one field more — and each check is handed the real answer. Nothing
reads a real credential out of `cfg`, and `.env` stays untouched until the review
is accepted.

Everything returns a `Result` and nothing raises: a wizard that dies on a dead
proxy is worse than one that says the proxy is dead. The one exception is
`Cancelled`, which is a person pressing Ctrl-C and has to travel.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Only what `config.load()` refuses to start without. Deliberately obvious
#: rubbish: if one of these ever reaches a network call it should fail loudly and
#: unmistakably, not look like a plausible credential.
PLACEHOLDERS = {
    "API_ID": "1",
    "API_HASH": "0" * 32,
    "BOT_TOKEN": "0:setup-wizard-placeholder-never-sent-anywhere",
    "ADMIN_IDS": "0",
}


def prepare() -> None:
    """
    Make `import bot.…` possible before any question has been answered.

    Set, not `setdefault`: an existing `.env` with a blank `API_ID=` would
    otherwise be read first by `config._load_dotenv` and raise the very error this
    is here to avoid. A real value already in the environment is overwritten too,
    and that is fine — nothing in the wizard reads these back.
    """
    for key, value in PLACEHOLDERS.items():
        os.environ[key] = value
    os.environ.setdefault("WORK_DIR", str(ROOT / "downloads"))


@dataclass
class Result:
    """
    One check's answer. `detail` is printed next to it, so it is a sentence.

    `hint` is the longer "here is what to do about it", printed only on failure —
    a check that fails is the one moment somebody needs three lines of help, and
    the one that passes needs none.
    """

    ok: bool
    detail: str = ""
    hint: str = ""


def _run(coro):
    """
    One coroutine, one event loop, torn down after.

    A fresh loop per check rather than one for the wizard: the checks are seconds
    apart with a person typing in between, and a loop left open across an `input()`
    is a loop nothing is servicing.
    """
    return asyncio.run(coro)


def _lend_an_event_loop() -> None:
    """
    Make sure the thread has a current event loop before pyrogram is imported.

    `pyrogram/sync.py` calls `asyncio.get_event_loop()` in its module body, and on
    3.12 that raises when no loop is current. `asyncio.run()` clears the current
    loop on its way out, and by the time the Telegram check runs, `_run` has been
    through here several times for the cookies and the proxies — so importing
    pyrogram lazily, which is what makes the other eleven questions fast, is exactly
    what breaks it. The bot never sees this because `main.py` imports pyrogram at the
    top, before any of this.

    Verified by the traceback it fixes: `RuntimeError: There is no current event
    loop in thread 'MainThread'` at the api_id/api_hash verification, on every
    install.
    """
    try:
        asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


# --------------------------------------------------------------------------- #
# The box itself
# --------------------------------------------------------------------------- #

def ffmpeg() -> Result:
    """
    ffmpeg and ffprobe, asked of the same function that gates the bot's own boot.

    Not a `shutil.which` here: `media.tools_available()` is what `main.py` refuses
    to start on, so anything it accepts and the wizard rejects — or the other way
    round — is a wizard that lies.
    """
    from bot import media

    ok, why = media.tools_available()
    return Result(ok, "ffmpeg and ffprobe are here" if ok else why,
                  hint="apt install ffmpeg" if not ok else "")


def node() -> Result:
    """
    Node, and `paysvc`'s dependencies. Only relevant when payments are on.

    `node_modules` matters as much as the binary: `paysvc` is the process that
    matches an incoming ₹ amount to an order, and without its packages it exits
    immediately, which reads as "payments do not work" rather than "npm was never
    run".
    """
    where = shutil.which("node")
    if not where:
        return Result(False, "node is not installed",
                      hint="apt install nodejs npm  (needed only for top-ups)")
    try:
        version = subprocess.run([where, "--version"], capture_output=True, text=True,
                                 timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):            # pragma: no cover
        version = "?"
    if not (ROOT / "paysvc" / "node_modules").is_dir():
        return Result(False, f"node {version} is here, but paysvc has no node_modules",
                      hint="cd paysvc && npm ci --omit=dev")
    return Result(True, f"node {version}, paysvc dependencies installed")


# --------------------------------------------------------------------------- #
# Credentials and services
# --------------------------------------------------------------------------- #

def telegram(api_id: str, api_hash: str, bot_token: str) -> Result:
    """
    The three Telegram credentials, exercised together, which is the only way.

    Pyrogram needs all three at once — the handshake uses none of them, then
    `initConnection` sends `api_id`/`api_hash`, then the token is checked — so
    "which one is wrong" is a question only the error can answer, and each of the
    four it can give is translated here.

    `in_memory=True` on purpose: a session file written by the wizard would be a
    second MTProto credential on disk, bound to a token that may still be edited
    on the review screen. The bot makes its own on first start.
    """
    try:
        _lend_an_event_loop()
        from pyrogram import Client, errors
    except ModuleNotFoundError as exc:                        # pragma: no cover
        return Result(False, f"pyrogram is not installed ({exc.name})",
                      hint="pip install -r requirements.txt")

    async def go() -> Result:
        app = Client(name="setup-check", api_id=int(api_id), api_hash=api_hash,
                     bot_token=bot_token, in_memory=True)
        try:
            await app.start()
            me = await app.get_me()
            return Result(True, f"@{me.username} — {me.first_name} (id {me.id})")
        except (errors.AccessTokenInvalid, errors.AccessTokenExpired):
            return Result(False, "Telegram refused the bot token",
                          hint="Get a fresh one from @BotFather: /mybots → your bot "
                               "→ API Token → Revoke current token.")
        except (errors.ApiIdInvalid, errors.ApiIdPublishedFlood):
            return Result(False, "API_ID and API_HASH do not match an app",
                          hint="They are read as a pair. Check both on "
                               "my.telegram.org → API development tools.")
        except KeyError:
            # The handshake carries no credential at all, so this is never a wrong
            # key — it is a network that will not carry MTProto. Hosts that filter
            # egress by TLS name (some PaaS sandboxes, corporate networks) reset
            # these connections while leaving api.telegram.org reachable, so a
            # working `curl …/getMe` proves nothing.
            return Result(False, "Telegram accepted the connection and sent nothing back",
                          hint="This is a blocked network, not a bad credential — the "
                               "first packet carries no api_id or token. This host "
                               "cannot reach Telegram's data centres; it needs "
                               "unfiltered outbound TCP.")
        except Exception as exc:
            return Result(False, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                await app.stop()
            except Exception:                                 # pragma: no cover
                pass

    try:
        return _run(go())
    except Exception as exc:                                  # pragma: no cover
        return Result(False, f"{type(exc).__name__}: {exc}")


def cookie(value: str, index: int = 1) -> Result:
    """
    One Terabox cookie: is it signed in, and which host is it signed in *to*?

    The host is the part worth printing. A Terabox session is bound to one of
    several `HOME_HOSTS`, and answers errno -6 on every other one — so "not signed
    in" and "asked the wrong host" look identical from outside. `check_cookie`
    walks them, and reporting which one answered turns a guess into a fact.
    """
    from bot.providers.terabox import Terabox

    try:
        health = _run(Terabox().check_cookie(value, index))
    except Exception as exc:                                  # pragma: no cover
        return Result(False, f"{type(exc).__name__}: {exc}")

    if not health.ok:
        why = health.detail or f"errno {health.errno}"
        return Result(False, f"not signed in — {why}",
                      hint="Terabox hands guests an `ndus` too, so having one proves "
                           "nothing. Open terabox.com, log in, then DevTools → "
                           "Application → Cookies and copy `ndus` again. Check it "
                           "with terabox.com/api/quota?checkfree=1 — storage "
                           "numbers means signed in, `user not login` means guest.")
    parts = [f"signed in at {health.home}"]
    if health.total_bytes:
        parts.append(f"{health.full_percent:.0f}% of its storage used")
    if not health.tokens:
        parts.append("no bdstoken yet (downloads may still work)")
    return Result(True, ", ".join(parts))


def proxy(url: str) -> Result:
    """
    One proxy, and the address traffic actually leaves from.

    The egress IP is the useful half: a proxy that answers but sends everything
    from this box's own address is not doing the thing it was bought for. HTTP 402
    is named separately by `egress.probe` because it means the whole plan has
    lapsed — every route in the list is dead at once, which is a list to replace
    rather than debug.
    """
    from bot import egress

    try:
        alive, detail = _run(egress.probe(url))
    except Exception as exc:                                  # pragma: no cover
        return Result(False, f"{type(exc).__name__}: {exc}")
    # `describe` and `probe`'s detail are both password-free by construction; the
    # raw URL is never printed by this function.
    return Result(alive, f"{egress.describe(url)} — {detail}")


def channel(entry: str, api_id: str, api_hash: str, bot_token: str) -> Result:
    """
    One force-join channel: does it resolve, and is the bot an administrator in it?

    Both halves matter and only the second is easy to get wrong. `get_chat_member`
    is how the gate decides whether to let somebody in, and a bot that is only a
    *member* of a channel cannot call it — Telegram answers `ChatAdminRequired`.
    The gate is deliberately fail-open, so that mistake does not lock anybody out;
    it silently lets everybody through instead, which is the worse failure because
    nothing looks broken. Catching it here is the whole reason this check exists.

    Parsed by `joingate._one`, not by a second parser living in the wizard: the ref
    checked here has to be the exact ref the bot will use, or this proves nothing.
    """
    from bot import joingate

    parsed = joingate._one(entry)
    if parsed is None:
        return Result(False, f"{entry} cannot be used as a force-join channel",
                      hint="A private channel needs its id and its invite link, "
                           "written as -1001234567890|https://t.me/+AbCdEf.")

    try:
        _lend_an_event_loop()
        from pyrogram import Client
    except ModuleNotFoundError as exc:                        # pragma: no cover
        return Result(False, f"pyrogram is not installed ({exc.name})",
                      hint="pip install -r requirements.txt")

    async def go() -> Result:
        app = Client(name="setup-channel", api_id=int(api_id), api_hash=api_hash,
                     bot_token=bot_token, in_memory=True)
        try:
            await app.start()
        except Exception as exc:
            return Result(False, f"could not reach Telegram ({type(exc).__name__})",
                          hint="The bot token or api pair is the problem, not the "
                               "channel — the review page will say which.")
        try:
            chat = await app.get_chat(parsed.ref)
            me = await app.get_chat_member(parsed.ref, "me")
        except Exception as exc:
            name = type(exc).__name__
            if name in ("UsernameNotOccupied", "UsernameInvalid"):
                return Result(False, f"there is no channel called {parsed.ref}",
                              hint="Check the spelling. It is the name in the "
                                   "channel's t.me link, not its title.")
            if name in ("UserNotParticipant", "UserNotParticipantError"):
                return Result(False, f"{parsed.ref} exists, but the bot is not in it",
                              hint="Channel → Manage → Administrators → Add Admin → "
                                   "your bot. Admin, not subscriber: a plain member "
                                   "cannot be asked who else is in the channel.")
            if name in ("ChatAdminRequired", "ChatAdminRequiredError"):
                return Result(
                    False, f"{parsed.ref} refused the question — the bot is not an admin",
                    hint="Channel → Manage → Administrators → Add Admin → your bot. "
                         "No permissions need ticking; being an admin is enough.")
            if name in ("ChannelPrivate", "PeerIdInvalid", "ChannelInvalid"):
                return Result(
                    False, f"{parsed.ref} is not visible to this bot",
                    hint="Add the bot to the channel first: channel → Manage → "
                         "Administrators → Add Admin → your bot. A bot cannot even "
                         "see a private channel it is not in.")
            return Result(False, f"{name}: {exc}")
        finally:
            try:
                await app.stop()
            except Exception:                                 # pragma: no cover
                pass

        title = getattr(chat, "title", None) or str(parsed.ref)
        members = getattr(chat, "members_count", None)
        if not joingate.joined(getattr(me, "status", None)):
            return Result(False, f"{title} — the bot is not in this channel",
                          hint="Channel → Manage → Administrators → Add Admin → "
                               "your bot.")
        if joingate.status_name(getattr(me, "status", None)) not in joingate.ADMIN:
            return Result(
                False, f"{title} — the bot is a member but NOT an admin",
                hint="It has to be an admin to see who else is in the channel; as "
                     "a plain member Telegram refuses the question. Channel → "
                     "Manage → Administrators → Add Admin → your bot. No "
                     "permissions need to be granted, admin is enough.")
        crowd = f", {members:,} members" if members else ""
        return Result(True, f"{title} — bot is an admin{crowd}")

    try:
        return _run(go())
    except Exception as exc:                                  # pragma: no cover
        return Result(False, f"{type(exc).__name__}: {exc}")


def paysvc(url: str) -> Result:
    """
    The Node payment service, asked whether it is up.

    Not fatal when it is down: `install.sh` installs it but the wizard runs before
    anything is started, so "not running yet" is the normal answer and the review
    says so. It becomes interesting on a re-run, when it should be up.
    """
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=5) as res:
            body = json.loads(res.read().decode("utf-8", "replace") or "{}")
    except urllib.error.URLError as exc:
        return Result(False, f"not answering on {url} ({exc.reason})",
                      hint="Normal before the first start. `deploy/run.sh start` "
                           "brings it up with the bot.")
    except Exception as exc:
        return Result(False, f"{type(exc).__name__}: {exc}")
    state = body.get("status") or body.get("ok") or "answered"
    return Result(True, f"{url} — {state}")


def database(url: str) -> Result:
    """
    Where the credits will live, connected to for real and checked table by table.

    Calls `db`'s own two connect helpers rather than opening a connection here, so
    the wizard proves the exact path the bot will take — WAL and the pragmas for
    SQLite, `prepare_threshold = None` and the pooler-safe settings for Postgres.
    A schema the wizard accepts and the bot then rejects would be the worst
    possible outcome of this question.
    """
    from bot import db

    url = url.strip()
    try:
        if url:
            conn = db._connect_postgres(url)                  # raises with real advice
        else:
            db.cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = db._connect_sqlite()
    except RuntimeError as exc:
        return Result(False, str(exc).splitlines()[0], hint=str(exc))
    except Exception as exc:
        return Result(False, f"{type(exc).__name__}: {exc}",
                      hint="Use the POOLER string on port 6543. Check the password "
                           "has not been pasted with a stray space, and that the "
                           "project is not paused.")
    try:
        absent = db.missing_tables(conn)
        if absent:
            return Result(False, "connected, but missing: " + ", ".join(absent),
                          hint="Paste supabase.txt into Supabase → SQL Editor → New "
                               "query and press Run, then check again.")
        where = f"postgres — {url.rsplit('@', 1)[-1].split('/')[0]}" if url \
            else f"local file — {db.cfg.db_path}"
        return Result(True, f"{where}, all {len(db.TABLES)} tables present")
    finally:
        try:
            conn.close()
        except Exception:                                     # pragma: no cover
            pass


def fampay(path: Path, sender: str) -> Result:
    """
    A saved bank alert, run through the gateway's own parser and sender check.

    This is the one check that proves settlement will work **before a rupee
    moves**, and it is why the wizard asks for a file at all. Two things have to
    hold, and they are independent:

      * `parseMessage` has to find `direction: in` and an amount. That is the mail
        the poller matches against an order's unique paise value.
      * `senderIsAuthentic` has to find `dkim=pass` for the sender's own domain in
        an `Authentication-Results` header. A From line alone is attacker-controlled
        text; the DKIM signature is what makes it mean anything.

    Run through the real package in `paysvc/node_modules`, not a Python
    re-implementation, because a second parser that disagrees with the first is a
    payment credited twice or never.
    """
    if not path.exists():
        return Result(False, f"{path.name} is not there yet")
    if not shutil.which("node"):
        return Result(False, "node is not installed, so this cannot be checked",
                      hint="apt install nodejs npm")
    if not (ROOT / "paysvc" / "node_modules").is_dir():
        return Result(False, "paysvc has no node_modules, so this cannot be checked",
                      hint="cd paysvc && npm ci --omit=dev")

    shim = (
        "import { readFileSync } from 'node:fs';\n"
        "const [file, sender] = process.argv.slice(1);\n"
        "const g = await import('upi-fampay-gateway');\n"
        "const m = g.parseMessage(readFileSync(file, 'binary'));\n"
        "const a = g.senderIsAuthentic(m.headers, sender);\n"
        "console.log('__FAMPAY__' + JSON.stringify({ subject: m.subject,\n"
        "  direction: m.credit.direction, paise: m.credit.amountPaise,\n"
        "  bankRef: m.credit.bankRef, dkim: a.ok, problems: a.problems }));\n"
    )
    try:
        done = subprocess.run(
            ["node", "--input-type=module", "-e", shim, str(path), sender],
            cwd=str(ROOT / "paysvc"), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:      # pragma: no cover
        return Result(False, f"could not run node ({exc})")

    line = next((l for l in done.stdout.splitlines() if l.startswith("__FAMPAY__")), "")
    if not line:
        tail = (done.stderr or done.stdout).strip().splitlines()
        return Result(False, "the parser could not read that file",
                      hint="\n".join(tail[-4:]) or "no output from node")
    found = json.loads(line[len("__FAMPAY__"):])

    problems = []
    if found["direction"] != "in":
        problems.append(f"this reads as money going {found['direction'] or 'nowhere'}, "
                        "not coming in")
    if not found["paise"]:
        problems.append("no amount could be found in it")
    if not found["dkim"]:
        problems.extend(found["problems"] or ["the sender could not be verified"])
    if problems:
        return Result(
            False, "; ".join(problems),
            hint="Save the mail itself, not a copy of its text: in Gmail open the "
                 "alert → ⋮ → Download message, and paste that whole file. The "
                 "headers are what carry the signature, and a copy-paste loses them.",
        )
    rupees = found["paise"] / 100
    ref = found["bankRef"] or "no bank reference"
    return Result(True, f"₹{rupees:,.2f} received, {ref}, signature verified")
