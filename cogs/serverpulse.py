"""Server pulse — the bot's ambient awareness of the ENTIRE server.

Every few minutes it walks the recent history of every text channel and
caches a compact digest ("what's happening everywhere right now"). That
digest is injected into the companion's prompts, the thoughts loop, and
the dream — so any reply can reference anything happening anywhere.

Also batches embeddings onto new STM entries so cross-channel semantic
recall stays fresh.
"""

import asyncio
import logging
import os
import time

from discord.ext import commands

import memory

log = logging.getLogger("cogs.serverpulse")

EVERY_S = int(os.environ.get("PULSE_EVERY_S", 180))
MSG_PER_CHANNEL = 3
MAX_CHANNELS = 10
DIGEST_CAP = 1800
SKIP = {"starboard", "bot-commands", "mod-log", "dream-diary", "announcements",
        "rules", "legal-disclaimer", "listening-post"}


class ServerPulseCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self.digest = "(the server is quiet right now)"
        self.updated = 0.0

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("pulse refresh failed")
            await asyncio.sleep(EVERY_S)

    async def refresh(self):
        for guild in self.bot.guilds:
            lines = []
            chans = [c for c in guild.text_channels
                     if c.name.lower() not in SKIP
                     and c.permissions_for(guild.me).read_messages]
            # freshest-activity channels first
            scored = []
            for ch in chans[:25]:
                try:
                    recent = []
                    async for m in ch.history(limit=MSG_PER_CHANNEL):
                        if m.author.bot or not m.content:
                            continue
                        recent.append(m)
                    if recent:
                        scored.append((recent[0].created_at.timestamp(), ch, recent))
                except Exception:
                    continue
            scored.sort(reverse=True)
            for ts, ch, recent in scored[:MAX_CHANNELS]:
                age_h = int((time.time() - ts) / 3600)
                body = " | ".join(f"{m.author.display_name}: {m.content[:110]}" for m in reversed(recent))
                lines.append(f"#{ch.name} ({age_h}h ago): {body}")
            if lines:
                self.digest = "\n".join(lines)[:DIGEST_CAP]
                self.updated = time.time()
            # batch-embed a few new STM entries for semantic recall
            n = await memory.embed_stm_entries()
            if n:
                log.debug("embedded %d stm entries", n)
            break  # single-guild bot

    def block(self) -> str:
        age_min = int((time.time() - self.updated) / 60) if self.updated else 999
        return (f"THE WHOLE SERVER RIGHT NOW (every channel, refreshed {age_min} min ago):\n"
                f"{self.digest}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerPulseCog(bot))
