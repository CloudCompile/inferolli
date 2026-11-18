"""Mod tools — the classic toolkit, with persistent case files.

/warn /mute /unmute /kick /ban /purge /lock /case  — all admin-only, all
logged to the mod-log, all recorded in a persistent case file per member
(survives restarts — unlike the old in-memory strikes). /case asks the AI
to summarize a member's history in plain words.
"""

import asyncio
import json
import logging
import time
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import ai
from ai import AIError
from helpers import is_admin, send_mod_log

log = logging.getLogger("cogs.modtools")

CASES_FILE = Path(__file__).parent.parent / "data" / "memory" / "cases.json"


def _load_cases() -> dict:
    try:
        return json.loads(CASES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def record_case(guild_id: int, user_id: int, kind: str, reason: str, by: str) -> dict:
    store = _load_cases()
    guild_cases = store.setdefault(str(guild_id), {})
    user_cases = guild_cases.setdefault(str(user_id), [])
    case = {"ts": time.time(), "kind": kind, "reason": reason[:300], "by": by}
    user_cases.append(case)
    guild_cases[str(user_id)] = user_cases[-100:]
    tmp = CASES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, ensure_ascii=False))
    tmp.replace(CASES_FILE)
    return case


def member_cases(guild_id: int, user_id: int, days: int = 30) -> list[dict]:
    store = _load_cases()
    cutoff = time.time() - days * 86400
    return [c for c in store.get(str(guild_id), {}).get(str(user_id), [])
            if c["ts"] >= cutoff]


def _target(guild: discord.Guild, who: str) -> discord.Member | None:
    uid = "".join(ch for ch in who if ch.isdigit())
    if uid:
        m = guild.get_member(int(uid))
        if m:
            return m
    cand = [m for m in guild.members if who.lower().lstrip("@") in m.display_name.lower()]
    return cand[0] if len(cand) == 1 else None


def _can_touch(interaction: discord.Interaction, member: discord.Member) -> str | None:
    if member.bot:
        return "that's a bot"
    if is_admin(member):
        return "they're an admin — this bot doesn't touch admins"
    if member.top_role >= interaction.user.top_role and interaction.user.id != interaction.guild.owner_id:
        return "they're above or level with you in the role ladder"
    return None


class ModToolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _ack(self, interaction, text: str, eph=True):
        # works whether or not the command already deferred
        if interaction.response.is_done():
            return interaction.followup.send(text, ephemeral=eph)
        return interaction.response.send_message(text, ephemeral=eph)

    @app_commands.command(name="warn", description="Warn a member (recorded, counts toward escalation)")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if bad := _can_touch(interaction, member):
            return await self._ack(interaction, bad)
        record_case(interaction.guild_id, member.id, "warn", reason, interaction.user.display_name)
        n = len(member_cases(interaction.guild_id, member.id))
        try:
            await member.send(f"⚠️ you've been warned in **{interaction.guild.name}**: {reason} "
                              f"(warning {n})")
        except discord.HTTPException:
            pass
        await send_mod_log(interaction.guild, await _cfg(), discord.Embed(
            title="⚠️ warning", colour=0xE8A33D,
            description=f"{member.mention} warned by {interaction.user.mention}: {reason} (#{n})"))
        await self._ack(interaction, f"warned {member.display_name} (#{n}).")

    @app_commands.command(name="mute", description="Timeout a member")
    async def mute(self, interaction: discord.Interaction, member: discord.Member,
                   minutes: app_commands.Range[int, 1, 40320], reason: str = "no reason given"):
        if bad := _can_touch(interaction, member):
            return await self._ack(interaction, bad)
        await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes),
                             reason=f"by {interaction.user}: {reason}")
        record_case(interaction.guild_id, member.id, f"mute {minutes}m", reason,
                    interaction.user.display_name)
        await send_mod_log(interaction.guild, await _cfg(), discord.Embed(
            title="🔇 muted", colour=0x8C6D2B,
            description=f"{member.mention} muted {minutes}m by {interaction.user.mention}: {reason}"))
        await self._ack(interaction, f"muted {member.display_name} for {minutes}m.")

    @app_commands.command(name="unmute", description="Remove a timeout")
    async def unmute(self, interaction: discord.Interaction, member: discord.Member):
        await member.timeout(None)
        record_case(interaction.guild_id, member.id, "unmute", "", interaction.user.display_name)
        await self._ack(interaction, f"unmuted {member.display_name}.")

    @app_commands.command(name="kick", description="Kick a member")
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if bad := _can_touch(interaction, member):
            return await self._ack(interaction, bad)
        await member.kick(reason=f"by {interaction.user}: {reason}")
        record_case(interaction.guild_id, member.id, "kick", reason, interaction.user.display_name)
        await send_mod_log(interaction.guild, await _cfg(), discord.Embed(
            title="🥾 kicked", colour=0xE06C00,
            description=f"{member.mention} kicked by {interaction.user.mention}: {reason}"))
        await self._ack(interaction, f"kicked {member.display_name}.")

    @app_commands.command(name="ban", description="Ban a member")
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str,
                  delete_days: app_commands.Range[int, 0, 7] = 1):
        if bad := _can_touch(interaction, member):
            return await self._ack(interaction, bad)
        await member.ban(delete_message_days=delete_days,
                         reason=f"by {interaction.user}: {reason}")
        record_case(interaction.guild_id, member.id, "ban", reason, interaction.user.display_name)
        await send_mod_log(interaction.guild, await _cfg(), discord.Embed(
            title="⛔ banned", colour=0xB00020,
            description=f"{member.mention} banned by {interaction.user.mention}: {reason} "
                        f"(deleted {delete_days}d of messages)"))
        await self._ack(interaction, f"banned {member.display_name}.")

    @app_commands.command(name="purge", description="Bulk delete recent messages in this channel")
    async def purge(self, interaction: discord.Interaction,
                    count: app_commands.Range[int, 1, 100],
                    member: discord.Member | None = None):
        ch = interaction.channel
        deleted = 0
        ids = []
        async for m in ch.history(limit=min(count * 3 if member else count, 300)):
            if m.author.bot and m.id != interaction.application_id:
                if member is None:
                    continue  # don't nuke the bot's own messages unless purging everything
            if member and m.author.id != member.id:
                continue
            if m.pinned:
                continue
            ids.append(m.id)
            deleted += 1
            if deleted >= count:
                break
        if ids:
            await ch.delete_messages(ids)
        record_case(interaction.guild_id, member.id if member else 0, "purge",
                    f"{deleted} msgs in #{ch.name}", interaction.user.display_name)
        await self._ack(interaction, f"deleted {deleted} message(s).")

    @app_commands.command(name="lock", description="Freeze this channel (admins keep access)")
    async def lock(self, interaction: discord.Interaction):
        over = interaction.channel.overwrites_for(interaction.guild.default_role)
        over.send_messages = False
        await interaction.channel.edit(overwrites=interaction.channel.overwrites
                                       | {interaction.guild.default_role: over})
        record_case(interaction.guild_id, interaction.channel.id, "lock",
                    interaction.channel.name, interaction.user.display_name)
        await self._ack(interaction, "🔒 channel frozen (admins keep access).")

    @app_commands.command(name="lockopen", description="Unfreeze this channel")
    async def lockopen(self, interaction: discord.Interaction):
        over = interaction.channel.overwrites_for(interaction.guild.default_role)
        over.send_messages = None
        await interaction.channel.edit(overwrites=interaction.channel.overwrites
                                       | {interaction.guild.default_role: over})
        await self._ack(interaction, "channel open again.")

    @app_commands.command(name="case", description="A member's case file — with an AI summary")
    async def case(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)  # the AI summary can take ~20s
        cases = member_cases(interaction.guild_id, member.id)
        r = await memory_get_rank(member.id)
        if not cases and not r.get("history"):
            return await self._ack(interaction, f"{member.display_name} — clean record.")
        e = discord.Embed(title=f"🗂️ case file — {member.display_name}", colour=0x586289)
        recent = "\n".join(
            f"`{c['ts'] and __import__('datetime').datetime.fromtimestamp(c['ts']).strftime('%m/%d')}` "
            f"{c['kind']}: {c['reason'][:80]} ({c['by']})" for c in cases[-8:][::-1])
        e.add_field(name=f"mod cases ({len(cases)} in 30d)", value=recent or "none", inline=False)
        if r.get("history"):
            rh = "\n".join(f"`{h['date']}` {h['tier']} ({h['score']})" for h in r["history"][-5:][::-1])
            e.add_field(name="standings history", value=rh[:1024], inline=False)
        summary = ""
        try:
            summary = await ai.chat_for("reflect", [
                {"role": "system", "content": (
                    "You keep the crew records of this Discord server. In 2 short lowercase sentences, "
                    "summarize this member's recent record — trajectory (improving/trouble), pattern, "
                    "and one honest note for the mods. Plain text.")},
                {"role": "user", "content": json.dumps(cases[-8:], default=str)[:800]},
            ], max_tokens=80, temperature=0.5, timeout=20.0)
            summary = summary.strip().strip('"')
        except (AIError, asyncio.TimeoutError):
            pass
        if summary:
            e.add_field(name="the read", value=f"*“{summary[:500]}”*", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)


async def _cfg():
    from config import DEFAULTS
    return DEFAULTS


async def memory_get_rank(uid: int):
    try:
        import memory
        return await memory.get_rank(uid)
    except Exception:
        return {}


async def setup(bot: commands.Bot):
    await bot.add_cog(ModToolsCog(bot))
