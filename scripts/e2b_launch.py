"""Create (or replace) the live Inferolli sandbox in E2B.

Usage: .venv/bin/python scripts/e2b_launch.py [--kill-old]
Reads secrets from .env; never prints them.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent

# --- load secrets from .env (never print) ---
env = {}
_env_path = ROOT / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")

E2B_API_KEY = os.environ.get("E2B_API_KEY") or env.get("E2B_API_KEY") or ""
assert E2B_API_KEY, "E2B_API_KEY missing"
os.environ["E2B_API_KEY"] = E2B_API_KEY

import e2b  # noqa: E402

TEMPLATE = "inferolli"

sandbox_envs = {
    "DISCORD_BOT_TOKEN": env["DISCORD_BOT_TOKEN"],
    "INFERENCEPORT_API_KEY": env["INFERENCEPORT_API_KEY"],
    "AI_MODEL": env.get("AI_MODEL", "gpt-6-luna"),
    "AI_FALLBACK_MODELS": env.get("AI_FALLBACK_MODELS", "gpt-5.6-luna,lightning"),
    "BOT_LOG_LEVEL": "INFO",
    # keep-alive: the bot resets its own running timeout
    "E2B_API_KEY": E2B_API_KEY,
    "E2B_KEEPALIVE": "1",
    # S3 brain backup (InferencePort bucket storage)
    "S3_ENDPOINT": env["S3_ENDPOINT"],
    "S3_BUCKET": env["S3_BUCKET"],
    "S3_ACCESS_KEY_ID": env["S3_ACCESS_KEY_ID"],
    "S3_SECRET_ACCESS_KEY": env["S3_SECRET_ACCESS_KEY"],
    # tonearm studio (the music sandbox)
    "TONEARM_URL": env.get("TONEARM_URL", ""),
    "TONEARM_API_KEY": env.get("TONEARM_API_KEY", ""),
    # vercel ai gateway (primary chat provider)
    "AI_GATEWAY_BASE_URL": env.get("AI_GATEWAY_BASE_URL", ""),
    "AI_GATEWAY_KEY": env.get("AI_GATEWAY_KEY", ""),
    "AI_GATEWAY_MODEL": env.get("AI_GATEWAY_MODEL", ""),
    # the template id — the bot uses it to reincarnate itself at the 24h wall
    "E2B_TEMPLATE_ID": "inferolli",
    # mixture-of-models: per-job assignments
    **{k: v for k, v in env.items() if k.startswith("AI_JOB_")},
}


def log(msg: str):
    print(f"[launch {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _all_sandboxes(state=None):
    """EVERY sandbox across ALL paginator pages (the default page is tiny —
    page-1-only sweeps are how eight bots accumulated). `state` filters
    client-side: this SDK's list() takes no state kwarg."""
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


def kill_all_inferolli(keep: str | None = None) -> int:
    """Kill EVERY running inferolli-template sandbox (SDK-based — the CLI's
    list output broke mid-version and silently blinded the old sweep)."""
    victims = []
    for b in _all_sandboxes():
        if b.sandbox_id != keep and (getattr(b, "name", "") == "inferolli"
                                     or getattr(b, "template_id", "") == "4no9nprthep01xg42hgy"):
            victims.append(b.sandbox_id)
    killed = 0
    for sid in victims:
        try:
            e2b.Sandbox.kill(sid)
            killed += 1
        except Exception as e:
            print(f"  could not kill {sid}: {str(e)[:60]}")
    if killed:
        print(f"killed {killed} stale inferolli sandbox(es)")
    return killed


def main():
    old_id = None
    marker = ROOT / "data" / "e2b_sandbox_id"
    if marker.exists():
        old_id = marker.read_text().strip()

    print("creating sandbox from template:", TEMPLATE)
    sbx = e2b.Sandbox.create(
        TEMPLATE,
        envs=sandbox_envs,
        timeout=60 * 60,  # 1h of running; keepalive pings every 15min to extend
        lifecycle={"on_timeout": "pause", "auto_resume": True},
    )
    print("sandbox id:", sbx.sandbox_id)

    # record it locally (for ops/cleanup)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(sbx.sandbox_id)

    # THE fix: kill every OTHER running inferolli sandbox (by template, not by id)
    kill_all_inferolli(keep=sbx.sandbox_id)

    # wait for the bot to come online — launched via setsid: a NEW session,
    # immune to the exec-handle's process-group reaping (bots died this way)
    print("waiting for the bot (the start command boots it)...")

    # persist the secrets to /bot/.env — backups upload it to S3 so the
    # 24h-wall reincarnation can bootstrap a successor with no VPS
    env_file = ROOT / ".env"
    if env_file.exists():
        blob = env_file.read_bytes()
        sbx.files.write("/bot/.env", blob)
        print(f"secrets persisted to /bot/.env ({len(blob)//1024} KB)")

    print("waiting for the bot process...")
    for _ in range(60):
        r = sbx.commands.run("pgrep -f 'python -u bot.py' >/dev/null && echo UP || echo DOWN",
                             timeout=10)
        if "UP" in r.stdout:
            break
        time.sleep(3)
    else:
        print("bot did not come up in 3min — logs: e2b sandbox exec <id> 'tail -50 /bot/bot.log'")
        return 1

    r = sbx.commands.run("tail -8 /bot/bot.log 2>/dev/null || true", timeout=10)
    print("--- bot log tail ---")
    print(r.stdout[-900:])
    print("live sandbox:", sbx.sandbox_id)
    print("connect: e2b sandbox connect", sbx.sandbox_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
