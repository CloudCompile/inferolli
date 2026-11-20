"""Self-assign roles — a button picker in the roles channel.

Earned crew ranks live on the XP ladder and are NEVER pickable; this picker
is for the opt-in stuff — notification roles (pings, models, dreams) and
social roles (creator, builder, gamer). Members click a button to take or
drop a role, no admin involved.

Admin surface:
  /roles_post    — (re)post the picker in this channel; buttons heal on boot
  /roles_add     — make a role pickable (validated: no ranks, no staff, no
                   integrations, nothing above the bot in the hierarchy)
  /roles_remove  — pull a role out of the picker
  /roles         — list what's currently pickable

The pickable list and the picker message id live in the guild config, so a
restart re-attaches the view to the same message (custom_ids are stable, but
re-attaching also re-renders after role renames and repairs a deleted view).
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import memory
from config import get as get_cfg, update as update_cfg

log = logging.getLogger("cogs.roles")

MAX_PICKABLE = 25                    # discord component limit per message


def forbidden_role_reason(role, me_top) -> str | None:
    """Why this role can't go in the picker, or None if it can."""
    if role is None:
        return "no such role"
    if getattr(role, "managed", False):
        return "that role is managed by an integration (bots own it)"
    from cogs.levels import TIER_NAMES
    if role.name in set(TIER_NAMES):
        return "that's an earned crew rank — ranks aren't pickable, they're climbed"
    low = role.name.lower()
    if low in ("admin", "staff", "@everyone") or "mute" in low or "bilge" in low:
        return "that role is staff or infrastructure — not something to hand out"
    try:
        if role >= me_top:
            return "that role sits above me in the hierarchy — move my role up first"
    except TypeError:
        pass
    return None


class PickerButton(discord.ui.Button):
    def __init__(self, role_id: int, label: str):
        super().__init__(label=label[:80], style=discord.ButtonStyle.secondary,
                         custom_id=f"roles:toggle:{role_id}")
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if role is None:
            return await interaction.response.send_message(
                "that role has been deleted — an admin should run /roles_remove",
                ephemeral=True)
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="self-assign picker")
                await interaction.response.send_message(
                    f"dropped **{role.name}**", ephemeral=True)
            else:
                await member.add_roles(role, reason="self-assign picker")
                await interaction.response.send_message(
                    f"you've got **{role.name}** now", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "i can't assign that one — my role needs to sit above it",
                ephemeral=True)


class PickerView(discord.ui.View):
    def __init__(self, pickable: list):
        """pickable: [(role_id, label)] in picker order."""
        super().__init__(timeout=None)
        for role_id, label in pickable[:MAX_PICKABLE]:
            self.add_item(PickerButton(role_id, label))


def _picker_embed(guild: discord.Guild, labels: list) -> discord.Embed:
    e = discord.Embed(
        title="🎭 pick your roles",
        description=("Click to take a role, click again to drop it.\n\n"
                     + "\n".join(f"· **{l}**" for l in labels)
                     + "\n\nCrew ranks aren't here — those are earned on the XP ladder. "
                       "Try `/rank` to see where you stand."),
        colour=0x2EC4B6)
    e.set_footer(text="the buttons never expire — if one breaks, an admin can /roles_post to fix it")
    return e


async def _labels_for(guild: discord.Guild, role_ids: list) -> list:
    out = []
    for rid in role_ids:
        # config json round-trips ids as strings; get_role wants ints —
        # without the coercion every label became "(deleted role …)"
        role = guild.get_role(int(rid))
        out.append(role.name if role else f"(deleted role {rid})")
    return out


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # heal on boot: re-attach the picker view to its message in every guild
        asyncio.create_task(self._heal_all())

    async def _heal_all(self):
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self.render(guild)
            except Exception as e:
                log.warning("picker heal failed in guild %s: %s", guild.id, str(e)[:80])

    async def render(self, guild: discord.Guild) -> discord.Message | None:
        """Re-render the picker onto its stored message (reposts if deleted)."""
        cfg = await get_cfg(guild.id)
        chan_id = cfg.get("roles_channel_id")
        msg_id = cfg.get("picker_message_id")
        role_ids = [int(r) for r in (cfg.get("pickable_roles") or [])]
        if not chan_id or not msg_id or not role_ids:
            return None
        chan = guild.get_channel(chan_id)
        if chan is None or not chan.permissions_for(guild.me).send_messages:
            return None
        labels = await _labels_for(guild, role_ids)
        view = PickerView(list(zip(role_ids, labels)))
        embed = _picker_embed(guild, labels)
        try:
            msg = await chan.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            msg = await chan.send(embed=embed, view=view)
            await update_cfg(guild.id,
                             lambda c: c.update({"picker_message_id": msg.id}))
        self.bot.add_view(view, message_id=msg.id)   # persistent across restarts
        return msg

    # ---------- admin commands ----------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        import helpers
        return helpers.is_admin(interaction.user)
    @app_commands.command(name="roles_post",
                          description="Post the role picker in this channel (admin)")
    async def roles_post(self, interaction: discord.Interaction):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("admin only", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        role_ids = [int(r) for r in (await get_cfg(guild.id)).get("pickable_roles") or []]
        labels = await _labels_for(guild, role_ids)
        msg = await interaction.channel.send(embed=_picker_embed(guild, labels),
                                             view=PickerView(list(zip(role_ids, labels))))
        await update_cfg(guild.id, lambda c: c.update({
            "roles_channel_id": interaction.channel.id,
            "picker_message_id": msg.id}))
        await interaction.followup.send("picker posted — buttons live on this message now", ephemeral=True)

    @app_commands.command(name="roles_add",
                          description="Make a role self-assignable (admin)")
    @app_commands.describe(role="The role members will be able to give themselves")
    async def roles_add(self, interaction: discord.Interaction, role: discord.Role):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("admin only", ephemeral=True)
        problem = forbidden_role_reason(role, interaction.guild.me.top_role)
        if problem:
            return await interaction.response.send_message(problem, ephemeral=True)

        async def mutate(cfg):
            ids = cfg.setdefault("pickable_roles", [])
            if role.id in ids:
                return None
            if len(ids) >= MAX_PICKABLE:
                raise ValueError(f"the picker is full ({MAX_PICKABLE} buttons max)")
            ids.append(role.id)

        try:
            await update_cfg(interaction.guild.id, mutate)
        except ValueError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await self.render(interaction.guild)
        await interaction.response.send_message(
            f"**{role.name}** is now pickable", ephemeral=True)

    @app_commands.command(name="roles_remove",
                          description="Pull a role out of the picker (admin)")
    async def roles_remove(self, interaction: discord.Interaction, role: discord.Role):
        if not self._is_admin(interaction):
            return await interaction.response.send_message("admin only", ephemeral=True)

        async def mutate(cfg):
            ids = cfg.setdefault("pickable_roles", [])
            cfg["pickable_roles"] = [i for i in ids if i != role.id]
        await update_cfg(interaction.guild.id, mutate)
        await self.render(interaction.guild)
        await interaction.response.send_message(
            f"**{role.name}** is out of the picker", ephemeral=True)

    @app_commands.command(name="roles", description="What's currently self-assignable")
    async def roles_list(self, interaction: discord.Interaction):
        role_ids = [int(r) for r in (await get_cfg(interaction.guild.id)).get("pickable_roles") or []]
        labels = await _labels_for(interaction.guild, role_ids)
        body = "\n".join(f"· **{l}**" for l in labels) or "nothing yet — an admin can /roles_add some"
        await interaction.response.send_message(
            f"🎭 **self-assignable roles**\n{body}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))