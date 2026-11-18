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
  echo "[start] no .env after 10 min — idling (dashboard-started sandbox?)"
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
export OPENCODE_SERVER_PASSWORD=inferolli-local-4096   # localhost-only; the client sends it too
(nohup opencode serve --port 4096 > /tmp/opencode-serve.log 2>&1 &)

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
