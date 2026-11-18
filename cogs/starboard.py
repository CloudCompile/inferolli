"""Starboard: ⭐ a great post enough times and it sails to #starboard."""

import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands

from config import update as update_cfg
from helpers import is_admin

log = logging.getLogger("cogs.starboard")

STAR_EMOJI = "⭐"
STORE = Path(os.environ.get("BOT_DATA_DIR", Path(__file__).parent.parent / "data")) / "starboard.json"


def _posted() -> set[int]:
    if STORE.exists():
        return set(json.loads(STORE.read_text()))
    return set()


def _mark(message_id: int) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    ids = _posted()
    ids.add(message_id)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    tmp.replace(STORE)


class StarboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or str(payload.emoji) != STAR_EMOJI:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        from config import get as get_cfg
        cfg = await get_cfg(guild.id)
        channel_id = cfg.get("starboard_channel_id")
        threshold = int(cfg.get("starboard_threshold", 3))
        if not channel_id or payload.channel_id == channel_id:
            return
        if payload.message_id in _posted():
            return

        channel = guild.get_channel(payload.channel_id)
        message = None
        if channel:
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.NotFound, discord.HTTPException):
                return
        if message is None or message.author.bot:
            return

        star_count = sum(r.count for r in message.reactions if str(r.emoji) == STAR_EMOJI)
        if star_count < threshold:
            return

        board = guild.get_channel(channel_id)
        if board is None or not board.permissions_for(guild.me).send_messages:
            return

        embed = discord.Embed(
            description=(message.content or "*no text*")[:2000],
            colour=discord.Colour.gold(),
            title=f"⭐ {star_count} · #{getattr(message.channel, 'name', 'unknown')}",
        )
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="", value=f"[jump to message]({message.jump_url})")
        if message.attachments:
            embed.set_image(url=message.attachments[0].url)

        await board.send(embed=embed)
        _mark(message.id)
        try:
            await message.add_reaction("🏅")  # marked as sailed
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StarboardCog(bot))
