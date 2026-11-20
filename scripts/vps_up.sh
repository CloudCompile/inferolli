#!/bin/bash
# Bring up Inferolli on a normal Linux box (the VPS) instead of an e2b sandbox.
#
# Three things have to hold for the bot to be audible, and each has bitten us:
#
#   1. EXACTLY ONE bot process. Two of them hold the same token, so each one
#      kicks the other out of voice — the command "joins for half a second and
#      then leaves" and then errors. Nothing in the logs says "duplicate".
#   2. PulseAudio has to be reachable at the dir that actually holds a socket.
#      In the sandbox that was /tmp/pulse-runtime; on a desktop it is
#      /run/user/<uid>. The bot now discovers it, but the studio needs it on
#      its command line too.
#   3. The studio must play into a sink NOBODY else uses, so the bot captures
#      the music and not the machine's own output.
#
# Idempotent: safe to re-run. Usage: scripts/vps_up.sh [--restart]
set -u
cd "$(dirname "$0")/.." || exit 1
ROOT=$PWD

RUNTIME=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
SINK=${TONEARM_AUDIO_SINK:-inferolli_studio}
PORT=${TONEARM_PORT:-8188}
TON_DIR=${TON_DIR:-$HOME/tonearm}
PROFILE=${TONEARM_PROFILE:-$HOME/.config/tonearm/inferolli-profile}
CHROME=${CHROME_BIN:-google-chrome}
export XDG_RUNTIME_DIR=$RUNTIME

slog() { echo "[vps_up $(date -u +%H:%M:%S)] $*"; }

# --- 1. PulseAudio -------------------------------------------------------------
if ! pactl info >/dev/null 2>&1; then
  slog "pulseaudio unreachable on $RUNTIME — starting it"
  pulseaudio -D --exit-idle-time=-1 --disable-shm >/dev/null 2>&1
  sleep 2
fi
if ! pactl info >/dev/null 2>&1; then
  slog "FATAL: no PulseAudio — the bot can only send digital silence"
  exit 1
fi

# --- 2. a private sink for the studio -----------------------------------------
if pactl list short sinks | awk '{print $2}' | grep -qx "$SINK"; then
  pactl suspend-sink "$SINK" 0 >/dev/null 2>&1 || true
  slog "studio sink $SINK already present"
else
  if pactl load-module module-null-sink sink_name="$SINK" \
        sink_properties=device.description="$SINK" >/dev/null; then
    slog "created studio sink $SINK"
  else
    slog "FATAL: could not create the studio sink"
    exit 1
  fi
fi

# --- 3. the studio (tonearm) --------------------------------------------------
studio_up() { curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/health"; }

if studio_up; then
  slog "studio already answering on :$PORT"
else
  if [ ! -d "$PROFILE/Default" ]; then
    slog "no Apple session profile at $PROFILE — copy one there to use music"
  else
    slog "launching the studio on :$PORT (sink $SINK)"
    ( cd "$TON_DIR" && TONEARM_AUDIO=1 TONEARM_AUDIO_SINK="$SINK" \
        TONEARM_CHROME_BIN="$CHROME" TONEARM_PORT="$PORT" \
        TONEARM_PROFILE="$PROFILE" TONEARM_ENABLE_WRITES=1 \
        setsid nohup python3 -m tonearm >>/tmp/tonearm-vps.log 2>&1 & )
    for i in $(seq 1 30); do
      studio_up && { slog "studio healthy after $((i*2))s"; break; }
      sleep 2
    done
    studio_up || slog "WARN: studio not answering yet (chromium startup is slow)"
  fi
fi

# --- 4. exactly one bot -------------------------------------------------------
# pgrep patterns are bracket-escaped so this script's own command line can never
# match itself (matching it kills the shell mid-run — learned the hard way).
pids=$(pgrep -f '[-]u bot\.py' || true)
if [ -n "$pids" ]; then
  if [ "${1:-}" = "--restart" ]; then
    slog "stopping bot: $(echo $pids | tr '\n' ' ')"
    kill $pids 2>/dev/null
    sleep 3
    kill -9 $(pgrep -f '[-]u bot\.py' || true) 2>/dev/null
  else
    slog "bot already running ($(echo $pids | tr '\n' ' ')) — not starting a second"
    exit 0
  fi
fi

[ -f "$ROOT/.env" ] || { slog "FATAL: no .env in $ROOT"; exit 1; }
slog "starting the bot"
cd "$ROOT" && setsid nohup .venv/bin/python -u bot.py >>"$ROOT/bot.log" 2>&1 &
sleep 8
if pgrep -f '[-]u bot\.py' >/dev/null; then
  slog "bot running (pid $(pgrep -f '[-]u bot\.py' | tr '\n' ' '))"
else
  slog "FATAL: the bot exited — see $ROOT/bot.log"
  tail -5 "$ROOT/bot.log"
  exit 1
fi
