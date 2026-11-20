#!/bin/bash
# The studio, run under systemd (Type=simple): ensure pulse, ensure the private
# sink, then become tonearm in the foreground so systemd supervises it.
#
# Why a wrapper instead of a long ExecStart line: the sink has to exist or the
# studio plays into the void, and a missing sink is not a startup error tonearm
# would report — it would just be silent.
set -u
cd "${TON_DIR:-$HOME/tonearm}" || exit 1

RUNTIME=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
SINK=${TONEARM_AUDIO_SINK:-inferolli_studio}
export XDG_RUNTIME_DIR=$RUNTIME

if ! pactl info >/dev/null 2>&1; then
  echo "studio: no PulseAudio on $RUNTIME — starting a private daemon" >&2
  pulseaudio -D --exit-idle-time=-1 --disable-shm >/dev/null 2>&1 || true
  sleep 2
fi

if pactl info >/dev/null 2>&1; then
  if pactl list short sinks | awk '{print $2}' | grep -qx "$SINK"; then
    pactl suspend-sink "$SINK" 0 >/dev/null 2>&1 || true
  else
    pactl load-module module-null-sink sink_name="$SINK" \
      sink_properties=device.description="$SINK" >/dev/null \
      && echo "studio: created sink $SINK" >&2
  fi
else
  echo "studio: WARNING no PulseAudio — music will be silent" >&2
fi

exec python3 -m tonearm
