#!/usr/bin/env bash
# The supervisor. Runs the bot and paysvc together and restarts either if it dies.
#
#   bash deploy/run.sh start|stop|restart|status|logs
#
# This is what install.sh wires up, via deploy/terabot-boot.service — a oneshot
# unit whose ExecStart is `run.sh start` and whose ExecStop is `run.sh stop`.
# The alternative unit in this folder, deploy/terabot.service, runs the *bot
# alone* under its own system user and leaves paysvc unsupervised; it is kept for
# an install with no payments, not for the normal one.
#
# It also works where systemd does not exist at all — containers, sandboxes, any
# box whose PID 1 is not an init system, where `systemctl` answers "offline".
#
# Both services are supervised: if either exits for any reason it is started
# again after a short pause. paysvc goes up first, because the bot's boot-time
# reconcile asks it about orders that settled while the bot was down.
#
# Stopping sends SIGTERM to the whole process group and waits. That wait is not
# politeness: the bot refunds every job still in flight when it sees SIGTERM, and
# killing it outright is how a user pays for a video that never arrives.

set -uo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_DIR="$APP_DIR/run"
LOG_DIR="$APP_DIR/logs"
VENV="$APP_DIR/.venv/bin/python"
STOP_WAIT=60          # seconds to let the bot finish refunding before SIGKILL
MAX_LOG_BYTES=$((20 * 1024 * 1024))

mkdir -p "$RUN_DIR" "$LOG_DIR"

c_ok=$'\033[1;32m'; c_bad=$'\033[1;31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say() { printf '%s\n' "$*"; }

pidfile() { echo "$RUN_DIR/$1.pid"; }
logfile() { echo "$LOG_DIR/$1.log"; }

# What the pidfile holds is a process *group* id, not just a pid: setsid made the
# supervisor its own group leader, so its pid and its pgid are the same number.
#
# Asking about the group rather than the leader is the whole point. Kill the
# supervisor and its worker is reparented to PID 1 and keeps running — still
# holding port 4400 — and a leader-only check calls that "down". `start` then
# launches a second copy, which cannot bind, so the supervisor restarts it every
# five seconds forever and `stop` has nothing to stop. Checking the group reports
# what is true, and `stop` signals the group, so the orphan is reachable.
#
# A stale pidfile — sandbox restarted, host rebooted — is "not running" rather than
# an error, so `start` after a restart just works.
alive_group() {
  local pgid=$1
  [[ -n ${pgid:-} ]] || return 1
  ps -eo pgid= -o pid= 2>/dev/null \
    | awk -v g="$pgid" '$1 == g { found = 1 } END { exit !found }'
}

running() {
  local pf; pf=$(pidfile "$1")
  [[ -f $pf ]] || return 1
  local pid; pid=$(cat "$pf" 2>/dev/null) || return 1
  alive_group "$pid"
}

# Running is not the same as being watched. If the group is alive but its leader is
# gone, the service is up and nothing will restart it when it next dies — worth
# saying out loud rather than showing a green "up".
supervised() {
  local pf; pf=$(pidfile "$1")
  [[ -f $pf ]] || return 1
  local pid; pid=$(cat "$pf" 2>/dev/null) || return 1
  [[ -n ${pid:-} ]] && kill -0 "$pid" 2>/dev/null
}

# --- the supervisor ---------------------------------------------------------
# Re-entered as `run.sh __supervise <name> <cmd...>`; not meant to be called by
# hand. Kept in this file rather than a second one so there is one script to copy
# to a box and one to read.
if [[ ${1:-} == __supervise ]]; then
  name=$2; shift 2
  log=$(logfile "$name")
  trap 'exit 0' TERM INT
  while true; do
    # Rotated here, not by logrotate: a 2 GB upload logged line by line for weeks
    # is the one thing on this box that grows without bound.
    if [[ -f $log ]] && (( $(wc -c < "$log") > MAX_LOG_BYTES )); then
      mv -f "$log" "$log.1"
    fi
    printf '\n[%s] starting: %s\n' "$(date -Is)" "$*" >> "$log"
    "$@" >> "$log" 2>&1
    code=$?
    printf '[%s] exited with %s — restarting in 5s\n' "$(date -Is)" "$code" >> "$log"
    sleep 5
  done
fi

start_one() {
  local name=$1; shift
  if running "$name"; then
    say "  ${c_dim}already up${c_off}  $name (pid $(cat "$(pidfile "$name")"))"
    return 0
  fi
  # setsid makes the supervisor a group leader, which is what lets stop take down
  # the supervisor and whatever it spawned in one signal, and what lets both
  # survive this ssh session closing.
  setsid nohup bash "${BASH_SOURCE[0]}" __supervise "$name" "$@" \
    < /dev/null >> "$(logfile "$name")" 2>&1 &
  echo $! > "$(pidfile "$name")"
  sleep 1
  if running "$name"; then
    say "  ${c_ok}started${c_off}     $name (pid $(cat "$(pidfile "$name")"))"
  else
    say "  ${c_bad}failed${c_off}      $name — see $(logfile "$name")"
    return 1
  fi
}

