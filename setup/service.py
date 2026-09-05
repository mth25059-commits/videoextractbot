"""
Making it come back after a reboot, and starting it now.

`deploy/run.sh` is the supervisor and stays the supervisor — it owns process-group
pidfiles, log rotation, and a `stop` that waits up to sixty seconds. systemd's job
here is narrow: bring that supervisor up after a reboot and take it down politely
on shutdown.

`ExecStop` is the line that matters. `run.sh stop` signals the process group and
waits, and that wait is what lets the queue refund every job still in flight.
systemd killing the bot outright is how somebody pays for a video that never
arrives — so the unit is `Type=oneshot` with `RemainAfterExit=yes` and a real
`ExecStop`, not `Type=simple`.

`deploy/terabot-boot.service` hard-codes `/opt/terabot` and `ubuntu`, which were
right for one box. Both are templated out here, because a stranger's VPS has the
repo wherever they cloned it and a login called something else.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The installed unit's name. Not `terabot.service` — that is the repo's other
#: unit, which runs the bot alone under a dedicated system user and would fight
#: this one over the same pidfiles if both were ever enabled.
UNIT = "videoextractbot.service"
UNIT_DIR = Path("/etc/systemd/system")
TEMPLATE = ROOT / "deploy" / "terabot-boot.service"


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def default_user() -> str:
    """
    Which login should own the processes.

    Under `sudo`, `SUDO_USER` is the person who typed it — which is the right
    answer and not `root`. Running the bot as root is not needed by anything it
    does, and a bot that unpacks strangers' archives is the last process to hand a
    uid of 0 to.
    """
    return os.environ.get("SUDO_USER") or getpass.getuser()


def render_unit(template: str, app_dir: Path, user: str) -> str:
    """
    The shipped unit with this box's directory and login in it.

    Replaces every occurrence, comments included: a unit whose comment says
    `/opt/terabot` while its `WorkingDirectory` says something else is a trap for
    whoever reads it next.
    """
    text = template.replace("/opt/terabot", str(app_dir))
    text = text.replace("User=ubuntu", f"User={user}")
    text = text.replace("Group=ubuntu", f"Group={user}")
    return text.replace("`ubuntu`", f"`{user}`")


def install_unit(app_dir: Path, user: str) -> tuple[bool, str]:
    """
    Write the unit, reload systemd, enable it for next boot. Never starts it.

    Enabling and starting are kept apart on purpose: `enable` is a promise about
    the next reboot and is safe to repeat, while `start` is this moment and belongs
    to the wizard's own last step, where its output can be shown.
    """
    if not TEMPLATE.exists():
        return False, f"{TEMPLATE} is missing"
    if not is_root():
        return False, "not running as root, so the boot service was not installed"
    if not shutil.which("systemctl"):
        return False, "this box has no systemctl — start it with deploy/run.sh instead"
    try:
        dest = UNIT_DIR / UNIT
        dest.write_text(render_unit(TEMPLATE.read_text(encoding="utf-8"), app_dir, user),
                        encoding="utf-8")
        dest.chmod(0o644)
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=60)
        subprocess.run(["systemctl", "enable", UNIT], check=True, timeout=60,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"{UNIT} installed and enabled — it will come back after a reboot"


def run_sh(app_dir: Path, *args: str, as_user: str = "") -> tuple[bool, str]:
    """
    Call the supervisor, dropping privileges when the wizard is root.

    Starting as root would leave root-owned pidfiles, logs and `downloads/` behind,
    and the next `run.sh` run by the operator would then fail to write any of them —
    a failure that looks like a broken script rather than a wrong owner.
    """
    cmd = ["bash", str(app_dir / "deploy" / "run.sh"), *args]
    if as_user and is_root() and as_user != "root":
        cmd = ["sudo", "-u", as_user, "-H", *cmd]
    try:
        done = subprocess.run(cmd, cwd=str(app_dir), text=True, timeout=300,
                              capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def own(app_dir: Path, user: str) -> None:
    """
    Hand the tree to the login that will run it, and keep `.env` at 600.

    Best effort and silent on failure: a box where this cannot be done is a box
    where the operator is already the owner, which is the common case when the
    wizard is not run as root.
    """
    if not is_root() or user == "root" or not shutil.which("chown"):
        return
    for path in (app_dir / "data", app_dir / "downloads", app_dir / "logs",
                 app_dir / "run", app_dir / "paysvc" / "data"):
        path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["chown", "-R", f"{user}:{user}", str(app_dir)],
                   check=False, timeout=300, capture_output=True)
    env = app_dir / ".env"
    if env.exists():
        env.chmod(0o600)
