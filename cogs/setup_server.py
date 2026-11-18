"""Server setup commands — the main event.

/setup quickstart applies a full community template (categories, channels,
roles, and permission overwrites). Finer-grained commands create individual
roles/channels, wire the Muted role, and lock/unlock channels.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from helpers import is_admin

log = logging.getLogger("cogs.setup")

MUTED_ROLE_NAME = "Muted"

# role presets: name -> (colour, key permissions beyond defaults)
ROLE_PRESETS = {
    "mod": (discord.Colour.red(), discord.Permissions(
        manage_messages=True, manage_channels=True, moderate_members=True,
        kick_members=True, view_channel=True, send_messages=True)),
    "member": (discord.Colour.blurple(), discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True,
        add_reactions=True, embed_links=True, connect=True, speak=True)),
}


def _category_overwrites(guild: discord.Guild, muted: discord.Role | None) -> dict:
    everyone = guild.default_role
    return {
        everyone: discord.PermissionOverwrite(send_messages=None, add_reactions=None),
        muted: discord.PermissionOverwrite(send_messages=False, add_reactions=False, speak=False, connect=False),
    } if muted else {everyone: discord.PermissionOverwrite(send_messages=None)}


def _readonly_overwrite(guild: discord.Guild) -> dict:
    return {guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False)}


class SetupGroup(app_commands.Group):
    """All /setup subcommands. Admin-only."""

    def __init__(self):
        super().__init__(name="setup", description="Set up the server")
        self.__original_description__ = self.description

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and is_admin(interaction.user):
            return True
        await interaction.response.send_message("You need **administrator** permissions to use this.", ephemeral=True)
        return False

    # ---------------- quickstart ----------------

    @app_commands.command(name="quickstart", description="Apply a full community template: categories, channels, roles, permissions")
    @app_commands.describe(
        server_name="Name for the general community category (optional)",
    )
    async def quickstart(self, interaction: discord.Interaction, server_name: str | None = None):
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(ephemeral=True)
        created, skipped = [], []

        async def get_or_create_role(name, **kwargs) -> discord.Role | None:
            role = discord.utils.get(guild.roles, name=name)
            if role:
                skipped.append(f"role #{name}")
                return role
            try:
                role = await guild.create_role(name=name, reason="quickstart")
                created.append(f"role @{name}")
                return role
            except discord.Forbidden:
                skipped.append(f"role @{name} (missing perms)")
                return None

        async def get_or_create_category(name, overwrites=None) -> discord.CategoryChannel | None:
            cat = discord.utils.get(guild.categories, name=name)
            if cat:
                skipped.append(f"category {name}")
                return cat
            try:
                cat = await guild.create_category(name, overwrites=overwrites, reason="quickstart")
                created.append(f"category {name}")
                return cat
            except discord.Forbidden:
                skipped.append(f"category {name} (missing perms)")
                return None

        async def get_or_create_channel(name, cat, kind, **kwargs):
            existing = discord.utils.get(cat.text_channels if kind == discord.ChannelType.text else cat.voice_channels, name=name)
            if existing:
                skipped.append(f"channel #{name}")
                return existing
            try:
                ch = await cat.create_text_channel(name, **kwargs) if kind == discord.ChannelType.text else await cat.create_voice_channel(name, **kwargs)
                created.append(f"channel #{name}")
                return ch
            except discord.Forbidden:
                skipped.append(f"channel #{name} (missing perms)")
                return None

        muted = await get_or_create_role(MUTED_ROLE_NAME)
        mod_role = await get_or_create_role("Mod")
        await get_or_create_role("Member")

        info_cat = await get_or_create_category(server_name or "INFORMATION")
        text_cat = await get_or_create_category("TEXT CHANNELS", _category_overwrites(guild, muted))
        voice_cat = await get_or_create_category("VOICE CHANNELS")

        if info_cat:
            await get_or_create_channel("welcome", info_cat, discord.ChannelType.text, overwrites=_readonly_overwrite(guild))
            await get_or_create_channel("announcements", info_cat, discord.ChannelType.text, overwrites=_readonly_overwrite(guild))
            await get_or_create_channel("rules", info_cat, discord.ChannelType.text, overwrites=_readonly_overwrite(guild))
        if text_cat:
            await get_or_create_channel("general", text_cat, discord.ChannelType.text)
            await get_or_create_channel("off-topic", text_cat, discord.ChannelType.text)
            await get_or_create_channel("bot-commands", text_cat, discord.ChannelType.text)
        if voice_cat:
            await get_or_create_channel("General Voice", voice_cat, discord.ChannelType.voice)

        # Wire the Muted role across every channel/category that lacks an explicit Muted overwrite.
        if muted:
            wired = 0
            for target in list(guild.categories) + list(guild.text_channels) + list(guild.voice_channels):
                if muted in target.overwrites:
                    continue
                try:
                    await target.set_permissions(muted, send_messages=False, add_reactions=False, speak=False, reason="quickstart: mute role")
                    wired += 1
                except discord.Forbidden:
                    break
            if wired:
                created.append(f"mute overwrites on {wired} channels")

        if mod_role and info_cat:
            for target in (info_cat,):
                try:
                    await target.set_permissions(mod_role, send_messages=True, manage_messages=True, reason="quickstart: mod access")
                except discord.Forbidden:
                    pass

        lines = []
        if created:
            lines.append("**Created:** " + ", ".join(created))
        if skipped:
            lines.append("**Skipped (already existed / no perms):** " + ", ".join(skipped))
        lines.append("\nNext: `/config automod` to turn on AI moderation, `/config proactive` for community vibes.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ---------------- individual pieces ----------------

    @app_commands.command(name="role", description="Create a role")
    @app_commands.describe(name="Role name", preset="Permission preset to base it on")
    @app_commands.choices(preset=[
        app_commands.Choice(name="mod", value="mod"),
        app_commands.Choice(name="member", value="member"),
        app_commands.Choice(name="empty", value="empty"),
    ])
    async def role_cmd(self, interaction: discord.Interaction, name: str, preset: str = "empty"):
        guild = interaction.guild
        assert guild
        await interaction.response.defer(ephemeral=True)
        if discord.utils.get(guild.roles, name=name):
            await interaction.followup.send(f"Role **@{name}** already exists.", ephemeral=True)
            return
        kwargs = {}
        if preset in ROLE_PRESETS:
            colour, perms = ROLE_PRESETS[preset]
            kwargs = {"colour": colour, "permissions": perms}
        try:
            r = await guild.create_role(name=name, reason=f"/setup role by {interaction.user}", **kwargs)
            await interaction.followup.send(f"Created {r.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I need **Manage Roles** and the new role must be below my top role.", ephemeral=True)

    @app_commands.command(name="channel", description="Create a channel, optionally inside a category")
    @app_commands.describe(name="Channel name", kind="Channel type", category="Category to nest it under (created if missing)")
    @app_commands.choices(kind=[
        app_commands.Choice(name="text", value="text"),
        app_commands.Choice(name="voice", value="voice"),
        app_commands.Choice(name="forum", value="forum"),
    ])
    async def channel_cmd(self, interaction: discord.Interaction, name: str, kind: str = "text", category: str | None = None):
        guild = interaction.guild
        assert guild
        await interaction.response.defer(ephemeral=True)

        parent = None
        if category:
            parent = discord.utils.get(guild.categories, name=category)
            if parent is None:
                try:
                    parent = await guild.create_category(category, reason=f"/setup channel by {interaction.user}")
                except discord.Forbidden:
                    await interaction.followup.send("I need **Manage Channels** to create that category.", ephemeral=True)
                    return

        kwargs = {"reason": f"/setup channel by {interaction.user}"}
        try:
            if kind == "text":
                ch = await guild.create_text_channel(name, category=parent, **kwargs)
            elif kind == "voice":
                ch = await guild.create_voice_channel(name, category=parent, **kwargs)
            else:
                ch = await guild.create_forum(name, category=parent, **kwargs)
            await interaction.followup.send(f"Created {ch.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I need **Manage Channels** for that.", ephemeral=True)

    @app_commands.command(name="lock", description="Make a channel read-only for @everyone")
    async def lock_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        guild = interaction.guild
        assert guild
        target = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        try:
            await target.set_permissions(guild.default_role, send_messages=False, reason=f"/setup lock by {interaction.user}")
            await interaction.followup.send(f"Locked {target.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I need **Manage Channels** for that.", ephemeral=True)

    @app_commands.command(name="unlock", description="Restore send permission in a channel")
    async def unlock_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        guild = interaction.guild
        assert guild
        target = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        try:
            await target.set_permissions(guild.default_role, send_messages=True, reason=f"/setup unlock by {interaction.user}")
            await interaction.followup.send(f"Unlocked {target.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I need **Manage Channels** for that.", ephemeral=True)


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.tree.add_command(SetupGroup())

    async def cog_unload(self):
        # find and remove the group so reloads don't stack duplicates
        for cmd in list(self.bot.tree.get_commands()):
            if isinstance(cmd, SetupGroup):
                self.bot.tree.remove_command(cmd.name)


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))
