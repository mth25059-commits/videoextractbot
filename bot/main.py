"""
Entry point.

    python -m bot.main

Boots the Pyrogram client, registers handlers, and starts the worker pool. The
payment callback listener and the job queue come up alongside it and are shut
down cleanly on SIGTERM so systemd restarts never leave a half-uploaded temp
file behind — and `scratch.sweep_at_boot` clears whatever a `kill -9` did leave,
before the first job asks for space.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import traceback

from pyrogram import Client, errors


def _config_problem(exc: BaseException) -> None:
    print(f"\n  ✖  Configuration problem\n     {exc}\n", file=sys.stderr)


# .config is imported first and on its own, because it builds `cfg` in its module
# body: a blank API_ID raises while Python is still walking this import list, far
# too early for main()'s except clause below to ever see it. Left alone that prints
# a twelve-line traceback whose last line is the only useful one — and the
# supervisor in deploy/run.sh reprints the whole thing every five seconds, which
# is how a one-line fix in .env turns into a log nobody wants to read.
try:
    from .config import ConfigError, cfg
except Exception as exc:
    # ConfigError lives in the module that just failed to import, so it cannot be
    # named in this except clause; the class name is all there is to match on. A
    # real bug in config.py keeps its traceback, which is what re-raising is for.
    if type(exc).__name__ != "ConfigError":
        raise
    _config_problem(exc)
    raise SystemExit(2) from None

from . import callback_server, db, media, nightly, payments, scratch, settings
from .handlers import (admin, fap, join, payment, start as start_handlers, terabox,
                       zipfiles)
from .queue import Queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")

#: The long half of `_connect`'s KeyError arm, kept out of the flow so the arm
#: reads as one line. `KeyError: 0` is TLObject.read() being handed nothing: the
#: first four bytes of the reply read as zero because there was no reply. It
#: surfaces at req_pq_multi — the very first packet, which carries no credential
#: at all, since api_id and api_hash are not sent until initConnection much
#: later. So this is never a wrong key; a wrong one comes back as ApiIdInvalid.
#:
#: What it means is that MTProto never reached Telegram. Hosts that put an egress
#: proxy in front of a sandbox allow TLS by SNI name; MTProto is not TLS and
#: offers no SNI, so the connection is accepted and then reset. Daytona's envoy
#: does exactly this — and it leaves api.telegram.org reachable, so
#: `curl .../getMe` answers perfectly while the bot cannot connect at all. That
#: combination is what makes this worth a paragraph instead of a line.
_MTPROTO_BLOCKED = (
    "Telegram accepted the connection and then sent nothing back, so the MTProto "
    "handshake never started. This is a blocked network, not a bad credential — the "
    "first handshake packet carries no api_id or api_hash at all. Hosts that filter "
    "outbound traffic (Daytona, some PaaS sandboxes, corporate networks) reset "
    "connections to Telegram's data centres while leaving api.telegram.org "
    "reachable, so a working `curl https://api.telegram.org/bot<token>/getMe` proves "
    "nothing here. This bot needs a host with unfiltered outbound TCP."
)


def build_client() -> Client:
    return Client(
        name="terabot",
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        bot_token=cfg.bot_token,
        workdir=str(cfg.db_path.parent),
        parse_mode=__import__("pyrogram").enums.ParseMode.HTML,
    )


def register_all(app: Client, jobs: Queue) -> None:
    """
    **Order matters, and only in one place.** `terabox.register` ends with
    `loose_text`, a catch-all text handler, and pyrogram runs the first handler in a
    group that matches — so every flow that owns a text prompt has to be registered
    before it or it will never see a message. `fap`, `zipfiles` and `payment` all do;
    terabox stays last.

    The box ran `payment.register` *after* terabox, which meant the "type your own
    amount" prompt was answered by `loose_text` with "wrong link — no video found".
    It is before terabox here, which is the fix.

    `join.register` is first, but for a different reason and it is not about this
    ordering rule at all: it installs its two handlers in group **-1**, and pyrogram
    gives every group a chance in ascending order. So the gate sees a message before
    anything below, and its `StopPropagation` is what keeps the rest from running.
    Everything else here shares group 0, where only the first match runs.
    """
    join.register(app)
    start_handlers.register(app)
    admin.register(app, jobs)
    zipfiles.register(app, jobs)
    fap.register(app, jobs)
    payment.register(app)
    terabox.register(app, jobs)


async def _reconcile(app: Client) -> None:
    """
    Settle anything that was paid while the bot was down, then tell those users.

    In the background, not in the boot path: it is a handful of HTTP calls to
    paysvc, and a bot that will not answer /start until they finish is worse than
    one that credits a stranded payment ten seconds late.
    """
    try:
        for done in await payments.reconcile():
            await payment.announce(app, done)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The pull path still exists: the user's own "I have paid" button, and the
        # next restart. Nothing is lost by this failing.
        log.exception("boot reconcile failed")


# A credential Telegram refuses is the same class of problem as a blank one in .env,
# and it deserves the same one sentence — but Pyrogram raises out of its own
# internals, and what comes out names nothing. The worst of them is not a credential
# at all: a network that cannot carry MTProto fails as `KeyError: 0` thirty-four lines
# deep in session/auth.py, and the supervisor in deploy/run.sh reprints all thirty-four
# every five seconds.
#
# These are the failures anyone actually meets on a fresh box. Anything else keeps its
# traceback, because an unrecognised failure here is a bug, not a deploy mistake.
async def _connect(app: Client) -> None:
    try:
        await app.start()
    except (errors.AccessTokenInvalid, errors.AccessTokenExpired) as exc:
        raise ConfigError(
            "BOT_TOKEN was refused by Telegram. Get a fresh one from @BotFather "
            "(/mybots -> your bot -> API Token -> Revoke current token), then put "
            "it in .env."
        ) from exc
    except (errors.ApiIdInvalid, errors.ApiIdPublishedFlood) as exc:
        raise ConfigError(
            "API_ID and API_HASH do not match an app on my.telegram.org. They are "
            "read as a pair, so check both in .env."
        ) from exc
    except (errors.AuthKeyUnregistered, errors.AuthKeyInvalid) as exc:
        # The session file outlives the token that made it. Revoking in @BotFather is
        # the common way to get here, and the fix is a file to delete — which is not
        # something anyone guesses from the exception name.
        raise ConfigError(
            f"The saved login no longer matches BOT_TOKEN — this is what revoking a "
            f"token leaves behind. Delete {cfg.db_path.parent / 'terabot.session'} "
            f"and start again; nothing else is lost, the database is a separate file."
        ) from exc
    except KeyError as exc:
        # Only when it came out of the handshake. A KeyError from anywhere else in
        # start() is a real bug and must not be relabelled as a deploy problem.
        where = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).replace("\\", "/")
        if "pyrogram/session/auth" not in where:
            raise
        raise ConfigError(_MTPROTO_BLOCKED) from exc


async def run() -> None:
    ok, why = media.tools_available()
    if not ok:
        # Better to refuse at boot than to take a paid job and fail on every one.
        raise ConfigError(why)

    db.connect()
    log.info("database ready — %s", db.describe())

    # Before any worker starts, and before the disk guard has to make anyone wait
    # for space: nothing under work_dir can belong to a job now, because no job
    # survives a restart. Anything there is what a kill or a crash left behind.
    scratch.sweep_at_boot()

    app = build_client()
    jobs = Queue(app)
    register_all(app, jobs)

    await _connect(app)
    me = await app.get_me()
    log.info("started as @%s (id %s)", me.username, me.id)
    log.info("admins: %s", ", ".join(str(i) for i in cfg.admin_ids))
    log.info("workers: %s link · %s zip · upload ceiling %s MB",
             cfg.max_concurrent_jobs, cfg.max_concurrent_zip_jobs, cfg.max_upload_mb)
    if not cfg.payments_enabled:
        log.warning("PAYSVC_SECRET is empty — top-ups are disabled until it is set")
    else:
        log.info("top-ups on · paysvc %s · callback 127.0.0.1:%s · ₹1 = %g credits",
                 cfg.paysvc_url, cfg.paid_callback_port,
                 settings.get("credits_per_rupee"))

    await jobs.start()
    janitor = asyncio.create_task(scratch.janitor(), name="scratch-janitor")
    reporter = asyncio.create_task(nightly.run(app, jobs), name="nightly-report")

    # Both only exist when there is a payment service to talk to. `serve` binds
    # 127.0.0.1 and nothing else — paysvc can move money, so it is never reachable
    # from outside the box.
    paid_door = None
    catch_up = None
    if cfg.payments_enabled:
        paid_door = await callback_server.serve(payment.make_on_paid(app))
        catch_up = asyncio.create_task(_reconcile(app))

    stop = asyncio.Event()

    def _shutdown(*_: object) -> None:
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            signal.signal(sig, _shutdown)  # Windows

    await stop.wait()
    log.info("stopping…")
    janitor.cancel()
    reporter.cancel()
    if catch_up is not None and not catch_up.done():
        catch_up.cancel()
    if paid_door is not None:
        paid_door.close()
        await paid_door.wait_closed()
    await jobs.stop()      # refunds anything still in flight
    await app.stop()
    log.info("stopped cleanly")


def main() -> int:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        # Reachable for what run() checks itself: ffmpeg missing, and any credential
        # Telegram refuses once it is actually asked.
        _config_problem(exc)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
