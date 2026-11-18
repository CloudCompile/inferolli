"""REVERT: restore the original themed names everywhere, clear topics.

Run: .venv/bin/python scripts/revert_names.py
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

ORIGINAL = [
    ("✦・ᴛʜᴇ ǫᴜᴀʏ — sᴛᴀʀᴛ ʜᴇʀᴇ", [
        "🛳️│docking-instructions",
        "📜│code-of-the-crew",
        "📣│harbor-broadcast",
    ]),
    ("⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ", [
        "🌊│main-deck",
        "🏖️│the-shallows",
        "🖼️│cargo-showcase",
        "⭐│starboard",
    ]),
    ("🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ", [
        "🔌│p2g-terminal",
        "⚡│lightning-lab",
        "🎲│decision-forge",
        "🧩│agent-shipyard",
        "🛡️│shield-watch",
        "📡│model-market",
    ]),
    ("🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ", [
        "🧯│troubleshoot",
        "🐛│bug-reports",
        "💡│feature-requests",
    ]),
    ("🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ", [
        "🌊│ambient-static",
        "🎧│deep-water-focus",
    ]),
    ("🔒・ᴛʜᴇ ʜᴇʟᴍ", [
        "🤖│bot-commands",
        "📒│mod-log",
    ]),
]

# clarified/renamed -> original
RENAME_BACK = {
    "🛳️│start-here": "🛳️│docking-instructions",
    "📜│rules": "📜│code-of-the-crew",
    "📣│announcements": "📣│harbor-broadcast",
    "🌊│general-chat": "🌊│main-deck",
    "🖼️│showcase": "🖼️│cargo-showcase",
    "⚡│lightning-models": "⚡│lightning-lab",
    "🎲│decision-models": "🎲│decision-forge",
    "🧩│agents-council": "🧩│agent-shipyard",
    "🛡️│ai-shield": "🛡️│shield-watch",
    "🧯│tech-support": "🧯│troubleshoot",
    "🌊│hangout-voice": "🌊│ambient-static",
    "🎧│focus-voice": "🎧│deep-water-focus",
}

CAT_BACK = {
    "⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ — ᴄʜᴀᴛ": "⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ",
    "🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ — ᴀᴘɪ · ᴍᴏᴅᴇʟs": "🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ",
    "🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ — ʜᴇʟᴘ": "🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ",
    "🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ — ᴠᴏɪᴄᴇ": "🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ",
    "🔒・ᴛʜᴇ ʜᴇʟᴍ — sᴛᴀꜰꜰ": "🔒・ᴛʜᴇ ʜᴇʟᴍ",
}


class Reverter(discord.Client):
    async def on_ready(self):
        try:
            guild = self.guilds[0]
            print(f"guild: {guild.name}")

            # 1. channels back to original names
            for old, orig in RENAME_BACK.items():
                ch = discord.utils.get(guild.channels, name=old)
                if ch is None:
                    continue
                await ch.edit(name=orig, reason="revert: original theme")
                print(f"renamed back: {old} -> {orig}")
                await asyncio.sleep(0.7)

            # 2. categories back
            for old, orig in CAT_BACK.items():
                cat = discord.utils.get(guild.categories, name=old)
                if cat:
                    await cat.edit(name=orig, reason="revert: original theme")
                    print(f"category back: {orig}")
                    await asyncio.sleep(0.7)

            # 3. clear every topic (original theme had none)
            for ch in guild.channels:
                if getattr(ch, "topic", None):
                    try:
                        await ch.edit(topic=None, reason="revert: original theme")
                        print(f"topic cleared: {ch.name}")
                        await asyncio.sleep(0.7)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            # 4. report anything left unaccounted for outside archive
            original_names = {n for _, chans in ORIGINAL for n in chans} | {CAT_BACK[o] for o in CAT_BACK}
            stray = [ch.name for cat in guild.categories
                     if "ᴀʀᴄʜɪᴠᴇ" not in cat.name and cat.name in original_names
                     for ch in cat.channels if ch.name not in original_names]
            if stray:
                print("UNMATCHED channels in themed categories:", stray)
                print("-> tell me what each one is and I'll map it to an original name")
            print("revert complete.")
        finally:
            await self.close()


if __name__ == "__main__":
    Reverter(intents=discord.Intents.default()).run(TOKEN)
