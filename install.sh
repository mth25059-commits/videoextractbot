#!/usr/bin/env bash
#
# videoextractbot — one command to install everything, then the wizard asks the rest.
#
#     git clone https://github.com/<you>/videoextractbot.git
#     cd videoextractbot
#     sudo bash install.sh
#
# What this does, and nothing more: installs the system packages, builds the Python
# environment, installs the payment service's Node packages, and hands over to
# `python -m setup`. Every question — token, admin id, UPI, cookies, proxies,
# prices, database — belongs to the wizard, which checks each answer as it goes and
# shows you everything on one page before it writes a single file.
#
# Tested on Ubuntu 24.04. Debian 12 works the same way.
#
# ── one thing this script deliberately does NOT do ─────────────────────────────
# It opens no firewall ports. The payment service listens on 127.0.0.1:4400 and
# the bot's payment callback on 127.0.0.1:8081, and both must stay that way:
# paysvc is the process that decides money has arrived, so anything that can reach
# it can grant credits. If you have a firewall, leave those two closed.

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RUN_AS="${SUDO_USER:-${USER:-root}}"

c_ok=$'\033[1;32m'; c_bad=$'\033[1;31m'; c_hi=$'\033[1;36m'; c_dim=$'\033[2m'
c_off=$'\033[0m'

say()  { printf '\n%s==>%s %s\n' "$c_hi" "$c_off" "$*"; }
fine() { printf '  %s✓%s  %s\n' "$c_ok" "$c_off" "$*"; }
note() { printf '     %s%s%s\n' "$c_dim" "$*" "$c_off"; }
die()  { printf '\n  %s✖%s  %s\n\n' "$c_bad" "$c_off" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo:   sudo bash install.sh"
command -v apt-get >/dev/null || die \
  "this installer speaks apt (Ubuntu/Debian). On anything else, install ffmpeg,
     python3-venv, nodejs, npm, unar and libarchive-tools by hand, then run:
         python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
         cd paysvc && npm ci --omit=dev && cd ..
         .venv/bin/python -m setup"

cd "$APP_DIR"
[[ -f requirements.txt && -d bot ]] || die "run this from inside the cloned repo"

# ───────────────────────────────────────────────────────────────────── packages
say "installing system packages  ${c_dim}(a few minutes on a fresh box)${c_off}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# ffmpeg              every video is remuxed before Telegram will take it
# python3-venv        the bot's own environment, kept out of the system python
# nodejs, npm         the UPI payment service is Node
# unar                RAR. `rarfile` is a wrapper, not a decoder, and it needs a
#                     binary — `unar` and NOT `unrar`, which is non-free and is
#                     not in Ubuntu's default repositories. unar reads RAR5 too.
# libarchive-tools    bsdtar, the fallback for archives the Python readers refuse
# git                 paysvc's one dependency is installed straight from GitHub
apt-get install -y -qq \
  ffmpeg python3 python3-venv python3-pip \
  nodejs npm \
  unar libarchive-tools \
  git curl ca-certificates

for tool in ffmpeg ffprobe node npm unar; do
  command -v "$tool" >/dev/null || die "$tool did not install — the bot needs it"
done
fine "ffmpeg $(ffmpeg -hide_banner -version | head -1 | cut -d' ' -f3)"
fine "node $(node --version)"
fine "unar present  ${c_dim}(RAR archives will open)${c_off}"

# ───────────────────────────────────────────────────────────────── python
say "building the Python environment"
python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
fine "$(./.venv/bin/python --version), $(./.venv/bin/pip list 2>/dev/null | wc -l) packages"

# ─────────────────────────────────────────────────────────────────── node deps
say "installing the payment service's packages"
if [[ -d paysvc ]]; then
  # `npm ci` when there is a lockfile — it installs exactly what was tested — and
  # `npm install` when there is not, because `ci` refuses without one.
  if [[ -f paysvc/package-lock.json ]]; then
    ( cd paysvc && npm ci --omit=dev --silent )
  else
    ( cd paysvc && npm install --omit=dev --silent )
  fi
  fine "paysvc dependencies installed"
else
  note "no paysvc/ in this tree — top-ups will not be available"
fi

# ────────────────────────────────────────────────────────────────── directories
say "directories"
mkdir -p data downloads logs run paysvc/data
chmod 750 data paysvc/data
if [[ "$RUN_AS" != "root" ]] && id -u "$RUN_AS" >/dev/null 2>&1; then
  chown -R "$RUN_AS:$RUN_AS" "$APP_DIR"
  fine "everything owned by $RUN_AS  ${c_dim}(the bot will not run as root)${c_off}"
else
  note "running as root — the bot will run as root too, which works but is not ideal"
fi

df -h "$APP_DIR" | tail -1 | awk '{printf "     %s free on %s\n", $4, $6}'

# ──────────────────────────────────────────────────────────────────── the wizard
cat <<EOF

  ${c_ok}Installed.${c_off}  Now the thirteen questions.

  ${c_dim}Have these ready. Anything you do not have yet can be left blank and
  added later by running the same command again:${c_off}

     1.  Bot token          @BotFather on Telegram  →  /mybots
     2.  Your Telegram id   @userinfobot  →  /start
     3.  api_id, api_hash   my.telegram.org  →  API development tools
     4.  A Terabox cookie   terabox.com, logged in  →  F12  →  Cookies  →  ndus
     5.  Your UPI id        only if you want to sell credits
     6.  A channel of yours  only to force people to join it — and the bot has
                             to be an ${c_off}${c_dim}administrator${c_off}${c_dim} there, not just a member

EOF

exec ./.venv/bin/python -m setup --dir "$APP_DIR"
