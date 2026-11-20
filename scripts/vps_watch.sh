#!/bin/bash
# Keep the VPS runtime alive: every minute, make sure the studio and the bot
# are up, and that there is exactly one of each.
#
# vps_up.sh is idempotent and refuses to start a second bot, so this can loop
# forever without ever fighting itself. Run it under setsid/nohup:
#
#   setsid nohup bash scripts/vps_watch.sh >> /tmp/inferolli-watch.log 2>&1 &
#
# Stop it with: pkill -f 'vps_watch\.sh'
set -u
cd "$(dirname "$0")/.." || exit 1

while true; do
  bash scripts/vps_up.sh >/dev/null 2>&1
  sleep 60
done
