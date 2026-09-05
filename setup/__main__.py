"""
`python -m setup` — the first-run wizard for videoextractbot.

Everything it does lives in the sibling modules; this file exists to turn arguments
into one call and to make sure Ctrl-C prints a sentence instead of a traceback.

    python -m setup                  # ask, review, verify, start
    python -m setup --no-start       # everything except starting it
    python -m setup --dir /srv/bot   # a tree somewhere other than this one
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ask
from .wizard import ROOT, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m setup",
        description="Set up videoextractbot: twelve questions, one review page, "
                    "then start.",
    )
    parser.add_argument("--dir", dest="app_dir", default=str(ROOT), type=str,
                        help="the bot's directory (default: the repo this is in)")
    parser.add_argument("--no-start", dest="start", action="store_false",
                        help="write and verify everything, but do not start it")
    args = parser.parse_args(argv)

    app_dir = Path(args.app_dir).resolve()
    if not (app_dir / ".env.example").exists():
        ask.bad(f"{app_dir} does not look like the bot's directory "
                f"(no .env.example in it)")
        return 2

    try:
        if app_dir != ROOT.resolve():
            # `.env` would land here while the bot that reads it — and its
            # `data/bot.db`, which `cfg.db_path` fixes to the code tree — lived over
            # there. Every check would tick and the bot would start with no `.env` at
            # all. `install.sh` passes its own directory, so this is only ever typed.
            ask.warn(f"--dir is {app_dir},\n"
                     f"     but this setup package lives in {ROOT}.\n"
                     f"     .env would be written to the first and read by the "
                     f"second. Run the\n     copy of `python -m setup` inside the "
                     f"tree you actually mean.")
            if not ask.ask_yes_no("carry on anyway?", False):
                return 2
        return run(app_dir, do_start=args.start)
    except ask.Cancelled:
        # Nothing has been written unless the review was accepted, and the review
        # says so before it is. So this really is "nothing happened".
        print("\n  Stopped. Nothing was changed — run it again whenever you like.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
