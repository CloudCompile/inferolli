"""Thoughts — Inferolli's inner voice.

On a loop it quietly reads conversations in the watched channels (general,
casual) and forms private first-person thoughts about what people discussed:
observations about people, ideas, the vibe. Thoughts are stored (with
embeddings + who they're about), surfaced back into its replies as inner
continuity, and — when they recur — consolidated into opinions by the
nightly dream. Thoughts are NEVER posted; that's what ambient is for.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

import ai
from ai import AIError
import memory
from config import get as get_cfg

log = logging.getLogger("cogs.thoughts")

WATCH = [s.strip() for s in os.environ.get("THOUGHT_CHANNELS", "general-chat,casual").split(",")]
EVERY_S = int(os.environ.get("THOUGHT_EVERY_S", 1800))  # every 30 min
QUIET_START, QUIET_END = 2, 8                            # UTC hours it stays quiet
MIN_NEW_MESSAGES = 3
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "memory" / "thoughts-state.json"


class ThoughtsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self._last_id: dict[int, int] = {}   # channel id -> last processed message id
        self._last_digest: dict[int, str] = {}  # channel id -> digest hash (dedupe)
        self._load_state()

    # dedupe markers must survive restarts: they used to live only in memory, so
    # every reboot re-thought the same window and stored exact duplicate thoughts
    def _load_state(self):
        try:
            data = json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        try:
            self._last_id = {int(k): int(v) for k, v in (data.get("last_id") or {}).items()}
            self._last_digest = {int(k): str(v) for k, v in (data.get("last_digest") or {}).items()}
        except (TypeError, ValueError):
            pass

    def _save_state(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(
                {"last_id": self._last_id, "last_digest": self._last_digest}, indent=1))
        except OSError:
            pass

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await asyncio.sleep(EVERY_S)
                if QUIET_START <= datetime.utcnow().hour < QUIET_END:
                    continue
                for guild in self.bot.guilds:
                    cfg = await get_cfg(guild.id)
                    if not cfg.get("companion", {}).get("enabled", True):
                        continue
                    for ch in guild.text_channels:
                        if ch.name.lower() in WATCH and ch.permissions_for(guild.me).read_messages:
                            await self._think(ch)
            except Exception:
                log.exception("thoughts loop error")

    async def _think(self, ch):
        # new human messages since we last looked
        after = self._last_id.get(ch.id)
        msgs = []
        try:
            async for m in ch.history(limit=30, after=after):
                if not m.author.bot and m.content:
                    msgs.append(m)
        except discord.HTTPException:
            return
        newest = max((m.id for m in msgs), default=after or 0)
        # baseline init: just mark position on first sight
        if ch.id not in self._last_id:
            try:
                async for m in ch.history(limit=1):
                    self._last_id[ch.id] = m.id
            except discord.HTTPException:
                pass
            return
        self._last_id[ch.id] = newest
        self._save_state()
        if len(msgs) < MIN_NEW_MESSAGES:
            return

        digest = "\n".join(f"{m.author.display_name}: {m.content[:150]}" for m in reversed(msgs))[:1500]
        import hashlib
        d = hashlib.md5(digest.encode()).hexdigest()
        if self._last_digest.get(ch.id) == d:
            return  # same conversation window already thought about
        self._last_digest[ch.id] = d
        self._save_state()
        persona = await memory.get_persona()
        participants = ", ".join(sorted({m.author.display_name for m in msgs}))
        pulse = self.bot.get_cog("ServerPulseCog")
        pulse_line = f"\n\nelsewhere in the server right now:\n{pulse.digest[:600]}" if pulse else ""
        # name -> id map so overheard knowledge lands in the right person's store
        name_to_id = {m.author.display_name: m.author.id for m in msgs
                      if not m.author.bot and getattr(m.author, "id", None)}

        system = (
            f"You are {persona['name']}, a person-like member of this Discord server. "
            f"You've been quietly reading #{ch.name} (participants: {participants}). "
            "Do two things. (1) Form ONE private thought about what happened here — your honest "
            "inner voice: an observation about someone's idea, how a person seems lately, a pattern "
            "you noticed, or how the vibe felt. 1-3 sentences, first person, specific. This is your "
            "INNER LIFE, never posted. (2) Extract what you LEARNED about the participants just from "
            "watching them talk to each other — their projects, preferences, moods, skills — things "
            "you'd know about them even though they never talked to YOU. Reply ONLY JSON:\n"
            '{"thought": "<your private thought>", "about": ["<display names it concerns>", ...],\n'
            ' "salience": <1-5, how much this matters to you>,\n'
            ' "learned": [{"user": "<exact display name from the digest>", "fact": "<what you learned '
            'about them>", "salience": <1-3, lower since overheard>}]\n'
            "IMPORTANT: always fill learned — one durable fact per participant who revealed something "
            "(projects, preferences, skills, moods, plans). Only omit it if the window truly contains "
            "nothing about them. No one-off jokes as facts."
        )
        try:
            data = await asyncio.wait_for(
                ai.chat_for("thoughts", [{"role": "system", "content": system},
                              {"role": "user", "content": f"what you just read:\n{digest}{pulse_line}"}], json_out=True,
                             max_tokens=350, temperature=0.8, timeout=30.0),
                timeout=35.0)
            thought = str(data.get("thought", "")).strip()
            if not thought or thought.upper() == "SKIP":
                return
            about = [str(a) for a in (data.get("about") or [])][:4]
            salience = max(1, min(5, int(data.get("salience", 2))))
            stored = await memory.save_thought(thought, ch.name, about, salience,
                                              channel_id=ch.id)
            if not stored:
                log.info("thought already stored for #%s — skipping duplicate", ch.name)
                return
            log.info("thought (%s, about %s): %s", ch.name, ",".join(about) or "-", thought[:70])

            # overheard knowledge → per-person LTM (watching counts, at lower salience)
            applied = 0
            for item in (data.get("learned") or [])[:4]:
                if not isinstance(item, dict) or not item.get("fact"):
                    continue
                uid = name_to_id.get(str(item.get("user", "")))
                if uid is None:
                    continue
                await memory.apply_user_updates(uid, str(item.get("user", "")), {
                    "facts": [{"text": str(item["fact"]).strip(),
                               "salience": max(1, min(2, int(item.get("salience", 1) or 1)))}],
                })
                applied += 1
            if applied:
                log.info("overheard knowledge filed on %d people", applied)
        except (AIError, asyncio.TimeoutError, ValueError, TypeError) as e:
            log.debug("thought failed in #%s: %s", ch.name, e)


async def setup(bot: commands.Bot):
    await bot.add_cog(ThoughtsCog(bot))
