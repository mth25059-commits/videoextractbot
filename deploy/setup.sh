#!/usr/bin/env bash
# One-shot setup for a fresh Debian/Ubuntu box.
#
#   curl -fsSL <raw-url>/deploy/setup.sh | bash
#   # or: git clone … /opt/terabot && bash /opt/terabot/deploy/setup.sh
#
# Installs ffmpeg + Python deps, creates the service user, and installs the
# systemd unit. It does NOT write .env — that is done by hand, once, because it
# holds the bot token and the UPI id.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/terabot}"
APP_USER="${APP_USER:-terabot}"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31m✖\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this as root (sudo bash deploy/setup.sh)"

say "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ffmpeg python3 python3-venv python3-pip git curl ca-certificates

command -v ffmpeg >/dev/null || die "ffmpeg did not install — the bot cannot run without it"

say "checking ffmpeg"
ffmpeg -hide_banner -version | head -1

say "creating user $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

say "python environment"
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

say "directories"
mkdir -p "$APP_DIR/data" "$APP_DIR/downloads"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR/data"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  say "created $APP_DIR/.env from the example — fill it in before starting"
fi

say "systemd"
install -m 644 "$APP_DIR/deploy/terabot.service" /etc/systemd/system/terabot.service
systemctl daemon-reload
systemctl enable terabot

cat <<EOF

  Done. Two things left, both by hand:

    1.  nano $APP_DIR/.env          # API_ID, API_HASH, BOT_TOKEN, ADMIN_IDS
    2.  systemctl start terabot
        journalctl -u terabot -f    # watch it come up

  Free disk on this box:
$(df -h "$APP_DIR" | tail -1 | sed 's/^/      /')

EOF
