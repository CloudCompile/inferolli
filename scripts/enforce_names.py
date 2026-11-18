"""Enforce canonical channel names + topics, replacing any manual renames.

Finds themed channels by keyword under ANY name, renames to canonical,
sets topics. Idempotent and safe to re-run.

Run: .venv/bin/python scripts/enforce_names.py
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

ARCHIVE_NAME = "🗄️・ᴀʀᴄʜɪᴠᴇ"

# canonical name -> (kind, match keywords, topic)
CHANNELS: dict[str, tuple[str, list[str], str]] = {
    "🛳️│start-here":     ("text",  ["docking", "start"],    "New here? Server tour, key links, and how the bot works."),
    "📜│rules":          ("text",  ["rules", "code-of"],    "Community rules — one minute to read, enforced by the automod."),
    "📣│announcements":  ("text",  ["broadcast", "announce"], "Product updates and server news. Posted by admins."),
    "🌊│general-chat":   ("text",  ["main-deck", "general-chat", "general"], "General chat — intros, day-to-day, anything friendly."),
    "🏖️│off-topic":      ("text",  ["shallows", "off-topic", "off"], "Non-InferencePort chatter. Memes welcome, sharks patrol."),
    "🖼️│showcase":       ("text",  ["showcase", "cargo"],   "Show off what you've built with the API. 3+ ⭐ sails to the starboard."),
    "⭐│starboard":      ("text",  ["starboard"],           "Best posts of the server, chosen by ⭐ reactions."),
    "🌙│dream-diary":    ("text",  ["dream-diary"],        "The automaton's nightly dreams — replayed, consolidated, written down."),
    "🔌│p2g-terminal":   ("text",  ["p2g"],                 "Pay-2-Go API help — endpoints, auth, credits, streaming."),
    "⚡│lightning-models": ("text", ["lightning"],           "Lightning models — fast streaming, benchmarks, integration."),
    "🎲│decision-models": ("text", ["decision"],            "Decision models / System One — classification with probabilities."),
    "🧩│agents-council": ("text",  ["agent"],               "Managed agents + Council MCP — building and wiring."),
    "🛡️│ai-shield":      ("text",  ["shield"],              "AI Shield — fraud, abuse and prompt-attack detection."),
    "📡│model-market":   ("text",  ["model-market"],        "Live catalog feed: new models and pricing changes. /models to look anything up."),
    "🧯│tech-support":   ("text",  ["troubleshoot", "tech-support"], "Stuck? Ask here with the three W's: what / expected / actual."),
    "🐛│bug-reports":    ("text",  ["bug"],                 "Report bugs — use the template, redact API keys."),
    "💡│feature-requests": ("text", ["feature"],            "Request features — one idea per thread, use-case first."),
    "🌊│hangout-voice":  ("voice", ["ambient", "hangout"],  "Voice hangout."),
    "🎧│focus-voice":    ("voice", ["deep-water", "focus"], "Work/focus room."),
    "🤖│bot-commands":   ("text",  ["bot-commands"],        "Test bot commands here without cluttering chat."),
    "📒│mod-log":        ("text",  ["mod-log"],             "AutoMod audit trail — deletions, timeouts, escalations."),
}

CAT_RENAMES = {
    "⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ": "⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ — ᴄʜᴀᴛ",
    "🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ": "🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ — ᴀᴘɪ · ᴍᴏᴅᴇʟs",
    "🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ": "🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ — ʜᴇʟᴘ",
    "🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ": "🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ — ᴠᴏɪᴄᴇ",
    "🔒・ᴛʜᴇ ʜᴇʟᴍ": "🔒・ᴛʜᴇ ʜᴇʟᴍ — sᴛᴀꜰꜰ",
}


class Enforcer(discord.Client):
    async def on_ready(self):
        try:
            guild = self.guilds[0]
            archive = discord.utils.get(guild.categories, name=ARCHIVE_NAME)
            archive_id = archive.id if archive else None

            # ---- categories ----
            for old, new in CAT_RENAMES.items():
                cat = discord.utils.get(guild.categories, name=old)
                if cat and cat.name != new:
                    await cat.edit(name=new, reason="clarify pass")
                    print(f"category: {old} -> {new}")
                    await asyncio.sleep(0.7)

            # ---- channels ----
            taken_ids: set[int] = set()

            def candidates(kind: str, keywords: list[str]):
                """Matching channels outside archive, newest-created first (our themed ones win)."""
                out = []
                for ch in guild.channels:
                    if ch.id in taken_ids or ch.id == archive_id or ch.category_id == archive_id:
                        continue
                    if (isinstance(ch, discord.VoiceChannel)) != (kind == "voice"):
                        continue
                    name_l = ch.name.lower()
                    if any(k in name_l for k in keywords):
                        out.append(ch)
                return sorted(out, key=lambda c: c.created_at, reverse=True)

            for canonical, (kind, keywords, topic) in CHANNELS.items():
                cands = candidates(kind, keywords)
                # exact canonical match first
                target = next((c for c in cands if c.name == canonical), cands[0] if cands else None)
                if target is None:
                    print(f"!! no candidate for {canonical} — would need creation")
                    continue
                taken_ids.add(target.id)
                if target.name != canonical:
                    await target.edit(name=canonical, reason="clarify pass: enforce")
                    print(f"renamed: {target.name} -> {canonical}")
                    await asyncio.sleep(0.7)
                # topic (voice channels have stricter word filters; fall back to short)
                try:
                    await target.edit(topic=topic, reason="clarify pass")
                except discord.HTTPException:
                    try:
                        await target.edit(topic=topic.split("—")[0].strip().split(".")[0] + ".",
                                          reason="clarify pass (short topic)")
                        print(f"topic (short): {canonical}")
                    except discord.HTTPException as e:
                        print(f"!! topic failed for {canonical}: {e}")
                else:
                    print(f"topic: {canonical}")
                await asyncio.sleep(0.7)

            print("done.")
        finally:
            await self.close()


if __name__ == "__main__":
    Enforcer(intents=discord.Intents.default()).run(TOKEN)
