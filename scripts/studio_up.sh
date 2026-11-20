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
  spec=$(pactl list short sinks | awk -v s="$SINK" '$2==s {print $4" "$5" "$6}')
  if [ "$spec" = "float32le 2ch 48000Hz" ]; then
    pactl suspend-sink "$SINK" 0 >/dev/null 2>&1 || true
  else
    # A sink left behind in another format (or by another daemon) is worse than
    # no sink: the capture reads at half realtime and the audio is gap-toothed.
    [ -n "$spec" ] && echo "studio: replacing sink $SINK ($spec -> float32le 2ch 48000Hz)" >&2
    for m in $(pactl list short modules | awk -v s="sink_name=$SINK" 'index($0, s) {print $1}'); do
      pactl unload-module "$m" >/dev/null 2>&1 || true
    done
    pactl load-module module-null-sink sink_name="$SINK" \
      rate=48000 channels=2 format=float32le \
      sink_properties=device.description="$SINK" >/dev/null \
      && echo "studio: created sink $SINK (48000/float32le)" >&2
  fi
else
  echo "studio: WARNING no PulseAudio — music will be silent" >&2
fi

exec python3 -m tonearm
