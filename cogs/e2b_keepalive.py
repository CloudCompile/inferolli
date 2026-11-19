"""E2B life cycle: keep-alive + nightly-walled reincarnation.

Why this file exists in the shape it does (2026-11-19 post-mortem):

The bot was supposed to reset its own sandbox timeout every 15 minutes so the
sandbox could never expire while the VPS slept. It never ran once. The gate
required three things and the *start command's* environment had none of them:

    E2B_KEEPALIVE   absent      (only exec sessions get it)
    E2B_SANDBOX_ID  empty string (only exec sessions get the real id)
    E2B_API_KEY     absent      (only in /bot/.env, never exported)

so every boot logged "not in e2b keepalive mode — skipping" and the sandbox
lived entirely off the VPS shepherd. When the VPS slept, the sandbox hit its
timeout and paused; the shepherd resumed it with a 5-MINUTE window (the CLI's
default), the bot could not boot and ping in 5 minutes, and it paused again.
From the outside: "inferolli keeps stopping."

Now it decides for itself, from sources that cannot silently be empty:
    id  → E2B_SANDBOX_ID → /bot/data/sandbox-id → /bot/data/e2b_sandbox_id
    key → E2B_API_KEY env → parsed out of /bot/.env
and if it still can't, it says exactly what is missing instead of skipping.

Second job, unchanged: REINCARNATION. E2B caps sandbox LIFETIME at 24h, and
pause+resume resets the window. At 23h of *uninterrupted running* the bot backs
up its brain and lets itself pause; the shepherd resumes it with a fresh window.
Paused time does not count — otherwise a resume looks instantly "too old" and
the bot re-pauses itself forever (which is exactly what it used to do).
"""

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx
from discord.ext import commands

log = logging.getLogger("cogs.e2b_keepalive")

API = "https://api.e2b.app"
PING_EVERY = int(os.environ.get("E2B_PING_EVERY_S", 120))  # well inside a 5-min resume window
LIFETIME_H = 23.0          # reincarnate an hour before the 24h wall
TIMEOUT_S = 3600           # the window we ask for on every ping

ID_FILES = ("/bot/data/sandbox-id", "/bot/data/e2b_sandbox_id")
_sleep = asyncio.sleep          # indirection so tests can run the loop without waiting
ENV_FILE = "/bot/.env"


# ---------- discovery helpers (pure, so they are testable off-sandbox) ----------

def looks_like_sandbox_id(value: str | None) -> bool:
    """E2B ids are short lowercase alphanumeric tokens.

    Rejects the two things that made this fail: the empty string, and the
    container hostname `e2b.local` — which is what /etc/hostname actually
    contains, contrary to the old comment claiming it held the sandbox id.
    """
    if not value:
        return False
    value = value.strip()
    if "." in value or len(value) < 12 or len(value) > 40:
        return False
    return all(c.isalnum() for c in value)


def sandbox_id_from_files(paths=ID_FILES) -> str | None:
    for path in paths:
        try:
            with open(path) as f:
                candidate = f.read().strip()
        except OSError:
            continue
        if looks_like_sandbox_id(candidate):
            return candidate
    return None


