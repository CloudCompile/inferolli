"""AI-powered automod v2 — the moderation copilot.

Every message gets a decision-model verdict (microsoft/decision-1 — the same
model class that made its name phishing emails: cheap, calibrated classification).
Enforcement is graduated and in-character:

  clean                    → nothing
  low, first offense       → person-style warning, no delete
  medium / repeated low    → delete + warning + strike + mod-log
  high (conf ≥ 0.85)       → delete + strike + mod-log
  3+ strikes in 1h         → timeout

Fail-open by design: if the AI errors out, the message stays.
"""

import asyncio
import logging
import time
from datetime import timedelta

import discord
from discord.ext import commands

import ai
from ai import AIError
from config import DEFAULTS
from helpers import is_admin, send_mod_log

log = logging.getLogger("cogs.automod")

FALLBACK_PROMPT = """You are a Discord moderation classifier. Analyze the message and reply with ONLY a JSON object:
{"violation": true|false, "severity": "low"|"medium"|"high", "reason": "<one short sentence>"}

Guidelines:
- low: mildly rude, low-effort spam, excessive caps, off-topic advertising.
- medium: harassment, slurs, sexual content, targeted insults.
- high: threats, doxxing, severe hate speech, scam/phishing links, invites to other servers, crash attempts.
- If the message is normal conversation, set violation to false.
Judge the message alone, not the person."""


class AutoModCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.strikes: dict[tuple[int, int], list[float]] = {}  # (guild_id, user_id) -> timestamps
        self._pending: set[int] = set()  # message ids currently being classified

    def _record_strike(self, guild_id: int, user_id: int, severity: str = "low") -> int:
        key = (guild_id, user_id)
        now = time.time()
        hits = [t for t in self.strikes.get(key, []) if now - t < 3600]
        hits.append(now)
        self.strikes[key] = hits
        try:  # persistent case file (the in-memory strikes die on restart)
            from cogs.modtools import record_case
            record_case(guild_id, user_id, "automod", f"{severity} violation", "automod")
        except Exception:
            pass
        return len(hits)

    async def _classify(self, message: discord.Message) -> tuple[str, float, str]:
        """Decision-model verdict. Returns (severity, confidence, reason).

        severity: "clean" | "low" | "medium" | "high"
        """
        state = (
            f"channel: #{message.channel.name}\n"
            f"author: {message.author.display_name}\n"
            f"message: {message.content[:600]}"
        )
        try:
            answers = await ai.decide(state[:1000], {
                "verdict": {
                    "type": "choice",
                    "instructions": (
                        "I keep a small Discord server's moderation. Rate this message: "
                        "clean = normal conversation. low = mildly rude, low-effort spam, "
                        "excessive caps, off-topic advertising. medium = harassment, slurs, "
                        "sexual content, targeted insults. high = threats, doxxing, severe hate, "
                        "scam/phishing links, invite spam, crash attempts. Judge the message "
                        "alone, not the person. Banter between regulars is usually clean."),
                    "criteria": {"clean": "fine", "low": "mild", "medium": "serious",
                                 "high": "removable now"},
                },
            }, timeout=15.0)
            a = answers.get("verdict", {})
            sev = a.get("choice", "clean")
            if sev not in ("clean", "low", "medium", "high"):
                sev = "clean"
            return sev, float(a.get("confidence", 0)), ""
        except AIError as e:
            log.debug("decision verdict failed, using chat fallback: %s", e)

        # fallback: the original chat classifier
        verdict = await ai.chat_for("automod", [
            {"role": "system", "content": FALLBACK_PROMPT},
            {"role": "user", "content": message.content[:1000]},
        ], json_out=True, max_tokens=100, temperature=0.0, timeout=15.0)
        if verdict.get("violation"):
            return verdict.get("severity", "medium"), 0.7, verdict.get("reason", "")
        return "clean", 1.0, "chat classifier: no violation"

    async def _warn_in_character(self, message: discord.Message, severity: str, reason: str):
        """A person-style warning, not a system message."""
        deleted = severity in ("medium", "high")
        try:
            line = await ai.chat_for("quick", [
                {"role": "system", "content": (
                    "You are Inferolli, a person-like member of this server who helps keep it tidy. "
                    "Someone just broke a rule (their message was "
                    f"{'deleted' if deleted else 'flagged'}: {reason}). Give them a SHORT warning: "
                    "one lowercase sentence, dry-witted, not preachy, no mod-speak. Like a friend "
                    "telling you to knock it off. No quotes, no emoji spam.")},
                {"role": "user", "content": f"their message was: {message.content[:200]}"},
            ], max_tokens=60, temperature=0.8, timeout=15.0)
            line = line.strip().strip('"')
            if not line or line.upper() == "SKIP" or len(line) > 200:
                raise AIError("bad warning line")
        except (AIError, asyncio.TimeoutError):
            line = ("oi — that's not what this channel's for, knock it off"
                    if deleted else "oi — easy on that stuff")
        try:
            await message.channel.send(f"{message.author.mention} {line}",
                                       allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

    async def _act(self, message: discord.Message, severity: str, conf: float,
                   reason: str, cfg: dict, strike_count: int) -> None:
        guild = message.guild
        am = cfg["automod"]
        acted = []

        delete = (severity == "high" and conf >= 0.85) or \
                 (severity == "medium" and conf >= 0.70) or \
                 (severity == "low" and conf >= 0.9 and strike_count >= 2)
        if delete:
            try:
                await message.delete()
                acted.append("deleted")
            except (discord.Forbidden, discord.NotFound):
                pass

        strike_count = self._record_strike(guild.id, message.author.id, severity)
        escalate = strike_count >= 3

        if escalate or (severity == "high" and am.get("timeout_on_high")):
            try:
                seconds = int(am.get("timeout_seconds", DEFAULTS["automod"]["timeout_seconds"]))
                if escalate:
                    seconds = max(seconds, 3600)
                await message.author.timeout(discord.utils.utcnow() + timedelta(seconds=seconds),
                                             reason=f"automod: {severity} x{strike_count}")
                acted.append(f"timed out {seconds // 60}min")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning("timeout failed in %s: %s", guild.id, e)

        if severity != "low" or delete:
            await self._warn_in_character(message, severity, reason)
            acted.append("warned")

        embed = discord.Embed(
            title=f"AutoMod — {severity}{'' if not escalate else ' (escalated)'}",
            description=f"{message.author.mention} in {message.channel.mention}",
            colour=discord.Colour.orange() if severity != "high" else discord.Colour.red(),
        )
        embed.add_field(name="Reason", value=reason or "decision-model verdict", inline=False)
        embed.add_field(name="Message", value=(message.content or "*no text*")[:500], inline=False)
        embed.add_field(name="Actions", value=", ".join(acted) or "none", inline=False)
        embed.set_footer(text=f"verdict conf {conf:.2f} · strikes (1h): {strike_count} · user id {message.author.id}")
        await send_mod_log(guild, cfg, embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # ignore DMs, bots, and moderators
        if message.guild is None or message.author.bot or isinstance(message.author, discord.Member) and is_admin(message.author):
            return
        if not message.content or len(message.content.strip()) < 3:
            return

        from config import get as get_cfg
        cfg = await get_cfg(message.guild.id)
        if not cfg["automod"]["enabled"]:
            return
        if message.id in self._pending:
            return
        self._pending.add(message.id)
        try:
            severity, conf, reason = await self._classify(message)
            log.info("automod verdict in #%s: %s (%.2f)", message.channel.name, severity, conf)
            if severity != "clean":
                # NOTE: discord.py's Message has no `.deleted` attribute — the old
                # check raised AttributeError here, so every flagged message
                # escaped action and automod silently failed open. Attempt the
                # delete and let the HTTPException tell us if it's already gone.
                await self._act(message, severity, conf, reason, cfg,
                                len(self.strikes.get((message.guild.id, message.author.id), [])))
        except AIError as e:
            log.debug("automod fail-open: %s", e)
        except Exception:
            log.exception("automod unexpected error")
        finally:
            self._pending.discard(message.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoModCog(bot))
