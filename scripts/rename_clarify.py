"""Clarify pass: surgical renames + category subtitles + channel topics.
Keeps the theme; makes every channel's purpose obvious at a glance.

Run: .venv/bin/python scripts/rename_clarify.py
"""

import asyncio
import os
from pathlib import Path

import discord

for _line in (Path(__file__).parent.parent / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]

RENAMES = {  # old -> new (theme kept, function obvious)
    "🛳️│docking-instructions": "🛳️│start-here",
    "📜│code-of-the-crew": "📜│rules",
    "📣│harbor-broadcast": "📣│announcements",
    "🌊│main-deck": "🌊│general-chat",
    "🏖️│the-shallows": "🏖️│off-topic",
    "🖼️│cargo-showcase": "🖼️│showcase",
    "⚡│lightning-lab": "⚡│lightning-models",
    "🎲│decision-forge": "🎲│decision-models",
    "🧩│agent-shipyard": "🧩│agents-council",
    "🛡️│shield-watch": "🛡️│ai-shield",
    "🌊│ambient-static": "🌊│hangout-voice",
    "🎧│deep-water-focus": "🎧│focus-voice",
    "🧯│troubleshoot": "🧯│tech-support",
}

CAT_RENAMES = {
    "⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ": "⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ — ᴄʜᴀᴛ",
    "🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ": "🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ — ᴀᴘɪ · ᴍᴏᴅᴇʟs",
    "🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ": "🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ — ʜᴇʟᴘ",
    "🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ": "🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ — ᴠᴏɪᴄᴇ",
    "🔒・ᴛʜᴇ ʜᴇʟᴍ": "🔒・ᴛʜᴇ ʜᴇʟᴍ — sᴛᴀꜰꜰ",
}

TOPICS = {
    "🛳️│start-here": "New here? Server tour, key links, and how the bot works.",
    "📜│rules": "Community rules — one minute to read, enforced by the automod.",
    "📣│announcements": "Product updates and server news. Posted by admins.",
    "🌊│general-chat": "General chat — intros, day-to-day, anything friendly.",
    "🏖️│off-topic": "Non-InferencePort chatter. Memes welcome, sharks patrol.",
    "🖼️│showcase": "Show off what you've built with the API. 3+ ⭐ sails to the starboard.",
    "⭐│starboard": "Best posts of the server, chosen by ⭐ reactions.",
    "🔌│p2g-terminal": "Pay-2-Go API help — endpoints, auth, credits, streaming.",
    "⚡│lightning-models": "Lightning models — fast streaming, benchmarks, integration.",
    "🎲│decision-models": "Decision models / System One — classification with probabilities.",
    "🧩│agents-council": "Managed agents + Council MCP — building and wiring.",
    "🛡️│ai-shield": "AI Shield — fraud, abuse and prompt-attack detection.",
    "📡│model-market": "Live catalog feed: new models and pricing changes. /models to look anything up.",
    "🧯│tech-support": "Stuck? Ask here with the three W's: what / expected / actual.",
    "🐛│bug-reports": "Report bugs — use the template, redact API keys.",
    "💡│feature-requests": "Request features — one idea per thread, use-case first.",
    "🌊│hangout-voice": "Voice hangout — jump in, no agenda.",
    "🎧│focus-voice": "Work/focus room — cameras optional, silence respected.",
    "🤖│bot-commands": "Test bot commands here without cluttering chat.",
    "📒│mod-log": "AutoMod audit trail — deletions, timeouts, escalations.",
}


class Clarifier(discord.Client):
    async def on_ready(self):
        try:
            guild = self.guilds[0]
            me = guild.me
            print(f"guild: {guild.name}")

            for old, new in RENAMES.items():
                ch = discord.utils.get(guild.channels, name=old)
                if ch is None:
                    print(f"  ~ not found (already renamed?): {old}")
                    continue
                await ch.edit(name=new, reason="clarify pass")
                print(f"  renamed: {old} -> {new}")
                await asyncio.sleep(0.7)

            for old, new in CAT_RENAMES.items():
                cat = discord.utils.get(guild.categories, name=old)
                if cat is None:
                    print(f"  ~ category not found: {old}")
                    continue
                await cat.edit(name=new, reason="clarify pass")
                print(f"  renamed: {old} -> {new}")
                await asyncio.sleep(0.7)

            for name, topic in TOPICS.items():
                ch = discord.utils.get(guild.channels, name=name)
                if ch is None:
                    print(f"  !! topic target missing: {name}")
                    continue
                try:
                    await ch.edit(topic=topic, reason="clarify pass")
                    print(f"  topic: {name}")
                    await asyncio.sleep(0.7)
                except discord.Forbidden:
                    print(f"  !! no perms for topic: {name}")
        finally:
            await self.close()


if __name__ == "__main__":
    Clarifier(intents=discord.Intents.default()).run(TOKEN)
