"""Ping preferences — granular, role-based, opt-out-able.

Categories (each has a Discord role; drop the role = silence):
  🔔 announcements — server announcements
  💬 ambient       — the bot choosing to ping you in conversation starters (pref-only)
  ⚓ standings     — nightly crew standings + your promotions
  🌙 dreams        — the nightly dream landing in the diary
  🎧 music         — listening parties starting
  📡 models        — model catalog updates

`/pings <kind> <on|off>` manages both the pref AND the role for you.
Direct replies + reminders always come through (you asked).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import memory

log = logging.getLogger("cogs.pings")

ROLE_NAMES = {
    "announcements": "🔔 pings",
    "standings": "⚓ standings",
    "dreams": "🌙 dreams",
    "music": "🎧 music",
    "models": "📡 models",
}
DEFAULT_PINGS = {"announcements": True, "ambient": True, "standings": True,
                 "dreams": True, "music": True, "models": True}


async def ensure_role(guild: discord.Guild, category: str) -> discord.Role | None:
    name = ROLE_NAMES.get(category)
    if not name:
        return None
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        try:
            role = await guild.create_role(name=name, mentionable=True,
                                           reason=f"pings opt-in ({category})")
        except discord.Forbidden:
            return None
    return role


async def mention_for(guild: discord.Guild, category: str) -> dict:
    """The mention string + allowed_mentions for a category's ping."""
    role = await ensure_role(guild, category)
    if role:
        return {"mention": role.mention,
                "allowed": discord.AllowedMentions(everyone=False, roles=True, users=False)}
    return {"mention": "", "allowed": discord.AllowedMentions.none()}


async def can_ambient_ping(user_id: int) -> bool:
    mem = await memory.get_user(user_id)
    return bool(mem.get("pings", {}).get("ambient", True))


class PingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="pings", description="Control what the bot pings you for")
    @app_commands.describe(kind="which pings", state="on or off")
    @app_commands.choices(kind=[
        app_commands.Choice(name="🔔 announcements", value="announcements"),
        app_commands.Choice(name="💬 ambient (bot pings you in chat)", value="ambient"),
        app_commands.Choice(name="⚓ nightly crew standings", value="standings"),
        app_commands.Choice(name="🌙 the nightly dream", value="dreams"),
        app_commands.Choice(name="🎧 listening parties", value="music"),
        app_commands.Choice(name="📡 model catalog updates", value="models"),
    ])
    @app_commands.choices(state=[
        app_commands.Choice(name="on (ping me)", value="on"),
        app_commands.Choice(name="off (stop pinging me)", value="off"),
    ])
    async def pings(self, interaction: discord.Interaction,
                    kind: app_commands.Choice[str], state: app_commands.Choice[str]):
        uid = interaction.user.id
        mem = await memory.get_user(uid, interaction.user.display_name)
        on = state.value == "on"
        mem.setdefault("pings", dict(DEFAULT_PINGS))
        mem["pings"][kind.value] = on
        await memory.save_user(uid, mem)

        note = ""
        role = await ensure_role(interaction.guild, kind.value)
        if role:
            try:
                if on and role not in interaction.user.roles:
                    await interaction.user.add_roles(role, reason="pings opt-in")
                    note = f" (gave you the {role.name} role)"
                elif not on and role in interaction.user.roles:
                    await interaction.user.remove_roles(role, reason="pings opt-out")
                    note = f" (removed the {role.name} role)"
            except discord.Forbidden:
                note = " (couldn't touch your role — drag it above mine or toggle manually)"

        await interaction.response.send_message(
            f"{'🔔' if on else '🔕'} {kind.name}: **{state.value}**{note}", ephemeral=True)

    @app_commands.command(name="pings_status", description="See your ping settings")
    async def pings_status(self, interaction: discord.Interaction):
        mem = await memory.get_user(interaction.user.id)
        p = mem.get("pings", {})
        lines = []
        for cat in ("announcements", "ambient", "standings", "dreams", "music", "models"):
            role = discord.utils.get(interaction.guild.roles, name=ROLE_NAMES.get(cat, ""))
            wearing = f" (role worn)" if (role and role in interaction.user.roles) else ""
            lines.append(f"{'🔔' if p.get(cat, True) else '🔕'} {cat}: "
                         f"**{'on' if p.get(cat, True) else 'off'}**{wearing}")
        await interaction.response.send_message(
            "\n".join(lines) + "\n\n*direct replies + your own reminders always come through.*",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PingsCog(bot))
