"""Seed starting messages into the themed channels (skips non-empty channels).

Mix of regular markdown and embeds, per channel purpose.
Run: .venv/bin/python scripts/seed_channels.py
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

IP_BLUE = 0x5B8DEF

# channel name -> list of sends; ("md", text) plain markdown, ("embed", Embed)
SEEDS: dict[str, list] = {
    "📜│code-of-the-crew": [
        ("embed", discord.Embed(
            title="📜 Code of the Crew",
            description=(
                "Short, seaworthy, and enforced by the Automaton.\n\n"
                "**1.** Keep it shipshape — no harassment, slurs, or targeting anyone.\n"
                "**2.** No spam, no crash attempts, no phishing links. The automod deletes and times out.\n"
                "**3.** Ship your own cargo — label affiliate/self-promo as such.\n"
                "**4.** Keep secrets seaworthy — no leaked keys, tokens, or others' personal data.\n"
                "**5.** Disputes? Ping the ⚓ Captains, not the whole crew.\n\n"
                "*Repeat offenders ride the bilge (timeout → walk the plank → ban).*"
            ),
            colour=IP_BLUE,
        ).set_footer(text="⚡ InferencePort AI · community rules")),
    ],
    "🌊│main-deck": [
        ("md", "🌊 **Welcome to the main deck.** Pull up a barrel, introduce yourself —\n\n"
               "• what are you building?\n"
               "• what models are you running?\n"
               "• cloud, local, or both?"),
        ("embed", discord.Embed(
            description="💡 **Tip:** mention <@1570591606795542638> with any InferencePort question — "
                        "it answers straight from the official docs, with citations. "
                        "Use `/support` for a docs-grounded deep dive.",
            colour=discord.Colour.blurple())),
    ],
    "🏖️│the-shallows": [
        ("md", "🏖️ **The shallows — off-topic waters.**\n"
               "Memes, hot takes, photos of your GPU rig running hot. Anything goes (within the code of the crew). "
               "Swim carefully — the automod patrols here too. 🦈"),
    ],
    "🖼️│cargo-showcase": [
        ("md", "🖼️ **Show us the cargo.**\n"
               "Built something with the API? Ship it here.\n\n"
               "**Format:**\n"
               "1. what it is (one line)\n"
               "2. link / screenshots\n"
               "3. what models powered it\n\n"
               "Outstanding builds get ⭐ — and ⭐⭐⭐ launches you to the starboard. 🏅"),
        ("embed", discord.Embed(
            title="🏆 Starboard rules",
            description="React with ⭐ to nominate a post. **3 stars** and it sails to <#"
                        "{starboard}>.",
            colour=discord.Colour.gold()),
         ),
    ],
    "🔌│p2g-terminal": [
        ("md", "🔌 **Pay-2-Go terminal — the credit-metered API.**\n"
               "Base URL: `https://api.inferenceport.ai/v1` · Bearer auth · OpenAI-compatible."),
        ("embed", discord.Embed(
            title="🚀 Your first call",
            description="```bash\ncurl https://api.inferenceport.ai/v1/chat/completions \\\n"
                        "  -H \"Authorization: Bearer $IP_API_KEY\" \\\n"
                        "  -H \"Content-Type: application/json\" \\\n"
                        "  -d '{\"model\": \"gpt-6-luna\", \"messages\": [{\"role\": \"user\", \"content\": \"ahoy\"}]}'\n"
                        "```\n"
                        "**Handy endpoints**\n"
                        "• `GET /v1/models` — full catalog + pricing\n"
                        "• `POST /v1/embeddings` — embeddings\n"
                        "• `POST /v1/systemone` — decision models\n"
                        "• `GET /v1/me` — wallet & usage\n\n"
                        "📚 Full reference: <https://docs.inferenceport.ai/en/latest/api/p2g-api.html>",
            colour=IP_BLUE).set_footer(text="credits metered per token · balance via /v1/me")),
    ],
    "⚡│lightning-lab": [
        ("md", "⚡ **Lightning lab** — for the fast stuff.\n"
               "Lightning Text v2 streams up to **1,000 words/second** through the same OpenAI-compatible "
               "endpoint (`model: \"lightning\"` is the auto-router). \n\n"
               "Show off benchmarks, latency screenshots, or your streaming integrations. "
               "Use `/models lightning` to see current variants and pricing. ⚡"),
    ],
    "🎲│decision-forge": [
        ("embed", discord.Embed(
            title="🎲 Decision forge — System One",
            description="Classify, score and evaluate **typed questions with probabilities** — not free-text guesses.\n\n"
                        "```json\n{\n"
                        "  \"model\": \"microsoft/decision-1\",\n"
                        "  \"state\": \"I was charged twice.\",\n"
                        "  \"questions\": {\n"
                        "    \"team\": {\"type\": \"choice\",\n"
                        "              \"criteria\": {\"billing\": \"charges & refunds\",\n"
                        "                             \"support\": \"how-to questions\"}}\n"
                        "  }\n}\n```\n"
                        "`POST /v1/systemone` · from $0.042/M input tokens\n\n"
                        "📚 <https://docs.inferenceport.ai/en/latest/api/decision-api.html>",
            colour=0x9B59B6).set_footer(text="chat-style routing, triage, scoring — with confidence values")),
    ],
    "🧩│agent-shipyard": [
        ("md", "🧩 **Agent shipyard** — for hosted agents and the Council.\n\n"
               "**Managed agents** — host an agent on your prompt, model and MCP tools, call it like a model.\n"
               "**Council MCP** — a council of models cross-reviewing into one answer, inside any MCP client.\n\n"
               "Yard talk: architectures, tool wiring, evals, and what your agents got up to this week. ⚓"),
    ],
    "🛡️│shield-watch": [
        ("embed", discord.Embed(
            title="🛡️ Shield watch — AI Shield",
            description="One API for **fraud, abuse, bot, prompt-attack, account-takeover, "
                        "web-attack, upload and data-leak** detection.\n\n"
                        "`POST /ai-shield/check` · `/analyze` · `/scan` · `/detect-phishing`\n\n"
                        "Discuss integrations, false positives, and threat stories here.\n"
                        "📚 <https://docs.inferenceport.ai/en/latest/api/shield.html>",
            colour=0xE67E22).set_footer(text="defend the harbor 🌊")),
    ],
    "📡│model-market": [
        ("embed", discord.Embed(
            title="📡 Model market — live feed",
            description="This channel tracks <https://api.inferenceport.ai/v1/models> — the full catalog "
                        "with live pricing.\n\n"
                        "• **New models** post automatically\n"
                        "• **Price changes** post automatically\n"
                        "• Removals too — so you know before your prod breaks\n\n"
                        "Use `/models <search>` for an on-demand lookup, e.g. `/models luna`.",
            colour=discord.Colour.gold()).set_footer(text="checks every 6 hours · prices per million tokens")),
    ],
    "🧯│troubleshoot": [
        ("embed", discord.Embed(
            title="🧯 Before you ask — the three W's",
            description="**What** happened (exact error, status code)\n"
                        "**What you expected**\n"
                        "**What you've tried**\n\n"
                        "Include: model id, request (minus your API key — never share keys!), and "
                        "`GET /v1/me` output if it's a credits issue.\n\n"
                        "💡 The bot can often answer instantly: `@` it with the error or use `/support`.",
            colour=0x3498DB)),
    ],
    "🐛│bug-reports": [
        ("md", "🐛 **Bug reports** — help the harpoon hit the target:\n\n"
               "```\n**What:** <short summary>\n**Repro:** <steps / request>\n**Expected:** <…>\n"
               "**Actual:** <…>\n**When:** <date + time>\n```\n"
               "Confirmed bugs get escalated to the ⚓ Captains. Please redact API keys. 🔒"),
    ],
    "💡│feature-requests": [
        ("md", "💡 **Feature requests** — dream big, ship bigger.\n"
               "One idea per thread, use-case first, API-second. Upvote with 👍 — the bridge reads this deck. 🚢"),
    ],
    "⭐│starboard": [
        ("md", "⭐ **The starboard.** Posts that collect **3+ ⭐** reactions sail here automatically. "
               "Gold posts only. 🏅"),
    ],
    "🤖│bot-commands": [
        ("embed", discord.Embed(
            title="🤖 Automaton handbook",
            description=(
                "**For everyone**\n"
                "• `@Inferolli <question>` — docs-grounded answers with citations\n"
                "• `/support <question>` — same, on demand\n"
                "• `/models <search>` — live catalog + pricing\n"
                "• `/ask <question>` — one-off AI question\n\n"
                "**For admins**\n"
                "• `/setup quickstart` — rebuild the channel layout\n"
                "• `/config automod on` · `/config proactive on` · `/config show`\n"
                "• `/announce <title> <text>` — styled + pinned announcement\n"
                "• `/pin <link>` — pin any message · `/config modlog|starboard|models <channel>`"
            ),
            colour=discord.Colour.blurple(),
        ).set_footer(text="fails open · admin commands are administrator-only")),
    ],
    "📒│mod-log": [
        ("md", "📒 **Mod log starts here.** AutoMod actions (deletions, timeouts, escalations) get posted "
               "with reasons and strike counts. Silence here is a good sign. ⚓"),
    ],
}


class Seeder(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.posted, self.skipped, self.errors = [], [], []

    async def on_ready(self):
        try:
            guild = self.guilds[0]
            print(f"guild: {guild.name}")
            starboard_id = None
            cfg_file = Path(__file__).parent.parent / "data" / "config.json"
            if cfg_file.exists():
                cfg = json.loads(cfg_file.read_text()).get(str(guild.id), {})
                starboard_id = cfg.get("starboard_channel_id")

            for chan_name, sends in SEEDS.items():
                chan = discord.utils.get(guild.text_channels, name=chan_name)
                if chan is None:
                    self.errors.append(f"channel missing: {chan_name}")
                    continue
                # skip channels that already have content
                try:
                    last = [m async for m in chan.history(limit=1)]
                    if last:
                        self.skipped.append(chan_name)
                        continue
                except discord.Forbidden:
                    self.errors.append(f"cannot read {chan_name}")
                    continue

                for kind, payload in sends:
                    if isinstance(payload, str):
                        text = payload.replace("{starboard}", str(starboard_id or ""))
                        await chan.send(text)
                    else:
                        if "{starboard}" in (payload.description or ""):
                            payload.description = payload.description.replace("{starboard}", str(starboard_id or ""))
                        await chan.send(embed=payload)
                    await asyncio.sleep(0.8)
                # pin the templates
                if chan_name in ("📜│code-of-the-crew", "🧯│troubleshoot", "🐛│bug-reports",
                                 "⭐│starboard", "🤖│bot-commands"):
                    try:
                        async for m in chan.history(limit=len(sends)):
                            await m.pin(reason="seed: template")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                self.posted.append(chan_name)

            print("\n=== SEED REPORT ===")
            print(f"posted in ({len(self.posted)}): {', '.join(self.posted)}")
            print(f"skipped non-empty: {', '.join(self.skipped) or 'none'}")
            if self.errors:
                print("errors:", "; ".join(self.errors))
        finally:
            await self.close()


if __name__ == "__main__":
    import json
    Seeder().run(TOKEN)
