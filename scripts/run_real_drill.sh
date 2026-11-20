#!/bin/bash
# Stop the supervised bot (so its token is free), run the media drill, restart.
S=$(pgrep -f 'bot/start\.sh' | head -1)
[ -n "$S" ] && kill "$S" 2>/dev/null
sleep 1
B=$(pgrep -f '\-u bot\.py' | head -1)
[ -n "$B" ] && kill "$B" 2>/dev/null
sleep 2
cd /bot || exit 1
set -a
. /bot/.env
set +a
timeout 100 python3 -u /bot/drill_real.py >/tmp/drill_real.out 2>/tmp/drill_real.err
echo "drill exit=$?"
echo "--- stdout:"; cat /tmp/drill_real.out | tail -16
echo "--- stderr:"; tail -8 /tmp/drill_real.err
setsid --fork bash -c 'exec bash /bot/start.sh >>/tmp/start.log 2>&1'
echo "--> supervisor restarted"