def key_from_env_file(path: str = ENV_FILE) -> str | None:
    """The API key lives in /bot/.env — which is sourced by start.sh, but a
    resumed sandbox keeps the env it was *created* with, so read the file too."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("E2B_API_KEY="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return value or None
    except OSError:
        pass
    return None


def resolve_sandbox_id() -> str | None:
    env_id = os.environ.get("E2B_SANDBOX_ID", "")
    if looks_like_sandbox_id(env_id):
        return env_id.strip()
    return sandbox_id_from_files()


def resolve_api_key() -> str | None:
    return os.environ.get("E2B_API_KEY") or key_from_env_file()


def was_suspended(last_seen: float, now: float, expected_every: float) -> bool:
    """True when far more time passed than the loop's own interval.

    While a sandbox is paused the process is frozen but the wall clock is not,
    so a gap of many intervals means we were suspended and resumed. The runtime
    window was reset by that resume, and the age clock must reset with it.
    """
    return (now - last_seen) > max(90.0, expected_every * 3)


# --------------------------------- cog ---------------------------------

class KeepAliveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self.window_start = time.time()   # start of the CURRENT e2b runtime window

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    # ---------- e2b api helpers (raw REST — the SDK isn't in the sandbox) ----------

    def _headers(self, key: str):
        return {"X-API-KEY": key}

    async def _api(self, method: str, path: str, json_body: dict | None = None,
                   timeout: float = 30.0, key: str = "", params: dict | None = None):
        async with httpx.AsyncClient(timeout=timeout) as c:
            return await c.request(method, f"{API}{path}", headers=self._headers(key),
                                   json=json_body, params=params)

    async def id_from_api(self, key: str) -> str | None:
        """Self-discovery for a sandbox that was created by a predecessor.

        A reincarnated sandbox has no launcher to write its id file, and the
        start command's env does not carry E2B_SANDBOX_ID — so it asks the API
        for the newest sandbox marked as a successor. The parent has no such
        mark, so this cannot resolve to the parent.
        """
        try:
            r = await self._api("GET", "/sandboxes", key=key, timeout=20.0)
        except Exception as e:
            log.warning("sandbox self-discovery failed: %s", e)
            return None
        if r.status_code != 200:
            log.warning("sandbox self-discovery failed: HTTP %s", r.status_code)
            return None
        try:
            data = r.json()
        except Exception:
            return None
        rows = data if isinstance(data, list) else (data.get("sandboxes") or [])
        best = None
        for row in rows:
            meta = row.get("metadata") or {}
            if meta.get("role") != "successor":
                continue
            started = row.get("startedAt") or ""
            if best is None or started > best[0]:
                best = (started, row.get("sandboxID") or row.get("id"))
        return best[1] if best else None

    async def ping(self, sandbox_id: str, key: str) -> bool:
        """Extend the running timeout. Returns True on success."""
        try:
            r = await self._api("POST", f"/sandboxes/{sandbox_id}/timeout",
                                {"timeout": TIMEOUT_S}, key=key)
        except Exception as e:
            log.warning("keepalive ping failed (network): %s", e)
            return False
        # 204 is what the endpoint actually returns on success (empty body);
        # only accepting 200 made a working keepalive look broken in the logs.
        if r.status_code in (200, 204):
            return True
        if r.status_code in (401, 403):
            log.error("keepalive UNAUTHORIZED (%s) — the API key is wrong; this sandbox "
                      "will pause when its timeout expires", r.status_code)
        else:
            log.warning("keepalive ping failed: HTTP %s %s", r.status_code, r.text[:120])
        return False

    # ---------- reincarnation ----------

    async def _reincarnate(self, sandbox_id: str, key: str):
        log.info("⏳ 23h reached — reincarnating (backup → successor → handover)")
        try:
            from cogs.backup import run_backup
            summary = await asyncio.get_event_loop().run_in_executor(None, run_backup)
            log.info("pre-migration backup: %s", summary)
        except Exception:
            log.exception("pre-migration backup failed — migrating anyway")

        bootstrap = {k: os.environ[k] for k in
                     ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY") if os.environ.get(k)}
        bootstrap["E2B_API_KEY"] = key
        bootstrap["E2B_KEEPALIVE"] = "1"
        r = await self._api("POST", "/sandboxes", {
            # the template ID, not the name: the start command env has an EMPTY
            # E2B_TEMPLATE_ID in this version, and a name may not resolve.
            "templateID": (os.environ.get("E2B_TEMPLATE_ID")
                           or os.environ.get("E2B_TEMPLATE")
                           or "4no9nprthep01xg42hgy"),
            "timeout": TIMEOUT_S,
            "autoPause": True,
            "envVars": bootstrap,
            # the successor finds itself by this mark (see id_from_api)
            "metadata": {"role": "successor", "parent": sandbox_id},
        }, timeout=60.0, key=key)
        if r.status_code not in (200, 201):
            log.error("successor creation FAILED: %s %s — staying alive as long as possible",
                      r.status_code, r.text[:120])
            return
        new_id = r.json().get("sandboxID") or r.json().get("sandboxId") or r.json().get("id")
        log.info("successor created: %s — pausing immediately so the two of us never "
                 "hold the same Discord session", new_id)

        log.info("handover complete — pausing myself")
        try:
            await self._api("POST", f"/sandboxes/{sandbox_id}/pause", timeout=15.0, key=key)
        except Exception:
            log.exception("self-pause failed (harmless — the wall will pause us)")

    # ---------- loop ----------

    async def _loop(self):
        await self.bot.wait_until_ready()

        sandbox_id = resolve_sandbox_id()
        key = resolve_api_key()
        if key and not sandbox_id:
            sandbox_id = await self.id_from_api(key)
            if sandbox_id:
                # remember it: start.sh and the next boot look here first
                try:
                    os.makedirs("/bot/data", exist_ok=True)
                    with open("/bot/data/sandbox-id", "w") as f:
                        f.write(sandbox_id)
                except OSError:
                    pass
                log.info("discovered own sandbox id from the API: %s", sandbox_id)
        if not sandbox_id or not key:
            missing = []
            if not sandbox_id:
                missing.append("sandbox id (E2B_SANDBOX_ID / /bot/data/sandbox-id)")
            if not key:
                missing.append("API key (E2B_API_KEY / /bot/.env)")
            log.error("keepalive DISABLED — cannot resolve %s. This sandbox will expire "
                      "on its own timeout.", " and ".join(missing))
            return

        log.info("keepalive armed: sandbox=%s, ping every %ss, window %ss",
                 sandbox_id, PING_EVERY, TIMEOUT_S)
        self.window_start = time.time()
        last_seen = time.time()

        while True:
            now = time.time()
            if was_suspended(last_seen, now, PING_EVERY):
                gap_h = (now - last_seen) / 3600
                log.info("detected a %.1fh suspension — resumed with a fresh runtime window", gap_h)
                self.window_start = now   # pause+resume resets e2b's clock; reset ours too
            last_seen = now

            ok = await self.ping(sandbox_id, key)
            if ok:
                log.info("keepalive ok — timeout extended to %ss", TIMEOUT_S)
                last_seen = time.time()

            age_h = (time.time() - self.window_start) / 3600
            if age_h >= LIFETIME_H:
                # E2B caps lifetime at 24h. Migrate ourselves instead of waiting for
                # the VPS: the successor boots in ~90s, versus being offline until
                # the VPS wakes up (it sleeps most of the night).
                await self._reincarnate(sandbox_id, key)
                # If the successor could not be created, stop pinging and let the
                # lifecycle pause us — the shepherd can still resume a paused box.
                log.info("24h wall approaching — letting the sandbox pause; the "
                         "shepherd resumes us with a fresh window if still needed")
                return

            await _sleep(PING_EVERY)


async def setup(bot: commands.Bot):
    await bot.add_cog(KeepAliveCog(bot))
