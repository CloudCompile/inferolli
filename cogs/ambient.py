"""Ambient life — Inferolli choosing, by itself, to start conversations.

Not a random meter: on a loop, it looks at where humans are actually talking,
then asks the decision model (microsoft/decision-1) whether IT wants to start
something — as a person would ("it's been quiet, I have a thought, Chris is
around and this is their thing"). With a high bar, caps, and quiet hours,
because a person with good social sense doesn't monologue into the void.

Also: emoji reactions, chosen occasionally with the same judgment.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

import discord
from discord.ext import commands

import ai
from ai import AIError
import helpers
from cogs.pings import can_ambient_ping
import memory
from abilities import ACTIONS_PROMPT, execute as execute_action
from config import get as get_cfg

log = logging.getLogger("cogs.ambient")

import discord
from discord.ext import commands

import ai
from ai import AIError
import memory
from config import get as get_cfg

log = logging.getLogger("cogs.ambient")

EVERY_S = int(os.environ.get("AMBIENT_EVERY_S", 1500))        # evaluate every 25 min
QUIET_START, QUIET_END = 2, 8                                  # UTC hours it stays quiet
DAILY_CAP = int(os.environ.get("AMBIENT_DAILY_CAP", 4))
CHANNEL_COOLDOWN_S = 3 * 3600
GLOBAL_COOLDOWN_S = 45 * 60
CONFIDENCE = 0.80
SKIP_CHANNELS = {"starboard", "bot-commands", "mod-log", "dream-diary", "announcements",
                 "rules", "legal-disclaimer", "model-market", "tech-support",
                 "bug-reports", "feature-requests", "hangout-voice", "focus-voice"}

REACT_COOLDOWN_S = 15 * 60
REACT_CONFIDENCE = 0.86

# "im bored / anyone wanna talk" — an overheard invitation: if no human picks
# it up within LONELY_WAIT_S, the bot may choose to step in (low bar — they
# asked for company; but it can still choose not to)
LONELY_WAIT_S = int(os.environ.get("LONELY_WAIT_S", 180))
LONELY_CONFIDENCE = 0.60
LONELY_PER_USER_COOLDOWN = 6 * 3600
_INVITE_RE = re.compile(
    r"\b(im|i'm|i am)?\s*(so\s+)?(bored|lonely|alone)\b|\banyone\s+(wanna|want to|around|here|online)\b"
    r"|\bwanna\s+talk\b|\bsomeone\s+talk\b|\bhello\??\s*\?+\b|\bis\s+anyone\s+(there|around)\b", re.I)


class AmbientCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._task = None
        self._last_start: dict[int, float] = {}   # channel id -> monotonic
        self._last_any = 0.0
        self._today = None
        self._started_today = 0
        self._last_react: dict[int, float] = {}   # channel id -> monotonic
        self._lonely_last: dict[int, float] = {}  # user id -> monotonic
        self._lonely_tasks: set = set()

    async def cog_load(self):
        self._task = asyncio.create_task(self._loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    # ---------- the loop ----------

    async def _loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await asyncio.sleep(EVERY_S)
                for guild in self.bot.guilds:
                    cfg = await get_cfg(guild.id)
                    if not cfg.get("companion", {}).get("enabled", True):
                        continue
                    await self._maybe_start(guild)
            except Exception:
                log.exception("ambient loop error")

    def _quiet_hours(self) -> bool:
        h = datetime.utcnow().hour
        return QUIET_START <= h < QUIET_END

    def _under_cap(self) -> bool:
        today = datetime.utcnow().date()
        if self._today != today:
            self._today = today
            self._started_today = 0
        return self._started_today < DAILY_CAP

    async def _pick_channel(self, guild: discord.Guild):
        """The text channel with the most recent human activity worth joining."""
        best, best_ts = None, 0.0
        for ch in guild.text_channels:
            if ch.name.lower() in SKIP_CHANNELS:
                continue
            if not ch.permissions_for(guild.me).send_messages:
                continue
            if time.monotonic() - self._last_start.get(ch.id, -10**9) < CHANNEL_COOLDOWN_S:
                continue
            try:
                async for m in ch.history(limit=1):
                    if m.author.bot or (datetime.now(m.created_at.tzinfo or datetime.timezone.utc)
                                        - m.created_at).total_seconds() > 3600:
                        continue
                    ts = m.created_at.timestamp()
                    if ts > best_ts:
                        best, best_ts = ch, ts
            except discord.HTTPException:
                continue
        return best

    async def _maybe_start(self, guild: discord.Guild):
        if self._quiet_hours() or not self._under_cap():
            return
        if time.monotonic() - self._last_any < GLOBAL_COOLDOWN_S:
            return
        ch = await self._pick_channel(guild)
        if ch is None:
            return

        # what's happening + who's around
        history = []
        async for m in ch.history(limit=25):
            history.append(m)
        history = [m for m in history if not m.author.bot][:12]
        if len(history) < 3:
            return
        excerpt = "\n".join(f"{m.author.display_name}: {m.content[:150]}" for m in reversed(history))
        recent_human = {m.author.id: m.author for m in history if isinstance(m.author, discord.Member)}
        since = time.monotonic() - self._last_spoke(ch)

        # decision-model call: do I want to start something?
        state = (f"channel: #{ch.name}\nI last spoke here {since/60:.0f} min ago; "
                 f"started a conversation myself {self._started_today} times today\n"
                 f"people around: " + ", ".join(m.display_name for m in recent_human.values()) + "\n"
                 f"recent messages:\n{excerpt[:1200]}")
        try:
            answers = await ai.decide(state[:1800], {
                "start": {
                    "type": "choice",
                    "instructions": (
                        "I'm Inferolli, a person-like member of this Discord server, deciding whether to "
                        "start a conversation unprompted. I'd start something when: the chat stalled on "
                        "something I have a genuine take on, someone said something I want to pull a thread "
                        "on, I can offer something useful or funny, or I haven't talked with these people in "
                        "a while and I miss them. I'd stay out when: they're mid-flow on something technical "
                        "I'd derail, I spoke recently, I have nothing beyond 'hey', or the vibe is closed. "
                        "Starting things too often is worse than never."),
                    "criteria": {"yes": "start something", "no": "stay out"},
                }},
                timeout=20.0)
            a = answers.get("start", {})
            if not (a.get("choice") == "yes" and float(a.get("confidence", 0)) >= CONFIDENCE):
                return
        except AIError as e:
            log.debug("ambient gate failed: %s", e)
            return

        # candidates: only people who haven't opted out of ambient pings
        candidates = []
        for uid, m in recent_human.items():
            if not await can_ambient_ping(uid):
                continue
            mem = await memory.get_user(uid, m.display_name)
            candidates.append((uid, m.display_name, memory.affinity_tier(mem["affinity"])))
        cand_line = "\n".join(f"- <@{uid}> {name} (rapport: {tier})" for uid, name, tier in candidates[:8])
        opted_in_ids = [uid for uid, _, _ in candidates]

        persona = await memory.get_persona()
        system = (
            f"You are {persona['name']}. " + _persona_line(persona) +
            "\n\nYou decided to start a conversation in #" + ch.name + ". Write ONE natural opener: "
            "1-2 sentences, lowercase-ish, like a person who just thought of something — NOT a greeting, "
            "NOT a poll, not 'how is everyone'. You may mention at most one person by their tag below, "
            "and only if what you say genuinely relates to them. No headers, no quotes.\n"
            f"People around (mention only if it truly fits):\n{cand_line}\n\n"
            f"Recent messages you're joining:\n{excerpt[:1000]}"
        )
        try:
            async with ch.typing():
                msg = await ai.chat_for("reply", [{"role": "system", "content": system}],
                                    max_tokens=250, temperature=0.9, timeout=25.0)
            msg = msg.strip().strip('"')
            if not msg or msg.upper() == "SKIP" or len(msg) > 400:
                return
            if not helpers.may_send(ch.id):
                log.info("ambient start suppressed in #%s (send guard)", ch.name)
                return
            await ch.send(msg[:400],
                          allowed_mentions=discord.AllowedMentions(
                              users=[discord.Object(id=u) for u in opted_in_ids],
                              roles=False, everyone=False))
            helpers.mark_sent(ch.id)
            self._last_start[ch.id] = time.monotonic()
            self._last_any = time.monotonic()
            self._started_today += 1
            me = guild.me
            await memory.remember_stm(me.id, me.display_name, ch.name, msg[:300], bot_present=True,
                                      channel_id=ch.id)
            log.info("ambient start in #%s: %r", ch.name, msg[:60])
        except (AIError, asyncio.TimeoutError) as e:
            log.debug("ambient opener failed: %s", e)

    def _last_spoke(self, ch) -> float:
        companion = self.bot.get_cog("CompanionCog")
        return getattr(companion, "_last_spoke", {}).get(ch.id, 0) if companion else 0

    # ---------- overheard invitations ----------

    async def _lonely_check(self, message: discord.Message):
        """Someone put out an invitation ('im bored, anyone wanna talk?').
        Wait a few minutes — if no human picked it up, I may choose to."""
        try:
            await asyncio.sleep(LONELY_WAIT_S)
        except asyncio.CancelledError:
            return

        # did anyone (human) respond in the meantime?
        try:
            async for m in message.channel.history(limit=10, after=message):
                if not m.author.bot:
                    return  # someone showed up — not my moment
        except discord.HTTPException:
            return

        mem = await memory.get_user(message.author.id, message.author.display_name)
        state = (f"in #{message.channel.name}, {message.author.display_name} "
                 f"(rapport: {memory.affinity_tier(mem['affinity'])}, known: {mem['messages_seen']} messages) "
                 f"said {LONELY_WAIT_S // 60} minutes ago and nobody has replied:\n"
                 f"\"{message.content[:200]}\"")
        try:
            answers = await ai.decide(state[:1200], {
                "step_in": {
                    "type": "choice",
                    "instructions": (
                        "I'm Inferolli, a person-like server member. Someone said they're bored/lonely "
                        "and nobody's picked it up. I'd step in when: I know them at least a little, I "
                        "have something real to open with (a shared interest, a follow-up on their life, "
                        "a genuine question). I'd stay out when: I don't know them and 'hi' is all I've "
                        "got, they seem okay-joking rather than actually lonely, or the channel has "
                        "moved on. Low bar overall — they asked for company."),
                    "criteria": {"yes": "step in", "no": "let it be"},
                }},
                timeout=20.0)
            a = answers.get("step_in", {})
            if not (a.get("choice") == "yes" and float(a.get("confidence", 0)) >= LONELY_CONFIDENCE):
                return  # chose not to — a person can do that
        except AIError as e:
            log.debug("lonely gate failed: %s", e)
            return

        persona = await memory.get_persona()
        what_you_know = "; ".join(f["text"] for f in sorted(mem["facts"], key=lambda x: -x.get("salience", 1))[:4])
        system = (
            f"You are {persona['name']}. " + _persona_line(persona) +
            "\n\nNobody responded to this member's invitation to talk. You're going to be the one who "
            "shows up. Open with something REAL: reference what you know about them, something they've "
            "been into, or a specific easy-to-answer question. 1-2 sentences, warm but casual, like a "
            "friend who wandered in — never pitying, never 'I noticed nobody replied'."
        )
        user = (f"what you know about them: {what_you_know or 'nothing yet — keep it light and easy'}\n"
                f"their message: \"{message.content[:200]}\"")
        try:
            async with message.channel.typing():
                reply = await ai.chat_for("reply", [{"role": "system", "content": system},
                                       {"role": "user", "content": user}],
                                      max_tokens=200, temperature=0.8, timeout=25.0)
            reply = reply.strip().strip('"')
            if not reply or reply.upper() == "SKIP" or len(reply) > 400:
                return
            if not helpers.may_send(message.channel.id):
                log.info("lonely reply suppressed in #%s (send guard)", message.channel.name)
                return
            await message.reply(reply[:400], mention_author=False)
            helpers.mark_sent(message.channel.id)
            me = message.guild.me
            await memory.remember_stm(me.id, me.display_name, message.channel.name,
                                      f"(replied to {message.author.display_name}'s invitation) {reply[:200]}",
                                      bot_present=True, channel_id=message.channel.id)
            log.info("lonely-check: stepped in for %s in #%s", message.author.display_name,
                     message.channel.name)
        except (AIError, asyncio.TimeoutError) as e:
            log.debug("lonely reply failed: %s", e)

    # ---------- reactions ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not message.content:
            return

        # ---- overheard invitation: "im bored anyone wanna talk" ----
        if (_INVITE_RE.search(message.content)
                and message.channel.name.lower() not in SKIP_CHANNELS
                and message.channel.permissions_for(message.guild.me).send_messages
                and time.monotonic() - self._lonely_last.get(message.author.id, -10**9) > LONELY_PER_USER_COOLDOWN
                and not (message.mentions and message.guild.me in message.mentions)):
            self._lonely_last[message.author.id] = time.monotonic()
            t = asyncio.create_task(self._lonely_check(message))
            self._lonely_tasks.add(t)
            t.add_done_callback(self._lonely_tasks.discard)

        if message.mentions and message.guild.me in message.mentions:
            return  # direct conversations get replies, not reactions
        if time.monotonic() - self._last_react.get(message.channel.id, 0) < REACT_COOLDOWN_S:
            return
        me = message.guild.me
        if not message.channel.permissions_for(me).add_reactions:
            return

        self._last_react[message.channel.id] = time.monotonic()
        try:
            answers = await ai.decide(
                f"in #{message.channel.name}: {message.author.display_name} said: {message.content[:200]}",
                {"react": {"type": "choice", "instructions": (
                    "I'm Inferolli, a person-like server member. Would I drop an emoji reaction on this "
                    "message like a person skimming chat would — funny, based, relatable? Almost always no."),
                    "criteria": {"yes": "react", "no": "scroll past"}}},
                timeout=15.0)
            a = answers.get("react", {})
            if not (a.get("choice") == "yes" and float(a.get("confidence", 0)) >= REACT_CONFIDENCE):
                return
            try:
                emoji = await ai.chat_for("quick", 
                    [{"role": "user", "content": (
                        f"Pick ONE emoji to react to this message, the way a chill person would. "
                        f"Reply with only the emoji.\n\n\"{message.content[:200]}\"")}],
                    max_tokens=20, temperature=0.3, timeout=15.0)
                emoji = emoji.strip().split()[0][:8]
                await message.add_reaction(emoji)
                await memory.remember_stm(me.id, me.display_name, message.channel.name,
                                          f"(reacted {emoji} to {message.author.display_name})",
                                          bot_present=True, channel_id=message.channel.id)
            except (AIError, asyncio.TimeoutError, IndexError):
                pass
        except AIError as e:
            log.debug("react gate failed: %s", e)


def _persona_line(p: dict) -> str:
    jokes = "; ".join(p.get("running_jokes", [])[-3:]) or "none yet"
    self_model = (p.get("self_model") or "").strip()
    sm = (self_model[:200] + "…") if len(self_model) > 200 else self_model
    return (f"a member of this server — a person, not an assistant. {p.get('vibe', '')} "
            f"who you are (your own words): {sm or '(still figuring that out)'} "
            f"your takes: " + "; ".join(f"{o['topic']}: {o['stance']}" for o in p.get("opinions", [])[:6]) +
            f" | running jokes you're in on: {jokes} | energy {p.get('energy', 0.5):.0%}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AmbientCog(bot))