stop_one() {
  local name=$1 pf pid waited=0
  pf=$(pidfile "$name")
  if ! running "$name"; then
    rm -f "$pf"
    say "  ${c_dim}not running${c_off} $name"
    return 0
  fi
  pid=$(cat "$pf")
  kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
  # Waiting on the group, not on the leader. With an orphaned worker the leader is
  # already gone, so a leader-only wait returns at once and reports "stopped" while
  # the bot is still mid-upload — skipping the very wait this exists for, which is
  # the bot refunding every job in flight before it goes.
  while (( waited < STOP_WAIT )) && alive_group "$pid"; do
    sleep 1; waited=$((waited + 1))
  done
  if alive_group "$pid"; then
    say "  ${c_bad}killing${c_off}     $name — did not stop in ${STOP_WAIT}s"
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
  else
    say "  ${c_ok}stopped${c_off}     $name (after ${waited}s)"
  fi
  rm -f "$pf"
}

# --- preflight --------------------------------------------------------------
# Checked before anything is started, because both of these fail in a way that
# looks like a bug in the bot: no venv and `python -m bot.main` cannot import
# pyrogram, no .env and config.py raises on the first line it reads.
preflight() {
  local bad=0
  [[ -x $VENV ]] || { say "  ${c_bad}✖${c_off} no venv at $VENV — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; bad=1; }
  [[ -f $APP_DIR/.env ]] || { say "  ${c_bad}✖${c_off} no .env in $APP_DIR — copy .env.example and fill it in"; bad=1; }
  command -v ffmpeg >/dev/null || { say "  ${c_bad}✖${c_off} ffmpeg is not installed — the bot refuses to start without it"; bad=1; }
  if [[ -d $APP_DIR/paysvc ]]; then
    command -v node >/dev/null || { say "  ${c_bad}✖${c_off} node is not installed — paysvc cannot run"; bad=1; }
    [[ -d $APP_DIR/paysvc/node_modules ]] || { say "  ${c_bad}✖${c_off} paysvc/node_modules missing — run: cd paysvc && npm install"; bad=1; }
  fi
  return $bad
}

case "${1:-}" in
  start)
    preflight || exit 2
    shift
    want=("$@"); (( ${#want[@]} )) || want=(paysvc bot)
    say "starting in $APP_DIR"
    # paysvc first: the bot's boot reconcile asks it which orders settled while
    # the bot was down, and a refused connection there is a wasted retry.
    for name in "${want[@]}"; do
      case "$name" in
        paysvc) [[ -d $APP_DIR/paysvc ]] && start_one paysvc node "$APP_DIR/paysvc/server.js" ;;
        bot)    start_one bot "$VENV" -m bot.main ;;
        *)      say "  ${c_bad}✖${c_off} unknown service: $name (want paysvc or bot)"; exit 1 ;;
      esac
    done
    say ""
    say "  logs:  bash deploy/run.sh logs"
    ;;
  stop)
    shift
    want=("$@"); (( ${#want[@]} )) || want=(bot paysvc)
    say "stopping"
    for name in "${want[@]}"; do stop_one "$name"; done
    ;;
  restart)
    shift
    # Passed straight through, so `restart bot` restarts only the bot. Without this
    # the one command `status` tells you to run for a single service would quietly
    # bounce both, and bouncing the bot cancels and refunds whatever it is carrying.
    bash "${BASH_SOURCE[0]}" stop "$@"
    bash "${BASH_SOURCE[0]}" start "$@"
    ;;
  status)
    for name in paysvc bot; do
      [[ $name == paysvc && ! -d $APP_DIR/paysvc ]] && continue
      if running "$name"; then
        say "  ${c_ok}up${c_off}    $name  pid $(cat "$(pidfile "$name")")  $(du -h "$(logfile "$name")" 2>/dev/null | cut -f1) of log"
        supervised "$name" || say "        ${c_bad}↳ unsupervised${c_off} — it is serving, but nothing will restart it. Fix: bash deploy/run.sh restart $name"
      else
        say "  ${c_bad}down${c_off}  $name"
      fi
    done
    if command -v curl >/dev/null && [[ -f $APP_DIR/.env ]]; then
      secret=$(grep -E '^PAYSVC_SECRET=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d ' "'"'"'')
      if [[ -n ${secret:-} ]]; then
        say ""
        say "  paysvc /health:"
        curl -sS --max-time 5 -H "x-paysvc-secret: $secret" \
          http://127.0.0.1:4400/health 2>&1 | head -c 600 | sed 's/^/    /'
        say ""
      fi
    fi
    ;;
  logs)
    tail -n 40 -f "$LOG_DIR"/*.log
    ;;
  *)
    say "usage: bash deploy/run.sh {start|stop|restart|status|logs} [paysvc|bot]"
    say ""
    say "  A service can be named to act on just that one: \`start paysvc\` brings"
    say "  up the payment side on its own, which is what you want before the"
    say "  Telegram credentials are in .env — the bot would otherwise restart in a"
    say "  loop against a config error every five seconds."
    exit 1
    ;;
esac
