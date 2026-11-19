#!/usr/bin/env python3
"""The shepherd — keeps Inferolli alive on E2B forever.

Runs on the VPS as a systemd service. Every 15 minutes:
  * no inferolli sandbox running?  → launch one (brain restores from S3)
  * newest one older than 23.5h?   → backup its brain INSIDE it, then relaunch
                                     (E2B pauses sandboxes at exactly 24h of
                                      lifetime; the keepalive can't cross that
                                      wall, so we migrate before it)

This is the piece that makes the bot immortal: crashes, 24h walls, anything —
the shepherd notices within 15 minutes and the ship sails again.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

E2B = str(Path.home() / ".npm-global/bin/e2b")
LAUNCH = Path(__file__).parent / "e2b_launch.py"
PYTHON = Path(__file__).parent.parent / ".venv/bin/python"
MAX_AGE_H = 25.0   # the cog pauses itself at ~23h; only intervene past 25h
CHECK_EVERY_S = 15 * 60


def log(msg: str):
    print(f"[shepherd {datetime.utcnow().strftime('%m/%d %H:%M')}] {msg}", flush=True)


def env():
    env = dict(os.environ)
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


# the shepherd's OWN SDK calls (Sandbox.list/connect) need the key in THIS
# process — env() above only reached subprocesses, so every list() failed with
# "API key is required" and the shepherd relaunched the bot every 15 minutes
for _k, _v in env().items():
    os.environ.setdefault(_k, _v)


def _all_sandboxes(state=None):
    """EVERY sandbox across ALL paginator pages (the default page is tiny —
    page-1-only sweeps are how eight bots accumulated). `state` is filtered
    client-side: this SDK version's list() takes no state kwarg, and paused
    sandboxes may not appear at all."""
    import e2b
    out = []
    try:
        pag = e2b.Sandbox.list()
        while True:
            out.extend(pag.next_items())
            if not pag.has_next:
                break
    except Exception as err:
        print("[list failed: %s]" % err)
        return out
    if state:
        out = [b for b in out if getattr(b, "state", "") == state]
    return out


def inferolli_sandboxes():
    """[(id, started_at)] for running inferolli-template sandboxes — via the SDK
    (the CLI's list output broke: its auth gets revoked mid-version)."""
    out = []
    for b in _all_sandboxes():
        try:
            if getattr(b, "name", "") == "inferolli" or \
               getattr(b, "template_id", "") == "4no9nprthep01xg42hgy":
                started = getattr(b, "started_at", None)
                out.append((b.sandbox_id, started))
        except Exception:
            continue
    return out


def _age_hours(started) -> float:
    """Hours since a sandbox started. SDK timestamps are timezone-AWARE —
    comparing them to a naive utcnow() crashed the whole check."""
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds() / 3600


def _parse_paused_rows(text: str) -> list[str]:
    """Sandbox ids from `e2b sandbox list --state paused` output.

    The ALIAS column must be exactly `inferolli` — the old check was
    `"inferolli" in line`, which matched any row that merely mentioned the name,
    and the pause/resume cycle it triggered every 15 minutes froze the live bot.
    """
    out = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        if parts[0].lower() in ("sandbox", "id"):
            continue
        if parts[2] != "inferolli":          # ALIAS column
            continue
        if "Paused" not in line:
            continue
        out.append(parts[0])
    return out


def paused_inferolli() -> list[str]:
    """Paused inferolli sandboxes, if the CLI can tell us."""
    try:
        r = subprocess.run([E2B, "sandbox", "list", "--state", "paused"],
                           capture_output=True, text=True, env=env(), timeout=60)
        return _parse_paused_rows(r.stdout)
    except Exception as e:
        log(f"paused list failed: {e}")
        return []


def extend_window(sandbox_id: str, seconds: int = 3600) -> bool:
    """Reset a sandbox's runtime timeout via REST.

    `e2b sandbox resume` grants 5 minutes and the CLI has no flag to change it.
    Five minutes is less than a cold boot, so a resumed sandbox paused again
    before the bot could extend itself — the resume looked successful and the
    bot still went away. Always extend immediately after resuming.
    """
    import json
    import urllib.request
    key = env().get("E2B_API_KEY", "")
    if not key:
        log("cannot extend window: no E2B_API_KEY in the environment")
        return False
    req = urllib.request.Request(
        f"https://api.e2b.app/sandboxes/{sandbox_id}/timeout",
        data=json.dumps({"timeout": seconds}).encode(),
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"window extended to {seconds}s for {sandbox_id} (HTTP {r.status})")
            return True
    except Exception as e:
        log(f"window extension failed for {sandbox_id}: {e}")
        return False


def exec_in(sandbox_id: str, cmd: str, timeout=120):
    import e2b
    sbx = e2b.Sandbox.connect(sandbox_id)
    r = sbx.commands.run(cmd, timeout=timeout)
    return r.stdout.strip()


def launch():
    log("launching a fresh inferolli...")
    r = subprocess.run([str(PYTHON), str(LAUNCH)], capture_output=True, text=True,
                       env=env(), timeout=600)
    tail = [l for l in r.stdout.splitlines() if "live sandbox" in l or "not come up" in l]
    log("launch result: " + (tail[-1] if tail else f"rc={r.returncode} {r.stderr[-150:]}"))


def main():
    log("shepherd online")
    if not env().get("E2B_API_KEY"):
        log("WARNING: no E2B_API_KEY — every check will look like 'no sandboxes'")
    while True:
        try:
            boxes = inferolli_sandboxes()
            # paused inferolli? resume it — pause+resume RESETS the 24h runtime
            # window and the process survives frozen. cheapest continuity there is.
            # (the SDK list() can't see paused sandboxes — use the CLI for this)
            paused = paused_inferolli()
            if paused:
                live = {b[0] for b in boxes}
                for sid in paused:
                    if sid in live:
                        # a stale "Paused" row for a sandbox that is demonstrably
                        # running — resume is a pause+snapshot+resume cycle that
                        # freezes the bot for minutes. Never touch it.
                        log(f"skipping resume of {sid} — it is already running")
                        continue
                    log(f"paused inferolli {sid} — resuming (fresh 24h window)")
                    try:
                        import e2b
                        e2b.Sandbox.resume(sid) if hasattr(e2b.Sandbox, "resume") else \
                            subprocess.run([E2B, "sandbox", "resume", sid], env=env(), timeout=120)
                    except Exception as e:
                        log(f"resume failed: {e}")
                    else:
                        # the CLI gave us a 5-minute window — buy a real one now
                        extend_window(sid)
                boxes = inferolli_sandboxes()
            if not boxes:
                log("no inferolli running — reviving")
                launch()
            else:
                # newest = the one to keep; older ones get killed by the launch script
                boxes.sort(key=lambda b: b[1], reverse=True)
                newest_id, newest_start = boxes[0]
                age_h = _age_hours(newest_start)
                # capture the bot's log EVERY check — if it dies, we have the post-mortem
                try:
                    tail = exec_in(newest_id, "tail -c 4000 /bot/bot.log", timeout=30)
                    Path("/home/inferenceport/discord-bot/data/last-log.txt").write_text(tail)
                except Exception:
                    pass
                if age_h > MAX_AGE_H:
                    log(f"newest sandbox is {age_h:.1f}h old — migrating before the 24h wall")
                    try:
                        out = exec_in(newest_id,
                                      "cd /bot && python3 -c \"from cogs.backup import run_backup; print(run_backup())\"")
                        log(f"pre-migration backup: {out}")
                    except Exception as e:
                        log(f"pre-migration backup failed ({e}) — relaunching anyway (S3 has last night's)")
                    launch()
                else:
                    log(f"healthy: {newest_id} ({age_h:.1f}h old)")
        except Exception as e:
            log(f"shepherd error: {e}")
        time.sleep(CHECK_EVERY_S)


if __name__ == "__main__":
    main()
