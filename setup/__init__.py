"""
The first-run setup wizard, run as `python -m setup`.

Five modules, split by what they are allowed to touch:

    ask.py       printing, one prompt, and the validators. Never reaches a network.
    envfile.py   reading and writing .env, and the backups. Never asks anything.
    checks.py    every real verification — cookies, proxies, credentials, database.
    service.py   systemd and deploy/run.sh.
    wizard.py    the twelve questions, the review page, and the order of it all.

It lives beside `bot/` rather than under `deploy/` because it is Python that
imports `bot` — `deploy/` holds shell and unit files, and a package that has to be
importable does not belong there.

Nothing in here is imported by the bot. `bot/` never imports `setup/`, in either
direction at runtime, which is why the wizard can hold placeholder credentials in
`os.environ` without that ever being true of the running bot.
"""
