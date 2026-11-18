"""State of the ship — the bot's Sunday review of its own server.

Built entirely from what it already collects: the week's dreams, the crew
standings, the pulse digests and its 7-day archive. Posted to
📣│announcements every Sunday at 18:00 UTC — with one genuine suggestion
from its own opinions about what the server should try next.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

import ai
from ai import AIError
import memory
from config import get as get_cfg

log = logging.getLogger("cogs.shiplog")

CHECK_EVERY_S = 15 * 60
SUNDAY, POST_HOUR_UTC = 6, 18
DREAMS_FILE = Path(__file__).parent.parent / "data" / "memory" / "dreams.json"


class ShipLogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self._last_posted_week = None

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(CHECK_EVERY_S)
            now = datetime.utcnow()
            if now.weekday() != SUNDAY or now.hour != POST_HOUR_UTC:
                continue
            week = now.isocalendar()[1]
            if self._last_posted_week == week:
                continue
            try:
                for guild in self.bot.guilds:
                    await self.post_state_of_ship(guild)
                self._last_posted_week = week
            except Exception:
                log.exception("state of the ship failed")

    async def post_state_of_ship(self, guild: discord.Guild):
        cfg = await get_cfg(guild.id)
        target = (guild.get_channel(cfg.get("levels_channel_id") or 0)
                  or guild.get_channel(cfg.get("dreams_channel_id"))
                  or discord.utils.get(guild.text_channels, name="📣│announcements"))
        if target is None:
            return

        # material: dreams of the week
        try:
            dreams = json.loads(DREAMS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            dreams = []
        now = time.time()
        week_dreams = [d for d in dreams if now - d.get("ts", 0) < 7 * 86400]
        dream_lines = "\n".join(f"- {d['dream'][:140]}" for d in week_dreams) or "(none)"

        # material: standings
        ranks = await memory.all_ranks()
        top = sorted(ranks.items(), key=lambda kv: -kv[1].get("score", 0))[:5]
        top_lines = "\n".join(f"- <@{uid}> — {r['tier']} ({r['score']:.0f})" for uid, r in top) or "(no standings yet)"

        # material: activity from the archive
        entries = await memory.stm_window(hours=24 * 7)
        per_channel = {}
        for e in entries:
            per_channel[e["channel"]] = per_channel.get(e["channel"], 0) + 1
        busiest = max(per_channel, key=per_channel.get) if per_channel else "nowhere"
        total = len(entries)
        humans = len({e["user_id"] for e in entries})

        # its own suggestion: one thing to try next
        suggestion = ""
        try:
            suggestion = await ai.chat_for("soul", [
                {"role": "system", "content": (
                    "You are Inferolli, a person-like member of this server writing its weekly "
                    "ship report. End it with ONE suggestion — a genuine, small thing the server "
                    "should try next week (an event, a channel habit, a fix). One sentence, "
                    "first person, specific to what actually happened this week. No headers.")},
                {"role": "user", "content": (
                    f"busiest channel: #{busiest} ({total} messages from {humans} people this week)\n"
                    f"the week's dreams:\n{dream_lines[:900]}\n"
                    f"top crew: {', '.join(n for _, n in [(u, r) for u, r in top][:5])}")},
            ], max_tokens=120, temperature=0.9, timeout=30.0)
            suggestion = suggestion.strip().strip('"')[:300]
        except (AIError, asyncio.TimeoutError):
            pass

        e = discord.Embed(
            title="🚢 state of the ship — week recap",
            description=(
                f"**the week in numbers:** {total} messages from {humans} people; "
                f"busiest channel: #{busiest}\n\n"
                f"**dreams I had this week:**\n{dream_lines[:800]}\n\n"
                f"**top crew right now:**\n{top_lines}\n\n"
                + (f"**what we should try next:** {suggestion}" if suggestion else "")),
            colour=0x2B6D8C)
        e.set_footer(text="written from my own memory — every word of it I lived")
        await target.send(embed=e)
        log.info("state of the ship posted to #%s", target.name)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShipLogCog(bot))
