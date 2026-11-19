#!/bin/bash
# Inferolli entrypoint — THE template start command (runs at boot as pid-1's child).
# Waits for the launcher to persist /bot/.env, then restores + runs everything.
set -u
cd /bot

# --- wait for the secrets (the launcher writes /bot/.env right after create;
#     dashboard-started sandboxes never get one and idle out below) ---
WAITED=0
while [ ! -f /bot/.env ] && [ "$WAITED" -lt 600 ]; do
  sleep 5
  WAITED=$((WAITED + 5))
done
if [ ! -f /bot/.env ]; then
  # A sandbox created by a PREDECESSOR (the 24h reincarnation path) has no
  # launcher to write this file, and the canonical copy lives in S3 — which we
  # cannot reach without credentials. Chicken and egg. Creation envs DO reach
  # the start command, so rebuild .env from them; the S3 restore below then
  # replaces it with the real one.
  echo "[start] no .env file — rebuilding it from the environment"
  : > /bot/.env
  for k in DISCORD_BOT_TOKEN INFERENCEPORT_API_KEY E2B_API_KEY AI_GATEWAY_KEY \
           S3_ENDPOINT S3_BUCKET S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY \
           TONEARM_URL TONEARM_API_KEY TINYFISH_API_KEY; do
    v="$(printenv "$k" 2>/dev/null || true)"
    [ -n "$v" ] && printf '%s=%s\n' "$k" "$v" >> /bot/.env
  done
fi
if [ ! -s /bot/.env ]; then
  echo "[start] no .env and no credentials in the environment — idling"
  exec sleep infinity
fi
set -a; . /bot/.env; set +a

# --- the desktop display: Xvfb on :99, XFCE4 riding on it ---
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp > /dev/null 2>&1 &
export DISPLAY=:99
sleep 1.5
(dbus-launch --exit-with-session > /tmp/dbus.env 2>/dev/null; \
  . /tmp/dbus.env 2>/dev/null; xfwm4 & xfdesktop & xfce4-panel & xfce4-terminal &) \
  > /dev/null 2>&1

# --- opencode server: the bot's persistent agent sessions (per-channel threads) ---
export XDG_DATA_HOME=/bot/data/xdg   # opencode session storage lives in the backed-up brain
mkdir -p "$XDG_DATA_HOME"
# opencode needs the provider keys — the secure envs don't reach the start command,
# so source the restored .env (it's there: the launcher persists it at boot)
set -a; . /bot/.env; set +a
# opencode mints its own password each start and prints it to the serve log; the
# client reads it from there. Do NOT export a fixed one — a stale value means
# every request 401s and the primary reply engine dies quietly.
unset OPENCODE_SERVER_PASSWORD
(nohup opencode serve --port 4096 > /tmp/opencode-serve.log 2>&1 &)
for _ in $(seq 1 20); do
  grep -q 'server password' /tmp/opencode-serve.log 2>/dev/null && break
  sleep 0.5
done

# --- our own runtime window: do not rely on the VPS being awake ---
# `e2b sandbox resume` grants only 5 minutes by default, which is less than a
# cold boot; without this the sandbox pauses again before the bot can ever
# extend it, and from the outside that looks like the bot randomly stopping.
export E2B_KEEPALIVE=1
if [ -z "${E2B_SANDBOX_ID:-}" ] || [ "${E2B_SANDBOX_ID}" = "e2b.local" ]; then
  if [ -s /bot/data/sandbox-id ]; then
    export E2B_SANDBOX_ID="$(cat /bot/data/sandbox-id)"
  fi
fi
if [ -n "${E2B_SANDBOX_ID:-}" ] && [ -n "${E2B_API_KEY:-}" ]; then
  python3 - <<'EOPY' >> /bot/bot.log 2>&1
import json, os, urllib.request
sid, key = os.environ["E2B_SANDBOX_ID"], os.environ["E2B_API_KEY"]
req = urllib.request.Request(
    f"https://api.e2b.app/sandboxes/{sid}/timeout",
    data=json.dumps({"timeout": 3600}).encode(),
    headers={"X-API-KEY": key, "Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"[start] e2b window extended at boot (HTTP {r.status})")
except Exception as e:
    print(f"[start] e2b window extension FAILED: {e}")
EOPY
else
  echo "[start] no sandbox id or API key — cannot extend the e2b window" >> /bot/bot.log
fi

# --- the stack watchdog: any dead piece gets revived (children of the start
#     command survive; nothing exec-launched does — so this is the healer) ---
(while true; do
  sleep 30
  pgrep -f 'Xvfb :99' >/dev/null || { Xvfb :99 -screen 0 1440x900x24 -nolisten tcp > /dev/null 2>&1 & sleep 2; }
  pgrep -f 'x11vnc -display' >/dev/null || {
    (x11vnc -display :99 -forever -shared -nopw -quiet -viewonly -rfbport 5901 > /tmp/x11vnc-ro.log 2>&1 &)
    (x11vnc -display :99 -forever -shared -nopw -quiet -rfbport 5900 > /tmp/x11vnc-rw.log 2>&1 &)
  }
  pgrep -f 'uvicorn desktop_gate' >/dev/null || \
    (nohup python3 -m uvicorn desktop_gate:app --host 127.0.0.1 --port 8080 > /tmp/gate.log 2>&1 &)
  pgrep -f 'cloudflared tunnel' >/dev/null || \
    (nohup cloudflared tunnel --url http://localhost:8080 --no-autoupdate > /tmp/cloudflared.log 2>&1 &)
  pgrep -f 'opencode serve' >/dev/null || \
    (nohup opencode serve --port 4096 > /tmp/opencode-serve.log 2>&1 &)
done > /dev/null 2>&1 &)

# --- restore latest brain backup from S3 (creds now sourced) ---
echo "[start] restoring brain backup..."
python -c "from cogs.backup import restore_latest; print('[start]', restore_latest())" 2>&1 | tail -1

# --- run the bot under a supervisor loop (logs to /bot/bot.log) ---
while true; do
  echo "=== [supervisor] starting bot $(date)" >> /bot/bot.log
  python -u bot.py >> /bot/bot.log 2>&1
  code=$?
  echo "[supervisor] bot exited with $code — restarting in 5s" >> /bot/bot.log
  sleep 5
done
