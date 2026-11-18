"""One-shot server reorganization for the InferencePort AI community.

Idempotent: safe to re-run; existing things are skipped, 403s are tolerated.

- moves every manageable channel into the archive category, deletes emptied categories
- creates themed roles + permission setup, assigns Crew to all humans
- builds the themed channel layout (small-caps categories, emoji│name channels)
- rewires the bot's config (mod-log, welcome, starboard channels)
- posts a formatted welcome embed

Run: .venv/bin/python scripts/reorganize.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import discord

for _line in (Path(__file__).parent.parent / ".env").read_text().splitlines():
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
TOKEN = os.environ["DISCORD_BOT_TOKEN"]

ARCHIVE_NAME = "🗄️・ᴀʀᴄʜɪᴠᴇ"

# (name, colour, permissions, hoist)
ROLES = [
    ("⚓ Captain", discord.Colour.gold(), discord.Permissions(administrator=True), True),
    ("🧭 First Mate", discord.Colour.blurple(), discord.Permissions(
        manage_messages=True, moderate_members=True, kick_members=True,
        manage_threads=True, view_channel=True, send_messages=True), True),
    ("🚢 Crew", discord.Colour.teal(), discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True,
        add_reactions=True, embed_links=True, connect=True, speak=True,
        use_external_emojis=True, attach_files=True), True),
    ("🔬 Research Diver", discord.Colour.purple(), discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True), False),
    ("🔇 Bilged", discord.Colour.dark_gray(), discord.Permissions.none(), False),
]

# (category name, admin-only, readonly, channels: (name, kind))
LAYOUT = [
    ("✦・ᴛʜᴇ ǫᴜᴀʏ — sᴛᴀʀᴛ ʜᴇʀᴇ", False, True, [
        ("🛳️│docking-instructions", "text"),
        ("📜│code-of-the-crew", "text"),
        ("📣│harbor-broadcast", "text"),
    ]),
    ("⚓・ᴛʜᴇ ʜᴀʀʙᴏʀ", False, False, [
        ("🌊│main-deck", "text"),
        ("🏖️│the-shallows", "text"),
        ("🖼️│cargo-showcase", "text"),
        ("⭐│starboard", "text"),
        ("🌙│dream-diary", "text"),
    ]),
    ("🧠・ɪɴꜰᴇʀᴇɴᴄᴇ ᴅᴇᴄᴋ", False, False, [
        ("🔌│p2g-terminal", "text"),
        ("⚡│lightning-lab", "text"),
        ("🎲│decision-forge", "text"),
        ("🧩│agent-shipyard", "text"),
        ("🛡️│shield-watch", "text"),
        ("📡│model-market", "text"),
    ]),
    ("🛟・ᴅᴏᴄᴋsɪᴅᴇ sᴜᴘᴘᴏʀᴛ", False, False, [
        ("🧯│troubleshoot", "text"),
        ("🐛│bug-reports", "text"),
        ("💡│feature-requests", "text"),
    ]),
    ("🎧・sɪɢɴᴀʟ ᴛᴏᴡᴇʀ", False, False, [
        ("🌊│ambient-static", "voice"),
        ("🎧│deep-water-focus", "voice"),
    ]),
    ("🔒・ᴛʜᴇ ʜᴇʟᴍ", True, False, [
        ("🤖│bot-commands", "text"),
        ("📒│mod-log", "text"),
    ]),
]


def tol() -> None:
    print(".", end="", flush=True)


class Reorganizer(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.moved, self.created, self.errored = [], [], []

    async def on_ready(self):
        try:
            await self.run_all()
        finally:
            await self.close()

    async def safe(self, coro, label: str):
        try:
            return await coro
        except (discord.Forbidden, discord.HTTPException) as e:
            self.errored.append(f"{label}: {e}")
            print(f"\n!! {label}: {e}")
            return None

    async def run_all(self):
        guild = self.guilds[0]
        me = guild.me
        bot_role = me.top_role
        print(f"guild: {guild.name} ({guild.id}) | bot role: {bot_role.name}")

        # ---- 1. roles ------------------------------------------------------
        existing = {r.name: r for r in guild.roles}
        roles = dict(existing)
        bot_perms = me.guild_permissions
        for name, colour, perms, hoist in ROLES:
            role = existing.get(name)
            if role is None:
                # a bot cannot create a role holding perms it doesn't have itself —
                # intersect with ours and report anything dropped
                dropped = [n for n, v in perms if v and not getattr(bot_perms, n, False)] if perms else []
                safe_perms = discord.Permissions(**{n: v for n, v in perms if getattr(bot_perms, n, False)})
                role = await self.safe(
                    guild.create_role(name=name, colour=colour, permissions=safe_perms,
                                      hoist=hoist, reason="reorganize: theme roles"),
                    f"create role {name}")
                if role:
                    roles[name] = role
                    self.created.append(f"role {name}")
                    if dropped:
                        self.errored.append(f"role {name}: created WITHOUT {', '.join(dropped)} "
                                            f"(I don't hold them — grant manually if wanted)")
                    await asyncio.sleep(0.6)
        crew = roles.get("🚢 Crew")
        bilged = roles.get("🔇 Bilged")

        # give Crew to every human without a themed role yet
        themed = {roles[n] for n, *_ in ROLES if n in roles}
        if crew:
            async for member in guild.fetch_members(limit=None):
                if member.bot or any(themed & set(member.roles)):
                    continue
                await self.safe(member.add_roles(crew, reason="reorganize: default crew"),
                                f"crew -> {member.name}")
                await asyncio.sleep(0.4)

        # Muted role wiring: deny send/react/speak via channel overwrites
        if bilged:
            for target in list(guild.categories) + list(guild.channels):
                if isinstance(target, discord.CategoryChannel) or bilged not in target.overwrites:
                    await self.safe(
                        target.set_permissions(bilged, send_messages=False, add_reactions=False,
                                               speak=False, reason="reorganize: mute wiring"),
                        f"mute-wire {target.name}")

        # ---- 2. archive everything manageable ------------------------------
        archive = discord.utils.get(guild.categories, name=ARCHIVE_NAME)
        if archive is None:
            archive = await self.safe(guild.create_category(ARCHIVE_NAME), "create archive")
        layout_cat_names = {c[0] for c in LAYOUT} | {ARCHIVE_NAME}

        if archive:
            for ch in list(guild.channels):
                if ch.id == archive.id or ch.category_id == archive.id:
                    continue
                if isinstance(ch, discord.CategoryChannel):
                    continue
                if not ch.permissions_for(me).manage_channels or not ch.permissions_for(me).view_channel:
                    self.errored.append(f"cannot manage #{ch.name} — left in place")
                    continue
                await self.safe(ch.edit(category=archive, reason="reorganize: archive"),
                                f"archive {ch.name}")
                self.moved.append(ch.name)
                await asyncio.sleep(0.7)

            for cat in list(guild.categories):
                if cat.id == archive.id or cat.name in layout_cat_names:
                    continue
                if not cat.channels and cat.permissions_for(me).manage_channels:
                    await self.safe(cat.delete(reason="reorganize: emptied"), f"delete {cat.name}")

        # ---- 3. themed layout ----------------------------------------------
        for cat_name, admin_only, readonly, channels in LAYOUT:
            cat = discord.utils.get(guild.categories, name=cat_name)
            if cat is None:
                cat = await self.safe(guild.create_category(cat_name), f"create category {cat_name}")
                if cat is None:
                    continue
                self.created.append(cat_name)

            if admin_only:
                ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      bot_role: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
                await self.safe(cat.edit(overwrites=ow, reason="reorganize: staff-only"),
                                f"staff-ow {cat_name}")
            elif readonly:
                ow = {guild.default_role: discord.PermissionOverwrite(send_messages=False),
                      bot_role: discord.PermissionOverwrite(send_messages=True)}
                await self.safe(cat.edit(overwrites=ow, reason="reorganize: readonly zone"),
                                f"ro-ow {cat_name}")

            for name, kind in channels:
                existing_ch = discord.utils.get(guild.channels, name=name)
                if existing_ch:
                    continue
                if kind == "voice":
                    ch = await self.safe(cat.create_voice_channel(name, reason="reorganize: theme"), f"create {name}")
                else:
                    ch = await self.safe(cat.create_text_channel(name, reason="reorganize: theme"), f"create {name}")
                if ch:
                    self.created.append(name)
                    await asyncio.sleep(0.7)

        # ---- 4. rewire bot config ------------------------------------------
        cfg_file = Path(__file__).parent.parent / "data" / "config.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
        g = cfg.setdefault(str(guild.id), {})
        for cfg_key, chan_name in [("mod_log_channel_id", "📒│mod-log"),
                                   ("welcome_channel_id", "🛳️│docking-instructions"),
                                   ("starboard_channel_id", "⭐│starboard"),
                                   ("models_channel_id", "📡│model-market")]:
            ch = discord.utils.get(guild.text_channels, name=chan_name)
            if ch:
                g[cfg_key] = ch.id
        tmp = cfg_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        tmp.replace(cfg_file)

        # ---- 5. welcome embed ----------------------------------------------
        welcome = discord.utils.get(guild.text_channels, name="🛳️│docking-instructions")
        if welcome and welcome.permissions_for(me).send_messages:
            await self.safe(welcome.purge(limit=50, reason="reorganize: fresh start"), "purge welcome")
            embed = discord.Embed(
                title="🛳️ Welcome aboard — InferencePort AI",
                description=(
                    "One platform for cloud **and** local models — chat, images, video, audio, 3D, "
                    "embeddings, decision models and realtime voice behind one OpenAI-compatible API.\n\n"
                    "**ᴄʜᴀʀᴛ ᴏꜰ ᴛʜᴇ ꜱʜɪᴘ**\n"
                    "⚡ **Lightning** — text streaming up to 1,000 words/second\n"
                    "🔌 **Pay-2-Go** — credit-metered API at `api.inferenceport.ai/v1`\n"
                    "🎲 **Decisions** — typed questions with probabilities via `/v1/systemone`\n"
                    "🧩 **Agents & Council** — hosted agents, cross-reviewed answers\n"
                    "🛡️ **AI Shield** — fraud, abuse and prompt-attack detection\n\n"
                    "📚 Docs → <https://docs.inferenceport.ai>\n"
                    "🧭 Console → <https://console.inferenceport.ai>\n\n"
                    f"Mention {self.user.mention} anytime — it answers from the official docs, with citations. "
                    "Star ⭐ great posts to launch them to the starboard."
                ),
                colour=0x5B8DEF,
            )
            embed.set_footer(text="⚡ InferencePort AI · run models your way")
            msg = await self.safe(welcome.send(embed=embed), "welcome embed")
            if msg:
                await self.safe(msg.pin(reason="reorganize: keep visible"), "pin welcome")

        # ---- report ----------------------------------------------------------
        print("\n=== REPORT ===")
        print(f"archived ({len(self.moved)}): {', '.join(self.moved) or 'none'}")
        print(f"created ({len(self.created)}): {', '.join(self.created) or 'none'}")
        if self.errored:
            print("errors:")
            for e in self.errored:
                print(f"  ! {e}")


if __name__ == "__main__":
    Reorganizer().run(TOKEN)
