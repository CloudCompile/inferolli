"""Post the launch announcements to the broadcast channel.

Run: .venv/bin/python scripts/announce_launch.py
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

BOT_ID = 1570591606795542638
GOLD = 0xF1C40F
BLUE = 0x5B8DEF


def platform_embed(bot_mention: str) -> discord.Embed:
    e = discord.Embed(
        title="⚡ Welcome to InferencePort AI",
        description=(
            "One account. One console. **Cloud and local models.**\n"
            "InferencePort AI runs models on your own hardware, in the cloud, or both — "
            "all behind one OpenAI-compatible API.\n\n"
            "**ᴡʜᴀᴛ's ᴏɴ ᴅᴇᴄᴋ**\n"
            "⚡ **Lightning** — text streaming up to 1,000 words/second\n"
            "🔌 **Pay-2-Go API** — credit-metered: text, image, video, audio, 3D, embeddings, decisions, realtime\n"
            "🎲 **Decision models** — typed questions with probabilities via `/v1/systemone`\n"
            "🧩 **Managed agents & Council MCP** — hosted agents; a council of models cross-reviewing into one answer\n"
            "🛡️ **AI Shield** — fraud, abuse, prompt-attack and data-leak detection\n"
            "🧮 **Model marketplace** — 224+ models, live pricing\n\n"
            "**ʟɪɴᴋs**\n"
            "🌐 Platform → <https://inferenceport.ai>\n"
            "📚 Docs → <https://docs.inferenceport.ai>\n"
            "🧭 Console → <https://console.inferenceport.ai>\n"
            "🛒 Marketplace → <https://inferenceport.ai/marketplace.html>\n"
            "🖥️ Desktop app → <https://inferenceport.ai/install.html>\n"
            "❤️ Open source → <https://github.com/sharktide/inferenceport-ai>"
        ),
        colour=GOLD,
        url="https://inferenceport.ai",
    )
    e.set_footer(text="run models your way ⚡ · questions? mention the bot or use /support")
    return e


def bot_embed(bot_mention: str) -> discord.Embed:
    return discord.Embed(
        title=f"🤖 Say hello to Inferolli — {bot_mention}",
        description=(
            "Your docs-grounded first mate for this server.\n\n"
            "**ᴡʜᴀᴛ ɪᴛ ᴅᴏᴇs**\n"
            "📚 **Docs answers with citations** — mention it with any InferencePort question and it answers "
            "from the official docs, linking the pages it used. If the docs don't cover it, it says so — no guessing.\n"
            "🧮 **`/models <search>`** — the live model catalog with pricing, on demand.\n"
            "📡 **Model market feed** — new models and price changes post automatically in <#"
            "{model_market}>.\n"
            "⭐ **Starboard** — 3+ ⭐ launches a post to the hall of fame.\n"
            "🛡️ **AutoMod** — AI moderation with reasons, timeouts and a mod-log trail (admin-managed).\n\n"
            "**ᴛʀʏ ɪᴛ ɴᴏᴡ**\n"
            "• {mention} how do I authenticate to the P2G API?\n"
            "• `/models luna` · `/support how does credit billing work?`\n\n"
            "*Admins:* `/config show` to see the ship's settings, `/setup` to rebuild the deck."
        ),
        colour=BLUE,
    )


class Announcer(discord.Client):
    async def on_ready(self):
        try:
            guild = self.guilds[0]
            print(f"guild: {guild.name}")

            chan = discord.utils.get(guild.text_channels, name="📣│harbor-broadcast") \
                or discord.utils.get(guild.text_channels, name="📣│announcements")
            if chan is None:
                print("!! no announcements channel found")
                return

            model_market = discord.utils.get(guild.text_channels, name="📡│model-market")
            bot_mention = self.user.mention if self.user else "<@1570591606795542638>"

            e1 = platform_embed(bot_mention)
            e2 = bot_embed(bot_mention)
            if model_market:
                e2.description = e2.description.replace("{model_market}", str(model_market.id))
            e2.description = e2.description.replace("{mention}", bot_mention)

            md1 = "⚓ ⚡ **sᴛᴇᴀᴅʏ ᴀs sʜᴇ ɢᴏᴇs — ᴛʜᴇ ᴘᴏʀᴛ ɪs ᴏᴘᴇɴ!** ⚡ ⚓"
            md2 = "— · — · — · — · — · — · — · — · — · — · — · — · —"
            md3 = "🤖 **ᴀʟsᴏ ᴀʙᴏᴀʀᴅ: ᴛʜᴇ sʜɪᴘ's ᴏᴡɴ ᴀᴜᴛᴏᴍᴀᴛᴏɴ.**"

            await chan.send(md1)
            m1 = await chan.send(embed=e1)
            await chan.send(md2)
            await chan.send(md3)
            m2 = await chan.send(embed=e2)

            await m1.pin(reason="launch announcement")
            await m2.pin(reason="bot introduction")
            print("announcements posted + pinned.")
        finally:
            await self.close()


if __name__ == "__main__":
    Announcer(intents=discord.Intents.default()).run(TOKEN)
