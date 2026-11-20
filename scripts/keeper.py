#!/usr/bin/env python3
"""The keeper — a resume-only rescue that does not need the VPS.

Why it exists: Inferolli's sandbox extends its own timeout, so it should never
expire. E2B still caps lifetime at 24h, and pause+resume is what resets that
window, so once a day the bot pauses itself on purpose and waits. The VPS
shepherd resumes it — but the VPS is off most of the night, which is exactly
when a sandbox that paused at 22:00 sits idle until morning.

This script is the out-of-VPS half: anything with a clock and the E2B API key
can run it (GitHub Actions cron, a worker, a laptop). It extends the running
sandbox, resumes the newest paused one, and — if there is nothing at all —
creates one, passing the secrets through the creation environment so the
sandbox can rebuild its own /bot/.env and boot with nobody writing files into
it. That last part is what makes the bot independent of the VPS.

    python3 scripts/keeper.py            # human-readable
    python3 scripts/keeper.py --json     # for a workflow log

Exit code is 0 whenever the check itself worked, even if nothing needed doing,
so a cron job does not look broken every half hour.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.e2b.app"
TEMPLATE_ALIAS = "inferolli"
TEMPLATE_ID = "4no9nprthep01xg42hgy"
WINDOW_S = 3600


def _req(method: str, path: str, key: str, body: dict | None = None,
         params: dict | None = None, timeout: int = 30):
    url = f"{API}{path}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-API-KEY": key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw.strip() else None)


def list_sandboxes(key: str, state: str) -> list[dict]:
    """v2 is the only listing that can see paused sandboxes.

    `/sandboxes` (v1) returns running ones only, which made a paused bot look
    like no bot at all.
    """
    try:
        status, body = _req("GET", "/v2/sandboxes", key, params={"state": state})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"listing {state} sandboxes failed: HTTP {e.code}") from e
    if status != 200 or not isinstance(body, list):
        raise RuntimeError(f"listing {state} sandboxes failed: HTTP {status}")
    return body


def is_inferolli(row: dict) -> bool:
    return (row.get("alias") == TEMPLATE_ALIAS
            or row.get("templateID") == TEMPLATE_ID)


SECRET_KEYS = (
    "DISCORD_BOT_TOKEN", "INFERENCEPORT_API_KEY", "E2B_API_KEY", "AI_GATEWAY_KEY",
    "S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
    "TONEARM_URL", "TONEARM_API_KEY", "TINYFISH_API_KEY",
)


def boot_env() -> dict:
    """The secrets a brand-new sandbox needs, taken from this process."""
    return {k: os.environ[k] for k in SECRET_KEYS if os.environ.get(k)}


def env_text(env: dict) -> str:
    """The /bot/.env file for a fresh sandbox. `set -a; . .env` must parse it,
    so any value with spaces or shell metacharacters gets quoted."""
    lines = []
    for k, v in env.items():
        v = str(v)
        if not v or any(c in v for c in " \t\n'\"$&|;<>()`"):
            v = "'" + v.replace("'", "'\\''") + "'"
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


def can_create() -> bool:
    """A sandbox with no Discord token is a sandbox that boots into nothing."""
    env = boot_env()
    return "DISCORD_BOT_TOKEN" in env and "E2B_API_KEY" in env


def create(key: str) -> dict:
    """Create a sandbox AND hand it its secrets.

    Creation envs used to reach the start command; re-verified 2026-11-20 and
    they no longer do (both /sandboxes + envVars and /v2/sandboxes + envs
    deliver nothing to it — only exec sessions see them). So the secrets go in
    as a FILE, through the e2b SDK, which is the one channel that can write
    into a sandbox from outside. start.sh's wait loop watches for /bot/.env,
    so this lands before it gives up.

    The SDK is deliberately imported here, not at the top: the resume/extend
    path must work without it (nothing to install on a thin cron box), and only
    creation needs the extra dependency.
    """
    try:
        import e2b
    except ImportError as e:
        raise RuntimeError("creating needs the e2b SDK (pip install e2b) "
                           "to write /bot/.env into the sandbox") from e
    env = boot_env()
    status, body = _req("POST", "/sandboxes", key, {
        "templateID": TEMPLATE_ID,
        "timeout": WINDOW_S,
        "autoPause": True,
        "metadata": {"role": "primary", "createdBy": "keeper"},
    }, timeout=120)
    if status not in (200, 201) or not isinstance(body, dict):
        raise RuntimeError(f"create failed: HTTP {status}")
    sid = body.get("sandboxID")
    sbx = e2b.Sandbox.connect(sid)
    sbx.files.write("/bot/.e2b-sandbox-id", sid)
    sbx.files.write("/bot/.env", env_text(env).encode())
    return body


def run_once(key: str | None = None, create_if_missing: bool = True) -> dict:
    """Keep exactly one Inferolli sandbox alive: extend, resume, or create."""
    key = key or os.environ.get("E2B_API_KEY", "")
    if not key:
        raise RuntimeError("no E2B_API_KEY — nothing can be done without it")

    report = {"running": [], "resumed": [], "created": None,
              "failed": [], "needs_launcher": False}

    running = [r for r in list_sandboxes(key, "running") if is_inferolli(r)]
    report["running"] = [r.get("sandboxID") for r in running]
    if len(running) > 1:
        # two running bot sandboxes share one Discord token: both answer, and
        # the reply rate doubles. Not the keeper's job to kill anything — just
        # say it out loud, because it is invisible from Discord.
        report["duplicates"] = [r.get("sandboxID") for r in running[1:]]
    if running:
        # one is enough; extras belong to the VPS shepherd's cleanup
        for row in running:
            sid = row.get("sandboxID")
            try:
                _req("POST", f"/sandboxes/{sid}/timeout", key, {"timeout": WINDOW_S})
            except Exception as e:
                report["failed"].append((sid, f"extend: {e}"))
        return report

    paused = [r for r in list_sandboxes(key, "paused") if is_inferolli(r)]
    if not paused:
        if create_if_missing and can_create():
            try:
                report["created"] = create(key).get("sandboxID")
            except Exception as e:
                report["failed"].append(("-", f"create: {e}"))
        else:
            report["needs_launcher"] = True
        return report

    # newest first: that is the one that paused most recently. Only ONE is
    # resumed — the others are leftovers from a handover, and resuming them
    # would put two bots on the same Discord token (which kicks both off).
    paused.sort(key=lambda r: r.get("startedAt") or "", reverse=True)
    report["left_paused"] = [r.get("sandboxID") for r in paused[1:]]
    for row in paused[:1]:
        sid = row.get("sandboxID")
        try:
            _req("POST", f"/sandboxes/{sid}/resume", key, timeout=120)
        except Exception as e:
            report["failed"].append((sid, f"resume: {e}"))
            continue
        report["resumed"].append(sid)
        try:
            # a resume comes back with a FIVE MINUTE window — shorter than a
            # cold boot, so without this the bot pauses again before it logs in
            _req("POST", f"/sandboxes/{sid}/timeout", key, {"timeout": WINDOW_S})
        except Exception as e:
            report["failed"].append((sid, f"extend: {e}"))

    return report


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    try:
        report = run_once()
    except Exception as e:
        print(f"keeper failed: {e}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"running: {report['running'] or 'none'}")
        print(f"resumed: {report['resumed'] or 'none'}")
        if report.get("duplicates"):
            print(f"WARNING more than one bot is running on this token: {report['duplicates']}")
        if report.get("created"):
            print(f"created: {report['created']}")
        if report.get("left_paused"):
            print(f"left paused (handover leftovers): {report['left_paused']}")
        for sid, why in report["failed"]:
            print(f"failed: {sid} — {why}")
        if report["needs_launcher"]:
            print("nothing running or paused, and no DISCORD_BOT_TOKEN here to create one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
