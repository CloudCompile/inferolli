"""Admin commands: turn automod/proactive on and off, set log channels, /ask."""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

import ai
from ai import AIError
from config import get as get_cfg
from config import update as update_cfg
import memory
import rag
from helpers import is_admin

log = logging.getLogger("cogs.admin")


def moderator_only():
    async def check(interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and is_admin(interaction.user):
            return True
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return False
    return app_commands.check(check)


class ConfigGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="config", description="Bot settings for this server")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and is_admin(interaction.user):
            return True
        await interaction.response.send_message("You need **administrator** permissions to use this.", ephemeral=True)
        return False

    @app_commands.command(name="show", description="Show current bot settings")
    async def show(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        cfg = await get_cfg(interaction.guild.id)
        am = cfg["automod"]
        companion_on = cfg.get("companion", {}).get("enabled", True)

        def chan(cid):
            if not cid:
                return "*not set*"
            c = interaction.guild.get_channel(cid)
            return c.mention if c else f"*deleted ({cid})*"

        await interaction.response.send_message(
            "**AutoMod:** " + ("🟢 ON" if am["enabled"] else "🔴 off") +
            f" — delete on low: `{am['delete_on_low']}`, timeout on high: `{am['timeout_on_high']}` ({am['timeout_seconds']}s)\n"
            "**Companion:** " + ("🟢 ON — I'm around, chatting like a person" if companion_on else "🔴 off") + "\n"
            f"**Mod log:** {chan(cfg['mod_log_channel_id'])}\n"
            f"**Welcome channel:** {chan(cfg['welcome_channel_id'])}\n"
            f"**Starboard:** {chan(cfg['starboard_channel_id'])} (threshold {cfg.get('starboard_threshold', 3)}⭐)\n"
            f"**Model feed:** {chan(cfg['models_channel_id'])}\n"
            f"**Dreams:** {chan(cfg['dreams_channel_id']) or '*private (set with /config dreams)*'}",
            ephemeral=True,
        )

    @app_commands.command(name="automod", description="Turn AI automod on or off")
    @app_commands.describe(on="Whether automod is enabled")
    async def automod(self, interaction: discord.Interaction, on: bool):
        assert interaction.guild is not None
        await update_cfg(interaction.guild.id, lambda c: c["automod"].__setitem__("enabled", on))
        await interaction.response.send_message(f"AutoMod is now **{'ON' if on else 'OFF'}**.", ephemeral=True)

    @app_commands.command(name="companion", description="Turn person-mode (memory + opinions + proactive chatting) on or off")
    @app_commands.describe(on="Whether the companion personality is enabled")
    async def companion(self, interaction: discord.Interaction, on: bool):
        assert interaction.guild is not None
        await update_cfg(interaction.guild.id, lambda c: c["companion"].__setitem__("enabled", on))
        await interaction.response.send_message(
            f"Companion mode is now **{'ON' if on else 'OFF'}** "
            + ("— I'll be around. ⚓" if on else "— going quiet."), ephemeral=True)

    @app_commands.command(name="forget", description="Make the bot forget everything about a user (admin)")
    @app_commands.describe(user="Whose memory to wipe")
    async def forget(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild is not None
        p = memory.USER_DIR / f"{user.id}.json"
        if p.exists():
            p.unlink()
            await interaction.response.send_message(f"Forgotten everything about {user.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No memories of {user.mention} found.", ephemeral=True)

    @app_commands.command(name="starboard", description="Set the ⭐ starboard channel and threshold")
    @app_commands.describe(channel="Where starred posts land (leave empty to disable)", threshold="Stars needed (default 3)")
    async def starboard(self, interaction: discord.Interaction, channel: discord.TextChannel | None,
                        threshold: app_commands.Range[int, 1, 25] | None = None):
        assert interaction.guild is not None

        def mutate(c):
            c["starboard_channel_id"] = channel.id if channel else None
            if threshold is not None:
                c["starboard_threshold"] = threshold

        await update_cfg(interaction.guild.id, mutate)
        await interaction.response.send_message(
            f"Starboard {'set to ' + channel.mention if channel else 'disabled'}"
            + (f" at {threshold} ⭐" if threshold else "") + ".", ephemeral=True)

    @app_commands.command(name="models", description="Set the channel for model-catalog updates (new models, pricing changes)")
    async def models(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        assert interaction.guild is not None
        channel_id = channel.id if channel else None
        await update_cfg(interaction.guild.id, lambda c: c.__setitem__("models_channel_id", channel_id))
        await interaction.response.send_message(
            f"Model-feed channel {'set to ' + channel.mention if channel else 'cleared'}.", ephemeral=True)

    @app_commands.command(name="dreams", description="Set where the nightly dream gets posted (leave empty to keep dreams private)")
    async def dreams(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        assert interaction.guild is not None
        channel_id = channel.id if channel else None
        await update_cfg(interaction.guild.id, lambda c: c.__setitem__("dreams_channel_id", channel_id))
        await interaction.response.send_message(
            f"Dreams will now be {'posted to ' + channel.mention if channel else 'kept private'}.", ephemeral=True)

    @app_commands.command(name="levels", description="Set where the nightly crew standings get posted")
    async def levels(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        assert interaction.guild is not None
        channel_id = channel.id if channel else None
        await update_cfg(interaction.guild.id, lambda c: c.__setitem__("levels_channel_id", channel_id))
        await interaction.response.send_message(
            f"Crew standings will post to {channel.mention if channel else 'the dream diary fallback'}.",
            ephemeral=True)

    @app_commands.command(name="modlog", description="Set the channel where automod actions are logged")
    async def modlog(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        assert interaction.guild is not None
        channel_id = channel.id if channel else None
        await update_cfg(interaction.guild.id, lambda c: c.__setitem__("mod_log_channel_id", channel_id))
        await interaction.response.send_message(
            f"Mod log {'set to ' + channel.mention if channel else 'cleared'}.", ephemeral=True)

    @app_commands.command(name="welcome", description="Set the channel for greeting new members")
    async def welcome(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        assert interaction.guild is not None
        channel_id = channel.id if channel else None
        await update_cfg(interaction.guild.id, lambda c: c.__setitem__("welcome_channel_id", channel_id))
        await interaction.response.send_message(
            f"Welcome channel {'set to ' + channel.mention if channel else 'cleared'}.", ephemeral=True)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.tree.add_command(ConfigGroup())

    async def cog_unload(self):
        for cmd in list(self.bot.tree.get_commands()):
            if isinstance(cmd, ConfigGroup):
                self.bot.tree.remove_command(cmd.name)

    @app_commands.command(name="ask", description="Ask the AI a one-off question")
    @app_commands.describe(question="What to ask")
    async def ask(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        try:
            answer = await ai.chat_for("rag", 
                [{"role": "user", "content": question[:2000]}],
                max_tokens=800,
                timeout=45.0,
            )
            await interaction.followup.send(answer[:1900])
        except AIError as e:
            await interaction.followup.send(f"AI request failed: {e}")

    @app_commands.command(name="support", description="Ask an InferencePort AI question, answered from their docs")
    @app_commands.describe(question="Your question about the InferencePort API, models, console, CLI, etc.")
    async def support(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer()
        try:
            answer = await rag.grounded_answer(question[:1500])
            await interaction.followup.send(answer[:1900])
        except AIError as e:
            await interaction.followup.send(f"Docs lookup failed: {e}")

    @app_commands.command(name="announce", description="Post a styled announcement and pin it")
    @app_commands.describe(title="Headline", text="Body (markdown ok)",
                           channel="Target channel (default: an announcements/broadcast channel)",
                           ping="who gets pinged (default: the 🔔 pings role)")
    @app_commands.choices(ping=[
        app_commands.Choice(name="🔔 pings role (opt-out-able)", value="pings"),
        app_commands.Choice(name="@everyone (use sparingly)", value="everyone"),
        app_commands.Choice(name="no ping", value="none"),
    ])
    @moderator_only()
    async def announce(self, interaction: discord.Interaction, title: str, text: str,
                       channel: discord.TextChannel | None = None,
                       ping: app_commands.Choice[str] | None = None):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        target = channel or discord.utils.find(
            lambda c: "announc" in c.name.lower() and c.permissions_for(interaction.guild.me).send_messages,
            interaction.guild.text_channels) or interaction.channel

        from cogs.pings import announce_target
        mt = await announce_target(interaction.guild, ping.value if ping else "pings")
        content = mt["content_mention"]

        embed = discord.Embed(
            title=f"📣 {title}",
            description=text[:4000],
            colour=discord.Colour.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"announced by {interaction.user.display_name}")
        try:
            msg = await target.send(content=content, embed=embed, allowed_mentions=mt["allowed"])
            await msg.pin(reason=f"/announce by {interaction.user}")
            await interaction.followup.send(f"Announced and pinned in {target.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I need **Send Messages** (+ Manage Messages to pin) there.", ephemeral=True)

    @app_commands.command(name="pin", description="Pin a message by ID or link")
    @app_commands.describe(message="Message ID or discord.com link of the message to pin")
    @moderator_only()
    async def pin(self, interaction: discord.Interaction, message: str):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        m = re.search(r"(?:channels/)?(\d{15,})/(\d{15,})(?:/(\d{15,}))?", message)
        if m and m.lastgroup is None and len(m.groups()) >= 3:
            ch_id, msg_id = int(m.group(2)), int(m.group(3))
        else:
            ch_id, msg_id = interaction.channel.id, int(m.group(0)) if m else None
        if msg_id is None:
            await interaction.followup.send("Give me a message ID or a full discord.com message link.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(ch_id) or interaction.channel
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.pin(reason=f"/pin by {interaction.user}")
            await interaction.followup.send("📌 Pinned.", ephemeral=True)
        except (discord.NotFound, discord.HTTPException) as e:
            await interaction.followup.send(f"Couldn't pin that: {e}", ephemeral=True)

    @app_commands.command(name="ping", description="Check the bot is alive")
    async def ping(self, interaction: discord.Interaction):
        latency = self.bot.latency * 1000
        await interaction.response.send_message(f"🏓 {latency:.0f} ms", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
