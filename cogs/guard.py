"""The harbor watch — raid detection, lockdown, and onboarding roles.

* every new member instantly wears 🪵 Landlubber (the ladder exists — start
  at the bottom of it)
* join bursts are watched: if a cluster of joins lands close together, the
  decision model judges "is this a raid?" — a confident yes triggers
  lockdown (chat frozen server-wide, admins alerted), auto-lifted after 10
  minutes or by an admin's /unlock
"""

import logging
import time
from collections import deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import ai
from ai import AIError
import memory
from config import get as get_cfg
from helpers import send_mod_log

log = logging.getLogger("cogs.guard")

BURST_WINDOW_S = 90
BURST_ALERT_AT = 4        # decision model gets consulted at this many joins
LOCKDOWN_MINUTES = 10
LANDLUBBER = "🪵 Landlubber"


class GuardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.joins: deque = deque()          # (ts, user_id)
        self.locked_until: dict[int, float] = {}   # guild_id -> ts

    # ---------- raid watch ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # 1. onboarding: everyone starts at the bottom of the ladder
        try:
            from cogs.levels import ensure_level_roles
            roles = await ensure_level_roles(member.guild)
            start = roles.get(LANDLUBBER)
            if start:
                await member.add_roles(start, reason="onboarding — everyone starts at the bottom")
        except Exception as e:
            log.debug("onboarding role failed: %s", e)
        # 2. raid watch
        await self.check_join_burst(member)

    # ---------- commands ----------

    @app_commands.command(name="unlock", description="Lift a raid lockdown (admin)")
    async def unlock(self, interaction: discord.Interaction):
        ok = await self.lift_lockdown(interaction.guild, by=interaction.user)
        await interaction.response.send_message(
            "🔓 lockdown lifted." if ok else "no lockdown active.", ephemeral=True)

    # ---------- core ----------

    async def check_join_burst(self, member: discord.Member):
        guild = member.guild
        now = time.time()
        self.joins.append((now, member.id))
        recent = [t for t, _ in self.joins if now - t < BURST_WINDOW_S]
        if len(recent) < BURST_ALERT_AT or now < self.locked_until.get(guild.id, 0):
            return

        new_accounts = sum(1 for t, uid in self.joins
                           if now - t < BURST_WINDOW_S
                           and (m := guild.get_member(uid)) and m.created_at
                           and (now_ts(m) < now - 0) and fresh_account(m))
        state = (f"{len(recent)} members joined in the last {BURST_WINDOW_S}s "
                 f"({new_accounts} with accounts <2 days old). "
                 f"server has ~{len(guild.members)} members.")
        try:
            answers = await ai.decide(state[:600], {
                "raid": {"type": "choice", "instructions": (
                    "I watch this Discord server. Judge whether this join pattern is a raid "
                    "(bot wave / spam ring) worth locking chat down for 10 minutes. A slow "
                    "trickle of new humans is normal; a tight burst of fresh accounts is not."),
                         "criteria": {"yes": "raid — lock down", "no": "normal"}}},
                timeout=15.0)
            a = answers.get("raid", {})
            if not (a.get("choice") == "yes" and float(a.get("confidence", 0)) >= 0.75):
                return
        except AIError as e:
            log.debug("raid gate failed: %s", e)
            return

        await self.lockdown(guild, reason=f"{len(recent)} joins/{BURST_WINDOW_S}s, {new_accounts} fresh accounts")

    async def lockdown(self, guild: discord.Guild, reason: str):
        if time.time() < self.locked_until.get(guild.id, 0):
            return
        frozen = 0
        for ch in guild.text_channels:
            if ch.name in ("📒│mod-log",):
                continue
            over = ch.overwrites_for(guild.default_role)
            over.send_messages = False
            try:
                await ch.edit(overwrites=ch.overwrites | {guild.default_role: over})
                frozen += 1
            except discord.Forbidden:
                continue
        self.locked_until[guild.id] = time.time() + LOCKDOWN_MINUTES * 60
        e = discord.Embed(title="🔒 LOCKDOWN", colour=0xB00020,
                          description=f"raid suspected: {reason}\nchat frozen in {frozen} channels. "
                                      f"auto-lifts <t:{int(time.time()) + LOCKDOWN_MINUTES * 60}:R> "
                                      f"or with /unlock.")
        await send_mod_log(guild, await get_cfg(guild.id), e)

    async def lift_lockdown(self, guild: discord.Guild, by=None) -> bool:
        if not self.locked_until.pop(guild.id, None):
            return False
        for ch in guild.text_channels:
            over = ch.overwrites_for(guild.default_role)
            if over.send_messages is False:
                over.send_messages = None
                try:
                    await ch.edit(overwrites=ch.overwrites | {guild.default_role: over})
                except discord.Forbidden:
                    continue
        if by:
            log.info("lockdown lifted by %s", by)
        return True


def fresh_account(m: discord.Member) -> bool:
    if m.created_at is None:
        return False
    return (discord.utils.utcnow() - m.created_at).total_seconds() < 2 * 86400


def now_ts(m: discord.Member) -> float:
    return m.created_at.timestamp() if m.created_at else 0.0


async def setup(bot: commands.Bot):
    await bot.add_cog(GuardCog(bot))
