"""E2B life cycle: keep-alive + nightly-walled reincarnation.

Two jobs while running inside an E2B sandbox:

1. KEEP-ALIVE — pings the E2B API every 15 min to reset the running timeout
   (1h). If this ever dies, the sandbox auto-pauses (kept indefinitely,
   resumable) instead of being killed — a recoverable failure mode.

2. REINCARNATION — E2B caps sandbox LIFETIME at 24h total; the keepalive
   can't cross that wall. So at hour 23 the bot: backs up its brain + secrets
   to S3, creates its own successor (same template, bootstrap envs), waits
   for it to come up, then pauses itself. The successor's start.sh restores
   the brain AND the .env from S3. The bot migrates itself — no VPS needed
   (the VPS dies nightly between 9-10:30pm CST, so nothing may depend on it).
"""

import asyncio
import logging
import os
import time

import httpx
from discord.ext import commands

log = logging.getLogger("cogs.e2b_keepalive")

API = "https://api.e2b.app"
PING_EVERY = int(os.environ.get("E2B_PING_EVERY_S", 900))  # reset timeout every 15 min
LIFETIME_H = 23.0          # reincarnate an hour before the 24h wall
BOOT_WAIT_S = 100          # successor boot: restore + bot start ≈ 60-90s


class KeepAliveCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self.started = time.time()

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    # ---------- e2b api helpers (raw REST — the SDK isn't in the sandbox) ----------

    def _headers(self):
        return {"X-API-KEY": os.environ.get("E2B_API_KEY", "")}

    async def _api(self, method: str, path: str, json_body: dict | None = None,
                   timeout: float = 30.0):
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, f"{API}{path}", headers=self._headers(),
                                json=json_body)
            return r

    async def _ping(self, sandbox_id: str):
        r = await self._api("POST", f"/sandboxes/{sandbox_id}/timeout",
                            {"timeout": 3600})
        r.raise_for_status()
        return r.status_code

    # ---------- reincarnation ----------

    async def _reincarnate(self, sandbox_id: str):
        log.info("⏳ 23h reached — reincarnating (backup → successor → handover)")
        # 1. backup brain + secrets
        try:
            from cogs.backup import run_backup
            summary = await asyncio.get_event_loop().run_in_executor(None, run_backup)
            log.info("pre-migration backup: %s", summary)
        except Exception:
            log.exception("pre-migration backup failed — migrating anyway")

        # 2. create the successor: same template, bootstrap envs (S3 creds so its
        #    start.sh can restore the brain + the .env secrets blob)
        bootstrap = {k: os.environ[k] for k in
                     ("S3_ENDPOINT", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
                      "E2B_API_KEY") if os.environ.get(k)}
        bootstrap["E2B_KEEPALIVE"] = "1"
        r = await self._api("POST", "/sandboxes", {
            "templateID": os.environ.get("E2B_TEMPLATE_ID", "inferolli"),
            "timeout": 3600,
            "autoPause": True,
            "envVars": bootstrap,
        }, timeout=60.0)
        if r.status_code not in (200, 201):
            log.error("successor creation FAILED: %s %s — staying alive as long as possible",
                      r.status_code, r.text[:120])
            return  # the keepalive loop continues; we die at the wall instead
        new_id = r.json().get("sandboxID") or r.json().get("sandboxId") or r.json().get("id")
        log.info("successor created: %s — waiting for its boot", new_id)

        # 3. wait for the successor to be up (restore + bot start)
        for _ in range(24):
            await asyncio.sleep(BOOT_WAIT_S // 24 + 2)
            try:
                check = await self._api("GET", f"/sandboxes/{new_id}", timeout=10.0)
                if check.status_code == 200:
                    break
            except Exception:
                continue

        # 4. handover: pause myself (state preserved — resume is always possible)
        log.info("handover complete — pausing myself")
        try:
            await self._api("POST", f"/sandboxes/{sandbox_id}/pause", timeout=15.0)
        except Exception:
            log.exception("self-pause failed (harmless — the wall will pause us)")

    # ---------- loop ----------

    async def _loop(self):
        await self.bot.wait_until_ready()

        sandbox_id = os.environ.get("E2B_SANDBOX_ID", "")
        if not sandbox_id:  # inside an e2b sandbox, the hostname is the sandbox id
            try:
                with open("/etc/hostname") as f:
                    sandbox_id = f.read().strip()
            except OSError:
                pass
        key = os.environ.get("E2B_API_KEY", "")
        if not (os.environ.get("E2B_KEEPALIVE", "") == "1" and sandbox_id and key):
            log.info("not in e2b keepalive mode (no E2B_KEEPALIVE/E2B_SANDBOX_ID) — skipping")
            return

        headers = {"X-API-KEY": key}
        async with httpx.AsyncClient(timeout=20.0) as client:
            while True:
                try:
                    r = await client.post(f"{API}/sandboxes/{sandbox_id}/timeout",
                                          headers=headers, json={"timeout": 3600})
                    r.raise_for_status()
                    log.info("keepalive ok — timeout extended to 1h")
                except Exception as e:
                    log.warning("keepalive ping failed: %s", e)

                age_h = (time.time() - self.started) / 3600
                if age_h >= LIFETIME_H:
                    # E2B's 24h lifetime wall is coming. The OFFICIAL pattern:
                    # pause+resume resets the runtime window. So: back up the
                    # brain, STOP pinging, and let the lifecycle (on_timeout:
                    # pause) freeze us at the wall — the shepherd touches the
                    # paused sandbox within 15 min and we RESUME with a fresh
                    # 24h window, process intact. No rebuild, no restore.
                    try:
                        from cogs.backup import run_backup
                        summary = await asyncio.get_event_loop().run_in_executor(
                            None, run_backup)
                        log.info("pre-pause backup: %s", summary)
                    except Exception:
                        log.exception("pre-pause backup failed — pausing anyway")
                    log.info("24h wall approaching — backing up done, letting the "
                             "sandbox pause; the shepherd will resume us (fresh window)")
                    return

                await asyncio.sleep(PING_EVERY)


async def setup(bot: commands.Bot):
    await bot.add_cog(KeepAliveCog(bot))
