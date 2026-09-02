"""
Entry point.

    python -m bot.main

Boots the Pyrogram client, registers handlers, and starts the worker pool. The
payment callback listener and the job queue come up alongside it and are shut
down cleanly on SIGTERM so systemd restarts never leave a half-uploaded temp
file behind.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from pyrogram import Client

from . import db, media
from .config import ConfigError, cfg
from .handlers import admin, start as start_handlers, terabox, zipfiles
from .queue import Queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")


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
    start_handlers.register(app)
    admin.register(app, jobs)
    zipfiles.register(app, jobs)
    terabox.register(app, jobs)
    # Registered on their own branches:
    #   payment.register(app)           -> feat/payments
    #   soon.register(app)              -> feat/faphouse-button


async def run() -> None:
    ok, why = media.tools_available()
    if not ok:
        # Better to refuse at boot than to take a paid job and fail on every one.
        raise ConfigError(why)

    db.connect()
    log.info("database ready at %s", cfg.db_path)

    app = build_client()
    jobs = Queue(app)
    register_all(app, jobs)

    await app.start()
    me = await app.get_me()
    log.info("started as @%s (id %s)", me.username, me.id)
    log.info("admins: %s", ", ".join(str(i) for i in cfg.admin_ids))
    log.info("workers: %s concurrent · upload ceiling %s MB",
             cfg.max_concurrent_jobs, cfg.max_upload_mb)
    if not cfg.payments_enabled:
        log.warning("PAYSVC_SECRET is empty — top-ups are disabled until it is set")

    await jobs.start()

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
    await jobs.stop()      # refunds anything still in flight
    await app.stop()
    log.info("stopped cleanly")


def main() -> int:
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"\n  ✖  Configuration problem\n     {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
