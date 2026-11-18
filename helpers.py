"""Shared helpers used by multiple cogs."""

import time

import discord

import config


def is_admin(member: discord.Member) -> bool:
    """Admin-only: setup and config commands require actual Administrator perms."""
    return member.guild_permissions.administrator


# --- anti-storm send guard: no two AMBIENT bot messages in one channel this close ---
SEND_GUARD_S = 12
_last_bot_send: dict[int, float] = {}


def may_send(channel_id: int) -> bool:
    return time.monotonic() - _last_bot_send.get(channel_id, 0) > SEND_GUARD_S


def mark_sent(channel_id: int) -> None:
    _last_bot_send[channel_id] = time.monotonic()


async def send_mod_log(guild: discord.Guild, cfg: dict, embed: discord.Embed) -> None:
    """Best-effort post to the configured mod-log channel (silently skips if unset)."""
    channel_id = cfg.get("mod_log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel and channel.permissions_for(guild.me).send_messages:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass
