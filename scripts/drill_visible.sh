#!/bin/bash
# Drill: does the studio keep playing when chromium is VISIBLE (Xvfb) instead
# of headless? Headless pages are "hidden", and Apple Music pauses a hidden
# player — which looks exactly like "the music stops after 20 seconds".
set -u
export XDG_RUNTIME_DIR=/tmp/pulse-runtime
K=$(cat /home/user/.config/tonearm/master-key.txt)
api() { curl -s -m 12 -H "Authorization: Bearer $K" "http://127.0.0.1:8188$1"; }

echo "--> stopping the headless studio"
pkill -f 'python3 -m tonearm' 2>/dev/null
sleep 3

echo "--> starting a visible one under Xvfb"
pgrep -f 'Xvfb :99' >/dev/null || (setsid Xvfb :99 -screen 0 1280x800x24 >/tmp/xvfb.log 2>&1 &)
sleep 2
cd /home/user/tonearm || exit 1
DISPLAY=:99 TONEARM_HEADLESS=0 TONEARM_AUDIO=1 TONEARM_CHROME_BIN=chromium \
  TONEARM_PORT=8188 TONEARM_ENABLE_WRITES=1 \
  setsid nohup python3 -m tonearm >>/tmp/tonearm-visible.log 2>&1 &
for i in $(seq 1 30); do
  curl -s -m 2 http://127.0.0.1:8188/health >/dev/null 2>&1 && break
  sleep 2
done
echo "    health: $(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8188/health) after ${i}0s"

echo "--> playing Get Lucky"
api /api/playback/play-song/617154366 >/dev/null
for i in $(seq 1 12); do
  sleep 5
  P=$(api /api/playback/now-playing | python3 -c 'import json,sys
d=json.load(sys.stdin) if sys.stdin else {}
print(("playing" if d.get("isPlaying") else "STOPPED"), "|", (d.get("title") or "-")[:16])' 2>/dev/null)
  SI=$(pactl list short sink-inputs 2>/dev/null | wc -l)
  echo "    t=$((i*5))s | $P | sink_inputs=$SI"
done
